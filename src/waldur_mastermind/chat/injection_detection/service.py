import json
import logging
from functools import lru_cache

from waldur_mastermind.chat.injection_detection.base import (
    BaseDetector,
    DetectionAction,
    DetectionResult,
    SeverityLevel,
)
from waldur_mastermind.chat.injection_detection.regex_detector import RegexDetector

logger = logging.getLogger("waldur_mastermind.chat.injection_detection")


@lru_cache(maxsize=1)
def get_injection_service() -> "PromptInjectionService":
    detector = RegexDetector()
    return PromptInjectionService(detectors=[detector])


def _reset_for_testing():
    get_injection_service.cache_clear()


class PromptInjectionService:
    """Orchestrates detection across multiple detectors and scopes."""

    def __init__(self, detectors: list[BaseDetector]):
        self.detectors = detectors

    def check_user_input(self, text: str) -> DetectionResult:
        return self._run_detectors(text, scope="user_input")

    def check_tool_arguments(self, tool_name: str, arguments: dict) -> DetectionResult:
        # Only scan user-supplied argument values, not synthesized wrapper text
        values = []
        for v in arguments.values():
            if isinstance(v, str):
                values.append(v)
            elif isinstance(v, list | dict):
                values.append(json.dumps(v))
        arg_text = " ".join(values)
        if not arg_text.strip():
            return DetectionResult(
                is_injection=False,
                score=0.0,
                severity=SeverityLevel.NONE,
                action=DetectionAction.ALLOW,
                detection_method="none",
                details={"scope": "tool_arguments"},
            )
        return self._run_detectors(arg_text, scope="tool_arguments")

    def _run_detectors(self, text: str, scope: str) -> DetectionResult:
        results = []
        for detector in self.detectors:
            result = detector.detect(text)
            result.details["scope"] = scope
            results.append(result)

        if not results:
            return DetectionResult(
                is_injection=False,
                score=0.0,
                severity=SeverityLevel.NONE,
                action=DetectionAction.ALLOW,
                detection_method="none",
            )

        worst = max(results, key=lambda r: r.score)

        # Merge all matched patterns across detectors
        all_patterns = []
        for r in results:
            all_patterns.extend(r.matched_patterns)
        worst.matched_patterns = all_patterns

        if worst.severity in (SeverityLevel.HIGH, SeverityLevel.CRITICAL):
            logger.warning(
                "Prompt injection detected [%s] scope=%s score=%.2f patterns=%s",
                worst.severity.value,
                scope,
                worst.score,
                [p["category"] for p in worst.matched_patterns],
            )
        elif worst.severity in (SeverityLevel.LOW, SeverityLevel.MEDIUM):
            logger.info(
                "Possible prompt injection [%s] scope=%s score=%.2f",
                worst.severity.value,
                scope,
                worst.score,
            )

        return worst
