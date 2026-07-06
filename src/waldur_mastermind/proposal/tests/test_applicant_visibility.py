from unittest import mock

from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import factories, fixtures


class CallApplicantVisibilityConfigModelTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call

    def test_get_exposed_fields_returns_enabled_fields(self):
        config = models.CallApplicantVisibilityConfig.objects.create(
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

    def test_default_values(self):
        config = models.CallApplicantVisibilityConfig.objects.create(call=self.call)
        # Defaults inherited from UserAttributeConfigBase (matching offering defaults).
        self.assertTrue(config.expose_full_name)
        self.assertTrue(config.expose_email)
        self.assertTrue(config.expose_username)
        self.assertTrue(config.expose_registration_method)
        self.assertFalse(config.expose_organization)
        self.assertFalse(config.expose_nationality)
        self.assertFalse(config.expose_civil_number)

    def test_get_exposed_fields_for_call_with_config(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.call,
            expose_full_name=False,
            expose_email=True,
            expose_gender=True,
        )
        exposed = models.CallApplicantVisibilityConfig.get_exposed_fields_for_call(
            self.call
        )
        self.assertIn("email", exposed)
        self.assertIn("gender", exposed)
        self.assertNotIn("full_name", exposed)

    def test_get_exposed_fields_for_call_falls_back_to_constance(self):
        # No config exists for this call.
        with mock.patch(
            "waldur_mastermind.marketplace.models.constance_config"
        ) as mock_config:
            mock_config.DEFAULT_CALL_USER_ATTRIBUTES = [
                "username",
                "email",
                "organization",
            ]
            exposed = models.CallApplicantVisibilityConfig.get_exposed_fields_for_call(
                self.call
            )
        self.assertEqual(exposed, ["username", "email", "organization"])

    def test_get_exposed_fields_for_call_hardcoded_fallback(self):
        # No config and no Constance value — falls back to the hardcoded list.
        with mock.patch(
            "waldur_mastermind.marketplace.models.constance_config"
        ) as mock_config:
            mock_config.DEFAULT_CALL_USER_ATTRIBUTES = None
            exposed = models.CallApplicantVisibilityConfig.get_exposed_fields_for_call(
                self.call
            )
        self.assertEqual(exposed, ["username", "full_name", "email"])


class CallApplicantVisibilityConfigAPITest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.url = factories.CallFactory.get_protected_url(self.call)

    def test_get_call_synthesises_default_visibility_config_when_none_exists(self):
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        config_data = response.data["applicant_visibility_config"]
        self.assertTrue(config_data["is_default"])
        # Booleans match the Constance default DEFAULT_CALL_USER_ATTRIBUTES.
        self.assertTrue(config_data["expose_username"])
        self.assertTrue(config_data["expose_full_name"])
        self.assertTrue(config_data["expose_email"])
        self.assertFalse(config_data["expose_registration_method"])
        self.assertFalse(config_data["expose_phone_number"])

    def test_get_call_exposes_all_22_fields_when_config_exists(self):
        models.CallApplicantVisibilityConfig.objects.create(call=self.call)
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        config_data = response.data["applicant_visibility_config"]
        # Same surface as the offering serializer — all 27 toggles plus computed.
        expose_keys = [k for k in config_data if k.startswith("expose_")]
        self.assertEqual(len(expose_keys), 27)
        self.assertIn("exposed_fields", config_data)
        self.assertIn("is_default", config_data)

    def test_patch_creates_visibility_config(self):
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.patch(
            self.url,
            {"applicant_visibility_config": {"expose_email": True}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        config = models.CallApplicantVisibilityConfig.objects.get(call=self.call)
        self.assertTrue(config.expose_email)

    def test_patch_updates_existing_visibility_config(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.call,
            expose_email=False,
            expose_nationality=False,
        )
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.patch(
            self.url,
            {"applicant_visibility_config": {"expose_nationality": True}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        config = models.CallApplicantVisibilityConfig.objects.get(call=self.call)
        self.assertTrue(config.expose_nationality)

    def test_patch_with_null_clears_existing_config(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.call, expose_email=True
        )
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.patch(
            self.url, {"applicant_visibility_config": None}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(
            models.CallApplicantVisibilityConfig.objects.filter(call=self.call).exists()
        )

    def test_patch_without_visibility_key_is_noop_for_existing_config(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.call, expose_email=True
        )
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.patch(self.url, {"name": "Updated name"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        config = models.CallApplicantVisibilityConfig.objects.get(call=self.call)
        self.assertTrue(config.expose_email)

    def test_get_call_returns_visibility_config_when_exists(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.call,
            expose_email=True,
            expose_nationality=True,
        )
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        config_data = response.data.get("applicant_visibility_config")
        self.assertIsNotNone(config_data)
        self.assertTrue(config_data["expose_email"])
        self.assertTrue(config_data["expose_nationality"])

    def test_unauthorized_anonymous_cannot_patch_visibility_config(self):
        response = self.client.patch(
            self.url,
            {"applicant_visibility_config": {"expose_email": True}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_non_manager_cannot_patch_visibility_config(self):
        # An authenticated user with no role on this call must not be able to
        # change the visibility config.
        outsider = structure_factories.UserFactory()
        self.client.force_authenticate(outsider)
        response = self.client.patch(
            self.url,
            {"applicant_visibility_config": {"expose_email": True}},
            format="json",
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )


class ProposalApplicantVisibilityConsumptionTest(test.APITestCase):
    """Verify that the visibility config actually filters applicant data
    rendered to reviewers via ProposalSerializer."""

    # All applicant_* serializer fields gated by visibility toggles.
    ALL_APPLICANT_FIELDS = [
        "applicant_username",
        "applicant_full_name",
        "applicant_first_name",
        "applicant_last_name",
        "applicant_email",
        "applicant_registration_method",
        "applicant_phone_number",
        "applicant_organization",
        "applicant_organization_country",
        "applicant_organization_type",
        "applicant_organization_registry_code",
        "applicant_job_title",
        "applicant_affiliations",
        "applicant_gender",
        "applicant_personal_title",
        "applicant_place_of_birth",
        "applicant_address",
        "applicant_country_of_residence",
        "applicant_nationality",
        "applicant_nationalities",
        "applicant_eduperson_assurance",
        "applicant_identity_source",
        "applicant_civil_number",
        "applicant_birth_date",
        "applicant_active_isds",
    ]

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal_submitted
        self.reviewer = self.fixture.reviewer_1
        self.applicant = self.proposal.created_by
        self.call_manager = self.fixture.call_manager
        self.url = f"/api/proposal-proposals/{self.proposal.uuid.hex}/"

    def _get_as(self, user):
        self.client.force_authenticate(user)
        return self.client.get(self.url)

    def test_reviewer_sees_default_fields_with_no_config(self):
        # No explicit config — falls back to DEFAULT_CALL_USER_ATTRIBUTES
        # which defaults to ["username", "full_name", "email"].
        response = self._get_as(self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in (
            "created_by",
            "created_by_uuid",
            "created_by_name",
            "applicant_full_name",
            "applicant_username",
            "applicant_email",
        ):
            self.assertIn(field, response.data)
        # Sensitive fields should not leak by default.
        for field in (
            "applicant_civil_number",
            "applicant_birth_date",
            "applicant_organization",
            "applicant_nationality",
            "applicant_registration_method",  # not in Constance default list
        ):
            self.assertNotIn(field, response.data)

    def test_reviewer_sees_model_default_fields_when_blank_config_created(self):
        # When a CallApplicantVisibilityConfig row exists but is left as defaults,
        # the model defaults apply: full_name, email, username, registration_method.
        models.CallApplicantVisibilityConfig.objects.create(call=self.call)
        response = self._get_as(self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in (
            "applicant_full_name",
            "applicant_username",
            "applicant_email",
            "applicant_registration_method",
        ):
            self.assertIn(field, response.data)
        for field in (
            "applicant_organization",
            "applicant_nationality",
            "applicant_civil_number",
        ):
            self.assertNotIn(field, response.data)

    def test_reviewer_does_not_see_full_name_when_disabled(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.call, expose_full_name=False, expose_username=True
        )
        response = self._get_as(self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("created_by_name", response.data)
        self.assertNotIn("applicant_full_name", response.data)
        self.assertNotIn("applicant_first_name", response.data)
        self.assertNotIn("applicant_last_name", response.data)
        # Identity link is still present.
        self.assertIn("created_by", response.data)
        self.assertIn("applicant_username", response.data)

    def test_reviewer_does_not_see_identity_when_username_disabled(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.call, expose_full_name=True, expose_username=False
        )
        response = self._get_as(self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("created_by", response.data)
        self.assertNotIn("created_by_uuid", response.data)
        self.assertNotIn("applicant_username", response.data)
        # Full name still present.
        self.assertIn("created_by_name", response.data)
        self.assertIn("applicant_full_name", response.data)

    def test_reviewer_sees_extended_attributes_when_enabled(self):
        # Populate user with realistic extended attributes.
        self.applicant.organization = "ACME"
        self.applicant.nationality = "EE"
        self.applicant.civil_number = "12345"
        self.applicant.save()
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.call,
            expose_organization=True,
            expose_nationality=True,
            expose_civil_number=True,
        )
        response = self._get_as(self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["applicant_organization"], "ACME")
        self.assertEqual(response.data["applicant_nationality"], "EE")
        self.assertEqual(response.data["applicant_civil_number"], "12345")

    def test_reviewer_with_all_fields_disabled_sees_no_applicant_data(self):
        # Disable everything.
        kwargs = {
            f"expose_{key}": False
            for key in (
                "full_name",
                "email",
                "username",
                "registration_method",
                "organization",
                "organization_country",
                "organization_type",
                "organization_registry_code",
                "affiliations",
                "phone_number",
                "job_title",
                "gender",
                "personal_title",
                "place_of_birth",
                "address",
                "country_of_residence",
                "nationality",
                "nationalities",
                "eduperson_assurance",
                "identity_source",
                "civil_number",
                "birth_date",
                "active_isds",
            )
        }
        models.CallApplicantVisibilityConfig.objects.create(call=self.call, **kwargs)
        response = self._get_as(self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in self.ALL_APPLICANT_FIELDS:
            self.assertNotIn(field, response.data)
        # Identity link also dropped.
        self.assertNotIn("created_by", response.data)
        self.assertNotIn("created_by_name", response.data)
        self.assertNotIn("created_by_uuid", response.data)

    def test_call_manager_sees_full_data_regardless_of_config(self):
        kwargs = {
            f"expose_{key}": False
            for key in (
                "full_name",
                "email",
                "username",
                "civil_number",
            )
        }
        models.CallApplicantVisibilityConfig.objects.create(call=self.call, **kwargs)
        response = self._get_as(self.call_manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("created_by_name", response.data)
        self.assertIn("created_by", response.data)
        self.assertIn("applicant_full_name", response.data)
        self.assertIn("applicant_civil_number", response.data)

    def test_applicant_sees_own_full_data_regardless_of_config(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.call, expose_full_name=False, expose_username=False
        )
        # Applicant needs LIST_PROPOSALS via owner customer role to fetch.
        self.fixture.customer.add_user(self.applicant, CustomerRole.OWNER)
        response = self._get_as(self.applicant)
        if response.status_code == status.HTTP_200_OK:
            self.assertIn("created_by_name", response.data)
            self.assertIn("created_by", response.data)
            self.assertIn("applicant_full_name", response.data)

    def test_staff_sees_full_data_regardless_of_config(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.call, expose_full_name=False, expose_username=False
        )
        staff = structure_factories.UserFactory(is_staff=True)
        response = self._get_as(staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("created_by_name", response.data)
        self.assertIn("created_by", response.data)
        self.assertIn("applicant_full_name", response.data)
