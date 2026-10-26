import datetime
from unittest.mock import Mock, patch

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.configuration.testing import override_config
from waldur_core.onboarding import enums, models
from waldur_core.onboarding.backends import (
    AustriaRegisterBackend,
    EstonianAriregisterBackend,
)
from waldur_core.onboarding.backends.base import ErrorCode
from waldur_core.onboarding.validators import is_identity_verified_by_account
from waldur_core.permissions.enums import RoleEnum
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories

from . import factories
from .fixtures import (
    AUTHORIZED_CIVIL_NUMBER,
    get_estonian_ariregister_success_response,
)


class IdentityVerifiedByAccountTest(TestCase):
    """The register check vouches for the signed-in user only if the identity
    comes from the account and the user cannot edit it."""

    def test_civil_number_from_the_account(self):
        user = structure_factories.UserFactory(civil_number="38001085718")
        self.assertTrue(
            is_identity_verified_by_account(user, EstonianAriregisterBackend, {})
        )

    def test_typed_civil_number_of_somebody_else(self):
        user = structure_factories.UserFactory(civil_number="38001085718")
        self.assertFalse(
            is_identity_verified_by_account(
                user, EstonianAriregisterBackend, {"civil_number": "47101010033"}
            )
        )

    def test_typed_civil_number_equal_to_the_account(self):
        user = structure_factories.UserFactory(civil_number="38001085718")
        self.assertTrue(
            is_identity_verified_by_account(
                user, EstonianAriregisterBackend, {"civil_number": "38001085718"}
            )
        )

    def test_account_without_civil_number(self):
        user = structure_factories.UserFactory(civil_number=None)
        self.assertFalse(
            is_identity_verified_by_account(
                user, EstonianAriregisterBackend, {"civil_number": "38001085718"}
            )
        )

    def austrian_user(self):
        return structure_factories.UserFactory(
            first_name="Anna",
            last_name="Muster",
            birth_date=datetime.date(1980, 1, 2),
        )

    def test_names_the_user_can_edit(self):
        self.assertFalse(
            is_identity_verified_by_account(
                self.austrian_user(), AustriaRegisterBackend, {}
            )
        )

    @patch(
        "waldur_core.onboarding.validators.get_identity_provider_fields",
        return_value=["first_name", "last_name", "birth_date"],
    )
    def test_names_protected_by_the_identity_provider(self, _):
        self.assertTrue(
            is_identity_verified_by_account(
                self.austrian_user(),
                AustriaRegisterBackend,
                {"birth_date": "1980-01-02"},
            )
        )

    @patch(
        "waldur_core.onboarding.validators.get_identity_provider_fields",
        return_value=["first_name", "last_name", "birth_date"],
    )
    def test_typed_birth_date_that_differs_from_the_account(self, _):
        self.assertFalse(
            is_identity_verified_by_account(
                self.austrian_user(),
                AustriaRegisterBackend,
                {"birth_date": "1975-05-05"},
            )
        )


@override_config(
    ONBOARDING_ARIREGISTER_BASE_URL="https://demo-ariregxmlv6.rik.ee/",
    ONBOARDING_ARIREGISTER_USERNAME="username",
    ONBOARDING_ARIREGISTER_PASSWORD="password",
    ONBOARDING_ARIREGISTER_TIMEOUT=30,
)
@patch("waldur_core.onboarding.backends.estonia.requests.post")
class TypedIdentityManualReviewTest(APITestCase):
    """A typed identity sends a register-confirmed verification to manual review."""

    def setUp(self):
        self.legal_person_identifier = "70000310"
        self.board_member = structure_factories.UserFactory(
            civil_number=AUTHORIZED_CIVIL_NUMBER
        )
        # Signed in through TARA as somebody other than the board member.
        self.user = structure_factories.UserFactory(civil_number="39001010000")
        self.staff = structure_factories.UserFactory(is_staff=True)
        customer_checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.ONBOARDING_CUSTOMER_DATA,
        )
        checklist_factories.QuestionFactory(
            checklist=customer_checklist,
            question_type=checklist_enums.QuestionTypes.TEXT_INPUT,
            required=True,
        )

    def mock_register(self, mock_post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = get_estonian_ariregister_success_response(
            self.legal_person_identifier
        )
        mock_post.return_value = response

    def verification_url(self, uuid, action):
        url = reverse("onboarding-verification-detail", kwargs={"uuid": uuid})
        return f"http://testserver{url}{action}/"

    def run_validation_as(self, user, body):
        self.client.force_authenticate(user=user)
        response = self.client.post(
            factories.OnboardingVerificationFactory.get_list_url(
                action="start_verification"
            ),
            {
                "validation_method": enums.ValidationMethod.ARIREGISTER,
                "legal_person_identifier": self.legal_person_identifier,
                "legal_name": "Test Company OÜ",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        uuid = response.data["uuid"]
        response = self.client.post(
            self.verification_url(uuid, "run_validation"), body, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        return uuid, response.data

    def test_typed_civil_number_is_reviewed_manually(self, mock_post):
        self.mock_register(mock_post)

        uuid, verification = self.run_validation_as(
            self.user, {"civil_number": AUTHORIZED_CIVIL_NUMBER}
        )

        self.assertEqual(verification["status"], enums.VerificationStatus.ESCALATED)
        self.assertEqual(
            verification["error_message"], ErrorCode.PERSON_IDENTITY_NOT_VERIFIED
        )
        self.assertTrue(verification["verified_company_data"])
        response = self.client.post(
            self.verification_url(uuid, "create_customer"), {}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            Customer.objects.filter(
                registration_code=self.legal_person_identifier
            ).exists()
        )

    def test_approved_review_creates_the_organisation_from_register_data(
        self, mock_post
    ):
        self.mock_register(mock_post)
        uuid, _ = self.run_validation_as(
            self.user, {"civil_number": AUTHORIZED_CIVIL_NUMBER}
        )
        response = self.client.post(
            "/api/onboarding-justifications/create_justification/",
            {"verification_uuid": uuid},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        justification_uuid = response.data["uuid"]

        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            f"/api/onboarding-justifications/{justification_uuid}/approve/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            self.verification_url(uuid, "create_customer"), {}, format="json"
        )

        # The required customer question was never asked: the company data
        # comes from the register, only the person's identity was reviewed.
        self.assertEqual(response.status_code, 201, response.data)
        customer = Customer.objects.get(registration_code=self.legal_person_identifier)
        self.assertTrue(customer.has_user(self.user, RoleEnum.CUSTOMER_OWNER))

    def test_board_member_signed_in_with_their_own_identity_is_verified(
        self, mock_post
    ):
        self.mock_register(mock_post)

        _, verification = self.run_validation_as(self.board_member, {})

        self.assertEqual(verification["status"], enums.VerificationStatus.VERIFIED)

    def test_manual_verifications_still_need_the_customer_checklist(self, mock_post):
        verification = models.OnboardingVerification.objects.create(
            user=self.user,
            legal_person_identifier="12345678",
            status=enums.VerificationStatus.VERIFIED,
        )
        models.OnboardingJustification.objects.create(
            verification=verification, user=self.user
        )
        can_create, _ = verification.can_customer_be_created()
        self.assertFalse(can_create)
