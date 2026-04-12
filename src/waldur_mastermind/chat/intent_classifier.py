import re
from enum import Enum

# Number of recent assistant messages to check for tool_calls (mid-workflow detection).
# Only assistant-role messages are counted, so tool/user messages don't shrink the window.
_RECENT_ASSISTANT_WINDOW = 4

# Maximum length for greeting messages (longer messages are unlikely pure greetings)
_MAX_GREETING_LENGTH = 40

# --- Compiled regex patterns ---

_GREETING_PATTERNS = re.compile(
    r"^("
    r"hi|hello|hey|yo|howdy|hiya|greetings?"
    r"|good\s+(morning|afternoon|evening|day|night)"
    r"|thanks?(\s+you)?(\s+(very|so)\s+much)?"
    r"|thank\s+you(\s+(very|so)\s+much)?"
    r"|bye|goodbye|see\s+you|take\s+care|cheers"
    r"|what'?s\s+up|sup"
    r")\s*[!.,?]*$",
    re.IGNORECASE,
)

_TOOL_ACTION_PATTERNS = [
    re.compile(
        r"\b(show|list|display|get|fetch)\s+(me\s+)?(my|the|all)\s+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(create|make|provision|deploy|spin\s+up|build|launch)"
        r"\s+(a\s+)?(new\s+)?(vm|virtual\s+machine|server|instance)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bmy\s+(resources?|vms?|virtual\s+machines?|instances?|projects?|servers?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(find|match|discover|search)\s+(me\s+)?(calls?|proposals?|opportunities)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bmy\s+(proposals?|reviews?|submissions?|workload)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(review|summarize|analyze|guide|help\s+me\s+review)\s+"
        r"(proposal|submission)\s+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(call|round)\s+(status|insights?|progress|statistics|stats|briefing|going|doing)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bhow\s+is\s+.{0,30}(call|round|program)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(prepare|apply|submit).{0,30}(for|to).{0,40}(call|proposal|program)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(need|require|necessary).{0,60}(call|proposal|submission|application)\b",
        re.IGNORECASE,
    ),
]

_KNOWLEDGE_PATTERNS = [
    re.compile(
        r"^(what|how|why|when|where|who)\s+(is|are|does|do|can|should|would|could|was|were)\s+",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(explain|describe|tell\s+me\s+about|define)\s+",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(what\s+can\s+you\s+do|how\s+can\s+you\s+help|what\s+do\s+you\s+do|help\s+me\s+understand)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(how\s+to|how\s+do\s+i|can\s+i|is\s+it\s+possible\s+to)\s+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(best\s+practice|eol|end[\s._-]of[\s._-]life|deprecated?|lifecycle|troubleshoot|debug)\b",
        re.IGNORECASE,
    ),
]


class Intent(Enum):
    """Classified intent of a user message."""

    TOOL_ACTION = "tool_action"
    KNOWLEDGE = "knowledge"
    GREETING = "greeting"
    AMBIGUOUS = "ambiguous"

    @property
    def include_tools(self) -> bool:
        """Whether this intent should include tool schemas in the LLM call."""
        return self in (Intent.TOOL_ACTION, Intent.AMBIGUOUS)


def _has_recent_tool_calls(history: list[dict]) -> bool:
    """Check if recent assistant messages contain tool calls (mid-workflow).

    Filters to assistant-role messages before windowing so that expanded
    tool-result messages don't shrink the effective lookback.
    """
    assistant_msgs = [msg for msg in history if msg.get("role") == "assistant"]
    recent = assistant_msgs[-_RECENT_ASSISTANT_WINDOW:]
    return any(msg.get("tool_calls") for msg in recent)


def _matches_tool_action(text: str) -> bool:
    return any(p.search(text) for p in _TOOL_ACTION_PATTERNS)


def _matches_knowledge(text: str) -> bool:
    return any(p.search(text) for p in _KNOWLEDGE_PATTERNS)


def _matches_greeting(text: str) -> bool:
    return bool(_GREETING_PATTERNS.match(text))


def classify_intent(
    user_input: str,
    conversation_history: list[dict] | None = None,
) -> Intent:
    """Classify user intent based on keyword patterns and conversation context.

    Conservative design: only KNOWLEDGE and GREETING suppress tools.
    AMBIGUOUS (the default) keeps tools enabled.

    Args:
        user_input: The current user message text.
        conversation_history: Recent messages in OpenAI format (used to detect
            mid-workflow context where tools should stay enabled).

    Returns:
        Intent enum value.
    """
    text = user_input.strip()
    if not text:
        return Intent.AMBIGUOUS

    # 1. Context override: mid-workflow keeps tools
    if conversation_history and _has_recent_tool_calls(conversation_history):
        return Intent.AMBIGUOUS

    # 2. Detect signals
    has_tool_signal = _matches_tool_action(text)
    has_knowledge_signal = _matches_knowledge(text)

    # 3. Greeting (short messages only, no tool signal)
    if (
        len(text) <= _MAX_GREETING_LENGTH
        and not has_tool_signal
        and _matches_greeting(text)
    ):
        return Intent.GREETING

    # 4. Clear tool action
    if has_tool_signal:
        return Intent.TOOL_ACTION

    # 5. Knowledge queries: classify as AMBIGUOUS to let LLM decide whether
    # tools are useful. The LLM handles "What do I need for call X?" better
    # than regex at distinguishing actionable queries from pure knowledge.
    # Only greetings (above) suppress tools entirely.
    if has_knowledge_signal:
        return Intent.AMBIGUOUS

    # 7. Default
    return Intent.AMBIGUOUS
