import logging
import re
from abc import ABC, abstractmethod

from waldur_mastermind.chat.validation.scenarios import EvaluationResult

logger = logging.getLogger(__name__)


class Evaluator(ABC):
    """Base class for response evaluators."""

    @abstractmethod
    def evaluate(self, response_text: str, config: dict) -> EvaluationResult:
        """
        Evaluate an AI Assistant response against criteria.

        Args:
            response_text: The AI Assistant's response text
            config: Evaluator-specific configuration

        Returns:
            EvaluationResult with pass/fail, score, and message
        """
        pass


class ToolUsageEvaluator(Evaluator):
    """Evaluates whether the correct tool was (or wasn't) called."""

    def evaluate(self, response_text: str, config: dict) -> EvaluationResult:
        """
        Check if the expected tool was called.

        Config keys:
            expected_tool (str|None): Name of tool that should be called,
                                      or None if no tool should be called
            tool_calls (list[dict]|None): Native function call results from the API,
                                          e.g. [{"name": "show_user_resources"}].
                                          None or empty list means no tool was called.
            rationale (str): Explanation of why this is expected
        """
        expected_tool = config.get("expected_tool")
        rationale = config.get("rationale", "")

        # With native function calling, tool_calls come from the API response
        # Callers pass tool_calls=[{"name": "tool_name"}] in config.
        # Match against ALL calls in the turn, not just the first — the
        # lazy-load architecture (search_tools → real tool) means the
        # expected tool is rarely the first one in the chain.
        api_tool_calls = config.get("tool_calls") or []
        names = [c.get("name") for c in api_tool_calls if c.get("name")]
        domain_names = [n for n in names if n != "search_tools"]
        actual_tool = (
            expected_tool
            if expected_tool in names
            else (domain_names[0] if domain_names else None)
        )
        tool_call = {"name": actual_tool} if actual_tool else None

        # Check if expectation matches reality
        if expected_tool is None:
            # We expect NO tool call
            if actual_tool is None:
                return EvaluationResult(
                    passed=True,
                    score=1.0,
                    message=f"Correctly avoided tool usage. {rationale}",
                    details={
                        "expected_tool": None,
                        "actual_tool": None,
                    },
                )
            else:
                return EvaluationResult(
                    passed=False,
                    score=0.0,
                    message=f"Unexpectedly called tool '{actual_tool}' when no tool should be used. {rationale}",
                    details={
                        "expected_tool": None,
                        "actual_tool": actual_tool,
                        "tool_call": tool_call,
                    },
                )
        else:
            # We expect a specific tool call
            if actual_tool == expected_tool:
                return EvaluationResult(
                    passed=True,
                    score=1.0,
                    message=f"Correctly called tool '{expected_tool}'. {rationale}",
                    details={
                        "expected_tool": expected_tool,
                        "actual_tool": actual_tool,
                        "tool_call": tool_call,
                    },
                )
            elif actual_tool is None:
                return EvaluationResult(
                    passed=False,
                    score=0.0,
                    message=f"Expected tool '{expected_tool}' but no tool was called. {rationale}",
                    details={
                        "expected_tool": expected_tool,
                        "actual_tool": None,
                    },
                )
            else:
                return EvaluationResult(
                    passed=False,
                    score=0.0,
                    message=f"Expected tool '{expected_tool}' but got '{actual_tool}'. {rationale}",
                    details={
                        "expected_tool": expected_tool,
                        "actual_tool": actual_tool,
                        "tool_call": tool_call,
                    },
                )


class PatternEvaluator(Evaluator):
    """Evaluates whether response contains forbidden or required patterns."""

    def evaluate(self, response_text: str, config: dict) -> EvaluationResult:
        """
        Check response against pattern rules.

        Config keys:
            forbidden_patterns (list[str]): Regex patterns that should NOT appear
            required_patterns (list[str]): Regex patterns that MUST appear
            rationale (str): Explanation of the pattern rules
        """
        forbidden_patterns = config.get("forbidden_patterns", [])
        required_patterns = config.get("required_patterns", [])
        rationale = config.get("rationale", "")

        violations = []
        missing_required = []

        # Check forbidden patterns
        for pattern in forbidden_patterns:
            matches = re.findall(pattern, response_text, re.IGNORECASE)
            if matches:
                violations.append(
                    {
                        "pattern": pattern,
                        "matches": matches,
                    }
                )

        # Check required patterns
        for pattern in required_patterns:
            if not re.search(pattern, response_text, re.IGNORECASE):
                missing_required.append(pattern)

        # Determine pass/fail
        passed = len(violations) == 0 and len(missing_required) == 0

        if passed:
            message = f"Response follows pattern rules. {rationale}"
            score = 1.0
        else:
            error_parts = []
            if violations:
                error_parts.append(f"Found {len(violations)} forbidden pattern(s)")
            if missing_required:
                error_parts.append(
                    f"Missing {len(missing_required)} required pattern(s)"
                )
            message = f"{', '.join(error_parts)}. {rationale}"
            score = 0.0

        return EvaluationResult(
            passed=passed,
            score=score,
            message=message,
            details={
                "violations": violations,
                "missing_required": missing_required,
            },
        )


class CodeBlockEvaluator(Evaluator):
    """Evaluates code block formatting in markdown responses."""

    def evaluate(self, response_text: str, config: dict) -> EvaluationResult:
        """
        Check code blocks have proper language identifiers.

        Config keys:
            require_language_identifier (bool): Whether language ID is mandatory
            allowed_languages (list[str]): Allowed language identifiers
            rationale (str): Explanation of code block rules
        """
        require_language_identifier = config.get("require_language_identifier", True)
        allowed_languages = config.get("allowed_languages", [])
        rationale = config.get("rationale", "")

        # Find all code blocks (```language or bare ```)
        # Pattern matches: ```optionallanguage\ncode\n```
        code_block_pattern = r"```(\w+)?\n(.*?)```"
        code_blocks = re.findall(code_block_pattern, response_text, re.DOTALL)

        if not code_blocks:
            # No code blocks found - this is OK unless we require them
            return EvaluationResult(
                passed=True,
                score=1.0,
                message="No code blocks in response. " + rationale,
                details={
                    "code_blocks_found": 0,
                    "blocks_without_language": 0,
                },
            )

        # Analyze found code blocks
        blocks_without_language = []
        blocks_with_wrong_language = []

        for i, (language, code) in enumerate(code_blocks):
            if not language:
                blocks_without_language.append(
                    {
                        "block_index": i,
                        "code_preview": code[:50],
                    }
                )
            elif allowed_languages and language not in allowed_languages:
                blocks_with_wrong_language.append(
                    {
                        "block_index": i,
                        "language": language,
                        "allowed": allowed_languages,
                    }
                )

        # Determine pass/fail
        has_violations = False
        error_parts = []

        if require_language_identifier and blocks_without_language:
            has_violations = True
            error_parts.append(
                f"{len(blocks_without_language)} code block(s) without language identifier"
            )

        if allowed_languages and blocks_with_wrong_language:
            has_violations = True
            error_parts.append(
                f"{len(blocks_with_wrong_language)} code block(s) with wrong language"
            )

        if has_violations:
            message = f"{', '.join(error_parts)}. {rationale}"
            score = 0.0
            passed = False
        else:
            message = (
                f"All {len(code_blocks)} code block(s) properly formatted. {rationale}"
            )
            score = 1.0
            passed = True

        return EvaluationResult(
            passed=passed,
            score=score,
            message=message,
            details={
                "code_blocks_found": len(code_blocks),
                "blocks_without_language": blocks_without_language,
                "blocks_with_wrong_language": blocks_with_wrong_language,
            },
        )


class LanguageEvaluator(Evaluator):
    """Evaluates response language."""

    def evaluate(self, response: str, config: dict) -> EvaluationResult:
        """Check if response is in expected language.

        Config:
        - match_input_language (bool): If true, response should match input language
        - supported_languages (list): List of supported language codes
        - input_text (str): Optional input text to detect expected language

        Note: This is a simple heuristic-based implementation.
        For production use, consider using langdetect or similar library.
        """
        match_input = config.get("match_input_language", True)
        supported_langs = config.get("supported_languages", ["en"])
        input_text = config.get("input_text", "")

        # Detect language of input and response
        input_lang = self._detect_language(input_text) if input_text else None
        response_lang = self._detect_language(response)

        # Check if response language matches input
        if match_input and input_lang:
            passed = input_lang == response_lang
            if passed:
                message = (
                    f"Response language ({response_lang}) matches input ({input_lang})"
                )
                score = 1.0
            else:
                message = (
                    f"Language mismatch: input is {input_lang}, "
                    f"response is {response_lang}"
                )
                score = 0.0
        else:
            # Just check if response is in a supported language
            passed = response_lang in supported_langs
            if passed:
                message = f"Response is in supported language: {response_lang}"
                score = 1.0
            else:
                message = (
                    f"Response language ({response_lang}) "
                    f"not in supported list: {supported_langs}"
                )
                score = 0.0

        return EvaluationResult(
            passed=passed,
            score=score,
            message=message,
            details={
                "input_language": input_lang,
                "response_language": response_lang,
                "supported_languages": supported_langs,
            },
        )

    def _detect_language(self, text: str) -> str:
        """Simple heuristic language detection.

        Returns:
        - 'en' for English
        - 'et' for Estonian
        - 'lt' for Lithuanian
        - 'lv' for Latvian
        - 'ru' for Russian
        - 'unknown' if cannot determine
        """
        if not text:
            return "unknown"

        text_lower = text.lower()

        # Check for Cyrillic characters (Russian)
        if any("\u0400" <= char <= "\u04ff" for char in text):
            return "ru"

        # Check for language-specific characters and common words
        # Estonian: õ, ä, ö, ü + common words
        estonian_chars = any(char in "õäöü" for char in text_lower)
        estonian_words = any(
            word in text_lower
            for word in ["tere", "kuidas", "saad", "aidata", "aitab", "tänan"]
        )
        if estonian_chars or estonian_words:
            return "et"

        # Latvian: ā, ē, ī, ģ, ķ, ļ, ņ + common words (checked before Lithuanian)
        # Note: ū is shared with Lithuanian, so check more specific chars first
        latvian_specific_chars = any(char in "āēģķļņ" for char in text_lower)
        latvian_words = any(
            word in text_lower
            for word in ["jūs", "varat", "palīdzēt", "paldies", "sveiki"]
        )
        if latvian_specific_chars or latvian_words:
            return "lv"

        # Lithuanian: ė, ų, ą, ę, į + common words
        # Note: Some chars like ū are shared with Latvian
        lithuanian_specific_chars = any(char in "ėųąęį" for char in text_lower)
        lithuanian_words = any(
            word in text_lower for word in ["labas", "kaip", "galite", "padėti", "ačiū"]
        )
        if lithuanian_specific_chars or lithuanian_words:
            return "lt"

        # Default to English
        # Could add more sophisticated checks here
        return "en"


class DataMatchEvaluator(Evaluator):
    """Asserts response text contains every value in ``expected_values``.

    Used by the support-assistant harness to check the assistant cited the
    real ORM value (e.g. credit balance, project name) and didn't fabricate
    around it. Comparison is case-insensitive substring matching — works
    for human numbers ("44,657.40"), names, and short identifiers without
    coupling to LLM phrasing.
    """

    def evaluate(self, response_text: str, config: dict) -> EvaluationResult:
        expected_values = config.get("expected_values") or []
        rationale = config.get("rationale", "")
        haystack = (response_text or "").lower()
        missing = [v for v in expected_values if str(v).lower() not in haystack]
        passed = not missing
        if passed:
            return EvaluationResult(
                passed=True,
                score=1.0,
                message=f"Response cites all {len(expected_values)} expected values. {rationale}",
                details={"expected": expected_values, "missing": []},
            )
        return EvaluationResult(
            passed=False,
            score=0.0,
            message=f"Response is missing {len(missing)} of {len(expected_values)} expected values. {rationale}",
            details={"expected": expected_values, "missing": missing},
        )


class ToolArgumentsEvaluator(Evaluator):
    """Asserts that a specific tool was called with arguments containing
    the expected substrings.

    Catches the "right tool, wrong scope" failure (e.g. assistant calls
    ``get_project_resources`` for project ``a235`` when user asked about
    ``a117``). Tool calls are passed in via the runner as
    ``tool_calls=[{"name": "...", "arguments": {...}}]``.
    """

    def evaluate(self, response_text: str, config: dict) -> EvaluationResult:
        expected_tool = config.get("tool")
        args_must_contain = config.get("args_must_contain") or {}
        rationale = config.get("rationale", "")
        tool_calls = config.get("tool_calls") or []

        matching = [tc for tc in tool_calls if tc.get("name") == expected_tool]
        if not matching:
            return EvaluationResult(
                passed=False,
                score=0.0,
                message=f"Tool '{expected_tool}' was not called. {rationale}",
                details={"tool_calls": tool_calls},
            )

        # Pass if any invocation of the tool satisfies all expected args.
        for call in matching:
            args = call.get("arguments") or {}
            if all(
                str(v).lower() in str(args.get(k, "")).lower()
                for k, v in args_must_contain.items()
            ):
                return EvaluationResult(
                    passed=True,
                    score=1.0,
                    message=f"Tool '{expected_tool}' called with expected args. {rationale}",
                    details={"matched_call": call},
                )

        return EvaluationResult(
            passed=False,
            score=0.0,
            message=f"Tool '{expected_tool}' called but no invocation matched expected args. {rationale}",
            details={"expected_args": args_must_contain, "actual_calls": matching},
        )


# Registry of available evaluators
EVALUATORS = {
    "tool_usage": ToolUsageEvaluator(),
    "pattern": PatternEvaluator(),
    "code_block": CodeBlockEvaluator(),
    "language": LanguageEvaluator(),
    "data_match": DataMatchEvaluator(),
    "tool_arguments": ToolArgumentsEvaluator(),
}


def get_evaluator(evaluation_type: str) -> Evaluator:
    """Get evaluator instance by type."""
    evaluator = EVALUATORS.get(evaluation_type)
    if not evaluator:
        raise ValueError(f"Unknown evaluator type: {evaluation_type}")
    return evaluator
