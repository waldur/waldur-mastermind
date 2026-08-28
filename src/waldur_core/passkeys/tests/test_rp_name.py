"""The RP name must never reach the library empty.

``generate_registration_options`` rejects an empty ``rp_name`` outright, so an
unset setting does not degrade — it makes enrolment impossible. The helm chart
renders the key only when a value is given, so "unset" is the default path,
not an edge case.
"""

from constance.test import override_config
from django.test import TestCase

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.passkeys import policy, services
from waldur_core.passkeys.tests.authenticator import SoftwareAuthenticator
from waldur_core.passkeys.tests.helpers import ORIGIN, RP_ID
from waldur_core.structure.tests import factories as structure_factories


def enabled_without_rp_name():
    return override_waldur_core_settings(
        AUTHENTICATION_METHODS=["LOCAL_SIGNIN", "PASSKEY_SIGNIN"],
        PASSKEY_RP_ID=RP_ID,
        PASSKEY_RP_NAME="",
        PASSKEY_ALLOWED_ORIGINS=[ORIGIN],
    )


class RpNameFallbackTest(TestCase):
    @enabled_without_rp_name()
    @override_config(SITE_NAME="Example Cloud")
    def test_unset_rp_name_falls_back_to_the_site_name(self):
        self.assertEqual(policy.get_rp_name(), "Example Cloud")

    @override_waldur_core_settings(PASSKEY_RP_NAME="Explicit Name")
    def test_an_explicit_value_wins(self):
        self.assertEqual(policy.get_rp_name(), "Explicit Name")

    @enabled_without_rp_name()
    @override_config(SITE_NAME="")
    def test_an_empty_site_name_still_yields_something(self):
        """Both unset is possible; the library would reject an empty string."""
        self.assertTrue(policy.get_rp_name())

    @enabled_without_rp_name()
    @override_config(SITE_NAME="Example Cloud")
    def test_enrolment_works_with_no_rp_name_configured(self):
        """The regression: this raised ValueError and blocked enrolment."""
        user = structure_factories.UserFactory()
        authenticator = SoftwareAuthenticator()

        ceremony, options = services.start_registration(user)
        credential = services.finish_registration(
            ceremony,
            authenticator.register(options["challenge"], RP_ID, ORIGIN),
            "Laptop",
        )

        self.assertEqual(options["rp"]["name"], "Example Cloud")
        self.assertTrue(credential.pk)
