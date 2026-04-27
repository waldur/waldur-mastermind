import logging

from django.utils import timezone

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.proposal.models import (
    AssignmentItem,
    RequestedResource,
    Review,
    ReviewerStats,
)

logger = logging.getLogger(__name__)

_MAX_REVIEWS = 30


class ReviewWorkloadTool(BaseTool):
    """Summarizes a reviewer's pending work with prioritization by deadline and complexity."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.REVIEW_WORKLOAD,
            category=ToolCategory.PROPOSALS_REVIEWER,
            description=(
                "Show the reviewer's current workload: pending reviews "
                "prioritized by deadline, pending assignment invitations, "
                "and review statistics. Drill into one row with "
                "review_assistant (assigned reviewers only) for evaluation "
                "guidance, or proposal_overview to scan the proposal."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            usage_instructions=(
                "Use when the user asks about their review workload:\n"
                "  ✓ 'What reviews do I have?'\n"
                "  ✓ 'Show my pending reviews'\n"
                "  ✓ 'What's on my review plate?'\n"
                "  ✓ 'Do I have any urgent reviews?'\n"
                "  ✗ 'Show my proposals' — use find_matching_calls or proposal_overview"
            ),
            workflow_instructions="""\
=== REVIEWER WORKFLOW ===
review_workload → review_assistant: surface pending reviews, then
drill into one for structured evaluation guidance. review_assistant
only works for proposals the user is actually assigned to.

call_insights is a separate lens — call manager / staff "how is
call X going?" briefings, not per-proposal review work.

To deep-read a proposal a reviewer is evaluating, also load the
`proposals_researcher` category and call proposal_overview alongside
proposals_reviewer in the same search_tools batch.\
""",
        )

    def execute(self, user, arguments: dict) -> dict:
        now = timezone.now()

        reviews = (
            Review.objects.filter(reviewer=user)
            .select_related(
                "proposal",
                "proposal__round",
                "proposal__round__call",
            )
            .order_by("created")[:_MAX_REVIEWS]
        )

        review_items = []
        for review in reviews:
            proposal = review.proposal
            round_obj = proposal.round if proposal else None
            call = round_obj.call if round_obj else None

            deadline = None
            days_remaining = None
            if review.review_end_date:
                deadline = review.review_end_date.isoformat()
                try:
                    delta = review.review_end_date - now
                    days_remaining = delta.days
                except TypeError:
                    days_remaining = None

            resource_count = (
                RequestedResource.objects.filter(proposal=proposal).count()
                if proposal
                else 0
            )

            review_items.append(
                {
                    "proposal_slug": proposal.slug if proposal else "",
                    "proposal_name": proposal.name if proposal else "",
                    "call_name": call.name if call else "",
                    "state": review.state,
                    "summary_score": review.summary_score,
                    "deadline": deadline,
                    "days_remaining": days_remaining,
                    "resource_request_count": resource_count,
                }
            )

        pending_assignments = AssignmentItem.objects.filter(
            batch__reviewer_pool_entry__reviewer__user=user,
            status=AssignmentItem.Statuses.PENDING,
        ).select_related(
            "proposal",
            "batch",
            "batch__reviewer_pool_entry__call",
        )

        pending_items = []
        for item in pending_assignments:
            batch = item.batch
            call = batch.reviewer_pool_entry.call if batch.reviewer_pool_entry else None
            pending_items.append(
                {
                    "proposal_slug": item.proposal.slug if item.proposal else "",
                    "proposal_name": item.proposal.name if item.proposal else "",
                    "call_name": call.name if call else "",
                    "affinity_score": item.affinity_score,
                    "has_coi": item.has_coi,
                    "batch_expires": batch.expires_at.isoformat()
                    if batch.expires_at
                    else None,
                }
            )

        stats = None
        try:
            reviewer_stats = ReviewerStats.objects.get(reviewer_profile__user=user)
            stats = {
                "total_completed": reviewer_stats.total_reviews_completed,
                "total_declined": reviewer_stats.total_reviews_declined,
                "avg_score": reviewer_stats.average_score_given,
                "avg_review_time_days": reviewer_stats.average_review_time_days,
            }
        except ReviewerStats.DoesNotExist:
            pass

        in_review = [r for r in review_items if r["state"] == Review.States.IN_REVIEW]
        submitted = [r for r in review_items if r["state"] == Review.States.SUBMITTED]

        summary = (
            f"{len(in_review)} review{'s' if len(in_review) != 1 else ''} in progress, "
            f"{len(submitted)} completed, "
            f"{len(pending_items)} pending invitation{'s' if len(pending_items) != 1 else ''}."
        )

        return {
            "type": "success",
            "data": {
                "reviews": review_items,
                "pending_assignments": pending_items,
                "stats": stats,
                "in_review_count": len(in_review),
                "submitted_count": len(submitted),
                "pending_invitation_count": len(pending_items),
            },
            "summary": summary,
        }


tool_registry.register(ReviewWorkloadTool())
