from django.test import SimpleTestCase

from waldur_core.onboarding import enums
from waldur_core.server.constance_settings import ONBOARDING_VALIDATION_CHOICES


class OnboardingValidationChoicesTest(SimpleTestCase):
    def test_every_validation_method_can_be_enabled(self):
        # ONBOARDING_VALIDATION_METHODS only accepts these choices, so a
        # backend missing here cannot be switched on from the admin UI.
        configurable = {value for value, _ in ONBOARDING_VALIDATION_CHOICES}
        implemented = {value for value, _ in enums.ValidationMethod.CHOICES}

        self.assertEqual(configurable, implemented)
