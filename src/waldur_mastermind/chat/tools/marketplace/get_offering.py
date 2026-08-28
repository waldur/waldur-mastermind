"""AI Assistant tool: full details for a publicly viewable marketplace offering."""

import logging

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.marketplace.helpers import (
    is_anonymous_caller_blocked,
    offerings_queryset_for,
    serialize_offering_detailed,
)
from waldur_mastermind.chat.tools.registry import tool_registry

logger = logging.getLogger(__name__)


class GetOfferingTool(BaseTool):
    """Full details for a single publicly viewable offering."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.GET_OFFERING,
            category=ToolCategory.MARKETPLACE,
            description=(
                "Retrieve full details for a single publicly viewable "
                "marketplace offering, including plans, components, "
                "attributes and tags.\n"
                "\n"
                "After narrating the offering's plans / components / "
                "specs, ALWAYS close with an inline markdown link "
                "`[View offering](homeport_url)` using the offering's "
                "`homeport_url` field verbatim — plus "
                "`[Access](access_url)` when `access_url` is set."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "uuid": {
                        "type": "string",
                        "description": (
                            "Offering UUID from a search_offerings result "
                            "in the CURRENT turn."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "Exact offering name. Use when the offering "
                            "was surfaced in an EARLIER turn and its UUID "
                            "is no longer in your context."
                        ),
                    },
                },
                # At-least-one of uuid/name is enforced in execute().
            },
            usage_instructions=(
                "Use after search_offerings has identified an offering and "
                "the user wants deeper detail (plans, components, "
                "attributes).\n"
                "\n"
                "Picking between `uuid` and `name`:\n"
                "  - Just ran search_offerings this turn  →  pass `uuid`.\n"
                "  - Offering came up in an earlier turn, only name is "
                "in your context  →  pass `name`.\n"
                "  - When in doubt, prefer `name`. Names in your "
                "conversation history are reliable; UUIDs may be stale "
                "or fabricated.\n"
                "\n"
                "NEVER invent, guess, or fabricate a UUID. If you don't "
                "have a real UUID from the current turn, use `name` "
                "instead.\n"
                "\n"
                "RECOVERY: If this tool returns 'Offering not found by "
                "UUID', retry immediately with `name` using the exact "
                "offering name from your context — do NOT narrate the "
                "failure to the user. The retry usually succeeds.\n"
                "\n"
                "RENDERING — inline links, not buttons:\n"
                "After narrating the offering's plans / components / specs, "
                "close with an inline markdown link "
                "`[View offering](homeport_url)`. The `homeport_url` "
                "field is on the returned offering — use it verbatim.\n"
                "\n"
                "ACCESS ROUTING:\n"
                "When `access_url` is set, the offering publishes a direct "
                "access link (shown as 'Access' on its page) — ADD a "
                "second link `[Access](access_url)` after the offering "
                "link. It complements the offering page: do NOT claim the "
                "Hub request flow is unavailable, and do NOT label the "
                "link 'Request access' (that is the Hub's own order "
                "button). When `getting_started` is set, relay its "
                "prerequisites before pointing the user onward. Never "
                "imply that access has been granted."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        if is_anonymous_caller_blocked(user):
            return {
                "type": "error",
                "summary": "Marketplace browsing is currently disabled.",
            }

        uuid = arguments.get("uuid")
        name = arguments.get("name")
        if not uuid and not name:
            return {
                "type": "error",
                "summary": "Pass either `uuid` or `name` (exact match).",
            }

        qs = (
            offerings_queryset_for(user)
            .select_related("category", "customer")
            .prefetch_related("plans", "components", "tags")
        )
        if uuid:
            offering = qs.filter(uuid=uuid).first()
        else:
            offering = qs.filter(name=name).first()
        if not offering:
            # Actionable error: the LLM often passes a UUID from an earlier
            # turn that's no longer valid. Tell it to retry by name.
            if uuid and not name:
                summary = (
                    "Offering not found by UUID. If you recall the offering's "
                    "exact name from earlier in the conversation, retry by "
                    "passing `name` instead of `uuid`. Otherwise call "
                    "search_offerings first to get a fresh UUID."
                )
            elif name and not uuid:
                summary = (
                    f"No offering found with the exact name '{name}'. "
                    "Check the spelling against a recent search_offerings "
                    "result, or call search_offerings with a keyword."
                )
            else:
                summary = "Offering not available."
            return {"type": "error", "summary": summary}

        data = serialize_offering_detailed(offering)
        # Render directive in the summary — the LLM weights tool
        # results higher than usage_instructions for "what to do next".
        if data["access_url"]:
            summary = (
                f"Details for {data['name']} by {data['customer_name']}. "
                "This offering also publishes a direct access link: close "
                "your reply with two inline markdown links — "
                "`[View offering](homeport_url)` followed by "
                "`[Access](access_url)` — using both fields verbatim, and "
                "relay any prerequisites from `getting_started`. The access "
                "link complements the offering page; do not claim the Hub "
                "request flow is unavailable."
            )
        else:
            summary = (
                f"Details for {data['name']} by {data['customer_name']}. "
                "Close your reply with one inline markdown link: "
                "`[View offering](homeport_url)` using the offering's "
                "`homeport_url` field verbatim."
            )
        return {
            "type": "success",
            "data": {"offering": data},
            "summary": summary,
        }


tool_registry.register(GetOfferingTool())
