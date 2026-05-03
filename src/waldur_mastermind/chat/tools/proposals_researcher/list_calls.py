import logging

from django.db.models import Q
from django.utils import timezone

from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.chat.tools.account.helpers import validate_uuid
from waldur_mastermind.chat.tools.base import (
    MAX_LIST_RESULTS,
    BaseTool,
    ToolDefinition,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.proposal_helpers import call_detail_url
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.proposal.enums import CallStates, RoundStatuses
from waldur_mastermind.proposal.models import Call

logger = logging.getLogger(__name__)

_VALID_CALL_STATES = {s for s, _ in CallStates.CHOICES}
_VALID_ROUND_STATUSES = set(RoundStatuses.VALUES)


class ListCallsTool(BaseTool):
    """List calls visible to the user, filterable by state, round status, manager, or text."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.LIST_CALLS,
            category=ToolCategory.PROPOSALS_RESEARCHER,
            description=(
                "Browse calls visible to the current user, optionally filtered "
                "by call state, round status, managing organisation, or free-text "
                "search. Use for general browsing — for matchmaking against a "
                "research project use find_matching_calls."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": list(_VALID_CALL_STATES),
                        "description": (
                            "Filter by call state. Defaults to 'active' when "
                            "omitted (most common user intent)."
                        ),
                    },
                    "round_status": {
                        "type": "string",
                        "enum": list(_VALID_ROUND_STATUSES),
                        "description": (
                            "Filter to calls that have at least one round in this "
                            "status. 'open' = currently accepting submissions."
                        ),
                    },
                    "manager_uuid": {
                        "type": "string",
                        "format": "uuid",
                        "description": (
                            "CallManagingOrganisation UUID (fresh from this turn)."
                        ),
                    },
                    "manager_name": {
                        "type": "string",
                        "description": (
                            "Managing organisation name (typed by user or "
                            "carried over from an earlier turn)."
                        ),
                    },
                    "search": {
                        "type": "string",
                        "description": (
                            "Free-text search over call name and description. "
                            "Text only — never pass a UUID here."
                        ),
                    },
                },
                "required": [],
            },
            usage_instructions=(
                "Use when the user wants to browse calls without a research "
                "project description in hand:\n"
                "  ✓ 'What calls are open?'\n"
                "  ✓ 'List all archived calls'\n"
                "  ✓ 'Show calls from CSC'\n"
                "  ✗ 'Find calls for my GPU climate project' — use "
                "find_matching_calls instead (it does keyword matchmaking).\n"
                "  ✗ 'Tell me about call X' — use guide_proposal for one call.\n"
                "\n"
                "Default behaviour (no args) returns active calls only — that "
                "matches the most common 'show me what's on offer' intent.\n"
                "\n"
                "Picking `manager_uuid` vs `manager_name`: fresh from this turn "
                "→ uuid; earlier turn or typed by user → name. Prefer name in "
                "doubt. Never pass a UUID into `search`.\n"
                "\n"
                "ALWAYS render results as a markdown table with EXACTLY these "
                "columns and no others: Call Name | Manager | State | Action. "
                "The Action cell MUST be `[Open](url)` using each call's `url` "
                "field verbatim. Do NOT add a separate row of pill links above "
                "or below the table — the Action column is the only CTA."
            ),
            workflow_instructions="""\
=== PROPOSALS DISCOVERY FUNNEL ===
Five proposal-side tools form a browse → match → narrow → deep-dive funnel.
Move users through it in order unless they jump ahead with specifics.

1. BROWSE OR ASK  →  list_calls OR ask_user (depends on framing)
   RECOMMENDATION FRAMING: user says "recommend me proposals", "which calls should
   I apply to?", "what proposals are good for me?", "suggest a call" → call ask_user
   FIRST with 1-2 questions to gather intent:
   - "What is your research domain?" (free text)
   - "What compute resource do you need?" (options: GPU, CPU, Storage, Other/unsure)
   Then call find_matching_calls with the answers as keywords. Do NOT call list_calls.

   BROWSING FRAMING: user says "what calls are open?", "list archived calls",
   "show calls from [organization]", "what calls exist?" → call list_calls directly.
   End: name 2-3 calls and offer to deep-dive or matchmake.

2. MATCHMAKE  →  find_matching_calls
   User describes a research project; pull 2-5 keywords from their words
   and match against active calls. (If user said "recommend me proposals" with
   no description, the ask_user gate in Step 1 fires first, and find_matching_calls
   is only reached after ask_user provides the description.)
   End: invite deep-dive on the top match.

3. DEEP-DIVE ON ONE CALL  →  guide_proposal
   User picked one call and wants details + proposal-writing guidance.
   End: stop. Wait for explicit ask to draft / submit.

4. LIST PROPOSALS  →  list_proposals
   User asks about proposals in aggregate ('my proposals',
   'in_review proposals on call X'). Permission-scoped automatically.
   End: invite deep-dive via proposal_overview on a specific row.

5. DEEP-DIVE ON ONE PROPOSAL  →  proposal_overview
   Full structured summary for one proposal.

BRIDGE TO GRANTED RESOURCES: an accepted proposal materialises as
resources inside a project. When the user asks "what did I get",
"did my allocation arrive", "where are my resources", or "show what
the grant included", leave this funnel and load the `account`
category — call get_project_resources on the granted project, then
get_resource_usage for per-resource billing detail.

Skip-ahead is fine and expected.\
""",
        )

    def execute(self, user, arguments: dict) -> dict:
        manager_uuid = (arguments.get("manager_uuid") or "").strip()
        manager_name = (arguments.get("manager_name") or "").strip()
        search = (arguments.get("search") or "").strip()
        state = (arguments.get("state") or "").strip()
        round_status = (arguments.get("round_status") or "").strip()

        if manager_uuid and not validate_uuid(manager_uuid):
            return {
                "type": "validation_error",
                "summary": f"Invalid UUID for manager_uuid: {manager_uuid}",
            }
        if state and state not in _VALID_CALL_STATES:
            return {
                "type": "validation_error",
                "summary": f"Invalid state: {state}",
            }
        if round_status and round_status not in _VALID_ROUND_STATUSES:
            return {
                "type": "validation_error",
                "summary": f"Invalid round_status: {round_status}",
            }

        qs = filter_queryset_for_user(Call.objects.all(), user).select_related(
            "manager__customer"
        )

        # Default to active when caller didn't specify — matches the
        # 'show me what's on offer' intent that drives most queries.
        qs = qs.filter(state=state or CallStates.ACTIVE)

        if manager_uuid:
            qs = qs.filter(manager__uuid=manager_uuid)
        if manager_name:
            qs = qs.filter(manager__customer__name__icontains=manager_name)
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(description__icontains=search))

        if round_status:
            now = timezone.now()
            if round_status == RoundStatuses.OPEN:
                qs = qs.filter(round__start_time__lte=now, round__cutoff_time__gte=now)
            elif round_status == RoundStatuses.SCHEDULED:
                qs = qs.filter(round__start_time__gt=now)
            elif round_status == RoundStatuses.ENDED:
                qs = qs.filter(round__cutoff_time__lt=now)
            qs = qs.distinct()

        ordered_qs = qs.order_by("-created")
        total_count = ordered_qs.count()
        calls = list(ordered_qs[:MAX_LIST_RESULTS])
        truncated = total_count > MAX_LIST_RESULTS

        data = [
            {
                "uuid": str(c.uuid),
                "name": c.name,
                "slug": c.slug,
                "state": c.state,
                "manager": c.manager.customer.name if c.manager else "",
                "url": call_detail_url(str(c.uuid)),
            }
            for c in calls
        ]

        summary = (
            f"{len(data)} call{'s' if len(data) != 1 else ''} matching your filter."
        )
        if truncated:
            summary += (
                f" Showing first {MAX_LIST_RESULTS} of {total_count} — "
                "narrow filters to see more."
            )

        return {
            "type": "success",
            "data": {
                "calls": data,
                "total": len(data),
                "_total_count": total_count,
                "_truncated": truncated,
            },
            "summary": summary,
        }


tool_registry.register(ListCallsTool())
