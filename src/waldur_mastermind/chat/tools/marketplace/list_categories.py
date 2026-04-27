"""AI Assistant tool: list marketplace categories that have public offerings."""

import logging

from django.db.models import Count

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.marketplace.helpers import (
    is_public_marketplace_enabled,
    public_offerings_queryset,
)
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.marketplace import models as marketplace_models

logger = logging.getLogger(__name__)


class ListCategoriesTool(BaseTool):
    """List marketplace categories that currently expose at least one public offering."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.LIST_CATEGORIES,
            category=ToolCategory.MARKETPLACE,
            description=(
                "List marketplace categories that contain at least one "
                "publicly viewable offering. Useful for exploratory queries "
                "like 'what kinds of services are available?'."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            usage_instructions=(
                "Use when the user asks an open-ended question about what "
                "the marketplace offers, before they've narrowed down needs. "
                "Follow up with search_offerings once they pick a category."
            ),
            workflow_instructions="""\
=== MARKETPLACE DISCOVERY FUNNEL ===
Four marketplace tools form a guided browse → narrow → compare → deep-dive funnel.
It is an INFORMATION funnel, not an order funnel — it ends when the user has
the details they asked for. Move users through it in order unless they jump
ahead with specifics.

1. SHOW WHAT WE HAVE  →  list_categories OR ask_user (depends on framing)
   RECOMMENDATION FRAMING: user says "recommend me offerings", "suggest something",
   "what should I use for X", "what do you recommend?" → call ask_user FIRST with
   1-2 questions to gather intent (e.g., "What type of resource?" with options like
   Compute/GPU, Storage, Software/Services, Consulting). Then call search_offerings
   on the answer. Do NOT call list_categories in this path.

   BROWSING FRAMING: user says "what categories exist?", "what's available?",
   "show me services", "what can I do here?" → call list_categories directly.
   Then present results via ask_user as a button-group selection form (see Change B below).

   Change B — Categories-as-buttons: After calling list_categories, do NOT render
   results as a bullet list. Call ask_user with:
   - header: "Category"
   - question: "Which area are you looking for?"
   - options: each category title from the results (cap at 8, already sorted by
     offering_count in the tool output). Add "value" field to each option = the
     category UUID (so the next search_offerings call uses category_uuid directly).
   - allowFreeText: true (user can type a category name if not in top 8)
   - After ask_user call, stop with at most one short framing sentence.

2. NARROW TO A TOPIC  →  search_offerings
   Use once the user names a category, hardware, capability, or workload.
   Apply synonym expansion: "GPU" ↔ "accelerator", "storage" ↔ "filesystem",
   "notebook" ↔ "JupyterHub", "batch" ↔ "SLURM", "database" ↔ "Postgres".
   If the first keyword returns nothing, try one synonym before giving up.
   End of your response: if ≥2 results, invite a side-by-side compare;
   if 1 result, invite a deep-dive via get_offering.

3. COMPARE OPTIONS  →  compare_offerings
   Use when the user wants to weigh 2+ offerings against each other.
   Accept names (from earlier turns) or UUIDs (from current search).
   End of your response: recommend the best fit for the stated use case
   and invite a deep-dive via get_offering on that one.

4. DEEP-DIVE  →  get_offering
   Use when the user wants full plans, components, attributes for ONE
   offering. This tool is purely informational — showing offering
   details does NOT imply the user wants to provision, order, or
   create anything.
   End of your response: summarize the key details (plans, pricing,
   components) and stop. Do NOT suggest creating a VM, ordering, or
   provisioning resources. Wait for the user to explicitly ask.

Marketplace browsing (steps 1–4) and resource creation (e.g. VM
provisioning via plan_vm / create_vm) are SEPARATE workflows. Never
chain them automatically — the user may be exploring, comparing, or
just curious. Only enter a creation workflow when the user explicitly
asks to create/order/provision a resource.

Skip-ahead is fine: a user who says "show me SLURM pricing" goes
straight to search_offerings → get_offering; a user who says
"compare A and B" goes straight to compare_offerings. Never FORCE the
user through list_categories if they've already named a topic.
The funnel is a shape, not a script.\
""",
        )

    def execute(self, user, arguments: dict) -> dict:
        if not is_public_marketplace_enabled():
            return {
                "type": "error",
                "summary": "Marketplace browsing is currently disabled.",
            }

        public_offering_ids = public_offerings_queryset().values_list("id", flat=True)
        categories = (
            marketplace_models.Category.objects.filter(
                offerings__id__in=public_offering_ids
            )
            .annotate(public_offering_count=Count("offerings", distinct=True))
            .order_by("-public_offering_count", "title")
            .distinct()
        )

        data = [
            {
                "uuid": str(c.uuid),
                "title": c.title,
                "offering_count": c.public_offering_count,
            }
            for c in categories
        ]
        summary = f"{len(data)} categor{'y' if len(data) == 1 else 'ies'} with public offerings."
        return {
            "type": "success",
            "data": {"categories": data},
            "summary": summary,
        }


tool_registry.register(ListCategoriesTool())
