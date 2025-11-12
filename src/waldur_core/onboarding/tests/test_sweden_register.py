from unittest import mock
from unittest.mock import Mock

from constance.test import override_config
from django.test import TestCase

from waldur_core.onboarding.backends.base import ErrorCode, ValidationRequest
from waldur_core.onboarding.backends.sweden import SwedenRegisterBackend

from .fixtures import (
    BOLAGSVERKET_AUTHORIZED_PERSON_IDENTIFIER,
    BOLAGSVERKET_COMPANY_NAME,
    BOLAGSVERKET_LEGAL_PERSON_IDENTIFIER,
    BOLAGSVERKET_UNKNOWN_PERSON_IDENTIFIER,
    bolagsverket_company_response,
)


@override_config(
    ONBOARDING_BOLAGSVERKET_API_URL="https://gw-accept2.api.bolagsverket.se/",
    ONBOARDING_BOLAGSVERKET_TOKEN_API_URL="https://portal-accept2.api.bolagsverket.se/",
    ONBOARDING_BOLAGSVERKET_CLIENT_ID="client-id",
    ONBOARDING_BOLAGSVERKET_CLIENT_SECRET="client-secret",
)
class SwedenRegisterBackendTest(TestCase):
    def setUp(self):
        self.backend = SwedenRegisterBackend()
        patcher = mock.patch(
            "waldur_core.onboarding.backends.sweden.SwedenRegisterBackend.post"
        )
        self.mock_post = patcher.start()
        self.addCleanup(patcher.stop)

        response_payload = bolagsverket_company_response()

        def _post_side_effect(*args, **kwargs):
            return Mock(json=Mock(return_value=response_payload))

        self.mock_post.side_effect = _post_side_effect

    def _build_request(self, person_identifier: str) -> ValidationRequest:
        return ValidationRequest(
            country="SE",
            person_identifier=person_identifier,
            legal_person_identifier=BOLAGSVERKET_LEGAL_PERSON_IDENTIFIER,
            legal_name=BOLAGSVERKET_COMPANY_NAME,
        )

    def test_authorized_user_is_verified(self):
        request = self._build_request(BOLAGSVERKET_AUTHORIZED_PERSON_IDENTIFIER)

        result = self.backend.validate_company(request)

        self.assertTrue(result.is_valid)
        self.assertIn("Styrelseledamot", result.user_roles)
        self.assertEqual(result.company_data["name"], BOLAGSVERKET_COMPANY_NAME)
        self.mock_post.assert_called_with(
            "organisationer",
            data={
                "identitetsbeteckning": BOLAGSVERKET_LEGAL_PERSON_IDENTIFIER,
                "organisationInformationsmangd": ["FUNKTIONARER"],
            },
        )

    def test_unknown_user_is_not_authorized(self):
        request = self._build_request(BOLAGSVERKET_UNKNOWN_PERSON_IDENTIFIER)

        result = self.backend.validate_company(request)

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.NOT_AUTHORIZED)
        self.assertEqual(result.user_roles, [])
