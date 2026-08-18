from unittest import mock

from django.test import TestCase

from waldur_core.logging import log
from waldur_core.logging.tests import factories
from waldur_core.structure.tests import factories as structure_factories


class ScrubSensitiveTest(TestCase):
    def test_masks_sensitive_keys_recursively_without_mutating_input(self):
        data = {
            "password": "s3kret",
            "keycloak_password": "kc",
            "api_url": "https://example.com",
            "nested": {"vault_token": "t", "name": "ok"},
            "items": [{"client_secret": "cs"}],
        }

        result = log.scrub_sensitive(data)

        self.assertEqual(result["password"], "***")
        self.assertEqual(result["keycloak_password"], "***")
        # Not a credential name — left as-is.
        self.assertEqual(result["api_url"], "https://example.com")
        self.assertEqual(result["nested"]["vault_token"], "***")
        self.assertEqual(result["nested"]["name"], "ok")
        self.assertEqual(result["items"][0]["client_secret"], "***")
        # Input dict is not mutated.
        self.assertEqual(data["password"], "s3kret")


class WebHookScrubTest(TestCase):
    @mock.patch("waldur_core.logging.models.validate_outbound_url")
    @mock.patch("waldur_core.logging.models.requests.post")
    def test_process_scrubs_context_before_sending(self, post, _validate):
        webhook = factories.WebHookFactory(
            user=structure_factories.UserFactory(),
            destination_url="http://example.com/",
        )
        event = factories.EventFactory(
            context={"password": "s3kret", "customer_name": "Acme"}
        )

        webhook.process(event)

        post.assert_called_once()
        sent_context = post.call_args.kwargs["json"]["context"]
        self.assertEqual(sent_context["password"], "***")
        self.assertEqual(sent_context["customer_name"], "Acme")
