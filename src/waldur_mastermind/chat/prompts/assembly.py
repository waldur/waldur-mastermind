"""Assembles the final SYSTEM_PROMPT from individual prompt components."""

from waldur_mastermind.chat.prompts.persona import PERSONA
from waldur_mastermind.chat.prompts.scope_boundary import SCOPE_BOUNDARY
from waldur_mastermind.chat.prompts.tool_instructions import GENERIC_TOOL_INSTRUCTIONS
from waldur_mastermind.chat.prompts.ui_capabilities import UI_CAPABILITIES

SYSTEM_PROMPT = f"""{PERSONA}

{SCOPE_BOUNDARY}

{{tools}}

{GENERIC_TOOL_INSTRUCTIONS}

{UI_CAPABILITIES}"""
