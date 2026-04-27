import logging

from django.utils import timezone

from waldur_core.checklist.models import Question
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.chat.tools.account.helpers import validate_uuid
from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
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
            category=ToolCategory.PROPOSALS_RESEARCHER,
            description=(
                "Provide guidance on what is needed to submit a proposal to a specific call. "
                "Returns the call's requirements, available resources, deadlines, "
                "compliance checklist questions, and submission tips.\n"
                "\n"
                "After narrating the call's requirements / resources / "
                "checklist, ALWAYS close with a single inline markdown "
                "link: `[View call](url)` using the call's `url` field "
                "verbatim. Do NOT emit a separate pill button — the inline "
                "link is the only CTA."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": (
                            "Call UUID. Use when you have it fresh from this turn's "
                            "tool output (e.g. find_matching_calls)."
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
                # At-least-one of uuid/name is enforced in execute().
            },
            usage_instructions=(
                "Use when the user wants to understand a specific call's requirements:\n"
                "  ✓ 'What do I need for the Extreme Scale call?'\n"
                "  ✓ 'Help me prepare for call X'\n"
                "  ✓ 'What are the requirements for applying to the AI for Science call?'\n"
                "  ✗ 'Find calls for my project' — use find_matching_calls instead\n"
                "\n"
                "Picking `uuid` vs `name`: fresh from this turn → `uuid`; from an "
                "earlier turn or typed by the user → `name`. Prefer `name` in doubt — "
                "UUIDs from earlier turns may be stale or fabricated. Never pass a "
                "UUID into `name` or vice versa."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        call_uuid = (arguments.get("uuid") or "").strip()
        call_name = (arguments.get("name") or "").strip()

        if not call_uuid and not call_name:
            return {
                "type": "validation_error",
                "summary": "Pass either `uuid` or `name`.",
            }
        if call_uuid and not validate_uuid(call_uuid):
            return {
                "type": "validation_error",
                "summary": f"Invalid UUID for uuid: {call_uuid}",
            }

        now = timezone.now()

        qs = filter_queryset_for_user(Call.objects.all(), user)
        if call_uuid:
            call = qs.filter(uuid=call_uuid).first()
        else:
            call = qs.filter(name__icontains=call_name).first()
        if not call:
            identifier = call_uuid or call_name
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

        # Render directive in the result summary — LLM weights tool
        # results higher than usage_instructions for "what to do next".
        summary = (
            f"Guidance for call '{call.name}': {len(round_info)} rounds, "
            f"{len(resources)} resource types, {len(checklist_questions)} "
            "compliance questions. Close your reply with one inline "
            "markdown link: `[View call](url)` using the call's `url` "
            "field verbatim."
        )

        return {
            "type": "success",
            "data": call_data,
            "summary": summary,
        }


tool_registry.register(GuideProposalTool())
