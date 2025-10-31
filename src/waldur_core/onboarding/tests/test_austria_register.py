from unittest import mock
from unittest.mock import Mock

from constance.test import override_config
from django.test import TestCase

from waldur_core.onboarding.backends.austria import AustriaRegisterBackend
from waldur_core.onboarding.backends.base import ValidationRequest

from .fixtures import (
    AUTHORIZED_PERSON_IDENTIFIER,
    LEGAL_NAME,
    LEGAL_PERSON_IDENTIFIER,
    wico_fetch_company_profile,
    wico_resolve_register_number,
)


@override_config(
    ONBOARDING_WICO_API_URL="https://api.wirtschaftscompass.at/organisation/",
    ONBOARDING_WICO_TOKEN="test-token",
)
class AustriaRegisterBackendTest(TestCase):
    """Direct backend unit tests - comprehensive testing coverage."""

    def setUp(self):
        self.backend = AustriaRegisterBackend()
        mock_patch = mock.patch(
            "waldur_core.onboarding.backends.austria.AustriaRegisterBackend.get"
        )
        mock_get = mock_patch.start()
        self._get_patcher = mock_patch

        def _get_side_effect(path, params=None):
            class _Resp:
                def __init__(self, payload):
                    self._payload = payload

                def json(self):
                    return self._payload

            if str(path).startswith("v1/resolve"):
                return _Resp(wico_fetch_company_profile())
            if str(path).startswith("v1/") and str(path).endswith("/company-report"):
                return _Resp(wico_resolve_register_number())
            return Mock(json=lambda: {})

        mock_get.side_effect = _get_side_effect

    def tearDown(self):
        self._get_patcher.stop()

    def test_authorized_user_is_verified(self):
        request = ValidationRequest(
            country="AT",
            person_identifier={
                "first_name": AUTHORIZED_PERSON_IDENTIFIER["first_name"],
                "last_name": AUTHORIZED_PERSON_IDENTIFIER["last_name"],
                "birth_date": AUTHORIZED_PERSON_IDENTIFIER["birth_date"],
            },
            legal_person_identifier=LEGAL_PERSON_IDENTIFIER,
            legal_name=LEGAL_NAME,
        )

        response = self.backend.validate_company(request)

        self.assertTrue(response.is_valid)

    def test_first_name_mismatch_results_in_not_authorized(self):
        request = ValidationRequest(
            country="AT",
            person_identifier={
                "first_name": "Bill",
                "last_name": AUTHORIZED_PERSON_IDENTIFIER["last_name"],
                "birth_date": AUTHORIZED_PERSON_IDENTIFIER["birth_date"],
            },
            legal_person_identifier=LEGAL_PERSON_IDENTIFIER,
            legal_name=LEGAL_NAME,
        )

        response = self.backend.validate_company(request)

        self.assertFalse(response.is_valid)
