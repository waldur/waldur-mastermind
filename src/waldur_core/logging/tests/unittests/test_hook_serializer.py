import unittest

from waldur_core.logging import loggers, serializers


class HookSerializerTest(unittest.TestCase):
    def setUp(self):
        self.events = loggers.get_valid_events()[:3]

    def test_valid_web_settings(self):
        serializer = serializers.WebHookSerializer(
            data={
                "event_types": self.events,
                "destination_url": "http://example.com/",
                "content_type": "form",
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_valid_email_settings(self):
        serializer = serializers.EmailHookSerializer(
            data={"event_types": self.events, "email": "test@example.com"}
        )
        self.assertTrue(serializer.is_valid())

    def test_invalid_web_settings(self):
        serializer = serializers.WebHookSerializer(
            data={"event_types": self.events, "destination_url": "INVALID_URL"}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("destination_url", serializer.errors)

    def test_invalid_events(self):
        serializer = serializers.WebHookSerializer(
            data={
                "event_types": ["INVALID_EVENT"],
                "destination_url": "http://example.com/",
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("event_types", serializer.errors)
