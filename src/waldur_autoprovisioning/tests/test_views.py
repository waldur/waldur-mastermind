import uuid
from unittest.mock import patch

from rest_framework import status, test

from waldur_autoprovisioning.tests import factories as autoprovisioning_factories
from waldur_core.core.models import User
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories


def _user(**kwargs):
    return User.objects.create(
        username=kwargs.pop("username", f"u-{uuid.uuid4().hex[:8]}"),
        email=kwargs.pop("email", "u@example.com"),
        **kwargs,
    )


@patch("waldur_autoprovisioning.handlers.process_order_on_commit")
class RuleTestMatchEndpointTest(test.APITestCase):
    """Tests for POST /api/autoprovisioning-rules/{uuid}/test-match/."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.regular = structure_factories.UserFactory()

    def _post(self, rule, user_uuid):
        url = autoprovisioning_factories.RuleFactory.get_url(rule, "test-match")
        return self.client.post(url, {"user_uuid": str(user_uuid)})

    def test_non_staff_cannot_call(self, _):
        rule = autoprovisioning_factories.RuleFactory()
        target = _user()
        self.client.force_authenticate(self.regular)
        response = self._post(rule, target.uuid)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unknown_user_uuid_returns_404(self, _):
        rule = autoprovisioning_factories.RuleFactory()
        self.client.force_authenticate(self.staff)
        response = self._post(rule, uuid.uuid4())
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_malformed_user_uuid_returns_400(self, _):
        rule = autoprovisioning_factories.RuleFactory()
        self.client.force_authenticate(self.staff)
        url = autoprovisioning_factories.RuleFactory.get_url(rule, "test-match")
        response = self.client.post(url, {"user_uuid": "not-a-uuid"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_happy_path_would_provision_true(self, _):
        rule = autoprovisioning_factories.RuleFactory(
            user_email_patterns=[r".+@example\.com"]
        )
        target = _user(email="hit@example.com")
        self.client.force_authenticate(self.staff)
        response = self._post(rule, target.uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["would_provision"])
        self.assertEqual(response.data["block_reason"], "")
        self.assertEqual(response.data["resolved_project_name"], target.username)
        # Six filter results, email_patterns matched
        filter_names = {fr["name"] for fr in response.data["filter_results"]}
        self.assertEqual(
            filter_names,
            {
                "affiliations",
                "email_patterns",
                "identity_sources",
                "nationalities",
                "organization_types",
                "assurance_levels",
            },
        )
        email_fr = next(
            fr
            for fr in response.data["filter_results"]
            if fr["name"] == "email_patterns"
        )
        self.assertTrue(email_fr["configured"])
        self.assertTrue(email_fr["matched"])
        self.assertFalse(response.data["customer_lookup_performed"])

    def test_rule_filters_block_user(self, _):
        rule = autoprovisioning_factories.RuleFactory(
            user_email_patterns=[r".+@allowed\.com"]
        )
        target = _user(email="miss@other.com")
        self.client.force_authenticate(self.staff)
        response = self._post(rule, target.uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["would_provision"])
        self.assertIn("filters do not match", response.data["block_reason"])
        # Filter-block short-circuits before customer lookup is even relevant
        self.assertFalse(response.data["customer_lookup_performed"])

    @override_waldur_core_settings(
        PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS=["PROTECTED"]
    )
    def test_customer_not_found_when_org_flag_on(self, _):
        rule = autoprovisioning_factories.RuleFactory(
            customer=None,
            user_email_patterns=[r".+@example\.com"],
            use_user_organization_as_customer_name=True,
        )
        target = _user(
            email="hit@example.com",
            organization="NotARealOrg",
            registration_method="PROTECTED",
        )
        self.client.force_authenticate(self.staff)
        response = self._post(rule, target.uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["would_provision"])
        self.assertTrue(response.data["customer_lookup_performed"])
        self.assertEqual(response.data["customer_candidates"], [])
        self.assertIn("No organization found", response.data["block_reason"])
        self.assertFalse(response.data["customer_lookup_ambiguous"])

    def test_user_not_protected_blocks_org_lookup(self, _):
        rule = autoprovisioning_factories.RuleFactory(
            customer=None,
            user_email_patterns=[r".+@example\.com"],
            use_user_organization_as_customer_name=True,
        )
        target = _user(
            email="hit@example.com",
            organization="UnprotectedOrg",
            registration_method="LOCAL",
        )
        self.client.force_authenticate(self.staff)
        response = self._post(rule, target.uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["would_provision"])
        self.assertIn(
            "PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS",
            response.data["block_reason"],
        )

    @override_waldur_core_settings(
        PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS=["PROTECTED"]
    )
    def test_user_no_organization_claim(self, _):
        rule = autoprovisioning_factories.RuleFactory(
            customer=None,
            user_email_patterns=[r".+@example\.com"],
            use_user_organization_as_customer_name=True,
        )
        target = _user(
            email="hit@example.com", organization="", registration_method="PROTECTED"
        )
        self.client.force_authenticate(self.staff)
        response = self._post(rule, target.uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["would_provision"])
        self.assertIn("organization claim", response.data["block_reason"])

    @override_waldur_core_settings(
        PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS=["PROTECTED"]
    )
    def test_ambiguous_customer_returned(self, _):
        org_name = "SharedOrgName"
        structure_models.Customer.objects.create(name=org_name)
        structure_models.Customer.objects.create(name=org_name)
        rule = autoprovisioning_factories.RuleFactory(
            customer=None,
            user_email_patterns=[r".+@example\.com"],
            use_user_organization_as_customer_name=True,
        )
        target = _user(
            email="hit@example.com",
            organization=org_name,
            registration_method="PROTECTED",
        )
        self.client.force_authenticate(self.staff)
        response = self._post(rule, target.uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["would_provision"])
        self.assertTrue(response.data["customer_lookup_ambiguous"])
        self.assertEqual(len(response.data["customer_candidates"]), 2)
        for candidate in response.data["customer_candidates"]:
            self.assertEqual(candidate["name"], org_name)
            self.assertIn("uuid", candidate)
            self.assertIn("url", candidate)

    @override_waldur_core_settings(
        PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS=["PROTECTED"]
    )
    def test_single_customer_match_resolves_project_name(self, _):
        org_name = "ResolvableOrg"
        structure_models.Customer.objects.create(name=org_name)
        rule = autoprovisioning_factories.RuleFactory(
            customer=None,
            user_email_patterns=[r".+@example\.com"],
            use_user_organization_as_customer_name=True,
            project_name_template="{username}_workspace",
        )
        target = _user(
            email="hit@example.com",
            organization=org_name,
            registration_method="PROTECTED",
        )
        self.client.force_authenticate(self.staff)
        response = self._post(rule, target.uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["would_provision"])
        self.assertTrue(response.data["customer_lookup_performed"])
        self.assertEqual(len(response.data["customer_candidates"]), 1)
        self.assertEqual(
            response.data["resolved_project_name"], f"{target.username}_workspace"
        )
