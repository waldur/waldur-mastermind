"""
Tests for CallApplicantAttributeConfig (Option C: GDPR Enhancement).

Tests the ability to configure which applicant attributes are exposed
to call managers and reviewers for each call.
"""

from rest_framework import status, test

from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import factories, fixtures


class CallApplicantAttributeConfigModelTest(test.APITestCase):
    """Test CallApplicantAttributeConfig model."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call

    def test_get_exposed_fields_returns_enabled_fields(self):
        """get_exposed_fields returns list of enabled fields."""
        config = models.CallApplicantAttributeConfig.objects.create(
            call=self.call,
            expose_full_name=True,
            expose_email=True,
            expose_organization=False,
            expose_nationality=True,
        )
        exposed = config.get_exposed_fields()
        self.assertIn("full_name", exposed)
        self.assertIn("email", exposed)
        self.assertIn("nationality", exposed)
        self.assertNotIn("organization", exposed)

    def test_get_exposed_fields_for_call_with_config(self):
        """get_exposed_fields_for_call returns config fields when config exists."""
        models.CallApplicantAttributeConfig.objects.create(
            call=self.call,
            expose_full_name=True,
            expose_email=False,
            expose_organization=True,
        )
        exposed = models.CallApplicantAttributeConfig.get_exposed_fields_for_call(
            self.call
        )
        self.assertIn("full_name", exposed)
        self.assertIn("organization", exposed)
        self.assertNotIn("email", exposed)

    def test_get_exposed_fields_for_call_without_config(self):
        """get_exposed_fields_for_call returns defaults when no config exists."""
        exposed = models.CallApplicantAttributeConfig.get_exposed_fields_for_call(
            self.call
        )
        self.assertEqual(exposed, ["full_name", "email", "organization"])


class CallApplicantAttributeConfigAPITest(test.APITestCase):
    """Test CallApplicantAttributeConfig API endpoints."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call

    def test_get_default_config_when_none_exists(self):
        """Returns defaults when no explicit config exists."""
        self.client.force_authenticate(self.fixture.call_manager)
        url = (
            factories.CallFactory.get_protected_url(self.call)
            + "applicant_attribute_config/"
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data.get("is_default"))
        self.assertIn("full_name", response.data["exposed_fields"])

    def test_get_existing_config(self):
        """Returns existing config when it exists."""
        models.CallApplicantAttributeConfig.objects.create(
            call=self.call,
            expose_full_name=True,
            expose_email=False,
            expose_organization=True,
            expose_nationality=True,
        )
        self.client.force_authenticate(self.fixture.call_manager)
        url = (
            factories.CallFactory.get_protected_url(self.call)
            + "applicant_attribute_config/"
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("full_name", response.data["exposed_fields"])
        self.assertIn("nationality", response.data["exposed_fields"])
        self.assertNotIn("email", response.data["exposed_fields"])

    def test_create_config(self):
        """Can create applicant attribute config."""
        self.client.force_authenticate(self.fixture.call_manager)
        url = (
            factories.CallFactory.get_protected_url(self.call)
            + "update_applicant_attribute_config/"
        )
        response = self.client.post(
            url,
            {
                "expose_nationality": True,
                "expose_organization_type": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("nationality", response.data["exposed_fields"])
        self.assertIn("organization_type", response.data["exposed_fields"])

    def test_update_config(self):
        """Can update existing applicant attribute config."""
        models.CallApplicantAttributeConfig.objects.create(
            call=self.call,
            expose_full_name=True,
            expose_email=True,
        )
        self.client.force_authenticate(self.fixture.call_manager)
        url = (
            factories.CallFactory.get_protected_url(self.call)
            + "update_applicant_attribute_config/"
        )
        response = self.client.patch(
            url,
            {
                "expose_email": False,
                "expose_nationality": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify email is now disabled and nationality is enabled
        config = models.CallApplicantAttributeConfig.objects.get(call=self.call)
        self.assertFalse(config.expose_email)
        self.assertTrue(config.expose_nationality)

    def test_delete_config_reverts_to_defaults(self):
        """Deleting config reverts to system defaults."""
        models.CallApplicantAttributeConfig.objects.create(
            call=self.call,
            expose_nationality=True,
        )
        self.client.force_authenticate(self.fixture.call_manager)
        url = (
            factories.CallFactory.get_protected_url(self.call)
            + "delete_applicant_attribute_config/"
        )
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        # Verify config is gone
        self.assertFalse(
            models.CallApplicantAttributeConfig.objects.filter(call=self.call).exists()
        )

    def test_delete_nonexistent_config_returns_success(self):
        """Deleting non-existent config still returns success."""
        self.client.force_authenticate(self.fixture.call_manager)
        url = (
            factories.CallFactory.get_protected_url(self.call)
            + "delete_applicant_attribute_config/"
        )
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_unauthorized_user_cannot_access_config(self):
        """Unauthorized user cannot access config endpoints."""
        url = (
            factories.CallFactory.get_protected_url(self.call)
            + "applicant_attribute_config/"
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_regular_user_cannot_update_config(self):
        """Regular user cannot update applicant attribute config."""
        self.client.force_authenticate(self.fixture.user)
        url = (
            factories.CallFactory.get_protected_url(self.call)
            + "update_applicant_attribute_config/"
        )
        response = self.client.post(
            url,
            {"expose_nationality": True},
            format="json",
        )
        # Should be forbidden - user doesn't have UPDATE_CALL permission
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
        )


class CallApplicantAttributeConfigReviewerVisibilityTest(test.APITestCase):
    """Test reviewer visibility settings."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call

    def test_reviewers_see_applicant_details_default_false(self):
        """By default, reviewers don't see applicant details."""
        config = models.CallApplicantAttributeConfig.objects.create(
            call=self.call,
        )
        self.assertFalse(config.reviewers_see_applicant_details)

    def test_reviewers_see_applicant_details_can_be_enabled(self):
        """Reviewer visibility can be enabled."""
        config = models.CallApplicantAttributeConfig.objects.create(
            call=self.call,
            reviewers_see_applicant_details=True,
        )
        self.assertTrue(config.reviewers_see_applicant_details)

    def test_update_reviewer_visibility_via_api(self):
        """Can update reviewer visibility via API."""
        models.CallApplicantAttributeConfig.objects.create(
            call=self.call,
            reviewers_see_applicant_details=False,
        )
        self.client.force_authenticate(self.fixture.call_manager)
        url = (
            factories.CallFactory.get_protected_url(self.call)
            + "update_applicant_attribute_config/"
        )
        response = self.client.patch(
            url,
            {"reviewers_see_applicant_details": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        config = models.CallApplicantAttributeConfig.objects.get(call=self.call)
        self.assertTrue(config.reviewers_see_applicant_details)
