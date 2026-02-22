from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


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


class DetectionAction(Enum):
    ALLOW = "allow"
    FLAG = "flag"
    BLOCK = "block"


@dataclass
class DetectionResult:
    is_injection: bool
    score: float  # 0.0 to 1.0
    severity: SeverityLevel
    action: DetectionAction
    matched_patterns: list[dict[str, str | float]] = field(default_factory=list)
    detection_method: str = ""
    details: dict[str, str | float | bool] = field(default_factory=dict)


class BaseDetector(ABC):
    """Abstract base class for injection detectors."""

    @abstractmethod
    def detect(self, text: str) -> DetectionResult:
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass
