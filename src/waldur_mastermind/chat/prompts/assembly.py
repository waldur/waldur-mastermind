"""Assembles the final SYSTEM_PROMPT_TEMPLATE from individual prompt components.

Order optimizes for prefix caching: fully static content first, dynamic content
(scope_boundary varies by role, tools varies by tool-set) at the end. The longest
common prefix across requests is the persona + generic tool rules + UI capabilities
block, which providers can cache.
"""

from waldur_mastermind.chat.prompts.persona import PERSONA_TEMPLATE
from waldur_mastermind.chat.prompts.tool_instructions import (
    GENERIC_TOOL_INSTRUCTIONS_TEMPLATE,
)

SYSTEM_PROMPT_TEMPLATE = f"""{PERSONA_TEMPLATE}

{GENERIC_TOOL_INSTRUCTIONS_TEMPLATE}

{{custom_instructions}}

{{scope_boundary}}

{{tools}}"""
