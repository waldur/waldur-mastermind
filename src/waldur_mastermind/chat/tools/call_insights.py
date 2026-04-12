import logging

from django.db.models import Avg, Count, Q, StdDev
from django.utils import timezone

from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolName
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
            description=(
                "Analyze the health of a specific call: submission trends, "
                "review bottlenecks, score patterns, and actionable recommendations. "
                "Available to staff users and call managers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "call_name_or_uuid": {
                        "type": "string",
                        "description": "Name or UUID of the call to analyze. Omit to see all active calls.",
                    },
                },
                "required": [],
            },
            route_utterances=[
                "How is the AI for Science call going?",
                "Give me call insights and statistics",
                "Any issues with Round 2?",
                "Call status report for HPC allocation",
                "Show me the submission trends and review bottlenecks",
                "What's the acceptance rate for the current call?",
            ],
            usage_instructions=(
                "Use when a call manager or staff wants a status briefing:\n"
                "  ✓ 'How is the AI for Science call going?'\n"
                "  ✓ 'Give me call insights'\n"
                "  ✓ 'Any issues with Round 2?'\n"
                "  ✓ 'Call status report'\n"
                "  ✗ Regular users asking about their own proposals"
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

        identifier = arguments.get("call_name_or_uuid")
        now = timezone.now()

        if user.is_staff:
            call_qs = Call.objects.all()
        else:
            call_qs = filter_queryset_for_user(Call.objects.all(), user)

        if identifier:
            call = call_qs.filter(uuid=identifier).first()
            if not call:
                call = call_qs.filter(name__icontains=identifier).first()
            if not call:
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
            "ui_component": "markdown",
            "ui_data": {"c": summary},
        }


tool_registry.register(CallInsightsTool())
