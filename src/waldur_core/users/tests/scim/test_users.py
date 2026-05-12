"""Integration tests for the inbound SCIM ``/Users`` endpoint."""

from constance.test.unittest import override_config
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.core.models import User
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users.tests.scim.conftest import make_staff_token


@override_config(SCIM_INBOUND_ENABLED=True)
class UsersEndpointTest(test.APITestCase):
    def setUp(self):
        token_key, self.svc_user = make_staff_token()
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {token_key}",
            HTTP_ACCEPT="application/scim+json",
        )

    def _scim_user_body(self, **overrides):
        body = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "alice",
            "name": {"givenName": "Alice", "familyName": "Smith"},
            "emails": [{"value": "alice@example.com", "primary": True}],
            "active": True,
        }
        body.update(overrides)
        return body

    def test_create_user(self):
        response = self.client.post(
            "/scim/v2/Users",
            data=self._scim_user_body(),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["userName"], "alice")
        self.assertEqual(response.data["name"]["givenName"], "Alice")
        self.assertIn("location", {k.lower(): v for k, v in response.items()})

        user = User.objects.get(username="alice")
        self.assertEqual(user.first_name, "Alice")
        self.assertEqual(user.email, "alice@example.com")
        sources = user.attribute_sources
        self.assertEqual(sources["first_name"]["source"], "scim:default")
        self.assertIn("scim:default", user.active_isds)

    def test_create_user_with_external_id(self):
        body = self._scim_user_body(externalId="okta-12345")
        response = self.client.post("/scim/v2/Users", data=body, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["externalId"], "okta-12345")

    def test_create_user_duplicate_username_returns_409(self):
        self.client.post("/scim/v2/Users", data=self._scim_user_body(), format="json")
        response = self.client.post(
            "/scim/v2/Users", data=self._scim_user_body(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["scimType"], "uniqueness")

    def test_create_user_missing_username(self):
        body = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "emails": [{"value": "a@example.com"}],
        }
        response = self.client.post("/scim/v2/Users", data=body, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_user(self):
        user = structure_factories.UserFactory(username="bob")
        response = self.client.get(f"/scim/v2/Users/{user.uuid.hex}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["userName"], "bob")

    def test_get_user_not_found(self):
        response = self.client.get("/scim/v2/Users/00000000000000000000000000000000")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_list_users(self):
        structure_factories.UserFactory(username="alice")
        structure_factories.UserFactory(username="bob")
        response = self.client.get("/scim/v2/Users?startIndex=1&count=100")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        usernames = {r["userName"] for r in response.data["Resources"]}
        self.assertIn("alice", usernames)
        self.assertIn("bob", usernames)

    def test_filter_username_eq(self):
        structure_factories.UserFactory(username="alice")
        structure_factories.UserFactory(username="bob")
        response = self.client.get('/scim/v2/Users?filter=userName eq "alice"')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["totalResults"], 1)
        self.assertEqual(response.data["Resources"][0]["userName"], "alice")

    def test_put_replace_attributes(self):
        user = structure_factories.UserFactory(username="bob", first_name="Old")
        body = self._scim_user_body(
            userName="bob", name={"givenName": "New", "familyName": "Last"}
        )
        body["emails"] = [{"value": "bob@example.com", "primary": True}]
        response = self.client.put(
            f"/scim/v2/Users/{user.uuid.hex}", data=body, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertEqual(user.first_name, "New")
        self.assertEqual(user.email, "bob@example.com")

    def test_put_username_change_rejected(self):
        user = structure_factories.UserFactory(username="bob")
        body = self._scim_user_body(userName="bob-renamed")
        response = self.client.put(
            f"/scim/v2/Users/{user.uuid.hex}", data=body, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["scimType"], "mutability")

    def test_patch_replace_first_name(self):
        user = structure_factories.UserFactory(username="bob", first_name="Old")
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {"op": "replace", "path": "name.givenName", "value": "Newish"}
            ],
        }
        response = self.client.patch(
            f"/scim/v2/Users/{user.uuid.hex}", data=body, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertEqual(user.first_name, "Newish")

    def test_patch_deactivate(self):
        user = structure_factories.UserFactory(username="bob", is_active=True)
        # Bind user to scim source so removal triggers deactivation.
        user.active_isds = ["scim:default"]
        user.attribute_sources = {
            "email": {
                "source": "scim:default",
                "timestamp": "2024-01-01T00:00:00+00:00",
            }
        }
        user.save()
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": False}],
        }
        response = self.client.patch(
            f"/scim/v2/Users/{user.uuid.hex}", data=body, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_delete_soft_deactivates(self):
        user = structure_factories.UserFactory(username="bob", is_active=True)
        user.active_isds = ["scim:default"]
        user.save()
        response = self.client.delete(f"/scim/v2/Users/{user.uuid.hex}")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        user.refresh_from_db()
        self.assertFalse(user.is_active)


@override_config(SCIM_INBOUND_ENABLED=True)
class UsersEndpointAuthTest(test.APITestCase):
    def test_missing_token_returns_401(self):
        response = self.client.get("/scim/v2/Users")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_non_staff_token_returns_403(self):
        user = structure_factories.UserFactory(is_staff=False, is_active=True)
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.key}")
        response = self.client.get("/scim/v2/Users")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class UsersEndpointFeatureFlagTest(test.APITestCase):
    def test_flag_off_returns_403(self):
        token_key, _ = make_staff_token()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token_key}")
        response = self.client.get("/scim/v2/Users")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("SCIM_INBOUND_ENABLED", response.data["detail"])
