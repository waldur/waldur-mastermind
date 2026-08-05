import json
import logging
import re
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from waldur_core.core.enums import CoreStates
from waldur_openportal import models

logger = logging.getLogger(__name__)


def _merge_notes(remote_project):
    """
    Return the full deduplicated notes list from award_details().
    Called after last_confirmed_details has been set on the in-memory object
    so that award_details() unions local and remote notes correctly.
    """
    merged = remote_project.award_details()
    return json.loads(merged.to_json()).get("notes") or []


def _sync_project_link(remote_project):
    """
    Update link_project from last_confirmed_details.project_link.
    Called after last_confirmed_details has been set on the in-memory object.
    The remote portal always owns its project URL.
    """
    confirmed = remote_project.last_confirmed_details or {}
    link = confirmed.get("project_link")
    if link:
        remote_project.link_project = link


def _parse_allocation_from_details(details_json):
    """
    Extract the numeric allocation from an AwardDetails JSON dict.
    The allocation field is a string like '1000 GPUHR'; returns the
    numeric part as a Decimal, or None if absent or unparseable.
    """
    alloc_str = (details_json or {}).get("allocation")
    if not alloc_str:
        return None
    m = re.match(r"^\s*(\d+(?:\.\d+)?)", str(alloc_str))
    return Decimal(m.group(1)) if m else None


def reconcile_allocation(remote_project):
    """
    Derive current_allocation and pending_allocation from the details
    we already hold, rather than tracking them through each code path.

    Logic:
      sent      = allocation parsed from last_sent_details
      confirmed = allocation parsed from last_confirmed_details

      sent != confirmed (and both present) → change in flight:
          current_allocation  = confirmed  (what remote portal has)
          pending_allocation  = sent       (what we asked for)

      sent == confirmed, or only one present → no pending change:
          current_allocation  = confirmed or sent (whichever is available)
          pending_allocation  = None

    This self-corrects even if a notification is missed: the next time
    any detail is updated the fields converge to the right values.

    Does NOT call save() — caller is responsible for persisting.
    """
    sent = _parse_allocation_from_details(remote_project.last_sent_details)
    confirmed = _parse_allocation_from_details(remote_project.last_confirmed_details)

    if sent is not None and confirmed is not None:
        if sent != confirmed:
            remote_project.current_allocation = confirmed
            remote_project.pending_allocation = sent
        else:
            remote_project.current_allocation = confirmed
            remote_project.pending_allocation = None
    elif confirmed is not None:
        remote_project.current_allocation = confirmed
        remote_project.pending_allocation = None
    elif sent is not None:
        remote_project.current_allocation = sent
        remote_project.pending_allocation = None
    # else: no allocation data at all — leave fields unchanged


def _get_or_create_audit_entry(
    remote_project,
    event_type,
    *,
    previous_details=None,
    new_details=None,
    allocation_entry=None,
    performed_by=None,
    remote_response=None,
    note="",
):
    """
    Return the most recent audit entry for (remote_project, event_type) if its
    content fields match, otherwise create and return a new one.

    Content identity is defined by new_details, previous_details, note, and
    remote_response.  allocation_entry and performed_by are metadata links and
    are not compared.
    """
    last = (
        models.RemoteProjectAuditEntry.objects.filter(
            remote_project=remote_project,
            event_type=event_type,
        )
        .order_by("-timestamp")
        .first()
    )

    if (
        last is not None
        and last.new_details == new_details
        and last.previous_details == previous_details
        and last.note == note
        and last.remote_response == remote_response
    ):
        return last

    return models.RemoteProjectAuditEntry.objects.create(
        remote_project=remote_project,
        event_type=event_type,
        previous_details=previous_details,
        new_details=new_details,
        allocation_entry=allocation_entry,
        performed_by=performed_by,
        remote_response=remote_response,
        note=note,
    )


def get_or_create_remote_project(allocation, destination: str, remote_identifier=None):
    """
    Get or create a RemoteProject for this allocation.

    remote_identifier:
        str  — the remote portal's project ID (e.g. 'u6f.brics'),
               available only after the award is approved.
        None — the award is still pending approval; key on
               (destination, current_project).

    Defaults applied on creation:
        membership_control = OPEN — the receiving portal manages membership
                             until an organisation owner locks it down
        allowed_domains    = None — no restriction
        earliest_approve   = allocation.created + 1 hour
        link_award, link_call from proposal if attached

    On get (not created): syncs remote_allocation / current_project if
    they have changed.  When a pending record is found and
    remote_identifier is now known, its identifier is updated.
    """
    from datetime import timedelta

    from waldur_openportal.utils import get_proposal_links_for_project

    project = allocation.project

    # Compute defaults — used when a new record is created.
    link_award, link_call = get_proposal_links_for_project(project)

    # There is no call- or round-level award policy here, so a new award starts
    # unrestricted and an organisation owner narrows it per award through the
    # RemoteProject actions. The fork seeds these from fields on its Round
    # instead; that model carries scheduling only here, so the fields have no
    # home and are deliberately not carried over.
    creation_defaults = {
        "remote_allocation": allocation,
        "current_project": project,
        "state": models.RemoteProjectState.PENDING,
        "membership_control": models.MembershipControlChoices.OPEN,
        "earliest_approve": allocation.created + timedelta(hours=1),
        "allowed_domains": None,
        "link_award": link_award,
        "link_call": link_call,
        "link_renewal": None,
    }

    if remote_identifier is not None:
        # ---- approved path ----
        # 1. Check if there is an existing pending record for this
        #    project that we can upgrade (fill in the identifier).
        pending = models.RemoteProject.objects.filter(
            destination=destination,
            identifier__isnull=True,
            current_project=project,
        ).first()

        if pending is not None:
            update_fields = ["identifier", "modified"]
            pending.identifier = remote_identifier
            if pending.remote_allocation != allocation:
                pending.remote_allocation = allocation
                update_fields.append("remote_allocation")
            pending.save(update_fields=update_fields)
            return pending

        # 2. No pending record — get or create by (destination, identifier).
        remote_project, created = models.RemoteProject.objects.get_or_create(
            destination=destination,
            identifier=remote_identifier,
            defaults=creation_defaults,
        )

        if not created:
            changed = []
            if remote_project.remote_allocation != allocation:
                remote_project.remote_allocation = allocation
                new_ea = allocation.created + timedelta(hours=1)
                remote_project.earliest_approve = (
                    new_ea if new_ea > timezone.now() else None
                )
                changed.append("remote_allocation")
                changed.append("earliest_approve")
            if remote_project.current_project != project:
                remote_project.current_project = project
                changed.append("current_project")
            if changed:
                remote_project.save(update_fields=changed + ["modified"])

        return remote_project

    else:
        # ---- pending path ----
        # Key on (destination, identifier=None, current_project).
        remote_project, created = models.RemoteProject.objects.get_or_create(
            destination=destination,
            identifier=None,
            current_project=project,
            defaults={
                k: v for k, v in creation_defaults.items() if k != "current_project"
            },
        )

        if not created:
            if remote_project.state == models.RemoteProjectState.DELETED:
                # Revive the record: reset to the same defaults that would
                # have been applied on creation.
                for field, value in creation_defaults.items():
                    if field != "current_project":
                        setattr(remote_project, field, value)
                remote_project.error_message = ""
                remote_project.save()
            elif remote_project.remote_allocation != allocation:
                remote_project.remote_allocation = allocation
                new_ea = allocation.created + timedelta(hours=1)
                remote_project.earliest_approve = (
                    new_ea if new_ea > timezone.now() else None
                )
                remote_project.save(
                    update_fields=[
                        "remote_allocation",
                        "earliest_approve",
                        "modified",
                    ]
                )

        return remote_project


def record_award_rejected(remote_project, details_json, error_message):
    """
    Called when a create_award attempt is explicitly rejected by the
    remote portal (ManagedProjectRejectedError on initial add_project).

    Sets: last_sent_details (if provided), state=ERROR.
    Updates remote_allocation to ERRED if present.
    Creates audit entry with event_type=AWARD_REJECTED, note=error_message.
    """
    remote_project.state = models.RemoteProjectState.ERROR
    remote_project.error_message = error_message
    update_fields = ["state", "error_message", "modified"]
    if details_json is not None:
        remote_project.last_sent_details = details_json
        update_fields.append("last_sent_details")
    remote_project.save(update_fields=update_fields)

    alloc = remote_project.remote_allocation
    if alloc is not None:
        alloc.error_message = error_message
        alloc.set_erred()
        alloc.save()

    audit_entry = _get_or_create_audit_entry(
        remote_project,
        models.RemoteProjectAuditEventType.AWARD_REJECTED,
        new_details=details_json,
        note=error_message,
    )

    return audit_entry


def record_award_attempted(remote_project, details_json, note=""):
    """
    Called when a create_award attempt was made but no confirmation was
    received (the remote portal requires human approval, or the call
    raised an unexpected exception).

    Records what was attempted so there is a full audit trail even for
    pending / failed creation attempts.

    Sets: last_sent_details.
    Creates audit entry with event_type=AWARD_ATTEMPTED.
    """
    remote_project.last_sent_details = details_json
    update_fields = ["last_sent_details", "modified"]
    pending = _parse_allocation_from_details(details_json)
    if pending is not None:
        remote_project.pending_allocation = pending
        update_fields.append("pending_allocation")
    remote_project.save(update_fields=update_fields)

    audit_entry = _get_or_create_audit_entry(
        remote_project,
        models.RemoteProjectAuditEventType.AWARD_ATTEMPTED,
        new_details=details_json,
        note=note,
    )

    return audit_entry


def ensure_current_attachment(remote_project):
    """
    Ensure there is an open RemoteProjectAttachment for
    remote_project.current_project.

    Closes (sets detached_at=now) any open attachment pointing to a
    different project, then get_or_creates the open attachment for the
    current project.
    """
    now = timezone.now()
    current_project = remote_project.current_project

    # Close any open attachments for a different project
    models.RemoteProjectAttachment.objects.filter(
        remote_project=remote_project,
        detached_at__isnull=True,
    ).exclude(project=current_project).update(detached_at=now)

    # Get or create the open attachment for the current project
    attachment, _ = models.RemoteProjectAttachment.objects.get_or_create(
        remote_project=remote_project,
        project=current_project,
        detached_at__isnull=True,
    )

    return attachment


def record_award_created(
    remote_project,
    sent_details_json,
    confirmed_details_json,
    attachment=None,
):
    """
    Called when add_project() succeeds on the remote portal
    (synchronous confirmation).

    sent_details_json is what was sent to the remote.
    confirmed_details_json is what was refetched from the remote after
    confirmation — it may differ (e.g. remote added project_link, adjusted
    allocation, or appended notes).

    The allocation value is parsed directly from sent_details_json.

    Creates a confirmed RemoteProjectAllocationEntry if an allocation is
    present in sent_details_json.
    Sets: last_sent_details, last_confirmed_details,
          pending_details=None, pending_since=None,
          state=ACTIVE, last_contact_time=now,
          current_allocation (if allocation present).
    Updates remote_allocation to OK if present.
    Creates audit entry with event_type=AWARD_CREATED.
    """
    with transaction.atomic():
        remote_project = (
            models.RemoteProject.objects.select_for_update(skip_locked=True)
            .filter(pk=remote_project.pk)
            .first()
        )
        if remote_project is None:
            raise RuntimeError(
                "RemoteProject is locked by another task — skipping record_award_created"
            )

        now = timezone.now()
        allocation_value = _parse_allocation_from_details(
            sent_details_json
        ) or _parse_allocation_from_details(confirmed_details_json)

        allocation_entry = None
        if allocation_value is not None:
            allocation_entry = models.RemoteProjectAllocationEntry.objects.create(
                remote_project=remote_project,
                allocation=allocation_value,
                previous_allocation=remote_project.current_allocation,
                attachment=attachment,
                source_project=remote_project.current_project,
                confirmed_at=now,
            )

        if sent_details_json is not None:
            remote_project.last_sent_details = sent_details_json
        remote_project.last_confirmed_details = confirmed_details_json
        remote_project.pending_details = None
        remote_project.pending_since = None
        remote_project.state = models.RemoteProjectState.ACTIVE
        remote_project.error_message = ""
        remote_project.last_contact_time = now
        reconcile_allocation(remote_project)
        remote_project.notes = _merge_notes(remote_project)
        _sync_project_link(remote_project)

        remote_project.save()

        alloc = remote_project.remote_allocation
        if alloc is not None:
            alloc.state = CoreStates.OK
            alloc.error_message = ""
            alloc.save(update_fields=["state", "error_message", "modified"])

        audit_entry = _get_or_create_audit_entry(
            remote_project,
            models.RemoteProjectAuditEventType.AWARD_CREATED,
            new_details=confirmed_details_json,
            allocation_entry=allocation_entry,
        )

    return audit_entry


def record_award_sent(
    remote_project,
    details_json,
    attachment=None,
):
    """
    Called when update_award is about to be sent (before the network
    call).

    The allocation value is parsed directly from details_json.

    Creates a pending RemoteProjectAllocationEntry (confirmed_at=None)
    if the allocation differs from current_allocation.
    Sets: last_sent_details, pending_details, pending_since=now.
    Creates audit entry with event_type=AWARD_UPDATED.
    """
    now = timezone.now()
    allocation_value = _parse_allocation_from_details(details_json)

    allocation_entry = None
    if allocation_value is not None:
        current = remote_project.current_allocation
        if allocation_value != current:
            allocation_entry = models.RemoteProjectAllocationEntry.objects.create(
                remote_project=remote_project,
                allocation=allocation_value,
                previous_allocation=current,
                attachment=attachment,
                source_project=remote_project.current_project,
                confirmed_at=None,
            )

    remote_project.last_sent_details = details_json
    remote_project.pending_details = details_json
    remote_project.pending_since = now
    remote_project.state = models.RemoteProjectState.PENDING
    reconcile_allocation(remote_project)
    remote_project.save()

    audit_entry = _get_or_create_audit_entry(
        remote_project,
        models.RemoteProjectAuditEventType.AWARD_UPDATED,
        previous_details=remote_project.last_confirmed_details,
        new_details=details_json,
        allocation_entry=allocation_entry,
    )

    return audit_entry


def record_award_update_confirmed(
    remote_project,
    sent_details_json,
    confirmed_details_json,
    attachment=None,
    skip_locked=True,
):
    """
    Called when update_award is confirmed by the remote portal.

    sent_details_json is what was sent to the remote.
    confirmed_details_json is what was refetched from the remote after
    confirmation — it may differ from what was sent.

    The allocation value is parsed directly from sent_details_json.

    Finds the most recent unconfirmed RemoteProjectAllocationEntry and
    sets confirmed_at=now.  If none found, creates a new confirmed entry.
    Sets: last_sent_details, last_confirmed_details, pending_details=None,
          pending_since=None, state=ACTIVE, last_contact_time=now,
          current_allocation (if allocation present), pending_allocation=None.
    Creates audit entry with event_type=AWARD_UPDATE_CONFIRMED.

    skip_locked=False should be used when the caller knows the update was
    accepted and must reliably persist the ACTIVE transition (e.g. the
    synchronous success path in update_allocated_project).  The default
    True retains the original skip behaviour for background/async callers.
    """
    with transaction.atomic():
        qs = models.RemoteProject.objects.filter(pk=remote_project.pk)
        remote_project = qs.select_for_update(skip_locked=skip_locked).first()
        if remote_project is None:
            raise RuntimeError(
                "RemoteProject is locked by another task — skipping record_award_update_confirmed"
            )

        now = timezone.now()
        allocation_value = _parse_allocation_from_details(
            sent_details_json
        ) or _parse_allocation_from_details(confirmed_details_json)

        allocation_entry = None
        if allocation_value is not None:
            unconfirmed = (
                models.RemoteProjectAllocationEntry.objects.filter(
                    remote_project=remote_project,
                    confirmed_at__isnull=True,
                )
                .order_by("-submitted_at")
                .first()
            )

            if unconfirmed is not None:
                unconfirmed.confirmed_at = now
                unconfirmed.save()
                allocation_entry = unconfirmed
            elif allocation_value != remote_project.current_allocation:
                allocation_entry = models.RemoteProjectAllocationEntry.objects.create(
                    remote_project=remote_project,
                    allocation=allocation_value,
                    previous_allocation=remote_project.current_allocation,
                    attachment=attachment,
                    source_project=remote_project.current_project,
                    confirmed_at=now,
                )

        if sent_details_json is not None:
            remote_project.last_sent_details = sent_details_json

        remote_project.last_confirmed_details = confirmed_details_json
        remote_project.pending_details = None
        remote_project.pending_since = None
        remote_project.state = models.RemoteProjectState.ACTIVE
        remote_project.error_message = ""
        remote_project.last_contact_time = now
        reconcile_allocation(remote_project)
        remote_project.notes = _merge_notes(remote_project)
        _sync_project_link(remote_project)

        remote_project.save()

        alloc = remote_project.remote_allocation
        if alloc is not None:
            alloc.state = CoreStates.OK
            alloc.error_message = ""
            alloc.save(update_fields=["state", "error_message", "modified"])

        audit_entry = _get_or_create_audit_entry(
            remote_project,
            models.RemoteProjectAuditEventType.AWARD_UPDATE_CONFIRMED,
            new_details=confirmed_details_json,
            allocation_entry=allocation_entry,
        )

    return audit_entry


def record_award_update_rejected(remote_project, error_message, remote_response=None):
    """
    Called when update_award is rejected (ManagedProjectRejectedError).

    Sets: state=ERROR.
    Updates remote_allocation to ERRED if present.
    Creates audit entry with event_type=AWARD_UPDATE_REJECTED,
    remote_response=remote_response or {"error": error_message},
    note=error_message.
    """
    remote_project.state = models.RemoteProjectState.ERROR
    remote_project.error_message = error_message
    remote_project.save(update_fields=["state", "error_message", "modified"])

    alloc = remote_project.remote_allocation
    if alloc is not None:
        alloc.error_message = error_message
        alloc.set_erred()
        alloc.save()

    response_data = (
        remote_response if remote_response is not None else {"error": error_message}
    )

    audit_entry = _get_or_create_audit_entry(
        remote_project,
        models.RemoteProjectAuditEventType.AWARD_UPDATE_REJECTED,
        remote_response=response_data,
        note=error_message,
    )

    return audit_entry


def touch_last_contact(remote_project):
    """
    Record that we have just received a live response from the remote
    portal about this project (e.g. a successful usage or storage report
    fetch).

    Updates last_contact_time to now.  If the project was STALE,
    transitions it back to ACTIVE — hearing from the portal means the
    connection is healthy.
    """
    now = timezone.now()
    update_fields = ["last_contact_time", "modified"]

    remote_project.last_contact_time = now

    if remote_project.state == models.RemoteProjectState.STALE:
        remote_project.state = models.RemoteProjectState.ACTIVE
        update_fields.append("state")

    remote_project.save(update_fields=update_fields)


def record_resource_deleted(remote_project, note=""):
    """
    Called when delete_allocation() succeeds.

    Closes all open RemoteProjectAttachment (sets detached_at=now).
    Sets: remote_allocation=None, state=DELETED.
    Creates audit entry with event_type=RESOURCE_DELETED, note=note.
    """
    now = timezone.now()

    # Close all open attachments
    models.RemoteProjectAttachment.objects.filter(
        remote_project=remote_project,
        detached_at__isnull=True,
    ).update(detached_at=now)

    remote_project.remote_allocation = None
    remote_project.state = models.RemoteProjectState.DELETED
    remote_project.save()

    audit_entry = _get_or_create_audit_entry(
        remote_project,
        models.RemoteProjectAuditEventType.RESOURCE_DELETED,
        note=note,
    )

    return audit_entry
