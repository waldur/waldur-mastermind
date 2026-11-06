"""A round's ranking and meeting notes, and who reads and writes them.

Both are round-level records kept outside the workflow engine. Whoever
manages the call -- its call managers and the organisers of its managing
organisation -- writes them; the call's panel members read them; staff and
support read everything. Applicants and reviewers read neither. The call's
panel chair and vice-chair write the round's overall ranking as well.

A ranking places every proposal against the others, so nobody but staff
reads a round's ranking or meeting notes while on the applicant team of any
of its proposals or with a conflict of interest on record for one of them,
whatever role they hold on the call. And nobody, staff included, writes the
ranking of a proposal they are on the team of.

A round report -- a note of a step run once per round (``round_reports``)
-- and the ranking within it are narrower. Its author writes them, until
the report is submitted. They are read by staff, support, the call's
managers, the author, and those working on a later step of a covered
proposal that reads the round step as its input. Never by anyone on the
applicant team of, or in conflict with, any proposal the report covers:
the report is free text about all of them.
"""

import csv
import io
import math

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions, serializers

from waldur_core.permissions import models as permissions_models
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.utils import get_scope_ids, has_permission_on_any_source
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.proposal import (
    contributions,
    managers,
    models,
    proposal_roles,
    round_reports,
)
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import ProposalStates, RoundLifecycleStates
from waldur_mastermind.proposal.permissions import CALL_PERMISSION_SOURCES

# Proposals a round's ranking places: those still being evaluated.
RANKABLE_STATES = (ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW)

RECOMMENDED_PREFIX = "recommended:"
BASE_COLUMNS = (
    "proposal_uuid",
    "proposal_slug",
    "proposal_name",
    "rank",
    "final_score",
    "notes",
)
# The largest rank the database stores.
MAX_RANK = 2147483647
# A cell a spreadsheet would read as a formula.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
# The largest ranking file accepted: an export of a large round is a small
# fraction of it.
MAX_IMPORT_BYTES = 1024 * 1024
# Rows a ranking file may have beyond one per proposal of the round: blank
# lines and the like. Each row costs queries, so a file is never longer.
IMPORT_ROW_MARGIN = 20


# -- who reads and writes --------------------------------------------------------


def _managed_call_ids(user):
    call_ct = ContentType.objects.get_for_model(models.Call)
    org_ct = ContentType.objects.get_for_model(models.CallManagingOrganisation)
    on_call = get_scope_ids(user, call_ct, permission=PermissionEnum.UPDATE_CALL)
    on_org = get_scope_ids(user, org_ct, permission=PermissionEnum.UPDATE_CALL)
    return models.Call.objects.filter(
        Q(id__in=on_call) | Q(manager_id__in=on_org)
    ).values("id")


def _panel_call_ids(user):
    call_ct = ContentType.objects.get_for_model(models.Call)
    return get_scope_ids(user, call_ct, role=RoleEnum.CALL_PANEL_MEMBER)


def _conflicted_proposals(user):
    """Proposals ``user`` is on the applicant team of, or has a conflict of
    interest on record with."""
    proposal_ct = ContentType.objects.get_for_model(models.Proposal)
    on_team = permissions_models.UserRole.objects.filter(
        user=user, is_active=True, content_type=proposal_ct
    ).values("object_id")
    conflicts = models.ConflictOfInterest.objects.filter(
        reviewer__user=user, status__in=proposal_roles.BLOCKING_COI_STATUSES
    ).values("proposal_id")
    return models.Proposal.objects.filter(
        Q(created_by=user) | Q(id__in=on_team) | Q(id__in=conflicts)
    )


def readable_rounds(user):
    """Rounds whose ranking and meeting notes ``user`` reads."""
    rounds = models.Round.objects.all()
    if user.is_anonymous:
        return rounds.none()
    if user.is_staff:
        return rounds
    if not user.is_support:
        rounds = rounds.filter(
            Q(call_id__in=_managed_call_ids(user))
            | Q(call_id__in=_panel_call_ids(user))
            # A results reader, once the round's results are out.
            | Q(
                call_id__in=managers.get_results_readable_calls(user),
                lifecycle_state__in=RoundLifecycleStates.PUBLISHED,
            )
        )
    return rounds.exclude(id__in=_conflicted_proposals(user).values("round_id"))


def _reads_report_as_input(access, report) -> bool:
    """Whether ``access``'s user works on a later step of a proposal the
    report covers, a step reading the report's round step."""
    for proposal in report.proposals.all():
        instance = (
            models.ProposalWorkflowStepInstance.objects.filter(
                proposal=proposal, call_step_id=report.call_step_id
            )
            .select_related("call_step", "proposal__round__call")
            .first()
        )
        if (
            instance is not None
            and access.input_side(instance) == contributions.EVALUATOR_SIDE
        ):
            return True
    return False


def readable_report_ids(user):
    """Ids of the round reports ``user`` reads."""
    reports = models.RoundMeetingNote.objects.filter(call_step__isnull=False)
    if user.is_anonymous:
        return []
    if user.is_staff or user.is_support:
        return list(reports.values_list("id", flat=True))
    conflicted = _conflicted_proposals(user)
    reports = reports.exclude(proposals__in=conflicted)
    readable = set(
        reports.filter(
            Q(round__call_id__in=_managed_call_ids(user)) | Q(author=user)
        ).values_list("id", flat=True)
    )
    # The people of a later step reading the round step: only reports of a
    # step some step reads, on proposals where the user holds a role.
    held = models.ProposalRoleAssignment.objects.filter(user=user).values("proposal_id")
    candidates = (
        reports.exclude(id__in=readable)
        .filter(call_step__output_steps__isnull=False)
        .filter(
            Q(proposals__in=held)
            | Q(round__call_id__in=_call_role_call_ids(user))
            | Q(
                proposals__requestedresource__requested_offering__offering_id__in=(
                    contributions.offering_ids_managed_by(user)
                )
            )
        )
        .distinct()
        .prefetch_related("proposals")
    )
    access = contributions.Access(user)
    for report in candidates:
        if _reads_report_as_input(access, report):
            readable.add(report.pk)
    return list(readable)


def _call_role_call_ids(user):
    call_ct = ContentType.objects.get_for_model(models.Call)
    return permissions_models.UserRole.objects.filter(
        user=user, is_active=True, content_type=call_ct
    ).values("object_id")


def readable_rankings(user):
    """The overall rankings of the rounds ``user`` reads, and the rankings
    of the round reports they read, without a proposal they are on the team
    of or in conflict with."""
    if user.is_anonymous:
        return models.ProposalRanking.objects.none()
    rankings = models.ProposalRanking.objects.filter(
        Q(meeting_note__isnull=True, round__in=readable_rounds(user))
        | Q(meeting_note_id__in=readable_report_ids(user))
    )
    if not (user.is_staff or user.is_support):
        rankings = rankings.exclude(proposal__in=_conflicted_proposals(user))
    return rankings.select_related(
        "round__call", "proposal", "created_by", "meeting_note"
    )


def readable_meeting_notes(user):
    """The free notes of the rounds ``user`` reads, and the round reports
    they read."""
    return models.RoundMeetingNote.objects.filter(
        Q(call_step__isnull=True, round__in=readable_rounds(user))
        | Q(id__in=readable_report_ids(user))
    ).select_related("round__call", "author", "call_step__checklist")


def can_read(user, call_round) -> bool:
    return readable_rounds(user).filter(pk=call_round.pk).exists()


def can_write(user, call_round) -> bool:
    """Whether ``user`` edits the round's ranking and meeting notes."""
    if user.is_staff:
        return True
    return has_permission_on_any_source(
        user, PermissionEnum.UPDATE_CALL, call_round.call, CALL_PERMISSION_SOURCES
    ) and can_read(user, call_round)


def ensure_can_write(user, call_round):
    if not can_write(user, call_round):
        raise exceptions.PermissionDenied(
            _(
                "Only the call's managers can change the round's ranking and "
                "notes, and not while on the team of, or in conflict with, "
                "one of its proposals."
            )
        )


def can_write_ranking(user, call_round, meeting_note=None) -> bool:
    """Whether ``user`` writes the round's overall ranking, or with
    ``meeting_note`` the ranking within that round report.

    A report's ranking is its author's, while the report is a draft they
    still read and still write for (``round_reports.writes_for``). The
    overall ranking is the call's managers', and the call's panel chair's
    and vice-chair's.
    """
    if meeting_note is not None:
        return (
            meeting_note.author_id == user.pk
            and meeting_note.submitted_at is None
            and meeting_note.round_id == call_round.pk
            and meeting_note.pk in readable_report_ids(user)
            and round_reports.writes_for(user, meeting_note)
        )
    if can_write(user, call_round):
        return True
    return user.pk in proposal_permissions.panel_chair_ids(
        call_round.call
    ) and can_read(user, call_round)


def ensure_can_write_ranking(user, call_round, meeting_note=None):
    if not can_write_ranking(user, call_round, meeting_note):
        if meeting_note is not None:
            raise exceptions.PermissionDenied(
                _(
                    "Only the author of a round report ranks within it, until "
                    "the report is submitted."
                )
            )
        ensure_can_write(user, call_round)


def can_write_note(user, note) -> bool:
    """Whether ``user`` edits ``note``: a free note as the round's ranking,
    a round report by its author while it is a draft."""
    if note.call_step_id is None:
        return can_write(user, note.round)
    return can_write_ranking(user, note.round, note)


def ensure_not_on_team(user, proposal):
    """Nobody ranks a proposal they are on the team of, staff included."""
    if proposal_roles.is_on_applicant_team(user, proposal):
        raise exceptions.PermissionDenied(
            _("You cannot rank a proposal you are on the team of.")
        )


def is_ranking_locked(call_round) -> bool:
    return call_round.lifecycle_state in RoundLifecycleStates.RANKING_LOCKED


def ensure_ranking_open(call_round):
    if is_ranking_locked(call_round):
        raise exceptions.ValidationError(
            _(
                "The round is %(state)s: its ranking was fixed when it went to "
                "the decision."
            )
            % {"state": call_round.get_lifecycle_state_display()}
        )


def lock_round(call_round):
    return models.Round.objects.select_for_update().get(pk=call_round.pk)


# -- validation --------------------------------------------------------------------


def validate_proposal(call_round, proposal, meeting_note=None):
    if proposal.round_id != call_round.pk:
        raise exceptions.ValidationError(
            {"proposal": _("The proposal is not in this round.")}
        )
    if proposal.state not in RANKABLE_STATES:
        raise exceptions.ValidationError(
            {"proposal": _("Only proposals being evaluated can be ranked.")}
        )
    if (
        meeting_note is not None
        and not meeting_note.proposals.filter(pk=proposal.pk).exists()
    ):
        raise exceptions.ValidationError(
            {"proposal": _("The round report does not cover the proposal.")}
        )


def recommended_resources_detail(ranking):
    """The recommendation of ``ranking`` named for reading: per requested
    offering of the proposal, its name and each recommended component's
    name and unit, in the offering's component order. An offering or a
    component the proposal no longer has keeps its key as its name."""
    recommended = ranking.recommended_resources or {}
    if not recommended:
        return []
    requested = {
        requested_offering.uuid.hex: requested_offering
        for requested_offering in models.RequestedOffering.objects.filter(
            uuid__in=[str(key).replace("-", "") for key in recommended]
        )
        .select_related("offering")
        .prefetch_related("offering__components")
    }
    detail = []
    for offering_uuid, amounts in recommended.items():
        key = str(offering_uuid).replace("-", "")
        requested_offering = requested.get(key)
        components = (
            {
                component.type: component
                for component in requested_offering.offering.components.all()
            }
            if requested_offering is not None
            else {}
        )
        order = list(components)
        detail.append(
            {
                "requested_offering_uuid": key,
                "offering_name": (
                    requested_offering.offering.name
                    if requested_offering is not None
                    else key
                ),
                "components": [
                    {
                        "type": component_type,
                        "name": (
                            components[component_type].name or component_type
                            if component_type in components
                            else component_type
                        ),
                        "measured_unit": (
                            components[component_type].measured_unit
                            if component_type in components
                            else ""
                        ),
                        "amount": amount,
                    }
                    for component_type, amount in sorted(
                        (amounts or {}).items(),
                        key=lambda item: (
                            order.index(item[0]) if item[0] in order else len(order),
                            item[0],
                        ),
                    )
                ],
            }
        )
    return detail


def validate_recommended_resources(proposal, value):
    """Amounts per requested offering of the proposal, per offering component."""
    if not isinstance(value, dict):
        raise exceptions.ValidationError(
            {"recommended_resources": _("Expected an object.")}
        )
    requested = {
        resource.requested_offering.uuid.hex: resource.requested_offering
        for resource in proposal.requestedresource_set.select_related(
            "requested_offering__offering"
        )
    }
    cleaned = {}
    for offering_uuid, amounts in value.items():
        key = str(offering_uuid).replace("-", "")
        requested_offering = requested.get(key)
        if requested_offering is None:
            raise exceptions.ValidationError(
                {
                    "recommended_resources": _(
                        "%(uuid)s is not an offering the proposal requested."
                    )
                    % {"uuid": offering_uuid}
                }
            )
        if not isinstance(amounts, dict):
            raise exceptions.ValidationError(
                {"recommended_resources": _("Expected amounts per component.")}
            )
        components = set(
            requested_offering.offering.components.values_list("type", flat=True)
        )
        cleaned_amounts = {}
        for component, amount in amounts.items():
            if component not in components:
                raise exceptions.ValidationError(
                    {
                        "recommended_resources": _(
                            "%(offering)s has no component %(component)s."
                        )
                        % {
                            "offering": requested_offering.offering.name,
                            "component": component,
                        }
                    }
                )
            if amount is None or amount == "":
                continue
            if isinstance(amount, bool) or not isinstance(amount, int | float):
                raise exceptions.ValidationError(
                    {"recommended_resources": _("Amounts must be numbers.")}
                )
            if not math.isfinite(amount):
                raise exceptions.ValidationError(
                    {"recommended_resources": _("Amounts must be finite numbers.")}
                )
            if amount < 0:
                raise exceptions.ValidationError(
                    {"recommended_resources": _("Amounts cannot be negative.")}
                )
            cleaned_amounts[component] = amount
        if cleaned_amounts:
            cleaned[key] = cleaned_amounts
    return cleaned


def ensure_rank_free(call_round, rank, exclude_pk=None, meeting_note=None):
    taken = models.ProposalRanking.objects.filter(
        round=call_round, meeting_note=meeting_note, rank=rank
    )
    if exclude_pk is not None:
        taken = taken.exclude(pk=exclude_pk)
    holder = taken.select_related("proposal").first()
    if holder is not None:
        raise exceptions.ValidationError(
            {
                "rank": _("Rank %(rank)s is already given to %(proposal)s.")
                % {"rank": rank, "proposal": holder.proposal.name}
            }
        )


class RankingValuesSerializer(serializers.Serializer):
    """The values of one ranking, validated alike whether they come through the
    API or a line of an imported file."""

    rank = serializers.IntegerField(min_value=1, max_value=MAX_RANK)
    final_score = serializers.DecimalField(
        max_digits=10,
        decimal_places=3,
        required=False,
        allow_null=True,
        coerce_to_string=False,
    )
    notes = serializers.CharField(required=False, allow_blank=True)


# -- CSV -----------------------------------------------------------------------------


def _csv_safe(value):
    """``value`` with a leading quote when a spreadsheet would run it as a
    formula."""
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def _csv_unquote(value):
    """Undo ``_csv_safe`` on a value read back from an exported file."""
    if value.startswith("'") and value[1:].startswith(FORMULA_PREFIXES):
        return value[1:]
    return value


def _component_columns(call_round):
    """``recommended:<requested offering uuid>:<component type>`` per component
    of each offering of the round's call."""
    columns = []
    offerings = (
        models.RequestedOffering.objects.filter(call=call_round.call)
        .select_related("offering")
        .order_by("created", "id")
    )
    for requested_offering in offerings:
        for component in requested_offering.offering.components.order_by(
            "article_code", "type"
        ):
            columns.append(
                f"{RECOMMENDED_PREFIX}{requested_offering.uuid.hex}:{component.type}"
            )
    return columns


def _format_amount(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def export_csv(call_round, rankings, unranked_proposals=()):
    columns = [*BASE_COLUMNS, *_component_columns(call_round)]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    for ranking in rankings:
        row = {
            "proposal_uuid": ranking.proposal.uuid.hex,
            "proposal_slug": _csv_safe(ranking.proposal.slug),
            "proposal_name": _csv_safe(ranking.proposal.name),
            "rank": ranking.rank,
            "final_score": "" if ranking.final_score is None else ranking.final_score,
            "notes": _csv_safe(ranking.notes),
        }
        for offering_uuid, amounts in (ranking.recommended_resources or {}).items():
            for component, amount in amounts.items():
                column = f"{RECOMMENDED_PREFIX}{offering_uuid}:{component}"
                if column in columns:
                    row[column] = _format_amount(amount)
        writer.writerow(row)
    for proposal in unranked_proposals:
        writer.writerow(
            {
                "proposal_uuid": proposal.uuid.hex,
                "proposal_slug": _csv_safe(proposal.slug),
                "proposal_name": _csv_safe(proposal.name),
            }
        )
    return buffer.getvalue()


def _parse_amount(raw, line, column):
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        raise exceptions.ValidationError(
            {
                "file": _("Line %(line)s: %(column)s is not a number.")
                % {"line": line, "column": column}
            }
        )
    return int(value) if value.is_integer() else value


def _parse_values(row, line):
    """The rank, final score and notes of a line, validated as the API
    validates them."""
    raw_score = (row.get("final_score") or "").strip()
    serializer = RankingValuesSerializer(
        data={
            "rank": (row.get("rank") or "").strip(),
            "final_score": raw_score or None,
            "notes": _csv_unquote((row.get("notes") or "").strip()),
        }
    )
    if not serializer.is_valid():
        column, errors = next(iter(serializer.errors.items()))
        raise exceptions.ValidationError(
            {
                "file": _("Line %(line)s: %(column)s: %(error)s")
                % {"line": line, "column": column, "error": " ".join(errors)}
            }
        )
    return serializer.validated_data


def _find_proposal(call_round, row, line, user=None, meeting_note=None):
    """The proposal a line ranks: one of the round that ``user`` reads and
    that may be ranked here.

    Whatever stands in the way -- no such proposal, one the user does not
    read, one not being evaluated or not covered by the round report -- the
    line is refused alike, naming nothing: the import tells nothing of
    proposals beyond those the importer ranks.
    """
    proposals = models.Proposal.objects.filter(round=call_round)
    if user is not None:
        proposals = filter_queryset_for_user(proposals, user)
    uuid = (row.get("proposal_uuid") or "").strip().replace("-", "")
    slug = (row.get("proposal_slug") or "").strip()
    proposal = None
    try:
        if uuid:
            proposal = proposals.get(uuid=uuid)
        elif slug:
            proposal = proposals.get(slug=slug)
    except (models.Proposal.DoesNotExist, ValueError):
        pass
    if proposal is not None:
        try:
            validate_proposal(call_round, proposal, meeting_note)
        except exceptions.ValidationError:
            proposal = None
    if proposal is None:
        raise exceptions.ValidationError(
            {
                "file": _(
                    "Line %(line)s: no such proposal to rank here: none in "
                    "this round, or not being evaluated, or not covered by the "
                    "round report."
                )
                % {"line": line}
            }
        )
    return proposal


def parse_csv(call_round, content, meeting_note=None, user=None):
    """The rows of an imported ranking, validated; nothing is saved here.

    Proposals are looked up among those ``user`` reads, when given."""
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames or "rank" not in reader.fieldnames:
        raise exceptions.ValidationError(
            {"file": _("The file needs a header row with a rank column.")}
        )
    if not (
        "proposal_uuid" in reader.fieldnames or "proposal_slug" in reader.fieldnames
    ):
        raise exceptions.ValidationError(
            {"file": _("The file needs a proposal_uuid or proposal_slug column.")}
        )
    max_rows = (
        models.Proposal.objects.filter(round=call_round).count() + IMPORT_ROW_MARGIN
    )
    rows = []
    seen = set()
    for line, row in enumerate(reader, start=2):
        if line - 1 > max_rows:
            raise exceptions.ValidationError(
                {
                    "file": _(
                        "The file has more rows than the round has proposals; "
                        "at most %(max)s are read."
                    )
                    % {"max": max_rows}
                }
            )
        if not any((value or "").strip() for value in row.values() if value):
            continue
        raw_rank = (row.get("rank") or "").strip()
        if not raw_rank:
            # A proposal listed without a rank is left as it is, unread.
            continue
        proposal = _find_proposal(call_round, row, line, user, meeting_note)
        if proposal.pk in seen:
            raise exceptions.ValidationError(
                {
                    "file": _("Line %(line)s: the proposal is listed twice.")
                    % {"line": line}
                }
            )
        seen.add(proposal.pk)
        values = _parse_values(row, line)
        recommended = {}
        for column, raw in row.items():
            if not column or not column.startswith(RECOMMENDED_PREFIX):
                continue
            raw = (raw or "").strip()
            if not raw:
                continue
            try:
                offering_uuid, component = column[len(RECOMMENDED_PREFIX) :].split(
                    ":", 1
                )
            except ValueError:
                raise exceptions.ValidationError(
                    {
                        "file": _("Column %(column)s is not recognized.")
                        % {"column": column}
                    }
                )
            recommended.setdefault(offering_uuid, {})[component] = _parse_amount(
                raw, line, column
            )
        try:
            recommended = validate_recommended_resources(proposal, recommended)
        except exceptions.ValidationError as e:
            raise exceptions.ValidationError(
                {
                    "file": _("Line %(line)s: %(error)s")
                    % {"line": line, "error": e.detail["recommended_resources"]}
                }
            )
        rows.append(
            {
                "proposal": proposal,
                "rank": values["rank"],
                "final_score": values.get("final_score"),
                "recommended_resources": recommended,
                "notes": values.get("notes", ""),
                "line": line,
            }
        )
    return rows


@transaction.atomic
def import_rows(call_round, rows, user, meeting_note=None):
    """Create or update the ranking of each row; other rankings stay as they are.

    Ranks are checked across the round as it will be after the import, so a
    file can reorder proposals. All or nothing.
    """
    call_round = lock_round(call_round)
    ensure_ranking_open(call_round)
    for row in rows:
        if proposal_roles.is_on_applicant_team(user, row["proposal"]):
            raise exceptions.PermissionDenied(
                _("Line %(line)s: you cannot rank a proposal you are on the team of.")
                % {"line": row["line"]}
            )
    with connection.cursor() as cursor:
        # Ranks may be swapped row by row; they are checked together below.
        cursor.execute(
            "SET CONSTRAINTS proposal_ranking_unique_rank_per_round DEFERRED"
        )
    existing = {
        ranking.proposal_id: ranking
        for ranking in models.ProposalRanking.objects.filter(
            round=call_round, meeting_note=meeting_note
        )
    }
    final_ranks = {
        proposal_id: ranking.rank for proposal_id, ranking in existing.items()
    }
    for row in rows:
        final_ranks[row["proposal"].pk] = row["rank"]
    by_rank = {}
    for proposal_id, rank in final_ranks.items():
        by_rank.setdefault(rank, []).append(proposal_id)
    clashes = sorted(rank for rank, holders in by_rank.items() if len(holders) > 1)
    if clashes:
        raise exceptions.ValidationError(
            {
                "file": _("Rank %(ranks)s would be given to more than one proposal.")
                % {"ranks": ", ".join(str(rank) for rank in clashes)}
            }
        )
    created = updated = 0
    for row in rows:
        ranking = existing.get(row["proposal"].pk)
        if ranking is None:
            models.ProposalRanking.objects.create(
                round=call_round,
                meeting_note=meeting_note,
                proposal=row["proposal"],
                rank=row["rank"],
                final_score=row["final_score"],
                recommended_resources=row["recommended_resources"],
                notes=row["notes"],
                created_by=user,
            )
            created += 1
        else:
            ranking.rank = row["rank"]
            ranking.final_score = row["final_score"]
            ranking.recommended_resources = row["recommended_resources"]
            ranking.notes = row["notes"]
            ranking.save(
                update_fields=[
                    "rank",
                    "final_score",
                    "recommended_resources",
                    "notes",
                    "modified",
                ]
            )
            updated += 1
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SET CONSTRAINTS proposal_ranking_unique_rank_per_round IMMEDIATE"
            )
    except IntegrityError:
        # A rank taken by a ranking written while the file was read.
        raise exceptions.ValidationError(
            {"file": _("A rank in the file was given to another proposal meanwhile.")}
        )
    return created, updated


def save_guarded(func):
    """Run a ranking write, turning a rank taken concurrently into a 400."""
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                # Checked at once rather than at commit, so a clash is refused
                # here and not when an outer transaction ends.
                cursor.execute(
                    "SET CONSTRAINTS proposal_ranking_unique_rank_per_round IMMEDIATE"
                )
            return func()
    except IntegrityError:
        raise exceptions.ValidationError(
            {"rank": _("The rank or proposal is already ranked in this round.")}
        )
