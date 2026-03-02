import unittest

from waldur_mastermind.chat.input_guards.base import DetectionAction, PIIResult
from waldur_mastermind.chat.input_guards.pii_detector import PIIDetector
from waldur_mastermind.chat.input_guards.validators import (
    validate_czech_birth_number,
    validate_finland_hetu,
    validate_france_nir,
    validate_germany_steuer_id,
    validate_italy_codice_fiscale,
    validate_netherlands_bsn,
    validate_poland_pesel,
    validate_spain_dni_nie,
    validate_sweden_personnummer,
)


class PIIDetectorBlockTest(unittest.TestCase):
    """Test that credentials are blocked."""

    def setUp(self):
        self.detector = PIIDetector()

    def test_private_key_blocked(self):
        text = "Here is my key: -----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAK..."
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.BLOCK)
        self.assertTrue(
            any(d["entity_type"] == "pii_private_key" for d in result.pii_detections)
        )

    def test_openssh_private_key_blocked(self):
        text = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA..."
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.BLOCK)

    def test_aws_access_key_blocked(self):
        text = "My AWS key is AKIAIOSFODNN7REALKEY"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.BLOCK)
        self.assertTrue(
            any(d["entity_type"] == "pii_aws_access_key" for d in result.pii_detections)
        )

    def test_github_token_blocked(self):
        text = "Use this token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.BLOCK)
        self.assertTrue(
            any(d["entity_type"] == "pii_github_token" for d in result.pii_detections)
        )

    def test_gitlab_pat_blocked(self):
        text = "Token: glpat-abcdefghijklmnopqrst"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.BLOCK)

    def test_slack_token_blocked(self):
        text = "Slack: xoxb-123456789012-abcdefghij"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.BLOCK)

    def test_stripe_key_blocked(self):
        text = "sk_live_ABCDEFGHIJKLMNOPQRSTuv"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.BLOCK)

    def test_database_url_blocked(self):
        text = "postgres://admin:secretpassword@db.example.com:5432/mydb"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.BLOCK)
        self.assertTrue(
            any(d["entity_type"] == "pii_database_url" for d in result.pii_detections)
        )

    def test_password_context_blocked(self):
        text = "password=MySuperSecret123!"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.BLOCK)

    def test_sendgrid_key_blocked(self):
        text = "SG.abcdefghijklmnopqrstuv.wxyzABCDEFGHIJKLMNOPQR"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.BLOCK)

    def test_slack_webhook_blocked(self):
        text = "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.BLOCK)

    def test_gcp_api_key_blocked(self):
        text = "AIzaSyA1234567890abcdefghijklmnopqrstuv"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.BLOCK)

    def test_short_circuits_on_block(self):
        """BLOCK should short-circuit — only one detection needed."""
        text = "-----BEGIN RSA PRIVATE KEY-----\nkey content\n-----END RSA PRIVATE KEY-----"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.BLOCK)
        # Should have at least one detection
        self.assertGreaterEqual(len(result.pii_detections), 1)


class PIIDetectorRedactTest(unittest.TestCase):
    """Test that PII data triggers redaction."""

    def setUp(self):
        self.detector = PIIDetector()

    def test_estonian_id_redacted(self):
        text = "My isikukood is 38706181237"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.REDACT)
        self.assertTrue(
            any(d["entity_type"] == "pii_estonian_id" for d in result.pii_detections)
        )

    def test_estonian_iban_redacted(self):
        text = "My IBAN is EE382200221020145685"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.REDACT)
        self.assertTrue(
            any(d["entity_type"] == "pii_iban_estonian" for d in result.pii_detections)
        )

    def test_credit_card_redacted(self):
        text = "My card number is 4532 1111 1111 1112"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.REDACT)
        self.assertTrue(
            any(d["entity_type"] == "pii_credit_card" for d in result.pii_detections)
        )

    def test_email_warned_not_redacted(self):
        """Emails are in WARN tier (not REDACT) to avoid false positives in support chat."""
        text = "Contact me at john.doe@company.com"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.WARN)

    def test_estonian_phone_redacted(self):
        text = "Call me at +372 5123 4567"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.REDACT)


class PIIDetectorWarnTest(unittest.TestCase):
    """Test that possibly sensitive data triggers warnings."""

    def setUp(self):
        self.detector = PIIDetector()

    def test_jwt_warned(self):
        text = "Token: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.WARN)

    def test_generic_api_key_warned(self):
        text = "Set your api_key=abcdef1234567890ABCDEF in the config"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.WARN)


class PIIDetectorFalsePositiveTest(unittest.TestCase):
    """Test that common benign inputs don't trigger false positives."""

    def setUp(self):
        self.detector = PIIDetector()

    def test_clean_waldur_query(self):
        text = "How do I create a new project in Waldur?"
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.ALLOW)

    def test_example_email_not_detected(self):
        text = "Send email to user@example.com for testing"
        result = self.detector.detect(text)
        # example.com emails should be suppressed
        email_detections = [
            d for d in result.pii_detections if d["entity_type"] == "pii_email"
        ]
        self.assertEqual(len(email_detections), 0)

    def test_example_question_suppressed(self):
        text = "What is an example IBAN format? Something like EE382200221020145685?"
        result = self.detector.detect(text)
        # "example" context should suppress the IBAN detection
        self.assertNotEqual(result.action, DetectionAction.BLOCK)

    def test_invalid_estonian_id_not_detected(self):
        """Invalid checksum should not trigger detection."""
        text = "The number 38706181230 is here"
        result = self.detector.detect(text)
        estonian_detections = [
            d for d in result.pii_detections if d["entity_type"] == "pii_estonian_id"
        ]
        self.assertEqual(len(estonian_detections), 0)

    def test_invalid_credit_card_not_detected(self):
        """Invalid Luhn should not trigger credit card detection."""
        text = "Enter code 1234 5678 9012 3456"
        result = self.detector.detect(text)
        cc_detections = [
            d for d in result.pii_detections if d["entity_type"] == "pii_credit_card"
        ]
        self.assertEqual(len(cc_detections), 0)


class PIIDetectorContextScoringTest(unittest.TestCase):
    """Test context-aware confidence scoring."""

    def setUp(self):
        self.detector = PIIDetector()

    def test_boost_word_increases_confidence(self):
        """Presence of 'isikukood' should boost Estonian ID confidence."""
        text_with_boost = "My isikukood is 38706181237"
        text_without = "The number 38706181237 was mentioned"
        result_with = self.detector.detect(text_with_boost)
        result_without = self.detector.detect(text_without)
        # Both should detect, but boosted should have higher confidence
        detections_with = [
            d
            for d in result_with.pii_detections
            if d["entity_type"] == "pii_estonian_id"
        ]
        detections_without = [
            d
            for d in result_without.pii_detections
            if d["entity_type"] == "pii_estonian_id"
        ]
        self.assertTrue(detections_with, "Expected detection with boost word")
        self.assertTrue(detections_without, "Expected detection without boost word")
        self.assertGreater(
            detections_with[0]["confidence"], detections_without[0]["confidence"]
        )


class PIIDetectorRedactedTextTest(unittest.TestCase):
    """Test redacted text generation via DetectionResult properties."""

    def test_redacted_text_replaces_matches(self):
        text = "My IBAN is EE382200221020145685 please"
        detector = PIIDetector()
        result = detector.detect(text)
        self.assertEqual(result.action, DetectionAction.REDACT)
        # Redacted text is computed by the service, not the detector
        result.compute_redacted_text(text)
        redacted = result.redacted_text
        self.assertNotIn("EE382200221020145685", redacted)
        self.assertIn("REDACTED", redacted)

    def test_multiple_redactions(self):
        # Use a card number that passes Luhn but isn't in KNOWN_TEST_VALUES
        text = "IBAN: EE382200221020145685 and card 4532 1111 1111 1112"
        detector = PIIDetector()
        result = detector.detect(text)
        self.assertEqual(result.action, DetectionAction.REDACT)
        # Redacted text is computed by the service, not the detector
        result.compute_redacted_text(text)
        redacted = result.redacted_text
        self.assertNotIn("EE382200221020145685", redacted)
        self.assertNotIn("4532 1111 1111 1112", redacted)
        self.assertIn("REDACTED", redacted)


class PIIDetectorUserMessageTest(unittest.TestCase):
    """Test user-facing messages."""

    def test_block_user_message_mentions_display_names(self):
        result = PIIResult(
            score=0.95,
            action=DetectionAction.BLOCK,
            pii_detections=[
                {
                    "entity_type": "pii_private_key",
                    "display_name": "private key",
                    "action": "block",
                },
            ],
        )
        msg = result.user_message
        self.assertIn("private key", msg)
        self.assertNotIn("pii_private_key", msg)
        self.assertIn("blocked", msg.lower())

    def test_redact_user_message_mentions_display_names(self):
        result = PIIResult(
            score=0.65,
            action=DetectionAction.REDACT,
            pii_detections=[
                {
                    "entity_type": "pii_estonian_id",
                    "display_name": "Estonian ID code",
                    "action": "redact",
                },
            ],
        )
        msg = result.user_message
        self.assertIn("Estonian ID code", msg)
        self.assertNotIn("pii_estonian_id", msg)
        self.assertIn("redacted", msg.lower())

    def test_warn_user_message_mentions_display_names(self):
        result = PIIResult(
            score=0.50,
            action=DetectionAction.WARN,
            pii_detections=[
                {
                    "entity_type": "pii_jwt",
                    "display_name": "JWT token",
                    "action": "warn",
                },
            ],
        )
        msg = result.user_message
        self.assertIn("JWT token", msg)
        self.assertNotIn("pii_jwt", msg)
        self.assertIn("sensitive information", msg.lower())

    def test_allow_user_message_empty(self):
        result = PIIResult()
        self.assertEqual(result.user_message, "")


class PIIDetectorAWSAzurePatternTest(unittest.TestCase):
    """Test that AWS/Azure patterns require keyword context."""

    def setUp(self):
        self.detector = PIIDetector()

    def test_aws_secret_key_with_context_blocked(self):
        """AWS secret key with keyword prefix should be blocked."""
        text = 'aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"'
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.BLOCK)
        self.assertTrue(
            any(d["entity_type"] == "pii_aws_secret_key" for d in result.pii_detections)
        )

    def test_aws_secret_key_without_context_not_blocked(self):
        """Bare 40-char base64 string should NOT trigger without keyword prefix."""
        # This is just a random 40-char base64-ish string, not preceded by AWS keyword
        text = "Here is some data: wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY1"
        result = self.detector.detect(text)
        aws_detections = [
            d for d in result.pii_detections if d["entity_type"] == "pii_aws_secret_key"
        ]
        self.assertEqual(len(aws_detections), 0)

    def test_azure_key_with_context_blocked(self):
        """Azure key with keyword prefix should be blocked."""
        text = 'account_key="dGhpcyBpcyBhIHRlc3Qga2V5IHRoYXQgaXMgbG9uZyBlbm91Z2g=="'
        result = self.detector.detect(text)
        self.assertEqual(result.action, DetectionAction.BLOCK)
        self.assertTrue(
            any(d["entity_type"] == "pii_azure_key" for d in result.pii_detections)
        )

    def test_azure_key_without_context_not_blocked(self):
        """Bare base64+== string should NOT trigger without keyword prefix."""
        text = "Data: dGhpcyBpcyBhIHRlc3Qga2V5IHRoYXQgaXMgbG9uZyBlbm91Z2g=="
        result = self.detector.detect(text)
        azure_detections = [
            d for d in result.pii_detections if d["entity_type"] == "pii_azure_key"
        ]
        self.assertEqual(len(azure_detections), 0)


class PIIDetectorFalsePositiveExtendedTest(unittest.TestCase):
    """Extended false positive tests for common benign inputs."""

    def setUp(self):
        self.detector = PIIDetector()

    def test_known_test_card_not_detected(self):
        """Stripe test card 4111111111111111 should not be detected."""
        text = "Use test card 4111111111111111 for testing"
        result = self.detector.detect(text)
        cc_detections = [
            d for d in result.pii_detections if d["entity_type"] == "pii_credit_card"
        ]
        self.assertEqual(len(cc_detections), 0)

    def test_git_sha_not_detected(self):
        """40-char hex string (git SHA) should NOT trigger AWS/Azure detection."""
        text = "Commit: a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        result = self.detector.detect(text)
        # Should not match AWS secret key or Azure key patterns
        cloud_detections = [
            d
            for d in result.pii_detections
            if d["entity_type"] in ("pii_aws_secret_key", "pii_azure_key")
        ]
        self.assertEqual(len(cloud_detections), 0)


class PIIDetectorMixedDetectionTest(unittest.TestCase):
    """Test mixed injection and PII scenarios."""

    def setUp(self):
        self.detector = PIIDetector()

    def test_overlapping_ranges_redacted_cleanly(self):
        """Overlapping IBAN patterns should produce clean redacted output."""
        # Create a PIIResult with overlapping ranges
        result = PIIResult(
            score=0.65,
            action=DetectionAction.REDACT,
            pii_detections=[
                {
                    "entity_type": "pii_iban_estonian",
                    "matched_text": "EE38***",
                    "start": 10,
                    "end": 30,
                    "confidence": 0.65,
                    "action": "redact",
                },
                {
                    "entity_type": "pii_iban_general",
                    "matched_text": "EE38***",
                    "start": 10,
                    "end": 30,
                    "confidence": 0.60,
                    "action": "redact",
                },
            ],
        )
        result.compute_redacted_text("My IBAN: EE382200221020145685 thanks")
        redacted = result.redacted_text
        # Should have exactly one REDACTED placeholder, not two
        self.assertEqual(redacted.count("REDACTED"), 1)
        self.assertNotIn("EE382200221020145685", redacted)


class PIIDetectorEUNationalIDTest(unittest.TestCase):
    """Test EU national ID detection with correct action and valid test values."""

    def setUp(self):
        self.detector = PIIDetector()

    def _assert_detected(self, text, category, expected_action=DetectionAction.REDACT):
        result = self.detector.detect(text)
        detections = [d for d in result.pii_detections if d["entity_type"] == category]
        self.assertTrue(detections, f"Expected {category} to be detected in: {text!r}")
        self.assertEqual(result.action, expected_action)

    def test_italy_codice_fiscale_detected(self):
        text = "Il mio codice fiscale è RSSMRA85M01H501Q"
        self._assert_detected(text, "pii_italy_codice_fiscale")

    def test_france_nir_detected(self):
        text = "Mon numéro de sécurité sociale est 185017511500323"
        self._assert_detected(text, "pii_france_nir")

    def test_finland_hetu_detected(self):
        text = "Henkilötunnus: 131052-308T"
        self._assert_detected(text, "pii_finland_hetu")

    def test_spain_dni_detected(self):
        text = "Mi DNI es 12345678Z"
        self._assert_detected(text, "pii_spain_dni")

    def test_spain_nie_detected(self):
        text = "Mi NIE es X1234567L"
        self._assert_detected(text, "pii_spain_nie")

    def test_poland_pesel_detected(self):
        text = "Mój numer PESEL to 44051401359"
        self._assert_detected(text, "pii_poland_pesel")

    def test_germany_steuer_id_detected(self):
        text = "Meine Steuer-ID ist 11234567890"
        self._assert_detected(text, "pii_germany_steuer_id")

    def test_czech_birth_number_detected(self):
        text = "Mé rodné číslo je 750101/0011"
        self._assert_detected(text, "pii_czech_birth_number")

    def test_netherlands_bsn_detected(self):
        """BSN requires keyword context (low base weight)."""
        text = "Mijn BSN is 111222333"
        self._assert_detected(text, "pii_netherlands_bsn")

    def test_sweden_personnummer_detected(self):
        """Personnummer requires keyword context (low base weight)."""
        text = "Mitt personnummer är 850709-9805"
        self._assert_detected(text, "pii_sweden_personnummer")

    def test_eu_vat_warned(self):
        """EU VAT numbers should trigger WARN, not REDACT."""
        text = "Our VAT number is ATU12345678"
        self._assert_detected(text, "pii_eu_vat", DetectionAction.WARN)


class PIIDetectorEUValidatorTest(unittest.TestCase):
    """Test that validators correctly reject invalid checksums."""

    def test_italy_invalid_checksum(self):
        # Change the check letter from Q to A
        self.assertFalse(validate_italy_codice_fiscale("RSSMRA85M01H501A"))

    def test_france_nir_invalid_key(self):
        # Valid is 185017511500323, change key from 23 to 99
        self.assertFalse(validate_france_nir("185017511500399"))

    def test_finland_hetu_invalid_check(self):
        # Valid is 131052-308T, change T to A
        self.assertFalse(validate_finland_hetu("131052-308A"))

    def test_spain_dni_invalid_letter(self):
        # Valid is 12345678Z, change Z to A
        self.assertFalse(validate_spain_dni_nie("12345678A"))

    def test_spain_nie_invalid_letter(self):
        # Valid is X1234567L, change L to A
        self.assertFalse(validate_spain_dni_nie("X1234567A"))

    def test_poland_pesel_invalid_check(self):
        # Valid is 44051401359, change 9 to 0
        self.assertFalse(validate_poland_pesel("44051401350"))

    def test_germany_steuer_id_invalid_check(self):
        # Valid is 11234567890, change last digit
        self.assertFalse(validate_germany_steuer_id("11234567891"))

    def test_germany_steuer_id_invalid_frequency(self):
        # All digits unique (no duplicates) — fails structural rule
        self.assertFalse(validate_germany_steuer_id("12345678903"))

    def test_czech_birth_number_invalid_mod(self):
        # 10-digit number not divisible by 11
        self.assertFalse(validate_czech_birth_number("7501010012"))

    def test_netherlands_bsn_invalid_elfproef(self):
        self.assertFalse(validate_netherlands_bsn("123456789"))

    def test_sweden_personnummer_invalid_luhn(self):
        self.assertFalse(validate_sweden_personnummer("8507099806"))


class PIIDetectorEUFalsePositiveTest(unittest.TestCase):
    """Test that generic numbers don't trigger EU national ID detection."""

    def setUp(self):
        self.detector = PIIDetector()

    def test_bsn_without_keyword_not_detected(self):
        """A bare 9-digit number without BSN context should not be detected."""
        text = "Order number 111222333 was processed"
        result = self.detector.detect(text)
        bsn_detections = [
            d
            for d in result.pii_detections
            if d["entity_type"] == "pii_netherlands_bsn"
        ]
        self.assertEqual(len(bsn_detections), 0)

    def test_personnummer_without_keyword_not_detected(self):
        """A bare date-like number without keyword context should not trigger."""
        text = "Reference code 850709-9805 in the system"
        result = self.detector.detect(text)
        pn_detections = [
            d
            for d in result.pii_detections
            if d["entity_type"] == "pii_sweden_personnummer"
        ]
        self.assertEqual(len(pn_detections), 0)

    def test_short_number_not_steuer_id(self):
        """A random 11-digit number with bad checksum should not trigger."""
        text = "Tracking ID: 99999999999"
        result = self.detector.detect(text)
        steuer_detections = [
            d
            for d in result.pii_detections
            if d["entity_type"] == "pii_germany_steuer_id"
        ]
        self.assertEqual(len(steuer_detections), 0)

    def test_example_context_suppresses_eu_patterns(self):
        """Suppress words should reduce confidence below threshold."""
        text = "An example Finnish HETU format looks like 131052-308T"
        result = self.detector.detect(text)
        hetu_detections = [
            d for d in result.pii_detections if d["entity_type"] == "pii_finland_hetu"
        ]
        # "example" + "looks like" context should suppress
        self.assertEqual(len(hetu_detections), 0)
