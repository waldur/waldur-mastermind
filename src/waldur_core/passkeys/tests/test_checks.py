"""A misconfigured deployment must fail at startup, not at the login page.

Every case here is one where WebAuthn would otherwise present a button that
cannot work, with nothing in the logs to say why.
"""

from django.test import TestCase

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.passkeys.checks import (
    passkey_credentials_are_not_orphaned,
    passkey_settings_are_valid,
)
from waldur_core.passkeys.tests.factories import PasskeyCredentialFactory
from waldur_core.passkeys.tests.helpers import ORIGIN, RP_ID, enable_passkeys


def ids(errors):
    return sorted(e.id for e in errors)


class PasskeySettingsCheckTest(TestCase):
    @override_waldur_core_settings(AUTHENTICATION_METHODS=["LOCAL_SIGNIN"])
    def test_disabled_deployment_is_not_checked(self):
        """The default deployment never opted in, so nothing is validated."""
        self.assertEqual(passkey_settings_are_valid(None), [])

    @override_waldur_core_settings(
        AUTHENTICATION_METHODS=["LOCAL_SIGNIN", "PASSKEY_MFA"],
        PASSKEY_RP_ID="",
        PASSKEY_ALLOWED_ORIGINS=[],
    )
    def test_missing_rp_id_and_origins_are_errors(self):
        self.assertEqual(
            ids(passkey_settings_are_valid(None)),
            ["waldur.passkeys.E001", "waldur.passkeys.E003"],
        )

    @override_waldur_core_settings(
        AUTHENTICATION_METHODS=["PASSKEY_SIGNIN"],
        PASSKEY_RP_ID="https://waldur.example.com",
        PASSKEY_ALLOWED_ORIGINS=[ORIGIN],
    )
    def test_rp_id_must_be_a_bare_hostname(self):
        self.assertIn("waldur.passkeys.E002", ids(passkey_settings_are_valid(None)))

    @override_waldur_core_settings(
        AUTHENTICATION_METHODS=["PASSKEY_SIGNIN"],
        PASSKEY_RP_ID=RP_ID,
        PASSKEY_ALLOWED_ORIGINS=["waldur.example.com"],
    )
    def test_origin_without_a_scheme_is_an_error(self):
        self.assertIn("waldur.passkeys.E004", ids(passkey_settings_are_valid(None)))

    @override_waldur_core_settings(
        AUTHENTICATION_METHODS=["PASSKEY_SIGNIN"],
        PASSKEY_RP_ID=RP_ID,
        PASSKEY_ALLOWED_ORIGINS=["http://waldur.example.com"],
    )
    def test_plain_http_origin_is_an_error(self):
        self.assertIn("waldur.passkeys.E005", ids(passkey_settings_are_valid(None)))

    @override_waldur_core_settings(
        AUTHENTICATION_METHODS=["PASSKEY_SIGNIN"],
        PASSKEY_RP_ID="localhost",
        PASSKEY_ALLOWED_ORIGINS=["http://localhost:8000"],
    )
    def test_plain_http_localhost_is_allowed(self):
        """Browsers treat localhost as a secure context, so dev stacks work."""
        self.assertEqual(passkey_settings_are_valid(None), [])

    @override_waldur_core_settings(
        AUTHENTICATION_METHODS=["PASSKEY_SIGNIN"],
        PASSKEY_RP_ID=RP_ID,
        PASSKEY_ALLOWED_ORIGINS=["https://portal.example.net"],
    )
    def test_origin_not_subordinate_to_the_rp_id_is_an_error(self):
        self.assertIn("waldur.passkeys.E006", ids(passkey_settings_are_valid(None)))

    @override_waldur_core_settings(
        AUTHENTICATION_METHODS=["PASSKEY_SIGNIN"],
        PASSKEY_RP_ID="example.com",
        PASSKEY_ALLOWED_ORIGINS=["https://waldur.example.com"],
    )
    def test_subdomain_origin_under_a_parent_rp_id_is_allowed(self):
        self.assertEqual(passkey_settings_are_valid(None), [])

    @override_waldur_core_settings(
        AUTHENTICATION_METHODS=["PASSKEY_SIGNIN"],
        PASSKEY_RP_ID="example.com",
        PASSKEY_ALLOWED_ORIGINS=["https://notexample.com"],
    )
    def test_suffix_match_is_not_enough(self):
        """'notexample.com' ends with 'example.com' but is a different domain."""
        self.assertIn("waldur.passkeys.E006", ids(passkey_settings_are_valid(None)))

    @enable_passkeys()
    def test_valid_configuration_passes(self):
        self.assertEqual(passkey_settings_are_valid(None), [])


class OrphanedCredentialCheckTest(TestCase):
    @enable_passkeys()
    def test_matching_credentials_do_not_warn(self):
        PasskeyCredentialFactory(rp_id=RP_ID)
        self.assertEqual(passkey_credentials_are_not_orphaned(None), [])

    @enable_passkeys()
    def test_credentials_from_a_previous_rp_id_warn(self):
        PasskeyCredentialFactory(rp_id="old.example.com")
        warnings = passkey_credentials_are_not_orphaned(None)
        self.assertEqual(ids(warnings), ["waldur.passkeys.W001"])
        self.assertIn("1 active passkey", warnings[0].msg)

    @enable_passkeys()
    def test_revoked_credentials_are_not_counted(self):
        credential = PasskeyCredentialFactory(rp_id="old.example.com")
        credential.revoke()
        self.assertEqual(passkey_credentials_are_not_orphaned(None), [])

    @override_waldur_core_settings(AUTHENTICATION_METHODS=["LOCAL_SIGNIN"])
    def test_disabled_deployment_does_not_warn(self):
        PasskeyCredentialFactory(rp_id="old.example.com")
        self.assertEqual(passkey_credentials_are_not_orphaned(None), [])
