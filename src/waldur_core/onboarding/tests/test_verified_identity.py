import datetime

from rest_framework import status, test

from waldur_core.onboarding import enums, tasks
from waldur_core.onboarding.models import OnboardingVerification
from waldur_core.structure.tests import factories as structure_factories

from . import factories


class VerifiedCompanyLockTest(test.APITestCase):
    """A verification vouches for one company once verified or under review."""

    def make_verification(self, status_value):
        verification = factories.OnboardingVerificationFactory(
            legal_person_identifier="COMPANY-A", legal_name="Company A", country="EE"
        )
        verification.status = status_value
        verification.save()
        return verification

    def change(self, verification, user=None, **changes):
        self.client.force_authenticate(user or verification.user)
        return self.client.patch(
            factories.OnboardingVerificationFactory.get_url(verification),
            changes
            or {"legal_person_identifier": "COMPANY-B", "legal_name": "Company B"},
        )

    def assert_company_kept(self, verification):
        verification.refresh_from_db()
        self.assertEqual(verification.legal_person_identifier, "COMPANY-A")

    def test_company_of_verified_verification_cannot_be_changed(self):
        verification = self.make_verification(enums.VerificationStatus.VERIFIED)
        response = self.change(verification)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assert_company_kept(verification)

    def test_country_of_verified_verification_cannot_be_changed(self):
        verification = self.make_verification(enums.VerificationStatus.VERIFIED)
        response = self.change(verification, country="AT")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unchanged_company_is_accepted(self):
        # The portal re-sends the values it shows.
        verification = self.make_verification(enums.VerificationStatus.VERIFIED)
        response = self.change(
            verification, legal_person_identifier="COMPANY-A", legal_name="Company A"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_company_under_review_cannot_be_changed_in_any_status(self):
        for status_value in enums.VerificationStatus.VALUES:
            if status_value == enums.VerificationStatus.VERIFIED:
                continue
            with self.subTest(status=status_value):
                verification = self.make_verification(status_value)
                factories.OnboardingJustificationFactory(verification=verification)
                response = self.change(verification)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assert_company_kept(verification)

    def test_company_stays_locked_when_an_escalated_verification_expires(self):
        verification = self.make_verification(enums.VerificationStatus.ESCALATED)
        factories.OnboardingJustificationFactory(verification=verification)
        OnboardingVerification.objects.filter(pk=verification.pk).update(
            expires_at=datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)
        )
        tasks.expire_stale_verifications()
        verification.refresh_from_db()
        self.assertEqual(verification.status, enums.VerificationStatus.EXPIRED)

        response = self.change(verification)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assert_company_kept(verification)

    def test_company_can_be_corrected_before_review(self):
        # Including an escalated verification without a justification yet: the
        # portal corrects the company before it files one.
        for status_value in (
            enums.VerificationStatus.PENDING,
            enums.VerificationStatus.FAILED,
            enums.VerificationStatus.EXPIRED,
            enums.VerificationStatus.ESCALATED,
        ):
            with self.subTest(status=status_value):
                verification = self.make_verification(status_value)
                response = self.change(verification)
                self.assertEqual(
                    response.status_code, status.HTTP_200_OK, response.data
                )

    def test_company_can_be_corrected_after_a_rejected_justification(self):
        verification = self.make_verification(enums.VerificationStatus.FAILED)
        factories.OnboardingJustificationFactory(
            verification=verification,
            validation_decision=enums.ReviewDecision.REJECTED,
        )
        response = self.change(verification)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_staff_can_correct_a_verified_company(self):
        verification = self.make_verification(enums.VerificationStatus.VERIFIED)
        staff = structure_factories.UserFactory(is_staff=True)
        response = self.change(verification, user=staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_user_cannot_change_expiry(self):
        verification = self.make_verification(enums.VerificationStatus.PENDING)
        response = self.change(verification, expires_at="2099-01-01T00:00:00Z")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
