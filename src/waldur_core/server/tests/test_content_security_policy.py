"""Every response carries a Content-Security-Policy.

API responses get a policy that lets nothing load or run, so a response a
browser renders as a page cannot run script, be framed, or post a form. The
HTML pages mastermind serves itself (admin, browsable API, landing page) get a
policy that allows their own static files and nonce-carrying scripts only, and
each page must keep to it: no inline script without the nonce, no inline event
handlers. Views that set a policy of their own keep it.
"""

import base64
import hashlib
import re

from django.test import override_settings
from django.urls import include, re_path
from rest_framework import status, test
from saml2.pack import HTML_FORM_SPEC

from waldur_auth_saml2.views import Saml2LogoutCompleteView
from waldur_core.passkeys.tests.test_enforcement import _enrol_admin, enforce
from waldur_core.server.urls import urlpatterns as server_urlpatterns
from waldur_core.structure.tests import factories as structure_factories

API_POLICY = (
    "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
HEADER = "Content-Security-Policy"

# The URLs with REST framework's browsable login and logout, which are mounted
# only in debug mode, so the page is also checked with its logout link.
urlpatterns = [
    *server_urlpatterns,
    re_path(
        r"^api-auth-browsable/",
        include("rest_framework.urls", namespace="rest_framework"),
    ),
]

SCRIPT_TAG = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
EVENT_HANDLER = re.compile(r"<[a-z][^>]*\son[a-z]+\s*=", re.IGNORECASE)


def directives(policy):
    parsed = {}
    for part in policy.split(";"):
        tokens = part.split()
        if tokens:
            parsed[tokens[0]] = tokens[1:]
    return parsed


class ApiResponseTest(test.APITestCase):
    def test_a_json_response(self):
        self.client.force_authenticate(structure_factories.UserFactory())

        response = self.client.get("/api/users/me/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response[HEADER], API_POLICY)

    def test_a_json_error(self):
        response = self.client.get("/api/users/me/")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response[HEADER], API_POLICY)

    def test_a_cors_preflight(self):
        response = self.client.options(
            "/api/users/me/", HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response[HEADER], API_POLICY)

    def test_a_redirect_from_a_page_keeps_the_api_policy(self):
        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(response[HEADER], API_POLICY)

    def test_an_unknown_path(self):
        response = self.client.get("/no-such-page/")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        policy = directives(response[HEADER])
        self.assertEqual(policy["frame-ancestors"], ["'none'"])
        self.assertEqual(policy["base-uri"], ["'none'"])


class PagePolicyMixin:
    def assert_page_keeps_to_its_policy(self, response):
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        policy = directives(response[HEADER])
        for name in ("object-src", "base-uri", "frame-ancestors"):
            self.assertEqual(policy[name], ["'none'"], name)
        self.assertEqual(policy["form-action"], ["'self'"])
        script_src = policy["script-src"]
        self.assertNotIn("'unsafe-inline'", script_src)
        self.assertNotIn("'unsafe-eval'", script_src)
        nonces = {
            source[len("'nonce-") : -1]
            for source in script_src
            if source.startswith("'nonce-")
        }
        html = response.content.decode()
        for attributes in SCRIPT_TAG.findall(html):
            if "src=" in attributes or 'type="application/json"' in attributes:
                continue
            nonce = re.search(r'nonce="([^"]+)"', attributes)
            self.assertTrue(
                nonce and nonce.group(1) in nonces,
                f"inline script without the page's nonce: <script{attributes}>",
            )
        self.assertIsNone(EVENT_HANDLER.search(html), "inline event handler")


class HtmlPageTest(PagePolicyMixin, test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)

    def test_admin_login(self):
        self.assert_page_keeps_to_its_policy(self.client.get("/admin/login/"))

    def test_admin_index(self):
        self.client.force_login(self.staff)

        self.assert_page_keeps_to_its_policy(self.client.get("/admin/"))

    def test_admin_form_with_password_widgets_and_its_own_script(self):
        self.client.force_login(self.staff)

        self.assert_page_keeps_to_its_policy(
            self.client.get("/admin/structure/sharedservicesettings/add/")
        )

    def test_admin_list_with_dropdown_filters(self):
        structure_factories.ProjectFactory()
        structure_factories.ProjectFactory()
        self.client.force_login(self.staff)

        self.assert_page_keeps_to_its_policy(
            self.client.get("/admin/structure/project/")
        )

    def test_browsable_api(self):
        self.client.force_login(self.staff)

        self.assert_page_keeps_to_its_policy(
            self.client.get("/api/", HTTP_ACCEPT="text/html")
        )

    @override_settings(ROOT_URLCONF=__name__)
    def test_browsable_api_with_its_logout_link(self):
        self.client.force_login(self.staff)

        response = self.client.get("/api/", HTTP_ACCEPT="text/html")

        self.assert_page_keeps_to_its_policy(response)
        self.assertContains(response, "/api-auth-browsable/logout/")

    def test_health_check_page(self):
        self.assert_page_keeps_to_its_policy(
            self.client.get("/health-check/", HTTP_ACCEPT="text/html")
        )

    def test_admin_login_under_a_sub_path(self):
        # A deployment served under a prefix (SCRIPT_NAME) keeps the page
        # policy on its own pages.
        response = self.client.get("/admin/login/", SCRIPT_NAME="/mastermind")

        self.assert_page_keeps_to_its_policy(response)

    def test_landing_page(self):
        self.assert_page_keeps_to_its_policy(self.client.get("/"))


@enforce()
class AdminPasskeyPageTest(PagePolicyMixin, test.APITestCase):
    def test_the_challenge_page(self):
        staff = structure_factories.UserFactory(is_staff=True)
        _enrol_admin(staff)
        self.client.force_login(staff)

        response = self.client.get("/admin/passkey/")

        self.assert_page_keeps_to_its_policy(response)


class SamlPostBindingPageTest(test.APITestCase):
    SUBMIT_HASH = base64.b64encode(
        hashlib.sha256(b"document.forms[0].submit()").digest()
    ).decode()

    def reply(self, url, page=None):
        page = page or (
            '<html><body onload="document.forms[0].submit()">'
            f'<form method="post" action="{url}"></form></body></html>'
        )
        response = Saml2LogoutCompleteView.reply(
            {"headers": [("Content-type", "text/html")], "data": page, "url": url},
            lambda info: info["url"],
        )
        return directives(response[HEADER])

    def test_only_the_submit_handler_may_run(self):
        policy = self.reply("https://idp.example.org/slo")

        self.assertEqual(policy["default-src"], ["'none'"])
        self.assertEqual(
            policy["script-src"], ["'unsafe-hashes'", f"'sha256-{self.SUBMIT_HASH}'"]
        )
        self.assertEqual(policy["form-action"], ["https:"])
        self.assertEqual(policy["frame-ancestors"], ["'none'"])

    def test_a_handler_injected_through_the_target_is_not_allowed(self):
        # pysaml2 does not escape the form's action, which comes from metadata.
        url = 'https://idp.example.org/slo" onmouseover="alert(document.domain)'
        injected = base64.b64encode(
            hashlib.sha256(b"alert(document.domain)").digest()
        ).decode()

        policy = self.reply(url)

        self.assertNotIn(f"'sha256-{injected}'", policy["script-src"])
        self.assertEqual(len(policy["script-src"]), 2)

    def test_a_plain_http_identity_provider_is_allowed_by_its_origin(self):
        policy = self.reply("http://idp.test:8080/slo")

        self.assertEqual(policy["form-action"], ["https:", "http://idp.test:8080"])

    def test_pysaml2_still_submits_with_the_hashed_handler(self):
        self.assertIn('onload="document.forms[0].submit()"', HTML_FORM_SPEC)

    def test_a_target_that_is_no_valid_origin_adds_no_source(self):
        for url in (
            "http://idp.test:abc/slo",
            "http://idp;x.test/slo",
            "http://[bad/slo",
        ):
            self.assertEqual(self.reply(url)["form-action"], ["https:"], url)

    def test_an_ipv6_identity_provider_is_bracketed(self):
        policy = self.reply("http://[::1]:8080/slo")

        self.assertEqual(policy["form-action"], ["https:", "http://[::1]:8080"])
