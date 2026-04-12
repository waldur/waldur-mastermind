import logging

from django.db.models import Avg, Count, Q

from waldur_core.checklist.models import Answer, ChecklistCompletion
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.proposal_helpers import call_detail_url
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.proposal.models import Proposal, RequestedResource, Review

logger = logging.getLogger(__name__)


class ProposalOverviewTool(BaseTool):
    """Generates a structured summary of a proposal for reviewers and managers."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.PROPOSAL_OVERVIEW,
            description=(
                "Summarize a specific proposal: project details, team, "
                "resource requests, review status, and compliance. "
                "Useful for reviewers starting a review or managers scanning submissions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "proposal_identifier": {
                        "type": "string",
                        "description": "Proposal slug (e.g., 'R1-001') or UUID.",
                    },
                },
                "required": ["proposal_identifier"],
            },
            route_utterances=[
                "Summarize proposal P-023",
                "Tell me about proposal R1-001",
                "What is this proposal about?",
                "Give me an overview of the quantum computing proposal",
                "Show proposal details and review status",
            ],
            usage_instructions=(
                "Use when the user asks about a specific proposal:\n"
                "  ✓ 'Summarize proposal P-023'\n"
                "  ✓ 'Tell me about proposal R1-001'\n"
                "  ✓ 'What is proposal XYZ about?'\n"
                "  ✗ 'Show all proposals' — not for listing"
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        identifier = arguments.get("proposal_identifier", "")

        proposal = (
            filter_queryset_for_user(Proposal.objects.all(), user)
            .filter(Q(uuid=identifier) | Q(slug=identifier))
            .select_related(
                "round__call",
                "created_by",
                "project",
            )
            .first()
        )

        if not proposal:
            return {
                "type": "error",
                "summary": f"Proposal '{identifier}' not found or you don't have access.",
                "ui_component": "markdown",
                "ui_data": {
                    "c": f"Could not find proposal '{identifier}'. Check the slug or UUID."
                },
            }

        call = proposal.round.call if proposal.round else None
        round_obj = proposal.round

        resource_requests = RequestedResource.objects.filter(
            proposal=proposal
        ).select_related("requested_offering__offering", "call_resource_template")

        resources = []
        for rr in resource_requests:
            offering_name = (
                rr.requested_offering.offering.name
                if rr.requested_offering and rr.requested_offering.offering
                else ""
            )
            resources.append(
                {
                    "offering": offering_name,
                    "template": rr.call_resource_template.name
                    if rr.call_resource_template
                    else "",
                    "limits": rr.limits if rr.limits else {},
                }
            )

        reviews = Review.objects.filter(proposal=proposal)
        review_stats = reviews.aggregate(
            total=Count("id"),
            completed=Count("id", filter=Q(state=Review.States.SUBMITTED)),
            avg_score=Avg("summary_score", filter=Q(state=Review.States.SUBMITTED)),
        )

        compliance_status = None
        try:
            completion = ChecklistCompletion.objects.filter(
                scope_content_type__model="proposal",
                scope_object_id=proposal.pk,
            ).first()
            if completion:
                total_questions = completion.checklist.questions.count()
                answered = Answer.objects.filter(completion=completion).count()
                compliance_status = {
                    "total_questions": total_questions,
                    "answered": answered,
                    "is_completed": completion.is_completed,
                    "requires_review": completion.requires_review,
                }
        except Exception:
            pass

        deadline = None
        if round_obj and round_obj.cutoff_time:
            deadline = round_obj.cutoff_time.isoformat()

        proposal_data = {
            "slug": proposal.slug,
            "name": proposal.name,
            "state": proposal.state,
            "created_by": proposal.created_by.full_name if proposal.created_by else "",
            "created_by_org": proposal.created_by.organization
            if proposal.created_by
            else "",
            "call_name": call.name if call else "",
            "round_name": round_obj.slug if round_obj else "",
            "project_summary": proposal.project_summary[:500]
            if proposal.project_summary
            else "",
            "duration_days": proposal.duration_in_days,
            "is_confidential": proposal.project_is_confidential,
            "resource_requests": resources,
            "reviews": {
                "total": review_stats["total"],
                "completed": review_stats["completed"],
                "avg_score": round(review_stats["avg_score"], 1)
                if review_stats["avg_score"]
                else None,
            },
            "compliance": compliance_status,
            "deadline": deadline,
        }

        summary = (
            f"Proposal '{proposal.slug}' ({proposal.state}) in call '{call.name if call else 'N/A'}': "
            f"{review_stats['completed']}/{review_stats['total']} reviews completed"
            f"{', avg score ' + str(round(review_stats['avg_score'], 1)) if review_stats['avg_score'] else ''}."
        )

        nav_links = []
        if call:
            nav_links.append(
                {
                    "label": f"View call: {call.name}",
                    "url": call_detail_url(str(call.uuid)),
                    "variant": "secondary",
                }
            )

        return {
            "type": "success",
            "data": proposal_data,
            "summary": summary,
            "ui_component": "homeport_nav",
            "ui_data": {"links": nav_links, "context": summary},
        }


tool_registry.register(ProposalOverviewTool())
