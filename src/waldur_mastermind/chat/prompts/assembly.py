"""Assembles the final SYSTEM_PROMPT_TEMPLATE from individual prompt components."""

from waldur_mastermind.chat.prompts.persona import PERSONA_TEMPLATE
from waldur_mastermind.chat.prompts.tool_instructions import (
    GENERIC_TOOL_INSTRUCTIONS_TEMPLATE,
)
from waldur_mastermind.chat.prompts.ui_capabilities import UI_CAPABILITIES

SYSTEM_PROMPT_TEMPLATE = f"""{PERSONA_TEMPLATE}

{{scope_boundary}}

{{tools}}

{GENERIC_TOOL_INSTRUCTIONS_TEMPLATE}

{UI_CAPABILITIES}"""
