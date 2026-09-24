import json

from constance.test.unittest import override_config
from rest_framework import status
from rest_framework.authtoken.models import Token

from waldur_core.core.models import SshPublicKey, User
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.utils import add_user
from waldur_core.structure.tests import factories as structure_factories
from waldur_sram import models
from waldur_sram.mapping import SRAM_GROUP_EXTENSION_URN, SRAM_USER_EXTENSION_URN
from waldur_sram.tests import payloads
from waldur_sram.tests.base import BASE, SramScimTest


class GatingTest(SramScimTest):
    @override_config(SRAM_INTEGRATION_ENABLED=False)
    def test_disabled_integration_returns_403(self):
        response = self.client.get(f"{BASE}/Users")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_staff_token_returns_403(self):
        user = structure_factories.UserFactory()
        token, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.key}")
        response = self.client.get(f"{BASE}/Users")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_generic_scim_endpoint_is_still_served(self):
        response = self.client.get("/scim/v2/ServiceProviderConfig")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.client.get(f"{BASE}/ServiceProviderConfig")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_config(SRAM_INTEGRATION_ENABLED=False)
    def test_disabled_integration_hides_sram_discovery(self):
        self.client.credentials()  # discovery needs no token either way
        for path in (
            "ServiceProviderConfig",
            "ResourceTypes",
            "ResourceTypes/User",
            "Schemas",
            f"Schemas/{SRAM_USER_EXTENSION_URN}",
        ):
            response = self.client.get(f"{BASE}/{path}")
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, path)
        response = self.client.get("/scim/v2/ServiceProviderConfig")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_unknown_sram_path_is_a_scim_404(self):
        response = self.client.get(f"{BASE}/Nope")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class DiscoveryTest(SramScimTest):
    COMMON_KEYS = {"schemas", "id", "externalId", "meta"}

    def get(self, path):
        response = self.client.get(f"{BASE}/{path}")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        return response.json()

    def test_patch_is_not_advertised(self):
        self.assertFalse(self.get("ServiceProviderConfig")["patch"]["supported"])
        generic = self.client.get("/scim/v2/ServiceProviderConfig").json()
        self.assertTrue(generic["patch"]["supported"])

    def test_resource_types_name_the_sram_extensions(self):
        types = {rt["id"]: rt for rt in self.get("ResourceTypes")["Resources"]}
        user_ext = {e["schema"] for e in types["User"]["schemaExtensions"]}
        group_ext = {e["schema"] for e in types["Group"]["schemaExtensions"]}
        self.assertIn(SRAM_USER_EXTENSION_URN, user_ext)
        self.assertIn(SRAM_GROUP_EXTENSION_URN, group_ext)
        self.assertEqual(self.get("ResourceTypes/Group")["id"], "Group")

    def test_schemas_describe_sram_attributes(self):
        schemas = {s["id"]: s for s in self.get("Schemas")["Resources"]}
        self.assertIn(SRAM_USER_EXTENSION_URN, schemas)
        self.assertIn(SRAM_GROUP_EXTENSION_URN, schemas)
        user_attrs = {
            a["name"]
            for a in schemas["urn:ietf:params:scim:schemas:core:2.0:User"]["attributes"]
        }
        self.assertIn("x509Certificates", user_attrs)
        sram_user = self.get(f"Schemas/{SRAM_USER_EXTENSION_URN}")
        self.assertIn("eduPersonUniqueId", {a["name"] for a in sram_user["attributes"]})

    def assert_advertised(self, resource, resource_type):
        schemas = {s["id"]: s for s in self.get("Schemas")["Resources"]}
        rt = self.get(f"ResourceTypes/{resource_type}")
        extensions = {e["schema"] for e in rt["schemaExtensions"]}
        core = {a["name"] for a in schemas[rt["schema"]]["attributes"]}
        for urn in resource["schemas"]:
            self.assertIn(urn, {rt["schema"], *extensions})
        for key in resource:
            if key in self.COMMON_KEYS or key in core:
                continue
            self.assertIn(key, extensions, f"{resource_type}.{key} is not advertised")
            self.assertIn(key, schemas, f"no schema for {key}")

    def test_rendered_resources_only_use_advertised_attributes(self):
        body, _ = self.provision_user(ssh_keys=[payloads.KEY1])
        self.assert_advertised(self.sbs.lookup("Users", body["externalId"]), "User")

        _, member = self.provision_user(username="member")
        group = payloads.sram_group(member_ids=[member["id"]], labels=["hpc"])
        response = self.sbs.provision("Groups", group)
        self.assertIn(response.status_code, (200, 201), response.content)
        self.assert_advertised(self.sbs.lookup("Groups", group["externalId"]), "Group")


class UserProvisioningTest(SramScimTest):
    def test_create_then_update_through_sbs_lookup(self):
        body, created = self.provision_user(username="roger")
        self.assertEqual(created["externalId"], body["externalId"])
        self.assertEqual(created["meta"]["location"], f"/Users/{created['id']}")
        self.assertEqual(created["displayName"], "Roger Doe")

        body["name"]["givenName"] = "Rogier"
        response = self.sbs.provision("Users", body)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        user = User.objects.get(username="roger")
        self.assertEqual(user.first_name, "Rogier")
        self.assertEqual(User.objects.filter(username="roger").count(), 1)

    def test_echoes_sram_fields_so_sbs_sees_no_change(self):
        body, created = self.provision_user(ssh_keys=[payloads.KEY1])
        found = self.sbs.lookup("Users", body["externalId"])
        self.assertEqual(found["x509Certificates"], body["x509Certificates"])
        self.assertEqual(found[SRAM_USER_EXTENSION_URN], body[SRAM_USER_EXTENSION_URN])
        self.assertEqual(found["emails"][0]["value"], body["emails"][0]["value"])

    def test_display_name_fills_missing_names(self):
        body = payloads.sram_user(username="sarah", given_name="", family_name="")
        body["displayName"] = "Sarah van der Cross"
        self.sbs.provision("Users", body)
        user = User.objects.get(username="sarah")
        self.assertEqual((user.first_name, user.last_name), ("Sarah", "van der Cross"))

    def test_given_names_win_over_display_name(self):
        body = payloads.sram_user(username="sarah", given_name="Sara", family_name="")
        body["displayName"] = "Sarah Cross"
        self.sbs.provision("Users", body)
        user = User.objects.get(username="sarah")
        self.assertEqual(user.first_name, "Sara")

    def test_affiliations_are_stored(self):
        self.provision_user(affiliation="member@uni.nl, staff@uni.nl")
        user = User.objects.get(username="roger")
        self.assertEqual(user.affiliations, ["member@uni.nl", "staff@uni.nl"])

    @override_config(SCIM_INBOUND_SSH_KEYS_ENABLED=True)
    def test_ssh_keys_follow_x509_certificates(self):
        body, _ = self.provision_user(ssh_keys=[payloads.KEY1, payloads.KEY2])
        user = User.objects.get(username="roger")
        self.assertEqual(SshPublicKey.objects.filter(user=user).count(), 2)

        body["x509Certificates"] = [{"value": payloads.b64(payloads.KEY2)}]
        self.sbs.provision("Users", body)
        keys = list(SshPublicKey.objects.filter(user=user))
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].public_key.startswith(payloads.KEY2.rsplit(" ", 1)[0]))

    @override_config(SCIM_INBOUND_SSH_KEYS_ENABLED=True)
    def test_invalid_ssh_key_is_skipped(self):
        body = payloads.sram_user(ssh_keys=[payloads.KEY1])
        body["x509Certificates"].append({"value": payloads.b64("not a key")})
        body["x509Certificates"].append({"value": "%%%not-base64"})
        response = self.sbs.provision("Users", body)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(username="roger")
        self.assertEqual(SshPublicKey.objects.filter(user=user).count(), 1)

    def test_ssh_keys_untouched_when_sync_disabled(self):
        self.provision_user(ssh_keys=[payloads.KEY1])
        user = User.objects.get(username="roger")
        self.assertEqual(SshPublicKey.objects.filter(user=user).count(), 0)

    def test_existing_local_user_is_linked(self):
        local = structure_factories.UserFactory(username="roger")
        _, created = self.provision_user(username="roger")
        self.assertEqual(created["id"], local.uuid.hex)
        self.assertTrue(models.SramUser.objects.filter(user=local).exists())

    @override_config(
        SCIM_USER_MATCH_SCIM_ATTRIBUTE=f"{SRAM_USER_EXTENSION_URN}.eduPersonUniqueId"
    )
    def test_link_by_sram_unique_id(self):
        local = structure_factories.UserFactory(username="roger@test.sram.surf.nl")
        structure_factories.UserFactory(username="roger")
        _, created = self.provision_user(username="roger")
        self.assertEqual(created["id"], local.uuid.hex)

    @override_config(
        SCIM_USER_MATCH_SCIM_ATTRIBUTE=f"{SRAM_USER_EXTENSION_URN}.eduPersonUniqueId"
    )
    def test_new_user_is_named_after_sram_unique_id(self):
        _, created = self.provision_user(username="roger")
        user = User.objects.get(uuid=created["id"])
        self.assertEqual(user.username, "roger@test.sram.surf.nl")
        # SRAM sees its own userName back, so its sweep finds nothing to update.
        self.assertEqual(created["userName"], "roger")

        # SBS keeps sending userName=roger; that is not a rename.
        body, _ = self.provision_user(
            username="roger", external_id=created["externalId"]
        )
        self.assertEqual(User.objects.filter(username__startswith="roger").count(), 1)

    def test_users_survive_a_later_change_of_match_settings(self):
        body, created = self.provision_user(username="roger")
        path = f"{SRAM_USER_EXTENSION_URN}.eduPersonUniqueId"
        with override_config(SCIM_USER_MATCH_SCIM_ATTRIBUTE=path):
            body["name"]["familyName"] = "Changed"
            response = self.sbs.provision("Users", body)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        user = User.objects.get(uuid=created["id"])
        self.assertEqual((user.username, user.last_name), ("roger", "Changed"))

    def test_privileged_local_user_is_not_linked(self):
        structure_factories.UserFactory(username="admin", is_staff=True)
        response = self.sbs.provision("Users", payloads.sram_user(username="admin"))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(models.SramUser.objects.exists())

    def test_duplicate_external_id_is_a_conflict(self):
        body, _ = self.provision_user()
        response = self.client.post(
            f"{BASE}/Users", data=json.dumps(body), content_type="application/scim+json"
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_external_id_is_required(self):
        body = payloads.sram_user()
        del body["externalId"]
        response = self.client.post(
            f"{BASE}/Users", data=json.dumps(body), content_type="application/scim+json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_delete_deactivates_and_unlinks(self):
        body, created = self.provision_user()
        response = self.sbs.delete(created)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        user = User.all_objects.get(uuid=created["id"])
        self.assertFalse(user.is_active)
        self.assertIsNone(self.sbs.lookup("Users", body["externalId"]))

    def test_reprovisioning_a_deleted_user_reactivates_it(self):
        body, created = self.provision_user()
        self.sbs.delete(created)
        response = self.sbs.provision("Users", body)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["id"], created["id"])
        self.assertTrue(User.objects.get(uuid=created["id"]).is_active)

    def test_suspended_user_is_deactivated(self):
        body, created = self.provision_user()
        body["active"] = False
        self.sbs.provision("Users", body)
        user = User.all_objects.get(uuid=created["id"])
        self.assertFalse(user.is_active)
        found = self.sbs.lookup("Users", body["externalId"])
        self.assertFalse(found["active"])
        # A suspension keeps the attributes, so the next sweep sees no change.
        self.assertEqual(found["emails"][0]["value"], body["emails"][0]["value"])
        self.assertEqual(found["name"]["givenName"], "Roger")

        body["active"] = True
        self.sbs.provision("Users", body)
        self.assertTrue(User.objects.get(uuid=created["id"]).is_active)

    def test_suspended_user_is_created_inactive(self):
        _, created = self.provision_user(active=False)
        self.assertFalse(User.all_objects.get(uuid=created["id"]).is_active)

    def test_patch_is_rejected(self):
        _, created = self.provision_user()
        response = self.client.patch(
            f"{BASE}{created['meta']['location']}",
            data=json.dumps({"Operations": []}),
            content_type="application/scim+json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class GroupProvisioningTest(SramScimTest):
    def test_create_and_update_collaboration(self):
        _, roger = self.provision_user(username="roger")
        _, sarah = self.provision_user(username="sarah")
        body = payloads.sram_group(member_ids=[roger["id"]], labels=["hpc"])

        response = self.sbs.provision("Groups", body)
        self.assertEqual(
            response.status_code, status.HTTP_201_CREATED, response.content
        )
        created = response.json()
        self.assertEqual(created["meta"]["location"], f"/Groups/{created['id']}")
        group = models.SramGroup.objects.get(external_id=body["externalId"])
        self.assertEqual(group.kind, models.SramGroup.Kind.COLLABORATION)
        self.assertEqual(group.labels, ["hpc"])
        self.assertEqual(group.organisation_short_name, "uuc")
        self.assertEqual([u.uuid.hex for u in group.members.all()], [roger["id"]])

        body = payloads.sram_group(
            external_id=body["externalId"],
            member_ids=[sarah["id"]],
            display_name="Research 2",
        )
        response = self.sbs.provision("Groups", body)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        group.refresh_from_db()
        self.assertEqual(group.display_name, "Research 2")
        self.assertEqual(group.labels, [])
        self.assertEqual([u.uuid.hex for u in group.members.all()], [sarah["id"]])

    def test_sub_group_kind(self):
        response = self.sbs.provision(
            "Groups",
            payloads.sram_group(urn="uuc:research:admins", display_name="Admins"),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            models.SramGroup.objects.get().kind, models.SramGroup.Kind.GROUP
        )

    def test_echoes_extension_and_members(self):
        _, roger = self.provision_user()
        body = payloads.sram_group(member_ids=[roger["id"]], labels=["b", "a"])
        self.sbs.provision("Groups", body)
        found = self.sbs.lookup("Groups", body["externalId"])
        self.assertEqual(
            found[SRAM_GROUP_EXTENSION_URN], body[SRAM_GROUP_EXTENSION_URN]
        )
        self.assertEqual([m["value"] for m in found["members"]], [roger["id"]])

    def test_unknown_members_are_skipped(self):
        _, roger = self.provision_user()
        local = structure_factories.UserFactory()
        body = payloads.sram_group(
            member_ids=[roger["id"], local.uuid.hex, "not-a-uuid", "f" * 32]
        )
        response = self.sbs.provision("Groups", body)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        group = models.SramGroup.objects.get()
        self.assertEqual([u.uuid.hex for u in group.members.all()], [roger["id"]])

    def test_display_name_is_required(self):
        body = payloads.sram_group(display_name="")
        response = self.client.post(
            f"{BASE}/Groups",
            data=json.dumps(body),
            content_type="application/scim+json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_delete(self):
        body = payloads.sram_group()
        created = self.sbs.provision("Groups", body).json()
        response = self.sbs.delete(created)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.SramGroup.objects.exists())


class SweepTest(SramScimTest):
    def test_sweep_only_touches_sram_objects(self):
        customer = structure_factories.CustomerFactory()
        owner = structure_factories.UserFactory(username="owner")
        add_user(customer, owner, CustomerRole.OWNER)
        local = structure_factories.UserFactory(username="alice")

        kept_user, _ = self.provision_user(username="roger")
        gone_user, gone = self.provision_user(username="sarah")
        kept_group = payloads.sram_group()
        gone_group = payloads.sram_group(urn="uuc:old")
        self.sbs.provision("Groups", kept_group)
        gone_group_id = self.sbs.provision("Groups", gone_group).json()["id"]

        users = self.sbs.list_all("Users")
        self.assertEqual(sorted(u["userName"] for u in users), ["roger", "sarah"])

        deleted = self.sbs.sweep_delete(
            {kept_user["externalId"], kept_group["externalId"]}
        )
        self.assertEqual(
            sorted(deleted),
            sorted([("Groups", gone_group_id, 204), ("Users", gone["id"], 204)]),
        )

        local.refresh_from_db()
        owner.refresh_from_db()
        self.assertTrue(local.is_active)
        self.assertTrue(owner.is_active)
        self.assertTrue(customer.has_user(owner, CustomerRole.OWNER))
        self.assertTrue(self.service_account.is_active)
        self.assertEqual(
            list(models.SramGroup.objects.values_list("external_id", flat=True)),
            [kept_group["externalId"]],
        )

    def test_pagination_matches_sbs_loop(self):
        for index in range(5):
            self.provision_user(username=f"user{index}")
        response = self.client.get(f"{BASE}/Users?startIndex=3&count=2")
        data = response.json()
        self.assertEqual(data["totalResults"], 5)
        self.assertEqual(data["startIndex"], 3)
        self.assertEqual(len(data["Resources"]), 2)
        self.assertEqual(len(self.sbs.list_all("Users")), 5)
