"""Manual reviewer assignment: the draft batches a call manager builds by hand.

Proposals are assigned one reviewer at a time (the call's
``create-manual-assignment`` action) or many reviewers at once from a CSV file
of proposal IDs and reviewer emails (``import-assignments``). Both go through
``find_new_items`` and ``create_manual_batch``, so duplicate detection,
conflict-of-interest blocking and the reviewer's workload limit behave alike.
Batches are left as drafts: nothing reaches a reviewer until a manager sends
them.
"""

import csv
import io
import uuid as uuid_module

from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.structure.permissions import _get_customer
from waldur_mastermind.proposal import models, rankings
from waldur_mastermind.proposal.enums import ProposalStates

# Conflicts of interest that block an assignment until they are resolved.
BLOCKING_COI_STATUSES = ("pending", "recused")

ALREADY_ASSIGNED = _("Assignment already exists for this reviewer")


def check_reviewer_workload_limit(pool_entry, new_assignments, override, user):
    """Refuse assignments that would take a reviewer above their limit.

    ``pool_entry`` must be locked by the caller so that concurrent assignments
    to the same reviewer are counted one after the other. A call manager may
    pass ``override_workload_limit`` to assign anyway; the override is
    recorded as an event on the call.
    """
    if new_assignments <= 0:
        return
    open_assignments = pool_entry.get_open_assignments()
    if open_assignments + new_assignments <= pool_entry.max_assignments:
        return

    reviewer_name = (
        pool_entry.reviewer.user.full_name
        if pool_entry.reviewer
        else pool_entry.invited_email
    )
    params = {
        "reviewer": reviewer_name,
        "open": open_assignments,
        "limit": pool_entry.max_assignments,
        "new": new_assignments,
    }
    if not override:
        raise exceptions.ValidationError(
            _(
                "Reviewer %(reviewer)s has %(open)s open assignments and a limit "
                "of %(limit)s, so %(new)s more would exceed it. Set "
                "override_workload_limit to assign anyway."
            )
            % params
        )

    call = pool_entry.call
    event_logger.emit(
        f"{event_logger.literal(user.full_name or user.username)} assigned "
        f"{event_logger.literal(new_assignments)} more proposal(s) to reviewer "
        f"{event_logger.literal(reviewer_name)} in call "
        f"{event_logger.literal(call.name)}, above "
        f"the limit of {event_logger.literal(pool_entry.max_assignments)} "
        f"({event_logger.literal(open_assignments)} already open).",
        event_type=EventType.REVIEWER_WORKLOAD_LIMIT_OVERRIDDEN,
        event_context={"call": call},
        scopes=[_get_customer(call)],
    )


def is_under_evaluation(proposal):
    """Only proposals being evaluated may be given to reviewers."""
    return proposal.state in ProposalStates.UNDER_EVALUATION


def _assigned_proposal_ids(pool_entry, proposal_ids):
    """The ``proposal_ids`` already assigned to the reviewer: a live item in
    any of the reviewer's batches, or any item at all in the draft batch new
    items would join -- a conflict-blocked one included, which a second
    insert would duplicate."""
    return set(
        models.AssignmentItem.objects.filter(
            Q(
                status__in=[
                    models.AssignmentItemStatuses.PENDING,
                    models.AssignmentItemStatuses.ACCEPTED,
                    models.AssignmentItemStatuses.COI_BLOCKED,
                ]
            )
            | Q(batch__status=models.AssignmentBatchStatuses.DRAFT),
            batch__reviewer_pool_entry=pool_entry,
            proposal_id__in=proposal_ids,
        ).values_list("proposal_id", flat=True)
    )


def _blocking_coi_records(pool_entry, proposal_ids):
    """The reviewer's blocking conflict-of-interest records for
    ``proposal_ids``, by proposal id."""
    records = {}
    if not proposal_ids:
        return records
    for record in models.ConflictOfInterest.objects.filter(
        reviewer=pool_entry.reviewer,
        proposal_id__in=proposal_ids,
        status__in=BLOCKING_COI_STATUSES,
    ):
        records.setdefault(record.proposal_id, []).append(record)
    return records


def find_new_items(pool_entry, proposals):
    """Split ``proposals`` into the items a manual assignment would create and
    those already assigned to the reviewer.

    Returns ``(new_items, already_assigned)``, both in the order of
    ``proposals``; each new item is a ``(proposal, blocking_coi_records)``
    pair, and a proposal with blocking conflict-of-interest records becomes a
    conflict-blocked item. Runs two queries however many proposals there are,
    as the caller holds a lock on ``pool_entry`` meanwhile.
    """
    proposals = list(proposals)
    proposal_ids = {proposal.pk for proposal in proposals}
    assigned_ids = _assigned_proposal_ids(pool_entry, proposal_ids)
    coi_records = _blocking_coi_records(pool_entry, proposal_ids - assigned_ids)

    new_items = []
    already_assigned = []
    for proposal in proposals:
        if proposal.pk in assigned_ids:
            already_assigned.append(proposal)
        else:
            new_items.append((proposal, coi_records.get(proposal.pk, [])))
    return new_items, already_assigned


def count_workload(new_items):
    """Conflict-blocked items do not add to the reviewer's workload."""
    return sum(1 for _proposal, coi_records in new_items if not coi_records)


def create_manual_batch(
    call,
    pool_entry,
    new_items,
    user,
    manager_notes="",
    override_workload_limit=False,
):
    """Create the reviewer's draft manual batch, or extend the one there is,
    with an item per ``(proposal, coi_records)`` of ``new_items``.

    The caller holds a lock on ``pool_entry`` and has left out proposals that
    are already assigned (``find_new_items``). Raises a validation error when
    the items would take the reviewer above their workload limit, unless the
    limit is overridden, which is logged.
    """
    check_reviewer_workload_limit(
        pool_entry,
        count_workload(new_items),
        override_workload_limit,
        user,
    )

    batch, batch_created = models.AssignmentBatch.objects.get_or_create(
        call=call,
        reviewer_pool_entry=pool_entry,
        status=models.AssignmentBatchStatuses.DRAFT,
        defaults={
            "source": models.AssignmentSources.MANUAL,
            "manager_notes": manager_notes,
            "created_by": user,
        },
    )
    if not batch_created and manager_notes:
        batch.manager_notes = manager_notes
        batch.save(update_fields=["manager_notes"])

    for proposal, coi_records in new_items:
        item = models.AssignmentItem.objects.create(
            batch=batch,
            proposal=proposal,
            affinity_score=None,  # Manual assignment - no affinity
            has_coi=bool(coi_records),
            status=models.AssignmentItemStatuses.COI_BLOCKED
            if coi_records
            else models.AssignmentItemStatuses.PENDING,
        )
        if coi_records:
            item.coi_records.set(coi_records)
    return batch


# -- import from a CSV file ------------------------------------------------------


class ImportStatuses:
    READY = "ready"
    CREATED = "created"
    COI_BLOCKED = "coi_blocked"
    SKIPPED = "skipped"
    ERROR = "error"

    CHOICES = (
        (READY, "Ready: would be assigned (dry run)"),
        (CREATED, "Created: assigned in a draft batch"),
        (COI_BLOCKED, "Conflict-blocked item: a conflict of interest blocks it"),
        (SKIPPED, "Skipped: already assigned, or a repeated row"),
        (ERROR, "Error: not applied"),
    )


EMAIL_COLUMN = "reviewer_email"
UUID_COLUMN = "proposal_uuid"
SLUG_COLUMN = "proposal_slug"


def read_upload(upload):
    """The text of an uploaded CSV file: UTF-8, with or without a BOM, and no
    larger than the ranking import accepts."""
    # Read no further than the limit, whatever size the upload claims.
    raw = upload.read(rankings.MAX_IMPORT_BYTES + 1)
    if len(raw) > rankings.MAX_IMPORT_BYTES:
        raise exceptions.ValidationError(
            {
                "file": _("The file is larger than %(max)s bytes.")
                % {"max": rankings.MAX_IMPORT_BYTES}
            }
        )
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise exceptions.ValidationError({"file": _("The file is not UTF-8 text.")})


def parse_csv(content, max_rows):
    """The non-blank rows of an assignment file, as dicts with ``row`` (the
    line number), ``proposal_uuid``, ``proposal_slug`` and ``reviewer_email``.

    Refused with a ``file`` error: no header with the required columns,
    more than ``max_rows`` rows, or text the CSV reader cannot read.
    """
    reader = csv.reader(io.StringIO(content))
    try:
        header = next(reader, None)
    except csv.Error:
        raise rankings.unreadable_csv(reader)
    columns = [(name or "").strip().lower() for name in header or []]
    if EMAIL_COLUMN not in columns or not (
        UUID_COLUMN in columns or SLUG_COLUMN in columns
    ):
        raise exceptions.ValidationError(
            {
                "file": _(
                    "The file needs a header row with a reviewer_email column "
                    "and a proposal_slug or proposal_uuid column."
                )
            }
        )

    def cell(values, column):
        if column not in columns:
            return ""
        index = columns.index(column)
        return values[index].strip() if index < len(values) else ""

    rows = []
    try:
        for line, values in enumerate(reader, start=2):
            if line - 1 > max_rows:
                raise exceptions.ValidationError(
                    {
                        "file": _(
                            "The file has more rows than the round's proposals "
                            "times the call's reviewers; at most %(max)s are read."
                        )
                        % {"max": max_rows}
                    }
                )
            if not any(value.strip() for value in values):
                continue
            rows.append(
                {
                    "row": line,
                    "proposal_uuid": cell(values, UUID_COLUMN),
                    "proposal_slug": cell(values, SLUG_COLUMN),
                    "reviewer_email": cell(values, EMAIL_COLUMN),
                }
            )
    except csv.Error:
        raise rankings.unreadable_csv(reader)
    return rows


def max_import_rows(call, call_round):
    """A file never has more rows than every proposal of the round given to
    every reviewer of the call's pool, and a few blank ones."""
    proposals = models.Proposal.objects.filter(round=call_round).count()
    reviewers = models.CallReviewerPool.objects.filter(call=call).eligible().count()
    return proposals * reviewers + rankings.IMPORT_ROW_MARGIN


def _result(row):
    return {
        "row": row["row"],
        "proposal_uuid": None,
        "proposal_slug": row["proposal_slug"],
        "proposal_name": "",
        "reviewer_email": row["reviewer_email"],
        "status": ImportStatuses.ERROR,
        "reason": "",
    }


class _ProposalIndex:
    """The proposals of a round, by UUID and by slug (which need not be
    unique), with a fallback to the rest of the call to tell a proposal of
    another round from one that does not exist."""

    def __init__(self, call, call_round):
        self.call = call
        self.call_round = call_round
        self.by_uuid = {}
        self.by_slug = {}
        for proposal in models.Proposal.objects.filter(round=call_round):
            self.by_uuid[proposal.uuid.hex] = proposal
            self.by_slug.setdefault(proposal.slug.lower(), []).append(proposal)

    def find(self, row):
        """``(proposal, error)``: the proposal a row names, or why there is none."""
        raw_uuid = row["proposal_uuid"]
        slug = row["proposal_slug"]
        if raw_uuid:
            try:
                uuid = uuid_module.UUID(raw_uuid).hex
            except ValueError:
                return None, _("The proposal_uuid is not a valid UUID.")
            proposal = self.by_uuid.get(uuid)
            if proposal:
                return proposal, None
            in_call = models.Proposal.objects.filter(
                round__call=self.call, uuid=uuid
            ).exists()
        elif slug:
            matches = self.by_slug.get(slug.lower(), [])
            if len(matches) == 1:
                return matches[0], None
            if matches:
                return None, _(
                    "Several proposals of the round have this ID; use "
                    "proposal_uuid instead."
                )
            in_call = models.Proposal.objects.filter(
                round__call=self.call, slug__iexact=slug
            ).exists()
        else:
            return None, _("The row names no proposal.")
        if in_call:
            return None, _("The proposal is not in round %(round)s.") % {
                "round": self.call_round.name
            }
        return None, _("No proposal of this call has this ID.")


def _pool_entries_by_email(call, emails, lock):
    """The eligible pool entries of the call whose reviewer has one of
    ``emails``, by lowercased email. With ``lock``, they are locked, in a
    fixed order, so that concurrent assignments to the same reviewers are
    checked against their limits one at a time."""
    entries = (
        models.CallReviewerPool.objects.eligible()
        .filter(call=call)
        .annotate(email_lower=Lower("reviewer__user__email"))
        .filter(email_lower__in=emails)
        .select_related("reviewer__user", "call")
        .order_by("pk")
    )
    if lock:
        entries = entries.select_for_update(of=("self",))
    by_email = {}
    for entry in entries:
        by_email.setdefault(entry.email_lower, []).append(entry)
    return by_email


def import_assignments(
    call,
    call_round,
    rows,
    user,
    dry_run=True,
    override_workload_limit=False,
    manager_notes="",
):
    """Assign the proposals of ``call_round`` to the reviewers the ``rows``
    name, as ``create-manual-assignment`` would for each reviewer.

    Every row is reported with a status and a reason. A dry run changes
    nothing; otherwise the valid rows are applied together, each reviewer's
    in one draft manual batch, and invalid rows are left out.

    Returns ``{"rows", "totals", "batch_uuids"}``.
    """
    results = []
    with transaction.atomic():
        proposals = _ProposalIndex(call, call_round)
        entries_by_email = _pool_entries_by_email(
            call,
            {row["reviewer_email"].lower() for row in rows if row["reviewer_email"]},
            lock=not dry_run,
        )

        # Resolve each row on its own; group the valid ones by reviewer.
        groups = {}
        seen = {}
        for row in rows:
            result = _result(row)
            results.append(result)
            proposal, error = proposals.find(row)
            if proposal is None:
                result["reason"] = error
                continue
            result["proposal_uuid"] = proposal.uuid.hex
            result["proposal_slug"] = proposal.slug
            result["proposal_name"] = proposal.name
            if not is_under_evaluation(proposal):
                result["reason"] = _(
                    "The proposal is not under evaluation, so it cannot be "
                    "assigned to reviewers."
                )
                continue
            email = row["reviewer_email"].lower()
            if not email:
                result["reason"] = _("The row names no reviewer_email.")
                continue
            entries = entries_by_email.get(email, [])
            if not entries:
                result["reason"] = _(
                    "No accepted reviewer of the call's pool has this email."
                )
                continue
            if len(entries) > 1:
                result["reason"] = _(
                    "Several reviewers of the call's pool have this email."
                )
                continue
            entry = entries[0]
            key = (proposal.pk, entry.pk)
            if key in seen:
                result["status"] = ImportStatuses.SKIPPED
                result["reason"] = _(
                    "Repeated row: line %(row)s assigns the same proposal "
                    "to the same reviewer."
                ) % {"row": seen[key]}
                continue
            seen[key] = row["row"]
            groups.setdefault(entry, []).append((result, proposal))

        batch_uuids = []
        for entry, group in groups.items():
            new_items, already_assigned = find_new_items(
                entry, [proposal for _result_row, proposal in group]
            )
            results_by_proposal = {
                proposal.pk: result_row for result_row, proposal in group
            }
            for proposal in already_assigned:
                result_row = results_by_proposal[proposal.pk]
                result_row["status"] = ImportStatuses.SKIPPED
                result_row["reason"] = ALREADY_ASSIGNED

            if not override_workload_limit:
                new_items = _within_workload_limit(
                    entry, new_items, results_by_proposal
                )

            for proposal, coi_records in new_items:
                result_row = results_by_proposal[proposal.pk]
                if coi_records:
                    result_row["status"] = ImportStatuses.COI_BLOCKED
                    result_row["reason"] = _(
                        "A conflict of interest blocks this assignment until "
                        "it is resolved."
                    )
                elif dry_run:
                    result_row["status"] = ImportStatuses.READY
                else:
                    result_row["status"] = ImportStatuses.CREATED

            if new_items and not dry_run:
                batch = create_manual_batch(
                    call,
                    entry,
                    new_items,
                    user,
                    manager_notes,
                    override_workload_limit,
                )
                batch_uuids.append(batch.uuid.hex)

    totals = {status: 0 for status, _label in ImportStatuses.CHOICES}
    for result in results:
        totals[result["status"]] += 1
    return {"rows": results, "totals": totals, "batch_uuids": batch_uuids}


def _within_workload_limit(entry, new_items, results_by_proposal):
    """The ``new_items`` that fit the reviewer's workload limit, in file
    order; the rows of the rest are refused. Conflict-blocked items always
    fit, as they add nothing to the workload."""
    open_assignments = entry.get_open_assignments()
    room = max(entry.max_assignments - open_assignments, 0)
    kept = []
    for proposal, coi_records in new_items:
        if coi_records:
            kept.append((proposal, coi_records))
        elif room > 0:
            kept.append((proposal, coi_records))
            room -= 1
        else:
            result_row = results_by_proposal[proposal.pk]
            result_row["reason"] = _(
                "Over the reviewer's workload limit: %(open)s open "
                "assignments and a limit of %(limit)s. Set "
                "override_workload_limit to assign anyway."
            ) % {"open": open_assignments, "limit": entry.max_assignments}
    return kept
