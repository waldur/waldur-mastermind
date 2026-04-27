"""AI Assistant tool: full details for a publicly viewable marketplace offering."""

import logging

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.marketplace.helpers import (
    is_public_marketplace_enabled,
    public_offerings_queryset,
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
                "specs, ALWAYS close with a single inline markdown link: "
                "`[View offering](homeport_url)` using the offering's "
                "`homeport_url` field verbatim."
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
                "RENDERING — inline link, not buttons:\n"
                "After narrating the offering's plans / components / specs, "
                "close with a single inline markdown link "
                "`[View offering](homeport_url)`. The `homeport_url` "
                "field is on the returned offering — use it verbatim."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        if not is_public_marketplace_enabled():
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
            public_offerings_queryset()
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
        return {
            "type": "success",
            "data": {"offering": data},
            # Render directive in the summary — the LLM weights tool
            # results higher than usage_instructions for "what to do next".
            "summary": (
                f"Details for {data['name']} by {data['customer_name']}. "
                "Close your reply with one inline markdown link: "
                "`[View offering](homeport_url)` using the offering's "
                "`homeport_url` field verbatim."
            ),
        }


tool_registry.register(GetOfferingTool())
