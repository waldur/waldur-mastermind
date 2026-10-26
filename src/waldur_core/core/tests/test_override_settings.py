import re
from datetime import timedelta

from django.conf import settings
from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework import status, test

from waldur_core.configuration.registry import secret_keys
from waldur_core.configuration.secrets import (
    is_constance_secret,
    redact_constance_secrets,
)
from waldur_core.configuration.testing import override_config
from waldur_core.core.models import PersonalAccessToken
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.structure.tests.factories import UserFactory

# Setting names that look like credentials. Support reads every setting that
# is not secret_field in clear, so each match must be secret_field or listed
# in NON_SECRET_SETTINGS_WITH_CREDENTIAL_NAMES.
CREDENTIAL_NAME_PATTERN = re.compile(
    r"PASSWORD|TOKEN|SECRET|SALT|CREDENTIAL|PRIVATE|APIKEY|_KEY(_|$)"
)

NON_SECRET_SETTINGS_WITH_CREDENTIAL_NAMES = frozenset(
    {
        # Limits and budgets counted in tokens.
        "AI_ASSISTANT_TOKEN_LIMIT_DAILY",
        "AI_ASSISTANT_TOKEN_LIMIT_WEEKLY",
        "AI_ASSISTANT_TOKEN_LIMIT_MONTHLY",
        "AI_ASSISTANT_GLOBAL_DAILY_TOKEN_BUDGET",
        "ANONYMOUS_CHAT_REVIEW_DAILY_TOKEN_BUDGET",
        "PAT_MAX_TOKENS_PER_USER",
        # Durations.
        "API_TOKEN_MAX_LIFETIME_MINUTES",
        # Token endpoint URLs.
        "ONBOARDING_BOLAGSVERKET_TOKEN_API_URL",
        "ONBOARDING_DNB_TOKEN_URL",
        # Flags, types and formats.
        "OIDC_ACCESS_TOKEN_ENABLED",
        "ATLASSIAN_OAUTH2_TOKEN_TYPE",
        "MATRIX_TOKENS_MANAGED_BY",
        "ENABLE_ISSUES_FOR_USER_SSH_KEY_CHANGES",
        "SSH_KEY_ALLOWED_TYPES",
        "SSH_KEY_MIN_RSA_KEY_SIZE",
        "WALDUR_SUPPORT_ISSUE_KEY_PREFIX",
        # LiveKit API key ID; the credential is MATRIX_LIVEKIT_SECRET.
        "MATRIX_LIVEKIT_KEY",
    }
)

# Distinctive values that must never appear in a non-staff response.
SECRET_SENTINELS = {
    "OIDC_CLIENT_SECRET": "oidc-client-secret-sentinel-9f3a",
    "FREEIPA_PASSWORD": "freeipa-password-sentinel-9f3a",
    "SCIM_API_KEY": "scim-api-key-sentinel-9f3a",
    "AI_ASSISTANT_API_TOKEN": "ai-assistant-token-sentinel-9f3a",
    "MATRIX_USER_REGISTRATION_SECRET": "matrix-registration-secret-sentinel-9f3a",
    "ATLASSIAN_PASSWORD": "atlassian-password-sentinel-9f3a",
    "ONBOARDING_WICO_TOKEN": "wico-token-sentinel-9f3a",
}


class OverrideSettingsTest(test.APITestCase):
    def setUp(self):
        self.url = "/api/override-settings/"
        self.staff = UserFactory(is_staff=True)
        self.client.force_login(self.staff)

    def _override_settings_and_get(self, payload):
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, 200)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response

    @override_config(
        WALDUR_SUPPORT_ENABLED=True,
        ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True,
        NOTIFY_STAFF_ABOUT_APPROVALS=False,
        WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE=False,
        DISABLE_DARK_THEME=True,
    )
    def test_override_settings_bool_field(self):
        """
        Test post request to "api/override-settings/" doesn't affect the fields that are not passed via payload of the request.
        """

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

        self.assertFalse(response.data["WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE"])
        self.assertTrue(response.data["WALDUR_SUPPORT_ENABLED"])
        self.assertTrue(response.data["ANONYMOUS_USER_CAN_VIEW_OFFERINGS"])
        self.assertFalse(response.data["NOTIFY_STAFF_ABOUT_APPROVALS"])
        self.assertTrue(response.data["DISABLE_DARK_THEME"])

        payload = {
            "WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE": True,
            "ANONYMOUS_USER_CAN_VIEW_OFFERINGS": False,
        }
        response = self._override_settings_and_get(payload)

        self.assertTrue(response.data["WALDUR_SUPPORT_DISPLAY_REQUEST_TYPE"])
        self.assertTrue(response.data["WALDUR_SUPPORT_ENABLED"])
        self.assertFalse(response.data["ANONYMOUS_USER_CAN_VIEW_OFFERINGS"])
        self.assertFalse(response.data["NOTIFY_STAFF_ABOUT_APPROVALS"])
        self.assertTrue(response.data["DISABLE_DARK_THEME"])

    def test_override_settings_list_field(self):
        payload = {"FREEIPA_BLACKLISTED_USERNAMES": ["root", "admin", "system"]}
        response = self._override_settings_and_get(payload)
        self.assertEqual(
            response.data["FREEIPA_BLACKLISTED_USERNAMES"], ["root", "admin", "system"]
        )

    def test_override_settings_country_list_field(self):
        payload = {"COUNTRIES": ["US", "GB", "DE"]}
        response = self._override_settings_and_get(payload)
        self.assertEqual(response.data["COUNTRIES"], ["US", "GB", "DE"])

    def test_non_empty_field_can_be_changed(self):
        payload = {"FREEIPA_GROUPNAME_PREFIX": "hpc_"}
        response = self._override_settings_and_get(payload)
        self.assertEqual(response.data["FREEIPA_GROUPNAME_PREFIX"], "hpc_")

    def test_non_empty_field_can_not_be_blanked_out(self):
        """
        An empty FreeIPA prefix makes every entity in the directory look
        Waldur-managed, so the group synchronization would delete all of them.
        """
        for key in ("FREEIPA_GROUPNAME_PREFIX", "FREEIPA_USERNAME_PREFIX"):
            with self.subTest(key=key):
                response = self.client.post(self.url, {key: ""})
                self.assertEqual(response.status_code, 400)
                self.assertIn(key, response.data)

    def test_override_settings_mixed_fields(self):
        payload = {
            "WALDUR_SUPPORT_ENABLED": True,
            "FREEIPA_BLACKLISTED_USERNAMES": ["root", "admin"],
            "COUNTRIES": ["US", "FR"],
            "SITE_NAME": "Custom Site",
            "SUPPORT_PORTAL_URL": "https://support.example.com",
            "CROSSREF_MAILTO": "test@example.com",
        }
        response = self._override_settings_and_get(payload)
        self.assertEqual(response.data["WALDUR_SUPPORT_ENABLED"], True)
        self.assertEqual(
            response.data["FREEIPA_BLACKLISTED_USERNAMES"], ["root", "admin"]
        )
        self.assertEqual(response.data["COUNTRIES"], ["US", "FR"])
        self.assertEqual(response.data["SITE_NAME"], "Custom Site")


def _create_pat(user, scopes):
    expires_at = timezone.now() + timedelta(days=30)
    full_token, prefix, token_hash = PersonalAccessToken.generate_token(expires_at)
    pat = PersonalAccessToken.objects.create(
        user=user,
        name="override-settings",
        token_prefix=prefix,
        token_hash=token_hash,
        scopes=scopes,
        expires_at=expires_at,
    )
    pat._plaintext_token = full_token
    return pat


class ConstanceSecretHelpersTest(SimpleTestCase):
    def test_declared_secret_field_is_secret(self):
        self.assertTrue(is_constance_secret("SCIM_API_KEY"))
        self.assertTrue(is_constance_secret("OIDC_CLIENT_SECRET"))
        self.assertTrue(is_constance_secret("FREEIPA_PASSWORD"))

    def test_name_heuristic_is_off_for_non_secret_settings(self):
        # These names contain "token" but they are limits and flags, not credentials.
        self.assertFalse(is_constance_secret("AI_ASSISTANT_TOKEN_LIMIT_DAILY"))
        self.assertFalse(is_constance_secret("OIDC_ACCESS_TOKEN_ENABLED"))
        self.assertFalse(is_constance_secret("SITE_NAME"))

    def test_name_heuristic_catches_undeclared_secret_names(self):
        self.assertFalse(is_constance_secret("CUSTOM_API_TOKEN"))
        self.assertTrue(is_constance_secret("CUSTOM_API_TOKEN", name_heuristic=True))
        self.assertTrue(
            is_constance_secret("AI_ASSISTANT_TOKEN_LIMIT_DAILY", name_heuristic=True)
        )

    def test_every_declared_secret_is_blanked_and_nothing_else_is(self):
        values = {key: f"leak-{key}-value" for key in settings.CONSTANCE_CONFIG}
        redacted = redact_constance_secrets(values)

        self.assertEqual(values["OIDC_CLIENT_SECRET"], "leak-OIDC_CLIENT_SECRET-value")
        secret_values = []
        for key, definition in settings.CONSTANCE_CONFIG.items():
            if len(definition) >= 3 and definition[2] == "secret_field":
                secret_values.append(values[key])
                self.assertEqual(redacted[key], "")
            else:
                self.assertEqual(redacted[key], values[key])

        rendered = str(redacted)
        for secret_value in secret_values:
            self.assertNotIn(secret_value, rendered)

    def test_credential_like_settings_are_declared_secret(self):
        undeclared = {
            key
            for key in settings.CONSTANCE_CONFIG
            if CREDENTIAL_NAME_PATTERN.search(key)
        }
        undeclared -= secret_keys() | NON_SECRET_SETTINGS_WITH_CREDENTIAL_NAMES
        self.assertEqual(
            undeclared,
            set(),
            "Support users read these settings in clear. Declare each as "
            "secret_field, or add it to NON_SECRET_SETTINGS_WITH_CREDENTIAL_NAMES "
            "with the reason it is not a credential.",
        )

    def test_non_secret_allowlist_is_current(self):
        for key in NON_SECRET_SETTINGS_WITH_CREDENTIAL_NAMES:
            with self.subTest(key=key):
                self.assertIn(key, settings.CONSTANCE_CONFIG)
                self.assertRegex(key, CREDENTIAL_NAME_PATTERN)
                self.assertNotIn(key, secret_keys())

    def test_redact_keeps_secret_keys(self):
        redacted = redact_constance_secrets(
            {"OIDC_CLIENT_SECRET": "hidden", "SITE_NAME": "Visible"}
        )
        self.assertEqual(redacted["OIDC_CLIENT_SECRET"], "")
        self.assertEqual(redacted["SITE_NAME"], "Visible")


@override_config(
    PAT_ENABLED=True,
    SITE_NAME="Visible Site",
    AI_ASSISTANT_TOKEN_LIMIT_DAILY=12345,
    OIDC_ACCESS_TOKEN_ENABLED=True,
    **SECRET_SENTINELS,
)
class OverrideSettingsSecretAccessTest(test.APITestCase):
    def setUp(self):
        self.url = "/api/override-settings/"
        self.staff = UserFactory(is_staff=True)
        self.support = UserFactory(is_support=True)
        self.regular = UserFactory()

    def _assert_secrets_absent(self, response):
        body = response.content.decode()
        for value in SECRET_SENTINELS.values():
            self.assertNotIn(value, body)

    def _assert_secrets_blanked(self, response):
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for key in SECRET_SENTINELS:
            self.assertIn(key, response.data)
            self.assertEqual(response.data[key], "")
        self._assert_secrets_absent(response)
        self.assertEqual(response.data["SITE_NAME"], "Visible Site")
        self.assertEqual(response.data["AI_ASSISTANT_TOKEN_LIMIT_DAILY"], 12345)
        self.assertIs(response.data["OIDC_ACCESS_TOKEN_ENABLED"], True)

    def test_staff_get_returns_secret_values(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for key, value in SECRET_SENTINELS.items():
            self.assertEqual(response.data[key], value)
        self.assertEqual(response.data["SITE_NAME"], "Visible Site")

    def test_support_get_blanks_secrets_and_keeps_other_settings(self):
        self.client.force_login(self.support)
        response = self.client.get(self.url)
        self._assert_secrets_blanked(response)

    def test_staff_and_support_user_receives_secrets(self):
        user = UserFactory(is_staff=True, is_support=True)
        self.client.force_login(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["OIDC_CLIENT_SECRET"], SECRET_SENTINELS["OIDC_CLIENT_SECRET"]
        )

    def test_support_and_staff_see_the_same_keys(self):
        self.client.force_login(self.staff)
        staff_response = self.client.get(self.url)
        self.client.force_login(self.support)
        support_response = self.client.get(self.url)
        self.assertEqual(set(staff_response.data), set(support_response.data))

    def test_support_cannot_update_settings(self):
        self.client.force_login(self.support)
        response = self.client.post(
            self.url,
            {
                "SITE_NAME": "Changed by support",
                "OIDC_CLIENT_SECRET": "written-by-support-sentinel",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotIn(b"written-by-support-sentinel", response.content)
        self._assert_secrets_absent(response)

        self.client.force_login(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.data["SITE_NAME"], "Visible Site")
        self.assertEqual(
            response.data["OIDC_CLIENT_SECRET"], SECRET_SENTINELS["OIDC_CLIENT_SECRET"]
        )
        self.assertNotIn(b"written-by-support-sentinel", response.content)

    def test_regular_user_cannot_read_settings(self):
        self.client.force_login(self.regular)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self._assert_secrets_absent(response)

        response = self.client.post(self.url, {"SITE_NAME": "Changed"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_user_cannot_read_settings(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self._assert_secrets_absent(response)

    def _enable_pat(self, user):
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])

    def test_support_pat_with_support_scope_gets_blanked_secrets(self):
        self._enable_pat(self.support)
        pat = _create_pat(self.support, [PermissionEnum.SUPPORT_ACCESS.value])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.get(self.url)
        self._assert_secrets_blanked(response)

    def test_support_pat_without_support_scope_is_forbidden(self):
        self._enable_pat(self.support)
        pat = _create_pat(self.support, [PermissionEnum.LIST_ORDERS.value])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self._assert_secrets_absent(response)

    def test_staff_pat_with_staff_scope_returns_secrets(self):
        self._enable_pat(self.staff)
        pat = _create_pat(self.staff, [PermissionEnum.STAFF_ACCESS.value])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["FREEIPA_PASSWORD"], SECRET_SENTINELS["FREEIPA_PASSWORD"]
        )

    def test_staff_pat_with_only_support_scope_gets_blanked_secrets(self):
        self._enable_pat(self.staff)
        pat = _create_pat(self.staff, [PermissionEnum.SUPPORT_ACCESS.value])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.get(self.url)
        self._assert_secrets_blanked(response)

    def test_staff_pat_without_staff_or_support_scope_is_forbidden(self):
        self._enable_pat(self.staff)
        pat = _create_pat(self.staff, [PermissionEnum.LIST_ORDERS.value])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self._assert_secrets_absent(response)

    def test_support_pat_cannot_update_settings(self):
        self._enable_pat(self.support)
        pat = _create_pat(self.support, [PermissionEnum.SUPPORT_ACCESS.value])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.post(
            self.url,
            {"OIDC_CLIENT_SECRET": "pat-wrote-secret-sentinel"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotIn(b"pat-wrote-secret-sentinel", response.content)
        self._assert_secrets_absent(response)
