"""Smoke tests for the SCIM 2.0 discovery endpoints (RFC 7644 §4)."""

from constance.test.unittest import override_config
from rest_framework import status, test


class ScimDiscoveryTest(test.APITestCase):
    def test_feature_flag_off_returns_scim_error(self):
        with override_config(SCIM_INBOUND_ENABLED=False):
            response = self.client.get("/scim/v2/ServiceProviderConfig")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response["content-type"], "application/scim+json")
        body = response.json()
        self.assertEqual(
            body["schemas"], ["urn:ietf:params:scim:api:messages:2.0:Error"]
        )
        self.assertEqual(body["status"], "403")

    @override_config(SCIM_INBOUND_ENABLED=True)
    def test_service_provider_config(self):
        response = self.client.get("/scim/v2/ServiceProviderConfig")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertIn(
            "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig",
            body["schemas"],
        )
        self.assertTrue(body["patch"]["supported"])
        self.assertFalse(body["bulk"]["supported"])
        self.assertEqual(body["authenticationSchemes"][0]["type"], "oauthbearertoken")

    @override_config(SCIM_INBOUND_ENABLED=True)
    def test_resource_types_list(self):
        response = self.client.get("/scim/v2/ResourceTypes")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        ids = {r["id"] for r in body["Resources"]}
        self.assertSetEqual(ids, {"User", "Group"})

    @override_config(SCIM_INBOUND_ENABLED=True)
    def test_resource_type_detail(self):
        response = self.client.get("/scim/v2/ResourceTypes/User")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["id"], "User")

    @override_config(SCIM_INBOUND_ENABLED=True)
    def test_resource_type_unknown_returns_scim_404(self):
        response = self.client.get("/scim/v2/ResourceTypes/Bogus")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        body = response.json()
        self.assertEqual(
            body["schemas"], ["urn:ietf:params:scim:api:messages:2.0:Error"]
        )

    @override_config(SCIM_INBOUND_ENABLED=True)
    def test_schemas_list_includes_waldur_extension(self):
        response = self.client.get("/scim/v2/Schemas")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = {r["id"] for r in response.json()["Resources"]}
        self.assertIn("urn:ietf:params:scim:schemas:core:2.0:User", ids)
        self.assertIn("urn:ietf:params:scim:schemas:core:2.0:Group", ids)
        self.assertIn("urn:ietf:params:scim:schemas:extension:enterprise:2.0:User", ids)
        self.assertIn("urn:waldur:params:scim:schemas:extension:User:1.0", ids)
