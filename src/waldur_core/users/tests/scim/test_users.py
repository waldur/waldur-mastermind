"""Integration tests for the inbound SCIM ``/Users`` endpoint."""

from constance.test.unittest import override_config
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.core.models import SshPublicKey, User
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

    def test_patch_replace_whole_name(self):
        user = structure_factories.UserFactory(username="bob", first_name="Old")
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {
                    "op": "replace",
                    "path": "name",
                    "value": {"givenName": "Whole", "familyName": "Name"},
                }
            ],
        }
        response = self.client.patch(
            f"/scim/v2/Users/{user.uuid.hex}", data=body, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertEqual(user.first_name, "Whole")
        self.assertEqual(user.last_name, "Name")

    def test_patch_remove_whole_name(self):
        user = structure_factories.UserFactory(
            username="bob", first_name="Old", last_name="Name"
        )
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "remove", "path": "name"}],
        }
        response = self.client.patch(
            f"/scim/v2/Users/{user.uuid.hex}", data=body, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertEqual(user.first_name, "")
        self.assertEqual(user.last_name, "")

    def test_patch_enterprise_extension_urn_path(self):
        """Entra ID addresses extension fields with URN-prefixed paths."""
        user = structure_factories.UserFactory(username="bob")
        urn = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {
                    "op": "replace",
                    "path": f"{urn}:organization",
                    "value": "Example Lab",
                }
            ],
        }
        response = self.client.patch(
            f"/scim/v2/Users/{user.uuid.hex}", data=body, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertEqual(user.organization, "Example Lab")

    def test_patch_whole_extension_object(self):
        user = structure_factories.UserFactory(username="bob")
        urn = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {"op": "add", "path": urn, "value": {"organization": "Whole Org"}}
            ],
        }
        response = self.client.patch(
            f"/scim/v2/Users/{user.uuid.hex}", data=body, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertEqual(user.organization, "Whole Org")

    @override_config(
        SCIM_INBOUND_ALLOWED_ATTRIBUTES=["first_name", "email", "civil_number"]
    )
    def test_patch_remove_civil_number_clears_to_null(self):
        """civil_number has a unique constraint and must clear to NULL, not ''."""
        urn = "urn:waldur:params:scim:schemas:extension:User:1.0"
        for name, number in (("bob", "39001010001"), ("carol", "39001010002")):
            user = structure_factories.UserFactory(username=name, civil_number=number)
            body = {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "remove", "path": f"{urn}:civilNumber"}],
            }
            response = self.client.patch(
                f"/scim/v2/Users/{user.uuid.hex}", data=body, format="json"
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            user.refresh_from_db()
            self.assertIsNone(user.civil_number)

    def test_delete_two_users_with_civil_numbers(self):
        """Source-owned attribute clearing on deactivation must not collide on
        the civil_number unique constraint."""
        for name, number in (("bob", "39001010003"), ("carol", "39001010004")):
            user = structure_factories.UserFactory(username=name, civil_number=number)
            user.active_isds = ["scim:default"]
            user.attribute_sources = {
                "civil_number": {
                    "source": "scim:default",
                    "timestamp": "2024-01-01T00:00:00+00:00",
                }
            }
            user.save()
            response = self.client.delete(f"/scim/v2/Users/{user.uuid.hex}")
            self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
            user.refresh_from_db()
            self.assertIsNone(user.civil_number)
            self.assertFalse(user.is_active)

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

    def test_deactivated_user_is_still_readable(self):
        user = structure_factories.UserFactory(username="bob", is_active=False)
        response = self.client.get(f"/scim/v2/Users/{user.uuid.hex}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["active"])

    def test_patch_reactivate_deactivated_user(self):
        user = structure_factories.UserFactory(username="bob", is_active=False)
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": True}],
        }
        response = self.client.patch(
            f"/scim/v2/Users/{user.uuid.hex}", data=body, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(user.deactivation_reason, "")

    def test_create_with_username_of_deactivated_user_returns_409(self):
        structure_factories.UserFactory(username="alice", is_active=False)
        response = self.client.post(
            "/scim/v2/Users", data=self._scim_user_body(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["scimType"], "uniqueness")

    def test_filter_active_false_returns_deactivated_users(self):
        structure_factories.UserFactory(username="alice", is_active=True)
        structure_factories.UserFactory(username="bob", is_active=False)
        response = self.client.get("/scim/v2/Users?filter=active eq false")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        usernames = {r["userName"] for r in response.data["Resources"]}
        self.assertIn("bob", usernames)
        self.assertNotIn("alice", usernames)

    def test_filter_active_quoted_boolean_is_accepted(self):
        structure_factories.UserFactory(username="bob", is_active=False)
        response = self.client.get('/scim/v2/Users?filter=active eq "False"')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        usernames = {r["userName"] for r in response.data["Resources"]}
        self.assertIn("bob", usernames)

    def test_filter_active_with_string_operator_returns_400(self):
        response = self.client.get("/scim/v2/Users?filter=active co true")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["scimType"], "invalidFilter")

    def test_filter_active_with_garbage_value_returns_400(self):
        response = self.client.get('/scim/v2/Users?filter=active eq "maybe"')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["scimType"], "invalidFilter")


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


WALDUR_URN = "urn:waldur:params:scim:schemas:extension:User:1.0"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

# Cryptographically valid ed25519 public keys (the create path parses them).
KEY1 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJJ8hP1eFBxBCWiUaB5vsLAvFaYjs0zQ0gWOltsWd8LI key1@example.com"
KEY2 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH4VKrindOs5cEW4vv2NZfGAB6A1/tuYmOXv2emFcuSC key2@example.com"
KEY3 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJ8Q7PU4ES7C3W+mJuQ/RM0NX7WIONl78tMKAqwZeWXu key3@example.com"


@override_config(SCIM_INBOUND_ENABLED=True, SCIM_INBOUND_SSH_KEYS_ENABLED=True)
class SshKeysViaScimTest(test.APITestCase):
    """Inbound SCIM management of user SSH public keys (Waldur extension)."""

    def setUp(self):
        token_key, self.svc_user = make_staff_token()
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {token_key}",
            HTTP_ACCEPT="application/scim+json",
        )

    def _body(self, ssh_keys, **overrides):
        body = {
            "schemas": [
                "urn:ietf:params:scim:schemas:core:2.0:User",
                WALDUR_URN,
            ],
            "userName": "alice",
            "active": True,
            WALDUR_URN: {"sshPublicKeys": ssh_keys},
        }
        body.update(overrides)
        return body

    def _patch(self, uuid_hex, operations):
        return self.client.patch(
            f"/scim/v2/Users/{uuid_hex}",
            data={"schemas": [PATCH_SCHEMA], "Operations": operations},
            format="json",
        )

    def _keys(self, user):
        return SshPublicKey.objects.filter(user=user)

    def test_create_user_with_ssh_keys(self):
        response = self.client.post(
            "/scim/v2/Users",
            data=self._body([{"value": KEY1, "display": "laptop"}]),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        user = User.objects.get(username="alice")
        key = self._keys(user).get()
        self.assertEqual(key.name, "laptop")
        self.assertEqual(key.public_key, KEY1)
        self.assertFalse(key.is_shared)
        self.assertTrue(key.fingerprint_sha256)

    def test_created_keys_owned_by_target_user_not_service_account(self):
        self.client.post(
            "/scim/v2/Users",
            data=self._body([{"value": KEY1, "display": "laptop"}]),
            format="json",
        )
        key = SshPublicKey.objects.get(public_key=KEY1)
        self.assertEqual(key.user.username, "alice")
        self.assertNotEqual(key.user, self.svc_user)

    def test_get_user_returns_ssh_keys(self):
        user = structure_factories.UserFactory(username="bob")
        structure_factories.SshPublicKeyFactory(user=user, name="a", public_key=KEY1)
        structure_factories.SshPublicKeyFactory(user=user, name="b", public_key=KEY2)
        response = self.client.get(f"/scim/v2/Users/{user.uuid.hex}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        entries = response.data[WALDUR_URN]["sshPublicKeys"]
        values = {e["value"] for e in entries}
        self.assertSetEqual(values, {KEY1, KEY2})

    def test_put_replaces_ssh_keys_and_deletes_omitted(self):
        user = structure_factories.UserFactory(username="bob")
        structure_factories.SshPublicKeyFactory(user=user, name="a", public_key=KEY1)
        structure_factories.SshPublicKeyFactory(user=user, name="b", public_key=KEY2)
        body = self._body([{"value": KEY1, "display": "a"}], userName="bob")
        response = self.client.put(
            f"/scim/v2/Users/{user.uuid.hex}", data=body, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        remaining = {k.public_key for k in self._keys(user)}
        self.assertSetEqual(remaining, {KEY1})

    def test_put_without_ssh_attribute_leaves_keys_untouched(self):
        user = structure_factories.UserFactory(username="bob")
        structure_factories.SshPublicKeyFactory(user=user, name="a", public_key=KEY1)
        body = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "bob",
            "active": True,
        }
        response = self.client.put(
            f"/scim/v2/Users/{user.uuid.hex}", data=body, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._keys(user).count(), 1)

    def test_patch_add_ssh_key(self):
        user = structure_factories.UserFactory(username="bob")
        structure_factories.SshPublicKeyFactory(user=user, name="a", public_key=KEY1)
        response = self._patch(
            user.uuid.hex,
            [
                {
                    "op": "add",
                    "path": f"{WALDUR_URN}:sshPublicKeys",
                    "value": [{"value": KEY2, "display": "b"}],
                }
            ],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertSetEqual({k.public_key for k in self._keys(user)}, {KEY1, KEY2})

    def test_patch_remove_ssh_key_by_filter(self):
        user = structure_factories.UserFactory(username="bob")
        structure_factories.SshPublicKeyFactory(user=user, name="a", public_key=KEY1)
        structure_factories.SshPublicKeyFactory(user=user, name="b", public_key=KEY2)
        response = self._patch(
            user.uuid.hex,
            [{"op": "remove", "path": f'sshPublicKeys[value eq "{KEY1}"]'}],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertSetEqual({k.public_key for k in self._keys(user)}, {KEY2})

    def test_patch_remove_all_ssh_keys(self):
        user = structure_factories.UserFactory(username="bob")
        structure_factories.SshPublicKeyFactory(user=user, name="a", public_key=KEY1)
        structure_factories.SshPublicKeyFactory(user=user, name="b", public_key=KEY2)
        response = self._patch(
            user.uuid.hex, [{"op": "remove", "path": "sshPublicKeys"}]
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._keys(user).count(), 0)

    def test_patch_replace_ssh_keys(self):
        user = structure_factories.UserFactory(username="bob")
        structure_factories.SshPublicKeyFactory(user=user, name="a", public_key=KEY1)
        response = self._patch(
            user.uuid.hex,
            [
                {
                    "op": "replace",
                    "path": f"{WALDUR_URN}:sshPublicKeys",
                    "value": [{"value": KEY2, "display": "b"}],
                }
            ],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertSetEqual({k.public_key for k in self._keys(user)}, {KEY2})

    def test_invalid_ssh_key_material_returns_400_and_rolls_back(self):
        response = self.client.post(
            "/scim/v2/Users",
            data=self._body([{"value": "not-a-real-key", "display": "x"}]),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["scimType"], "invalidValue")
        self.assertFalse(User.objects.filter(username="alice").exists())

    @override_config(SSH_KEY_ALLOWED_TYPES=["ssh-rsa"])
    def test_disallowed_key_type_returns_400(self):
        response = self.client.post(
            "/scim/v2/Users",
            data=self._body([{"value": KEY1, "display": "x"}]),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["scimType"], "invalidValue")

    def test_missing_value_returns_400(self):
        response = self.client.post(
            "/scim/v2/Users",
            data=self._body([{"display": "no-value"}]),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["scimType"], "invalidValue")

    def test_duplicate_display_name_is_deduplicated(self):
        response = self.client.post(
            "/scim/v2/Users",
            data=self._body(
                [
                    {"value": KEY1, "display": "dup"},
                    {"value": KEY2, "display": "dup"},
                ]
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        user = User.objects.get(username="alice")
        keys = self._keys(user)
        self.assertEqual(keys.count(), 2)
        self.assertEqual(len({k.name for k in keys}), 2)

    def test_blank_display_names_do_not_collide(self):
        response = self.client.post(
            "/scim/v2/Users",
            data=self._body([{"value": KEY1}, {"value": KEY2}]),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        user = User.objects.get(username="alice")
        self.assertEqual(self._keys(user).count(), 2)

    def test_resync_same_key_is_idempotent(self):
        user = structure_factories.UserFactory(username="bob")
        structure_factories.SshPublicKeyFactory(user=user, name="a", public_key=KEY1)
        body = self._body([{"value": KEY1, "display": "a"}], userName="bob")
        response = self.client.put(
            f"/scim/v2/Users/{user.uuid.hex}", data=body, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._keys(user).count(), 1)

    @override_config(SCIM_INBOUND_SSH_KEYS_ENABLED=False)
    def test_ssh_keys_ignored_when_feature_disabled(self):
        response = self.client.post(
            "/scim/v2/Users",
            data=self._body([{"value": KEY1, "display": "laptop"}]),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        user = User.objects.get(username="alice")
        self.assertEqual(self._keys(user).count(), 0)
        self.assertNotIn("sshPublicKeys", response.data.get(WALDUR_URN, {}))
