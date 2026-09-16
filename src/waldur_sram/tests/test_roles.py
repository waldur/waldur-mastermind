from io import StringIO

from constance.test.unittest import override_config
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from rest_framework import status
from rest_framework.exceptions import ValidationError

from waldur_core.core.models import User
from waldur_core.permissions import signals
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import Role, RoleAvailability, UserRole
from waldur_core.permissions.utils import add_user
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_sram import models, roles
from waldur_sram.tests import payloads
from waldur_sram.tests.base import SramScimTest


class PlaceholderRoleTest(SramScimTest):
    def setUp(self):
        super().setUp()
        _, self.roger = self.provision_user(username="roger")
        _, self.sarah = self.provision_user(username="sarah")
        self.roger_user = User.objects.get(uuid=self.roger["id"])
        self.sarah_user = User.objects.get(uuid=self.sarah["id"])

    def push(self, body):
        response = self.sbs.provision("Groups", body)
        self.assertIn(response.status_code, (200, 201), response.content)
        return models.SramGroup.objects.get(external_id=body["externalId"])

    def holders(self, group, **filters):
        return set(
            UserRole.objects.filter(
                role=group.role, is_active=True, **filters
            ).values_list("user__username", flat=True)
        )

    def test_collaboration_gets_a_private_role_held_by_its_members(self):
        group = self.push(
            payloads.sram_group(display_name="Research", member_ids=[self.roger["id"]])
        )
        role = group.role
        customer = Customer.objects.get(backend_id="uuc")
        self.assertEqual(role.name, f"CUSTOMER.{customer.slug}.SRAM.research")
        self.assertEqual(role.description, "Research")
        self.assertEqual(role.content_type.model, "customer")
        self.assertFalse(role.is_system_role)
        self.assertEqual(role.permissions.count(), 0)
        self.assertEqual(
            list(RoleAvailability.objects.filter(role=role).values_list("object_id")),
            [(customer.id,)],
        )
        self.assertTrue(customer.has_user(self.roger_user, role))
        self.assertEqual(
            self.holders(group, source=roles.grant_source(group)), {"roger"}
        )

    def test_sub_group_gets_its_own_role(self):
        co = self.push(payloads.sram_group(urn="uuc:research"))
        admins = self.push(
            payloads.sram_group(
                urn="uuc:research:admins",
                display_name="Admins",
                member_ids=[self.sarah["id"]],
            )
        )
        self.assertNotEqual(co.role, admins.role)
        self.assertTrue(admins.role.name.endswith(".SRAM.research.admins"))
        self.assertEqual(self.holders(admins), {"sarah"})

    def test_removed_member_loses_the_role(self):
        body = payloads.sram_group(member_ids=[self.roger["id"], self.sarah["id"]])
        group = self.push(body)
        self.assertEqual(self.holders(group), {"roger", "sarah"})

        body["members"] = [m for m in body["members"] if m["value"] == self.sarah["id"]]
        self.push(body)
        self.assertEqual(self.holders(group), {"sarah"})
        revoked = UserRole.objects.get(role=group.role, user=self.roger_user)
        self.assertIn("Removed from SRAM collaboration Research", revoked.revoke_reason)

    def test_manual_grants_are_left_alone_until_the_group_is_deleted(self):
        body = payloads.sram_group(member_ids=[self.roger["id"]])
        group = self.push(body)
        customer = group.customer
        outsider = structure_factories.UserFactory()
        add_user(customer, outsider, group.role)
        # A member that already holds the role by hand gets no second grant.
        add_user(customer, self.sarah_user, group.role)
        body["members"].append({"value": self.sarah["id"]})
        self.push(body)
        self.assertEqual(
            UserRole.objects.filter(role=group.role, user=self.sarah_user).count(), 1
        )

        body["members"] = []
        self.push(body)
        self.assertEqual(
            self.holders(group), {outsider.username, self.sarah_user.username}
        )

        role_id = group.role_id
        response = self.sbs.delete(self.sbs.lookup("Groups", body["externalId"]))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Role.objects.filter(id=role_id).exists())
        self.assertFalse(customer.has_user(outsider))

    def test_delete_revokes_before_deleting(self):
        group = self.push(payloads.sram_group(member_ids=[self.roger["id"]]))
        customer = group.customer
        revoked = []

        def record(sender, instance, **kwargs):
            revoked.append(instance.user.username)

        signals.role_revoked.connect(record, dispatch_uid="test-sram-revoked")
        try:
            self.sbs.delete(self.sbs.lookup("Groups", group.external_id))
        finally:
            signals.role_revoked.disconnect(dispatch_uid="test-sram-revoked")
        self.assertEqual(revoked, ["roger"])
        self.assertFalse(customer.has_user(self.roger_user))
        self.assertTrue(Customer.objects.filter(pk=customer.pk).exists())

    def test_rename_updates_description_and_short_name_updates_code(self):
        body = payloads.sram_group(display_name="Research")
        group = self.push(body)
        role_id = group.role_id

        body["displayName"] = "Research Lab"
        body[payloads.SRAM_GROUP_EXTENSION_URN]["urn"] = "uuc:lab"
        group = self.push(body)
        self.assertEqual(group.role_id, role_id)
        self.assertEqual(group.role.description, "Research Lab")
        self.assertTrue(group.role.name.endswith(".SRAM.lab"))

    def test_suspended_member_does_not_hold_the_role(self):
        body = payloads.sram_group(member_ids=[self.roger["id"]])
        group = self.push(body)

        user_body = self.sbs.lookup("Users", self.roger["externalId"])
        suspended = payloads.sram_user(
            username="roger", external_id=self.roger["externalId"], active=False
        )
        # SBS re-sends only the user when it suspends or reinstates one.
        self.sbs.provision("Users", suspended)
        self.assertEqual(self.holders(group), set())
        self.roger_user.refresh_from_db()
        self.assertFalse(self.roger_user.is_active)

        suspended["active"] = True
        self.sbs.provision("Users", suspended)
        self.assertEqual(self.holders(group), {"roger"})
        self.assertTrue(user_body["active"])

    def test_deleted_user_loses_the_role(self):
        group = self.push(payloads.sram_group(member_ids=[self.roger["id"]]))
        self.sbs.delete(self.sbs.lookup("Users", self.roger["externalId"]))
        self.assertEqual(self.holders(group), set())
        self.assertFalse(group.members.exists())

    def test_role_cannot_be_granted_in_another_organization(self):
        group = self.push(payloads.sram_group())
        other = structure_factories.CustomerFactory()
        with self.assertRaises(ValidationError):
            add_user(other, self.roger_user, group.role)

    def test_slug_change_renames_the_role(self):
        group = self.push(payloads.sram_group())
        customer = group.customer
        customer.slug = "utopia"
        customer.save()
        group.role.refresh_from_db()
        self.assertEqual(group.role.name, "CUSTOMER.utopia.SRAM.research")

    def test_same_short_names_in_two_organizations_do_not_clash(self):
        first = self.push(payloads.sram_group(urn="uuc:research"))
        second = self.push(payloads.sram_group(urn="ufra:research"))
        self.assertNotEqual(first.role.name, second.role.name)


class PlaceholderTemplateTest(SramScimTest):
    def setUp(self):
        super().setUp()
        self.template = Role.objects.create(
            name="CUSTOMER.SRAM_MEMBER",
            content_type=ContentType.objects.get_for_model(Customer),
        )
        self.template.add_permission(PermissionEnum.LIST_CUSTOMER_USERS)

    def test_template_permissions_are_copied_and_resynced(self):
        with override_config(SRAM_PLACEHOLDER_ROLE_TEMPLATE="CUSTOMER.SRAM_MEMBER"):
            self.sbs.provision("Groups", payloads.sram_group())
        role = models.SramGroup.objects.get().role
        self.assertEqual(
            list(role.permissions.values_list("permission", flat=True)),
            [PermissionEnum.LIST_CUSTOMER_USERS.value],
        )
        self.assertIsNone(role.template)

        with override_config(SRAM_PLACEHOLDER_ROLE_TEMPLATE=""):
            call_command("sram_resync", stdout=StringIO())
        self.assertEqual(role.permissions.count(), 0)

    @override_config(SRAM_PLACEHOLDER_ROLE_TEMPLATE="CUSTOMER.NOPE")
    def test_unknown_template_is_refused(self):
        response = self.sbs.provision("Groups", payloads.sram_group())
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertFalse(models.SramGroup.objects.exists())
