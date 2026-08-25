import logging

from django.db.models import Avg, Count, Q

from waldur_core.checklist.models import Answer, ChecklistCompletion
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.chat.tools.account.helpers import validate_uuid
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
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
            category=ToolCategory.PROPOSALS_RESEARCHER,
            description=(
                "Summarize a specific proposal: project details, team, "
                "resource requests, review status, and compliance. "
                "Useful for reviewers starting a review or managers scanning submissions. "
                "If the proposal is accepted and the user wants to see "
                "the granted resources in the project, bridge to the "
                "`account` category (get_project_resources / get_resource_usage).\n"
                "\n"
                "After narrating the proposal's status / reviews / "
                "compliance, ALWAYS close with a single inline markdown "
                "link: `[View call](url)` using the parent call's `url` "
                "field verbatim. Do NOT emit a separate pill button — the "
                "inline link is the only CTA."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Proposal UUID.",
                    },
                    "slug": {
                        "type": "string",
                        "description": (
                            "Structured slug (e.g. 'R1-001'). Use when the user "
                            "references a proposal by its short ID."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": "Exact or partial proposal name.",
                    },
                },
                # At-least-one of uuid/slug/name is enforced in execute().
            },
            usage_instructions=(
                "Use when the user asks about a specific proposal:\n"
                "  ✓ 'Summarize proposal P-023'\n"
                "  ✓ 'Tell me about proposal R1-001'\n"
                "  ✓ 'What is proposal XYZ about?'\n"
                "  ✗ 'Show all proposals' — not for listing\n"
                "\n"
                "Slugs (R1-001) and names are NOT interchangeable — pass the "
                "right one explicitly.\n"
                "\n"
                "Picking `uuid` vs `name`: fresh from this turn → `uuid`; from an "
                "earlier turn or typed by the user → `name`. Prefer `name` in doubt — "
                "UUIDs from earlier turns may be stale or fabricated. Never pass a "
                "UUID into `name` or `slug`."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        proposal_uuid = (arguments.get("uuid") or "").strip()
        proposal_slug = (arguments.get("slug") or "").strip()
        proposal_name = (arguments.get("name") or "").strip()

        if not proposal_uuid and not proposal_slug and not proposal_name:
            return {
                "type": "validation_error",
                "summary": "Pass at least one of `uuid`, `slug`, or `name`.",
            }
        if proposal_uuid and not validate_uuid(proposal_uuid):
            return {
                "type": "validation_error",
                "summary": f"Invalid UUID for uuid: {proposal_uuid}",
            }

        qs = filter_queryset_for_user(Proposal.objects.all(), user).select_related(
            "round__call",
            "created_by",
            "project",
        )
        if proposal_uuid:
            proposal = qs.filter(uuid=proposal_uuid).first()
        elif proposal_slug:
            proposal = qs.filter(slug=proposal_slug).first()
        else:
            proposal = qs.filter(name__icontains=proposal_name).first()

        if not proposal:
            identifier = proposal_uuid or proposal_slug or proposal_name
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

        # Expose the parent call's URL on the data so the LLM can drop
        # it into the closing inline markdown link.
        if call:
            proposal_data["url"] = call_detail_url(str(call.uuid))

        # Render directive in the result summary — LLM weights tool
        # results higher than usage_instructions for "what to do next".
        score_clause = (
            f", avg score {round(review_stats['avg_score'], 1)}"
            if review_stats["avg_score"]
            else ""
        )
        summary = (
            f"Proposal '{proposal.slug}' ({proposal.state}) in call "
            f"'{call.name if call else 'N/A'}': "
            f"{review_stats['completed']}/{review_stats['total']} reviews "
            f"completed{score_clause}. Close your reply with one inline "
            "markdown link: `[View call](url)` using the parent call's "
            "`url` field verbatim."
        )

        return {
            "type": "success",
            "data": proposal_data,
            "summary": summary,
        }


tool_registry.register(ProposalOverviewTool())
