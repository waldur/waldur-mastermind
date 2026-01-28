import unittest

from waldur_mastermind.chat.validation.evaluators import (
    LanguageEvaluator,
    PatternEvaluator,
)


class LanguageQualityTest(unittest.TestCase):
    """Tests for language quality validation scenarios."""

    def setUp(self):
        """Set up evaluator for each test."""
        self.evaluator = PatternEvaluator()

    def test_emoji_detection(self):
        """Test that emoji usage is detected correctly."""
        config = {
            "forbidden_patterns": ["✓|✗|❌|✅"],
            "rationale": "No emojis unless requested",
        }

        # Should pass without emojis
        result = self.evaluator.evaluate("Here are your resources.", config)
        self.assertTrue(result.passed)

        # Should fail with emojis
        result = self.evaluator.evaluate("Resources loaded ✅", config)
        self.assertFalse(result.passed)

    def test_time_estimates_detection(self):
        """Test that time estimates are detected."""
        config = {
            "forbidden_patterns": [
                "\\b(few|couple of)\\s+(minutes|hours)\\b",
                "\\b(quick|fast)\\s+(fix|change)\\b",
            ],
            "rationale": "Never give time estimates",
        }

        # Should pass
        result = self.evaluator.evaluate("I'll help you set up authentication.", config)
        self.assertTrue(result.passed)

        # Should fail
        result = self.evaluator.evaluate("This is a quick fix.", config)
        self.assertFalse(result.passed)

        result = self.evaluator.evaluate("Takes a few minutes.", config)
        self.assertFalse(result.passed)

    def test_excessive_praise_detection(self):
        """Test that excessive praise is detected."""
        config = {
            "forbidden_patterns": [
                "\\byou('re| are) absolutely (right|correct)\\b",
                "\\b(excellent|brilliant) idea\\b",
            ],
            "rationale": "Avoid excessive praise",
        }

        # Should pass
        result = self.evaluator.evaluate("JWT is a valid approach.", config)
        self.assertTrue(result.passed)

        # Should fail
        result = self.evaluator.evaluate("You're absolutely right!", config)
        self.assertFalse(result.passed)

        result = self.evaluator.evaluate("That's an excellent idea.", config)
        self.assertFalse(result.passed)

    def test_verbose_preambles_detection(self):
        """Test that verbose preambles are detected."""
        config = {
            "forbidden_patterns": [
                "\\bbefore (we|I) (get|dive) into\\b",
                "\\blet me explain in detail\\b",
            ],
            "rationale": "Keep responses concise",
        }

        # Should pass
        result = self.evaluator.evaluate(
            "A resource is a cloud infrastructure component.", config
        )
        self.assertTrue(result.passed)

        # Should fail
        result = self.evaluator.evaluate(
            "Before we dive into that, let me explain in detail.", config
        )
        self.assertFalse(result.passed)

    def test_professional_tone_detection(self):
        """Test that unprofessional language is detected."""
        config = {
            "forbidden_patterns": ["\\b(awesome|cool)\\b", "\\bhappy to help\\b"],
            "rationale": "Maintain professional tone",
        }

        # Should pass
        result = self.evaluator.evaluate("Use the marketplace API endpoint.", config)
        self.assertTrue(result.passed)

        # Should fail
        result = self.evaluator.evaluate("That's awesome! Happy to help.", config)
        self.assertFalse(result.passed)

    def test_hedging_language_detection(self):
        """Test that hedging language is detected."""
        config = {
            "forbidden_patterns": [
                "\\bI (think|believe|guess)\\b",
                "\\byou (might|could|may) want to\\b",
            ],
            "rationale": "Provide direct answers",
        }

        # Should pass
        result = self.evaluator.evaluate("Waldur supports LDAP authentication.", config)
        self.assertTrue(result.passed)

        # Should fail
        result = self.evaluator.evaluate("I think you might want to use LDAP.", config)
        self.assertFalse(result.passed)

    def test_action_announcements_detection(self):
        """Test that action announcements are detected."""
        config = {
            "forbidden_patterns": ["\\b(let me|I('ll| will)) (read|check)\\b"],
            "rationale": "Don't announce actions",
        }

        # Should pass
        result = self.evaluator.evaluate("The code is in models.py:45", config)
        self.assertTrue(result.passed)

        # Should fail
        result = self.evaluator.evaluate("Let me check that file for you.", config)
        self.assertFalse(result.passed)


class MultilingualSupportTest(unittest.TestCase):
    """Tests for multilingual language detection."""

    def setUp(self):
        """Set up language evaluator for each test."""
        self.evaluator = LanguageEvaluator()

    def test_english_detection(self):
        """Test English language detection."""
        lang = self.evaluator._detect_language("Hello, how can I help you?")
        self.assertEqual(lang, "en")

    def test_estonian_detection_by_chars(self):
        """Test Estonian detection by special characters."""
        lang = self.evaluator._detect_language("Tere, kuidas saad mind aidata?")
        self.assertEqual(lang, "et")

    def test_estonian_detection_by_words(self):
        """Test Estonian detection by common words."""
        lang = self.evaluator._detect_language("Tere! Aitäh!")
        self.assertEqual(lang, "et")

    def test_lithuanian_detection(self):
        """Test Lithuanian detection."""
        lang = self.evaluator._detect_language("Labas, kaip galite man padėti?")
        self.assertEqual(lang, "lt")

    def test_latvian_detection(self):
        """Test Latvian detection."""
        lang = self.evaluator._detect_language("Sveiki, kā jūs varat man palīdzēt?")
        self.assertEqual(lang, "lv")

    def test_russian_detection(self):
        """Test Russian detection by Cyrillic characters."""
        lang = self.evaluator._detect_language("Привет, как вы можете мне помочь?")
        self.assertEqual(lang, "ru")

    def test_language_match_pass(self):
        """Test that matching languages pass evaluation."""
        config = {
            "match_input_language": True,
            "supported_languages": ["en", "et", "lt", "lv", "ru"],
            "input_text": "Hello, how are you?",
        }

        # English input, English response
        result = self.evaluator.evaluate("I can help you with resources.", config)
        self.assertTrue(result.passed)
        self.assertEqual(result.score, 1.0)
        self.assertIn("matches input", result.message)

    def test_language_match_fail(self):
        """Test that mismatched languages fail evaluation."""
        config = {
            "match_input_language": True,
            "supported_languages": ["en", "et", "lt", "lv", "ru"],
            "input_text": "Tere, kuidas saad mind aidata?",
        }

        # Estonian input, English response (should fail)
        result = self.evaluator.evaluate("I can help you.", config)
        self.assertFalse(result.passed)
        self.assertEqual(result.score, 0.0)
        self.assertIn("mismatch", result.message.lower())

    def test_estonian_round_trip(self):
        """Test Estonian input gets Estonian response."""
        config = {
            "match_input_language": True,
            "supported_languages": ["en", "et", "lt", "lv", "ru"],
            "input_text": "Tere, kuidas saad mind aidata?",
        }

        # Estonian input, Estonian response
        result = self.evaluator.evaluate("Tere! Saan sind aidata.", config)
        self.assertTrue(result.passed)

    def test_lithuanian_round_trip(self):
        """Test Lithuanian input gets Lithuanian response."""
        config = {
            "match_input_language": True,
            "supported_languages": ["en", "et", "lt", "lv", "ru"],
            "input_text": "Labas, kaip galite man padėti?",
        }

        # Lithuanian input, Lithuanian response
        result = self.evaluator.evaluate("Labas! Galiu jums padėti.", config)
        self.assertTrue(result.passed)

    def test_latvian_round_trip(self):
        """Test Latvian input gets Latvian response."""
        config = {
            "match_input_language": True,
            "supported_languages": ["en", "et", "lt", "lv", "ru"],
            "input_text": "Sveiki, kā jūs varat man palīdzēt?",
        }

        # Latvian input, Latvian response
        result = self.evaluator.evaluate("Sveiki! Es varu jums palīdzēt.", config)
        self.assertTrue(result.passed)

    def test_russian_round_trip(self):
        """Test Russian input gets Russian response."""
        config = {
            "match_input_language": True,
            "supported_languages": ["en", "et", "lt", "lv", "ru"],
            "input_text": "Привет, как вы можете мне помочь?",
        }

        # Russian input, Russian response
        result = self.evaluator.evaluate("Привет! Я могу вам помочь.", config)
        self.assertTrue(result.passed)
