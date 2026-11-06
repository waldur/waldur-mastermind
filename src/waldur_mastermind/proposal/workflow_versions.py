"""The version history of a call's and a call template's settings and workflow.

``record`` snapshots the call's settings and workflow in the call-transfer
format a template has, and keeps it only when it differs from the latest
version. While a call is a draft, versions one user records within
``COALESCE_WINDOW`` of each other are merged into the latest, so a session
in the step dialog is one version; from activation on, every version is kept.

A call created from a template starts with the template's document at the
version it was created from (the baseline) and, as the next version, the
call's own export right after the import, so whatever the import normalised
shows once, as an explicit change, and is never taken for an edit.

A call that has no version yet -- one created before versions were kept --
gets its current state as version 1 the first time it is written to or its
history is read (``ensure_baseline``).
"""

import datetime

from django.db import IntegrityError, transaction
from django.utils import timezone

from waldur_mastermind.proposal import call_transfer, models, workflow_diff
from waldur_mastermind.proposal.enums import CallStates

COALESCE_WINDOW = datetime.timedelta(minutes=15)

# Sections a version carries: the call's settings and workflow, as a template.
VERSION_SECTIONS = {"documents": False, "rounds": False, "offerings": False}

Reasons = models.WorkflowVersion.Reasons


def call_document(call) -> dict:
    """The call's settings and workflow as a version stores them."""
    document, _ = call_transfer.export_call(call, VERSION_SECTIONS)
    return document


def _latest(owner):
    return owner_versions(owner).order_by("-number").first()


def owner_versions(owner):
    if isinstance(owner, models.CallTemplate):
        return models.WorkflowVersion.objects.filter(template=owner)
    return models.WorkflowVersion.objects.filter(call=owner)


def _owner_fields(owner):
    if isinstance(owner, models.CallTemplate):
        return {"template": owner}
    return {"call": owner}


def _user(user):
    return user if user is not None and getattr(user, "pk", None) else None


def _summary(previous, document):
    if previous is None:
        return {}
    return workflow_diff.diff(previous.document, document)["summary"]


def _create(owner, number, document, digest, user, reason, summary):
    """Create the version, retrying the number once if a concurrent write
    took it first."""
    for _attempt in range(2):
        try:
            with transaction.atomic():
                return models.WorkflowVersion.objects.create(
                    number=number,
                    document=document,
                    document_hash=digest,
                    created_by=_user(user),
                    reason=reason,
                    summary=summary,
                    **_owner_fields(owner),
                )
        except IntegrityError:
            latest = _latest(owner)
            number = (latest.number if latest else 0) + 1
    return None


def record(owner, user=None, reason=Reasons.EDITED, document=None):
    """Record the current settings and workflow of ``owner`` -- a call or a
    call template -- when they changed since its latest version.

    Returns the version recorded or updated, or None when nothing changed.
    """
    if document is None:
        document = (
            owner.document
            if isinstance(owner, models.CallTemplate)
            else call_document(owner)
        )
    digest = models.document_hash(document)
    latest = _latest(owner)
    if latest is not None and latest.document_hash == digest:
        return None
    if isinstance(owner, models.CallTemplate):
        # A template's versions are its own version numbers.
        number = owner.version
        if latest is not None and latest.number >= number:
            number = latest.number + 1
        return _create(owner, number, document, digest, user, reason, {})
    if (
        latest is not None
        and reason == Reasons.EDITED
        and latest.reason == Reasons.EDITED
        and owner.state == CallStates.DRAFT
        and latest.created_by_id is not None
        and latest.created_by_id == getattr(user, "pk", None)
        and latest.modified >= timezone.now() - COALESCE_WINDOW
    ):
        previous = (
            owner_versions(owner).filter(number__lt=latest.number).last()
            if latest.number > 1
            else None
        )
        if previous is not None and previous.document_hash == digest:
            # Edited back to what the previous version was: nothing is left
            # of this session.
            latest.delete()
            return None
        latest.document = document
        latest.document_hash = digest
        latest.summary = _summary(previous, document)
        latest.save(update_fields=["document", "document_hash", "summary", "modified"])
        return latest
    number = (latest.number if latest else 0) + 1
    return _create(
        owner, number, document, digest, user, reason, _summary(latest, document)
    )


def record_on_commit(owner, user=None, reason=Reasons.EDITED):
    """``record`` once the current transaction commits."""
    owner_pk, owner_model = owner.pk, type(owner)

    def run():
        current = owner_model.objects.filter(pk=owner_pk).first()
        if current is not None:
            record(current, user, reason)

    transaction.on_commit(run)


def ensure_baseline(call, user=None):
    """Give a call that has no version yet its current state as version 1."""
    if not owner_versions(call).exists():
        record(call, user, Reasons.IMPORTED)


def record_created_from_template(call, template, user):
    """The baseline of a call created from ``template`` and, after it, the
    call as the import left it."""
    baseline = (
        owner_versions(template).filter(number=template.version).first()
        if template is not None
        else None
    )
    document = baseline.document if baseline is not None else template.document
    record(call, user, Reasons.CREATED_FROM_TEMPLATE, document=document)
    record(call, user, Reasons.IMPORTED)


def baseline(call):
    """The version a call created from a template started from, or None."""
    if not call.template_version:
        return None
    return (
        owner_versions(call)
        .filter(reason=Reasons.CREATED_FROM_TEMPLATE)
        .order_by("number")
        .first()
    )


def template_document(call, version=None):
    """The document of the call's template at ``version`` (its latest when
    None), or None when the template or that version is gone."""
    template = call.template
    if template is None:
        return None
    if version is None or version == template.version:
        return template.document
    found = owner_versions(template).filter(number=version).first()
    return found.document if found is not None else None


def resolve(owner, reference, user=None):
    """The document ``reference`` names on ``owner``.

    ``reference`` is a version number, ``current`` (the live state), or, on
    a call, ``template`` (the baseline it was created from) or
    ``template_latest`` (its template as it is now). Returns None when there
    is no such document.
    """
    reference = (reference or "").strip()
    if reference == "current":
        return (
            owner.document
            if isinstance(owner, models.CallTemplate)
            else call_document(owner)
        )
    if isinstance(owner, models.Call):
        if reference == "template":
            found = baseline(owner)
            if found is not None:
                return found.document
            return template_document(owner, owner.template_version)
        if reference == "template_latest":
            return template_document(owner)
    if reference.isdigit():
        found = owner_versions(owner).filter(number=int(reference)).first()
        return found.document if found is not None else None
    return None
