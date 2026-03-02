import json
import logging
from functools import lru_cache

from waldur_mastermind.chat.input_guards.base import (
    BaseDetector,
    InjectionResult,
    InputGuardResult,
    PIIResult,
    SeverityLevel,
)
from waldur_mastermind.chat.input_guards.injection_detector import RegexDetector
from waldur_mastermind.chat.input_guards.pii_detector import PIIDetector

logger = logging.getLogger("waldur_mastermind.chat.input_guards")


@lru_cache(maxsize=1)
def get_detection_service() -> "InputDetectionService":
    detectors = [RegexDetector(), PIIDetector()]
    return InputDetectionService(detectors=detectors)


def _reset_for_testing():
    get_detection_service.cache_clear()


class InputDetectionService:
    """Orchestrates detection across multiple detectors and scopes."""

    def __init__(self, detectors: list[BaseDetector]):
        self.detectors = detectors

    def check_user_input(self, text: str) -> InputGuardResult:
        return self._run_detectors(text, scope="user_input")

    def check_tool_arguments(self, tool_name: str, arguments: dict) -> InputGuardResult:
        # Only scan user-supplied argument values, not synthesized wrapper text
        values = []
        for v in arguments.values():
            if isinstance(v, str):
                values.append(v)
            elif isinstance(v, list | dict):
                values.append(json.dumps(v))
        arg_text = " ".join(values)
        if not arg_text.strip():
            return InputGuardResult()
        return self._run_detectors(arg_text, scope="tool_arguments")

    _MAX_SCAN_LENGTH = 50_000

    def _run_detectors(self, text: str, scope: str) -> InputGuardResult:
        # Truncate very long input to prevent regex CPU exhaustion
        if len(text) > self._MAX_SCAN_LENGTH:
            logger.warning(
                "Input truncated from %d to %d chars for %s scanning",
                len(text),
                self._MAX_SCAN_LENGTH,
                scope,
            )
            text = text[: self._MAX_SCAN_LENGTH]

        injection_result = InjectionResult()
        pii_result = PIIResult()

        for detector in self.detectors:
            result = detector.detect(text)
            if isinstance(result, InjectionResult):
                # Merge injection results: keep highest scoring, accumulate patterns
                if result.score > injection_result.score:
                    # Preserve patterns from the previous (lower-scoring) result
                    prev_patterns = injection_result.matched_patterns
                    injection_result = result
                    injection_result.matched_patterns = (
                        injection_result.matched_patterns + prev_patterns
                    )
                else:
                    injection_result.matched_patterns.extend(result.matched_patterns)
            elif isinstance(result, PIIResult):
                # Merge PII results: keep highest scoring, accumulate detections
                if result.score > pii_result.score:
                    prev_detections = pii_result.pii_detections
                    pii_result = result
                    pii_result.pii_detections = (
                        pii_result.pii_detections + prev_detections
                    )
                else:
                    pii_result.pii_detections.extend(result.pii_detections)

        # Compute redacted text to account for all merged detections
        pii_result.compute_redacted_text(text)

        if injection_result.severity in (SeverityLevel.HIGH, SeverityLevel.CRITICAL):
            logger.warning(
                "Prompt injection detected [%s] scope=%s score=%.2f patterns=%s",
                injection_result.severity.value,
                scope,
                injection_result.score,
                [p["category"] for p in injection_result.matched_patterns],
            )
        elif injection_result.severity in (SeverityLevel.LOW, SeverityLevel.MEDIUM):
            logger.info(
                "Possible prompt injection [%s] scope=%s score=%.2f",
                injection_result.severity.value,
                scope,
                injection_result.score,
            )

        return InputGuardResult(
            injection=injection_result,
            pii=pii_result,
        )
