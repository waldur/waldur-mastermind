import logging

from django.utils import timezone

from waldur_core.checklist.models import Question
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.proposal_helpers import call_detail_url
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.proposal.models import Call, CallResourceTemplate, Proposal

logger = logging.getLogger(__name__)


class GuideProposalTool(BaseTool):
    """Explains what is needed to submit a proposal to a specific call."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.GUIDE_PROPOSAL,
            description=(
                "Provide guidance on what is needed to submit a proposal to a specific call. "
                "Returns the call's requirements, available resources, deadlines, "
                "compliance checklist questions, and submission tips."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "call_name_or_uuid": {
                        "type": "string",
                        "description": "Name or UUID of the call to get guidance for.",
                    },
                },
                "required": ["call_name_or_uuid"],
            },
            route_utterances=[
                "What do I need to prepare for the Extreme Scale call?",
                "Help me understand the requirements for call X",
                "What documents are needed for the GPU Research Program?",
                "Guide me through the application process for this call",
                "What are the submission requirements and deadlines?",
                "How do I apply to the HPC allocation call?",
                "What fields do I need to fill for this proposal?",
            ],
            usage_instructions=(
                "Use when the user wants to understand a specific call's requirements:\n"
                "  ✓ 'What do I need for the Extreme Scale call?'\n"
                "  ✓ 'Help me prepare for call X'\n"
                "  ✓ 'What are the requirements for applying to the AI for Science call?'\n"
                "  ✗ 'Find calls for my project' — use find_matching_calls instead"
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        identifier = arguments.get("call_name_or_uuid", "")
        now = timezone.now()

        call = Call.objects.filter(uuid=identifier).first()
        if not call:
            call = Call.objects.filter(name__icontains=identifier).first()
        if not call:
            return {
                "type": "error",
                "summary": f"Call '{identifier}' not found.",
                "ui_component": "markdown",
                "ui_data": {
                    "c": f"Could not find a call matching '{identifier}'. Please check the name or UUID."
                },
            }

        rounds = call.round_set.all().order_by("start_time")
        open_rounds = rounds.filter(start_time__lte=now, cutoff_time__gte=now)
        upcoming_rounds = rounds.filter(start_time__gt=now)

        round_info = []
        for r in rounds:
            status = "closed"
            if r.start_time <= now <= r.cutoff_time:
                status = "open"
            elif r.start_time > now:
                status = "upcoming"
            round_info.append(
                {
                    "name": r.slug or f"Round {r.pk}",
                    "status": status,
                    "deadline": r.cutoff_time.isoformat() if r.cutoff_time else None,
                    "days_remaining": (r.cutoff_time - now).days
                    if r.cutoff_time and r.cutoff_time > now
                    else None,
                    "review_strategy": r.get_review_strategy_display()
                    if hasattr(r, "get_review_strategy_display")
                    else r.review_strategy,
                    "review_duration_days": r.review_duration_in_days,
                    "min_reviewers": r.minimum_number_of_reviewers,
                }
            )

        resource_templates = CallResourceTemplate.objects.filter(
            requested_offering__call=call
        ).select_related("requested_offering__offering")
        resources = []
        for tpl in resource_templates:
            offering_name = (
                tpl.requested_offering.offering.name
                if tpl.requested_offering and tpl.requested_offering.offering
                else tpl.name
            )
            resources.append(
                {
                    "name": tpl.name or offering_name,
                    "limits": tpl.limits if tpl.limits else {},
                    "attributes": tpl.attributes if tpl.attributes else {},
                    "is_required": tpl.is_required,
                    "description": tpl.description[:200] if tpl.description else "",
                }
            )

        checklist_questions = []
        if call.compliance_checklist_id:
            questions = Question.objects.filter(
                checklist_id=call.compliance_checklist_id
            ).order_by("order")
            for q in questions:
                checklist_questions.append(
                    {
                        "question": q.description[:200] if q.description else "",
                        "type": q.get_question_type_display()
                        if hasattr(q, "get_question_type_display")
                        else q.question_type,
                        "required": q.required,
                    }
                )

        proposal_stats = {
            "total": Proposal.objects.filter(round__call=call)
            .exclude(state=Proposal.States.CANCELED)
            .count(),
            "accepted": Proposal.objects.filter(
                round__call=call, state=Proposal.States.ACCEPTED
            ).count(),
        }

        call_data = {
            "name": call.name,
            "description": call.description[:1000] if call.description else "",
            "state": call.state,
            "rounds": round_info,
            "open_rounds_count": open_rounds.count(),
            "upcoming_rounds_count": upcoming_rounds.count(),
            "available_resources": resources,
            "compliance_questions": checklist_questions,
            "proposal_stats": proposal_stats,
            "fixed_duration_days": call.fixed_duration_in_days,
            "reviewer_identity_visible": call.reviewer_identity_visible_to_submitters,
            "reviews_visible": call.reviews_visible_to_submitters,
        }

        call_url = call_detail_url(str(call.uuid))
        call_data["url"] = call_url

        nav_links = [
            {"label": f"View call: {call.name}", "url": call_url, "variant": "primary"},
        ]

        summary = f"Guidance for call '{call.name}': {len(round_info)} rounds, {len(resources)} resource types, {len(checklist_questions)} compliance questions."

        return {
            "type": "success",
            "data": call_data,
            "summary": summary,
            "ui_component": "homeport_nav",
            "ui_data": {"links": nav_links, "context": summary},
        }


tool_registry.register(GuideProposalTool())
