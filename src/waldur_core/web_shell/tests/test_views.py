from django.conf import settings
from django.core.cache import cache
from django.test import override_settings
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.web_shell import tickets

TICKET_URL = "/api/web-shell-ticket/"
CONFIGURATION_URL = "/api/configuration/"
WEB_SHELL_URL = "http://localhost:18090/webshell/"


def web_shell_settings(debug=True, **core):
    return override_settings(
        DEBUG=debug,
        WALDUR_CORE={
            **settings.WALDUR_CORE,
            "WEB_SHELL_ENABLED": True,
            "WEB_SHELL_URL": WEB_SHELL_URL,
            **core,
        },
    )


@web_shell_settings()
class WebShellTicketTest(test.APITestCase):
    def test_staff_gets_a_single_use_link(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)

        response = self.client.post(TICKET_URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        base, _, ticket = response.data["url"].partition("#t=")
        self.assertEqual(base, WEB_SHELL_URL)
        self.assertEqual(tickets.load(ticket)[0], staff.uuid.hex)

    def test_link_is_bound_to_the_api_token_it_was_requested_with(self):
        staff = structure_factories.UserFactory(is_staff=True)
        token, _ = Token.objects.get_or_create(user=staff)
        self.client.force_authenticate(staff, token=token)

        response = self.client.post(TICKET_URL)

        ticket = response.data["url"].partition("#t=")[2]
        self.assertEqual(
            tickets.load(ticket).token_digest, tickets.token_digest(token.key)
        )

    def test_non_staff_is_forbidden(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.post(TICKET_URL)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_is_rejected(self):
        response = self.client.post(TICKET_URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @web_shell_settings(debug=False)
    def test_not_available_without_debug(self):
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        response = self.client.post(TICKET_URL)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @web_shell_settings(WEB_SHELL_ENABLED=False)
    def test_not_available_when_disabled(self):
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        response = self.client.post(TICKET_URL)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class WebShellConfigurationTest(test.APITestCase):
    def setUp(self):
        # get_public_settings caches its result for the life of the process.
        cache.clear()

    @web_shell_settings()
    def test_configuration_reports_web_shell_when_enabled(self):
        response = self.client.get(CONFIGURATION_URL)
        self.assertTrue(response.data["WALDUR_CORE"]["WEB_SHELL_ENABLED"])

    @web_shell_settings(debug=False)
    def test_configuration_hides_web_shell_without_debug(self):
        response = self.client.get(CONFIGURATION_URL)
        self.assertFalse(response.data["WALDUR_CORE"]["WEB_SHELL_ENABLED"])

    @web_shell_settings()
    def test_configuration_does_not_publish_the_web_shell_url(self):
        response = self.client.get(CONFIGURATION_URL)
        self.assertNotIn("WEB_SHELL_URL", response.data["WALDUR_CORE"])
