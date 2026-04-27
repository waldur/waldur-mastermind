import logging

from django.db.models import Avg, Count, Q, StdDev
from django.utils import timezone

from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.chat.tools.account.helpers import validate_uuid
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.models import (
    Call,
    ConflictOfInterest,
    Proposal,
    Review,
)

logger = logging.getLogger(__name__)


class CallInsightsTool(BaseTool):
    """Provides an intelligence briefing on a call's health: submissions, reviews, anomalies."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.CALL_INSIGHTS,
            category=ToolCategory.PROPOSALS_REVIEWER,
            description=(
                "Analyze the health of a specific call: submission trends, "
                "review bottlenecks, score patterns, and actionable recommendations. "
                "Available to staff users and call managers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": (
                            "Call UUID. Use when you have it fresh from this turn's "
                            "tool output."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "Exact or partial call name. Use when the call was named "
                            "in an earlier turn and its UUID isn't in your context."
                        ),
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use when a call manager or staff wants a status briefing:\n"
                "  ✓ 'How is the AI for Science call going?'\n"
                "  ✓ 'Give me call insights'\n"
                "  ✓ 'Any issues with Round 2?'\n"
                "  ✓ 'Call status report'\n"
                "  ✗ Regular users asking about their own proposals\n"
                "\n"
                "Omit both `uuid` and `name` to list every call this user can see.\n"
                "\n"
                "Picking `uuid` vs `name`: fresh from this turn → `uuid`; from an "
                "earlier turn or typed by the user → `name`. Prefer `name` in doubt — "
                "UUIDs from earlier turns may be stale or fabricated. Never pass a "
                "UUID into `name` or vice versa."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        if not user.is_staff:
            accessible_calls = filter_queryset_for_user(Call.objects.all(), user)
            if not accessible_calls.exists():
                return {
                    "type": "error",
                    "summary": "You don't have permission to view call insights.",
                    "ui_component": "markdown",
                    "ui_data": {
                        "c": "Call insights are available to staff and call managers only."
                    },
                }

        call_uuid = (arguments.get("uuid") or "").strip()
        call_name = (arguments.get("name") or "").strip()

        if call_uuid and not validate_uuid(call_uuid):
            return {
                "type": "validation_error",
                "summary": f"Invalid UUID for uuid: {call_uuid}",
            }

        now = timezone.now()

        if user.is_staff:
            call_qs = Call.objects.all()
        else:
            call_qs = filter_queryset_for_user(Call.objects.all(), user)

        if call_uuid or call_name:
            if call_uuid:
                call = call_qs.filter(uuid=call_uuid).first()
            else:
                call = call_qs.filter(name__icontains=call_name).first()
            if not call:
                identifier = call_uuid or call_name
                return {
                    "type": "error",
                    "summary": f"Call '{identifier}' not found.",
                    "ui_component": "markdown",
                    "ui_data": {"c": f"Could not find a call matching '{identifier}'."},
                }
            calls = [call]
        else:
            calls = list(call_qs.filter(state=Call.States.ACTIVE)[:10])

        insights = []
        for call in calls:
            proposals = Proposal.objects.filter(round__call=call)
            proposal_counts = proposals.aggregate(
                total=Count("id"),
                draft=Count("id", filter=Q(state=ProposalStates.DRAFT)),
                submitted=Count("id", filter=Q(state=ProposalStates.SUBMITTED)),
                in_review=Count("id", filter=Q(state=ProposalStates.IN_REVIEW)),
                accepted=Count("id", filter=Q(state=ProposalStates.ACCEPTED)),
                rejected=Count("id", filter=Q(state=ProposalStates.REJECTED)),
            )

            reviews = Review.objects.filter(proposal__round__call=call)
            review_stats = reviews.aggregate(
                total=Count("id"),
                completed=Count("id", filter=Q(state=Review.States.SUBMITTED)),
                in_progress=Count("id", filter=Q(state=Review.States.IN_REVIEW)),
                avg_score=Avg("summary_score", filter=Q(state=Review.States.SUBMITTED)),
                score_stddev=StdDev(
                    "summary_score", filter=Q(state=Review.States.SUBMITTED)
                ),
            )

            open_rounds = call.round_set.filter(
                start_time__lte=now, cutoff_time__gte=now
            )
            nearest_deadline = None
            for r in open_rounds:
                if nearest_deadline is None or r.cutoff_time < nearest_deadline:
                    nearest_deadline = r.cutoff_time

            proposals_without_reviews = (
                proposals.filter(
                    state__in=[ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW]
                )
                .annotate(review_count=Count("review"))
                .filter(review_count=0)
                .count()
            )

            overdue_reviews = reviews.filter(
                state=Review.States.IN_REVIEW,
            ).count()

            coi_count = ConflictOfInterest.objects.filter(
                proposal__round__call=call,
            ).count()

            total_decided = (proposal_counts["accepted"] or 0) + (
                proposal_counts["rejected"] or 0
            )
            acceptance_rate = (
                round((proposal_counts["accepted"] or 0) / total_decided * 100, 1)
                if total_decided > 0
                else None
            )

            insights.append(
                {
                    "call_name": call.name,
                    "call_uuid": str(call.uuid),
                    "state": call.state,
                    "proposals": proposal_counts,
                    "reviews": {
                        "total": review_stats["total"],
                        "completed": review_stats["completed"],
                        "in_progress": review_stats["in_progress"],
                        "avg_score": round(review_stats["avg_score"], 2)
                        if review_stats["avg_score"]
                        else None,
                        "score_stddev": round(review_stats["score_stddev"], 2)
                        if review_stats["score_stddev"]
                        else None,
                    },
                    "nearest_deadline": nearest_deadline.isoformat()
                    if nearest_deadline
                    else None,
                    "days_until_deadline": (nearest_deadline - now).days
                    if nearest_deadline
                    else None,
                    "proposals_without_reviews": proposals_without_reviews,
                    "overdue_reviews": overdue_reviews,
                    "coi_detected": coi_count,
                    "acceptance_rate": acceptance_rate,
                }
            )

        summary = (
            f"Insights for {len(insights)} call{'s' if len(insights) != 1 else ''}."
        )

        return {
            "type": "success",
            "data": {"insights": insights, "total": len(insights)},
            "summary": summary,
        }


tool_registry.register(CallInsightsTool())
