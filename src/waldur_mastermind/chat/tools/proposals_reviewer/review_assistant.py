import logging

from django.db.models import Avg, Count

from waldur_core.checklist.models import Answer, ChecklistCompletion
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.chat.tools.account.helpers import validate_uuid
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.proposal.models import (
    AssignmentItem,
    ConflictOfInterest,
    Proposal,
    RequestedResource,
    Review,
)

logger = logging.getLogger(__name__)


class ReviewAssistantTool(BaseTool):
    """AI co-pilot for reviewers: analyzes a proposal against call criteria and suggests focus areas."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.REVIEW_ASSISTANT,
            category=ToolCategory.PROPOSALS_REVIEWER,
            description=(
                "Help a reviewer evaluate a specific proposal. Analyzes the proposal "
                "against the call's criteria and provides structured guidance: strengths, "
                "areas to probe, suggested questions, and comparative context. "
                "Only available to reviewers assigned to the proposal."
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
                "Use when a reviewer asks for help evaluating a proposal:\n"
                "  ✓ 'Help me review proposal P-023'\n"
                "  ✓ 'What should I focus on for R1-005?'\n"
                "  ✓ 'Analyze proposal P-023 for my review'\n"
                "  ✗ 'Summarize proposal P-023' — use proposal_overview instead\n"
                "  ✗ Users who are not assigned reviewers for the proposal\n"
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
                "summary": f"Proposal '{identifier}' not found.",
                "ui_component": "markdown",
                "ui_data": {"c": f"Could not find proposal '{identifier}'."},
            }

        is_assigned = (
            Review.objects.filter(proposal=proposal, reviewer=user).exists()
            or AssignmentItem.objects.filter(
                proposal=proposal,
                batch__reviewer_pool_entry__reviewer__user=user,
            ).exists()
            or user.is_staff
        )

        if not is_assigned:
            return {
                "type": "error",
                "summary": "You are not assigned to review this proposal.",
                "ui_component": "markdown",
                "ui_data": {
                    "c": "The review assistant is only available to assigned reviewers and staff."
                },
            }

        call = proposal.round.call if proposal.round else None

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
                    "attributes": rr.attributes if rr.attributes else {},
                }
            )

        compliance_answers = []
        try:
            completion = ChecklistCompletion.objects.filter(
                scope_content_type__model="proposal",
                scope_object_id=proposal.pk,
            ).first()
            if completion:
                answers = Answer.objects.filter(completion=completion).select_related(
                    "question"
                )
                for a in answers:
                    compliance_answers.append(
                        {
                            "question": a.question.description[:200]
                            if a.question
                            else "",
                            "answer": str(a.answer_data)[:200] if a.answer_data else "",
                        }
                    )
        except Exception:
            pass

        other_proposals = (
            Proposal.objects.filter(round__call=call)
            .exclude(pk=proposal.pk)
            .exclude(state=Proposal.States.CANCELED)
        )
        context_stats = other_proposals.aggregate(
            count=Count("id"),
            avg_resources=Count("requestedresource", distinct=True),
        )

        avg_resource_limits = {}
        if call:
            all_resources = RequestedResource.objects.filter(
                proposal__round__call=call
            ).exclude(proposal=proposal)
            for rr in all_resources:
                if rr.limits:
                    for key, value in rr.limits.items():
                        if key not in avg_resource_limits:
                            avg_resource_limits[key] = {"total": 0, "count": 0}
                        try:
                            avg_resource_limits[key]["total"] += float(value)
                            avg_resource_limits[key]["count"] += 1
                        except (TypeError, ValueError):
                            pass

        avg_limits_summary = {
            k: round(v["total"] / v["count"], 1) if v["count"] > 0 else 0
            for k, v in avg_resource_limits.items()
        }

        coi_for_user = ConflictOfInterest.objects.filter(
            reviewer__user=user, proposal=proposal
        ).values_list("coi_type", flat=True)

        existing_reviews = Review.objects.filter(
            proposal=proposal, state=Review.States.SUBMITTED
        ).aggregate(
            count=Count("id"),
            avg_score=Avg("summary_score"),
        )

        review_data = {
            "proposal": {
                "slug": proposal.slug,
                "name": proposal.name,
                "state": proposal.state,
                "created_by": proposal.created_by.full_name
                if proposal.created_by
                else "",
                "organization": proposal.created_by.organization
                if proposal.created_by
                else "",
                "project_summary": proposal.project_summary[:1000]
                if proposal.project_summary
                else "",
                "duration_days": proposal.duration_in_days,
                "is_confidential": proposal.project_is_confidential,
                "has_civilian_purpose": proposal.project_has_civilian_purpose,
            },
            "call_name": call.name if call else "",
            "call_description": call.description[:500]
            if call and call.description
            else "",
            "resource_requests": resources,
            "compliance_answers": compliance_answers,
            "comparative_context": {
                "other_proposals_count": context_stats["count"],
                "avg_resource_limits": avg_limits_summary,
            },
            "existing_reviews": {
                "count": existing_reviews["count"],
                "avg_score": round(existing_reviews["avg_score"], 1)
                if existing_reviews["avg_score"]
                else None,
            },
            "coi_flags": list(coi_for_user),
            "disclaimer": "This AI analysis is advisory only. The reviewer makes the final evaluation.",
        }

        summary = (
            f"Review guide for '{proposal.slug}' ({proposal.name}) in call '{call.name if call else 'N/A'}'. "
            f"{len(resources)} resource requests, {len(compliance_answers)} compliance answers, "
            f"{existing_reviews['count']} other reviews submitted."
        )

        return {
            "type": "success",
            "data": review_data,
            "summary": summary,
        }


tool_registry.register(ReviewAssistantTool())
