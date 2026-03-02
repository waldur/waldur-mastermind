from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

_SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


class SeverityLevel(Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @classmethod
    def choices(cls):
        return [(s.value, s.value) for s in cls]

    @classmethod
    def from_score(cls, score: float) -> "SeverityLevel":
        if score >= 0.9:
            return cls.CRITICAL
        if score >= 0.7:
            return cls.HIGH
        if score >= 0.5:
            return cls.MEDIUM
        if score >= 0.3:
            return cls.LOW
        return cls.NONE

    @classmethod
    def from_pii_action(cls, action: "DetectionAction") -> "SeverityLevel":
        mapping = {
            DetectionAction.BLOCK: cls.CRITICAL,
            DetectionAction.REDACT: cls.HIGH,
            DetectionAction.WARN: cls.MEDIUM,
            DetectionAction.FLAG: cls.LOW,
        }
        return mapping.get(action, cls.NONE)

    def get_score_range(self) -> tuple[float | None, float | None]:
        """Return (min_inclusive, max_exclusive) score range for this severity."""
        ranges = {
            "critical": (0.9, None),
            "high": (0.7, 0.9),
            "medium": (0.5, 0.7),
            "low": (0.3, 0.5),
            "none": (None, 0.3),
        }
        return ranges[self.value]

    def __gt__(self, other):
        if not isinstance(other, SeverityLevel):
            return NotImplemented
        return _SEVERITY_ORDER[self.value] > _SEVERITY_ORDER[other.value]

    def __ge__(self, other):
        if not isinstance(other, SeverityLevel):
            return NotImplemented
        return _SEVERITY_ORDER[self.value] >= _SEVERITY_ORDER[other.value]

    def __lt__(self, other):
        if not isinstance(other, SeverityLevel):
            return NotImplemented
        return _SEVERITY_ORDER[self.value] < _SEVERITY_ORDER[other.value]

    def __le__(self, other):
        if not isinstance(other, SeverityLevel):
            return NotImplemented
        return _SEVERITY_ORDER[self.value] <= _SEVERITY_ORDER[other.value]


# Ordered from least to most severe for comparison
_ACTION_PRIORITY = {
    "allow": 0,
    "flag": 1,
    "warn": 2,
    "redact": 3,
    "block": 4,
}


class DetectionAction(Enum):
    ALLOW = "allow"
    FLAG = "flag"
    WARN = "warn"
    REDACT = "redact"
    BLOCK = "block"

    @classmethod
    def choices(cls):
        return [(s.value, s.value) for s in cls]

    @classmethod
    def from_injection_severity(cls, severity: "SeverityLevel") -> "DetectionAction":
        mapping = {
            SeverityLevel.CRITICAL: cls.BLOCK,
            SeverityLevel.HIGH: cls.BLOCK,
            SeverityLevel.MEDIUM: cls.FLAG,
            SeverityLevel.LOW: cls.FLAG,
        }
        return mapping.get(severity, cls.ALLOW)

    def __gt__(self, other):
        if not isinstance(other, DetectionAction):
            return NotImplemented
        return _ACTION_PRIORITY[self.value] > _ACTION_PRIORITY[other.value]

    def __ge__(self, other):
        if not isinstance(other, DetectionAction):
            return NotImplemented
        return _ACTION_PRIORITY[self.value] >= _ACTION_PRIORITY[other.value]

    def __lt__(self, other):
        if not isinstance(other, DetectionAction):
            return NotImplemented
        return _ACTION_PRIORITY[self.value] < _ACTION_PRIORITY[other.value]

    def __le__(self, other):
        if not isinstance(other, DetectionAction):
            return NotImplemented
        return _ACTION_PRIORITY[self.value] <= _ACTION_PRIORITY[other.value]


@dataclass
class DetectionResult:
    """Base detection result with common fields."""

    score: float = 0.0
    severity: SeverityLevel = SeverityLevel.NONE
    action: DetectionAction = DetectionAction.ALLOW
    detection_method: str = ""

    @property
    def is_detected(self) -> bool:
        return self.action != DetectionAction.ALLOW


@dataclass
class InjectionResult(DetectionResult):
    """Result from injection detection."""

    matched_patterns: list[dict[str, str | float]] = field(default_factory=list)


@dataclass
class PIIResult(DetectionResult):
    """Result from PII detection."""

    pii_detections: list[dict] = field(default_factory=list)
    _redacted_text: str = ""

    @property
    def redacted_text(self) -> str:
        """Return text with PII matches replaced by [REDACTED]."""
        return self._redacted_text

    def compute_redacted_text(self, original_text: str) -> None:
        """Compute redacted text from original, then discard the original.

        This must be called once after construction. The original text is
        NOT stored on the instance to prevent accidental exposure of raw PII
        in error-tracking, debug pages, or log serialisation.
        """
        if not self.pii_detections or not original_text:
            self._redacted_text = original_text
            return
        if self.action == DetectionAction.BLOCK:
            self._redacted_text = "[Message blocked: sensitive credentials detected]"
            return

        text = original_text
        # Build (start, end, entity_type) tuples, sort by start ascending
        ranges = []
        for det in self.pii_detections:
            s, e = det.get("start"), det.get("end")
            if s is not None and e is not None:
                ranges.append((s, e, det.get("entity_type", "PII")))
        ranges.sort(key=lambda r: r[0])
        # Merge overlapping ranges (keep entity_type of the first/widest)
        merged = []
        for s, e, etype in ranges:
            if merged and s <= merged[-1][1]:
                # Overlapping — extend the previous range
                prev_s, prev_e, prev_etype = merged[-1]
                merged[-1] = (prev_s, max(prev_e, e), prev_etype)
            else:
                merged.append((s, e, etype))
        # Apply replacements from end to start
        for s, e, etype in reversed(merged):
            text = text[:s] + f"[{etype}_REDACTED]" + text[e:]
        self._redacted_text = text

    @property
    def user_message(self) -> str:
        """Human-readable message about what was detected."""
        if self.action == DetectionAction.BLOCK:
            blocked = [
                d.get("display_name", d["entity_type"])
                for d in self.pii_detections
                if d.get("action") == "block"
            ]
            if blocked:
                types = ", ".join(sorted(set(blocked)))
                return f"Sensitive credentials detected ({types}). Message blocked for security."
            return ""
        if self.action == DetectionAction.REDACT:
            redacted = [
                d.get("display_name", d["entity_type"])
                for d in self.pii_detections
                if d.get("action") == "redact"
            ]
            if redacted:
                types = ", ".join(sorted(set(redacted)))
                return f"Personal information detected and redacted ({types})."
            return ""
        if self.action == DetectionAction.WARN:
            warned = [
                d.get("display_name", d["entity_type"])
                for d in self.pii_detections
                if d.get("action") == "warn"
            ]
            if warned:
                types = ", ".join(sorted(set(warned)))
                return f"Your message may contain sensitive information ({types}). Please be cautious when sharing personal data."
            return ""
        return ""


@dataclass
class InputGuardResult:
    """Composite result returned by the service."""

    injection: InjectionResult = field(default_factory=InjectionResult)
    pii: PIIResult = field(default_factory=PIIResult)

    @property
    def action(self) -> DetectionAction:
        return max(self.injection.action, self.pii.action)

    @property
    def severity(self) -> SeverityLevel:
        return max(self.injection.severity, self.pii.severity)

    @property
    def is_flagged(self) -> bool:
        return self.injection.is_detected or self.pii.is_detected


class BaseDetector(ABC):
    """Abstract base class for injection detectors."""

    @abstractmethod
    def detect(self, text: str) -> DetectionResult:
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass
