"""Configurable matching of inbound SCIM users to existing accounts."""

from constance.test.unittest import override_config
from django.test import TestCase
from rest_framework import status, test

from waldur_core.core.models import User
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users.scim.server import matching
from waldur_core.users.tests.scim.conftest import make_staff_token

SRAM_URN = "urn:mace:surf.nl:sram:scim:extension:User"
SRAM_UID_PATH = f"{SRAM_URN}.eduPersonUniqueId"


def scim_user(**overrides):
    body = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User", SRAM_URN],
        "externalId": "abc@test.sram.surf.nl",
        "userName": "alice",
        "name": {"givenName": "Alice", "familyName": "Smith"},
        "emails": [
            {"value": "other@example.com"},
            {"value": "Alice@Example.com", "primary": True},
        ],
        SRAM_URN: {"eduPersonUniqueId": "Alice-UID@sram.surf.nl"},
    }
    body.update(overrides)
    return body


class ResolvePathTest(TestCase):
    def resolve(self, path):
        with override_config(SCIM_USER_MATCH_SCIM_ATTRIBUTE=path):
            return matching.match_value(scim_user())

    def test_default_is_user_name(self):
        self.assertEqual(matching.match_value(scim_user()), "alice")

    def test_primary_of_multi_valued_attribute(self):
        self.assertEqual(self.resolve("emails"), "Alice@Example.com")
        self.assertEqual(self.resolve("emails.value"), "Alice@Example.com")

    def test_sub_attribute(self):
        self.assertEqual(self.resolve("name.givenName"), "Alice")

    def test_extension_path_with_dots_in_urn(self):
        self.assertEqual(self.resolve(SRAM_UID_PATH), "Alice-UID@sram.surf.nl")

    def test_attribute_names_are_case_insensitive(self):
        self.assertEqual(self.resolve("USERNAME"), "alice")

    def test_missing_attribute(self):
        self.assertIsNone(self.resolve("urn:unknown:ext.value"))
        self.assertIsNone(self.resolve("nickName"))


@override_config(SCIM_INBOUND_ENABLED=True)
class MatchingEndpointTest(test.APITestCase):
    def setUp(self):
        token_key, _ = make_staff_token()
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {token_key}",
            HTTP_ACCEPT="application/scim+json",
        )

    def post(self, body):
        return self.client.post("/scim/v2/Users", data=body, format="json")

    def test_default_matches_user_name_against_username(self):
        structure_factories.UserFactory(username="alice")
        response = self.post(scim_user())
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @override_config(SCIM_USER_MATCH_SCIM_ATTRIBUTE=SRAM_UID_PATH)
    def test_extension_value_matches_username(self):
        existing = structure_factories.UserFactory(username="alice-uid@sram.surf.nl")
        structure_factories.UserFactory(username="alice")
        response = self.post(scim_user())
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn(existing.uuid.hex, response.data["detail"])

    @override_config(SCIM_USER_MATCH_SCIM_ATTRIBUTE=SRAM_UID_PATH)
    def test_new_account_is_named_after_matched_value(self):
        response = self.post(scim_user())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["userName"], "alice-uid@sram.surf.nl")
        self.assertFalse(User.objects.filter(username="alice").exists())

    @override_config(SCIM_USER_MATCH_SCIM_ATTRIBUTE=SRAM_UID_PATH)
    def test_missing_match_value_is_rejected(self):
        response = self.post(scim_user(**{SRAM_URN: {}}))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("eduPersonUniqueId", response.data["detail"])

    @override_config(SCIM_USER_MATCH_SCIM_ATTRIBUTE=SRAM_UID_PATH)
    def test_changing_the_matched_value_is_rejected(self):
        created = self.post(scim_user()).data
        body = scim_user(**{SRAM_URN: {"eduPersonUniqueId": "someone-else"}})
        response = self.client.put(
            f"/scim/v2/Users/{created['id']}", data=body, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["scimType"], "mutability")

        response = self.client.put(
            f"/scim/v2/Users/{created['id']}", data=scim_user(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @override_config(
        SCIM_USER_MATCH_WALDUR_ATTRIBUTE="email",
        SCIM_USER_MATCH_SCIM_ATTRIBUTE="emails",
    )
    def test_match_on_email(self):
        existing = structure_factories.UserFactory(
            username="someone", email="alice@example.com"
        )
        response = self.post(scim_user())
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn(existing.uuid.hex, response.data["detail"])

    @override_config(
        SCIM_USER_MATCH_WALDUR_ATTRIBUTE="email",
        SCIM_USER_MATCH_SCIM_ATTRIBUTE="emails",
    )
    def test_ambiguous_match_is_a_conflict(self):
        structure_factories.UserFactory(email="alice@example.com")
        structure_factories.UserFactory(email="ALICE@example.com")
        response = self.post(scim_user())
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("More than one account", response.data["detail"])
        self.assertEqual(User.objects.filter(username="alice").count(), 0)

    @override_config(
        SCIM_USER_MATCH_WALDUR_ATTRIBUTE="email",
        SCIM_USER_MATCH_SCIM_ATTRIBUTE="emails",
    )
    def test_email_matching_creates_from_user_name(self):
        response = self.post(scim_user())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        user = User.objects.get(uuid=response.data["id"])
        self.assertEqual(user.username, "alice")
        self.assertEqual(user.email, "Alice@Example.com")

    @override_config(
        SCIM_USER_MATCH_WALDUR_ATTRIBUTE="civil_number",
        ENABLED_USER_PROFILE_ATTRIBUTES=[],
    )
    def test_disabled_attribute_is_refused_at_runtime(self):
        response = self.post(scim_user())
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @override_config(SCIM_USER_MATCH_WALDUR_ATTRIBUTE="first_name")
    def test_non_identifying_attribute_is_refused_at_runtime(self):
        structure_factories.UserFactory(first_name="alice")
        response = self.post(scim_user())
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)


class MatchSettingValidationTest(test.APITestCase):
    url = "/api/override-settings/"

    def setUp(self):
        self.client.force_login(structure_factories.UserFactory(is_staff=True))

    def test_username_is_accepted(self):
        response = self.client.post(
            self.url, {"SCIM_USER_MATCH_WALDUR_ATTRIBUTE": "username"}
        )
        self.assertEqual(response.status_code, 200)

    def test_core_identifying_attribute_is_accepted(self):
        response = self.client.post(
            self.url, {"SCIM_USER_MATCH_WALDUR_ATTRIBUTE": "email"}
        )
        self.assertEqual(response.status_code, 200)

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=[])
    def test_disabled_attribute_is_rejected(self):
        response = self.client.post(
            self.url, {"SCIM_USER_MATCH_WALDUR_ATTRIBUTE": "civil_number"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("SCIM_USER_MATCH_WALDUR_ATTRIBUTE", response.data)

    @override_config(ENABLED_USER_PROFILE_ATTRIBUTES=["civil_number"])
    def test_enabled_attribute_is_accepted(self):
        response = self.client.post(
            self.url, {"SCIM_USER_MATCH_WALDUR_ATTRIBUTE": "civil_number"}
        )
        self.assertEqual(response.status_code, 200)

    def test_unknown_attribute_is_rejected(self):
        response = self.client.post(
            self.url, {"SCIM_USER_MATCH_WALDUR_ATTRIBUTE": "first_name"}
        )
        self.assertEqual(response.status_code, 400)
