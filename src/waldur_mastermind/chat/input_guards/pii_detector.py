import logging
import re

from waldur_mastermind.chat.input_guards.base import (
    BaseDetector,
    DetectionAction,
    PIIResult,
    SeverityLevel,
)
from waldur_mastermind.chat.input_guards.pii_context import calculate_pii_confidence
from waldur_mastermind.chat.input_guards.pii_credential_patterns import (
    ALL_PII_CREDENTIAL_PATTERNS,
)
from waldur_mastermind.chat.input_guards.validators import (
    luhn_check,
    validate_czech_birth_number,
    validate_estonian_id,
    validate_finland_hetu,
    validate_france_nir,
    validate_germany_steuer_id,
    validate_iban,
    validate_italy_codice_fiscale,
    validate_netherlands_bsn,
    validate_poland_pesel,
    validate_spain_dni_nie,
    validate_sweden_personnummer,
)

logger = logging.getLogger(__name__)


def _mask_text(text: str) -> str:
    """Show only first 4 chars of sensitive matches."""
    if len(text) <= 8:
        return text[:2] + "***"
    return text[:4] + "***"


# Map pattern categories to actions
PII_CATEGORY_ACTION_MAP = {
    # BLOCK tier — credentials
    "pii_private_key": DetectionAction.BLOCK,
    "pii_aws_access_key": DetectionAction.BLOCK,
    "pii_aws_secret_key": DetectionAction.BLOCK,
    "pii_gcp_api_key": DetectionAction.BLOCK,
    "pii_azure_key": DetectionAction.BLOCK,
    "pii_github_token": DetectionAction.BLOCK,
    "pii_gitlab_pat": DetectionAction.BLOCK,
    "pii_slack_token": DetectionAction.BLOCK,
    "pii_stripe_key": DetectionAction.BLOCK,
    "pii_database_url": DetectionAction.BLOCK,
    "pii_password_context": DetectionAction.BLOCK,
    "pii_sendgrid_key": DetectionAction.BLOCK,
    "pii_slack_webhook": DetectionAction.BLOCK,
    # REDACT tier — personal data
    "pii_estonian_id": DetectionAction.REDACT,
    "pii_iban_estonian": DetectionAction.REDACT,
    "pii_iban_general": DetectionAction.REDACT,
    "pii_credit_card": DetectionAction.REDACT,
    "pii_phone_estonian": DetectionAction.REDACT,
    "pii_phone_e164": DetectionAction.REDACT,
    "pii_italy_codice_fiscale": DetectionAction.REDACT,
    "pii_france_nir": DetectionAction.REDACT,
    "pii_finland_hetu": DetectionAction.REDACT,
    "pii_spain_dni": DetectionAction.REDACT,
    "pii_spain_nie": DetectionAction.REDACT,
    "pii_poland_pesel": DetectionAction.REDACT,
    "pii_germany_steuer_id": DetectionAction.REDACT,
    "pii_czech_birth_number": DetectionAction.REDACT,
    "pii_netherlands_bsn": DetectionAction.REDACT,
    "pii_sweden_personnummer": DetectionAction.REDACT,
    # WARN tier — possibly sensitive
    "pii_email": DetectionAction.WARN,
    "pii_jwt": DetectionAction.WARN,
    "pii_bearer_token": DetectionAction.WARN,
    "pii_generic_api_key": DetectionAction.WARN,
    "pii_eu_vat": DetectionAction.WARN,
}

# Confidence thresholds per action tier
_CONFIDENCE_THRESHOLDS = {
    DetectionAction.BLOCK: 0.7,
    DetectionAction.REDACT: 0.6,
    DetectionAction.WARN: 0.5,
}

# Categories that have checksum validators
_VALIDATOR_MAP = {
    "pii_estonian_id": validate_estonian_id,
    "pii_iban_estonian": validate_iban,
    "pii_iban_general": validate_iban,
    "pii_credit_card": luhn_check,
    "pii_italy_codice_fiscale": validate_italy_codice_fiscale,
    "pii_france_nir": validate_france_nir,
    "pii_finland_hetu": validate_finland_hetu,
    "pii_spain_dni": validate_spain_dni_nie,
    "pii_spain_nie": validate_spain_dni_nie,
    "pii_poland_pesel": validate_poland_pesel,
    "pii_germany_steuer_id": validate_germany_steuer_id,
    "pii_czech_birth_number": validate_czech_birth_number,
    "pii_netherlands_bsn": validate_netherlands_bsn,
    "pii_sweden_personnummer": validate_sweden_personnummer,
}


class PIIDetector(BaseDetector):
    """Detects PII and credentials in text using regex patterns with validation."""

    def __init__(self, patterns=None):
        self.patterns = ALL_PII_CREDENTIAL_PATTERNS if patterns is None else patterns
        self._compiled = [
            (re.compile(p, re.IGNORECASE | re.UNICODE), cat, weight, display)
            for p, cat, weight, display in self.patterns
        ]

    @property
    def name(self) -> str:
        return "pii"

    def detect(self, text: str) -> PIIResult:
        pii_detections = []
        highest_action = DetectionAction.ALLOW
        max_score = 0.0

        for compiled, category, base_weight, display_name in self._compiled:
            for match in compiled.finditer(text):
                matched_text = match.group(0)
                start = match.start()
                end = match.end()

                # Run validator if applicable
                validator = _VALIDATOR_MAP.get(category)
                has_checksum = False
                if validator:
                    if not validator(matched_text):
                        continue  # Failed validation — skip this match
                    has_checksum = True

                # Determine action for this category
                action = PII_CATEGORY_ACTION_MAP.get(category, DetectionAction.WARN)

                # Calculate context-aware confidence
                confidence = calculate_pii_confidence(
                    match_text=matched_text,
                    full_text=text,
                    start=start,
                    end=end,
                    entity_type=category,
                    has_checksum=has_checksum,
                    base_score=base_weight,
                    action_tier=action.value,
                )

                # Check confidence threshold
                threshold = _CONFIDENCE_THRESHOLDS.get(action, 0.5)
                if confidence < threshold:
                    continue

                pii_detections.append(
                    {
                        "entity_type": category,
                        "display_name": display_name,
                        "matched_text": _mask_text(matched_text),
                        "start": start,
                        "end": end,
                        "confidence": round(confidence, 2),
                        "action": action.value,
                    }
                )

                max_score = max(max_score, confidence)

                if action > highest_action:
                    highest_action = action

                # Short-circuit on first BLOCK-level match
                if action == DetectionAction.BLOCK:
                    return self._build_result(highest_action, max_score, pii_detections)

        return self._build_result(highest_action, max_score, pii_detections)

    def _build_result(
        self,
        action: DetectionAction,
        score: float,
        pii_detections: list[dict],
    ) -> PIIResult:
        if not pii_detections:
            return PIIResult(
                detection_method=self.name,
            )

        severity = SeverityLevel.from_pii_action(action)

        return PIIResult(
            score=score,
            severity=severity,
            action=action,
            detection_method=self.name,
            pii_detections=pii_detections,
        )
