from io import StringIO

from constance.test.unittest import override_config
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework import status

from waldur_core.core.models import User
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import add_user
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_sram import models, rules
from waldur_sram.tests import payloads
from waldur_sram.tests.base import SramScimTest

RULES_URL = "/api/sram-project-rules/"
GROUPS_URL = "/api/sram-groups/"


class RuleTestBase(SramScimTest):
    def setUp(self):
        super().setUp()
        self.staff = self.service_account
        _, roger = self.provision_user(username="roger")
        _, sarah = self.provision_user(username="sarah")
        self.roger = User.objects.get(uuid=roger["id"])
        self.sarah = User.objects.get(uuid=sarah["id"])
        self.co_body = payloads.sram_group(
            urn="uuc:research",
            labels=["hpc"],
            member_ids=[roger["id"], sarah["id"]],
        )
        self.co = self.push(self.co_body)
        self.customer = self.co.customer
        prefix = f"{self.co.external_id}_"
        self.ws1 = structure_factories.ProjectFactory(
            customer=self.customer, backend_id=f"{prefix}1"
        )
        self.ws2 = structure_factories.ProjectFactory(
            customer=self.customer, backend_id=f"{prefix}2"
        )
        self.unrelated = structure_factories.ProjectFactory(
            customer=self.customer, backend_id="other"
        )
        self.foreign = structure_factories.ProjectFactory(
            customer=structure_factories.CustomerFactory(), backend_id=f"{prefix}9"
        )

    def push(self, body):
        response = self.sbs.provision("Groups", body)
        self.assertIn(response.status_code, (200, 201), response.content)
        return models.SramGroup.objects.select_related("customer", "role").get(
            external_id=body["externalId"]
        )

    def make_rule(self, **kwargs):
        defaults = dict(name="members", project_role=ProjectRole.MEMBER)
        defaults.update(kwargs)
        # Saving reconciles (post_save → on_commit, immediate in tests).
        return models.SramProjectRule.objects.create(**defaults)

    def grants(self, role=None, **filters):
        return set(
            UserRole.objects.filter(
                role=role or ProjectRole.MEMBER,
                is_active=True,
                content_type__model="project",
                **filters,
            ).values_list("user__username", "object_id")
        )

    def expected(self, users, projects):
        return {(u.username, p.id) for u in users for p in projects}


class ProjectSelectionTest(RuleTestBase):
    def test_collaboration_members_get_the_role_on_selected_projects(self):
        rule = self.make_rule()
        self.assertEqual(
            self.grants(), self.expected([self.roger, self.sarah], [self.ws1, self.ws2])
        )
        user_role = UserRole.objects.filter(role=ProjectRole.MEMBER).first()
        self.assertEqual(user_role.source, rules.grant_source(rule, self.co))

    def test_exact_and_regex_and_slug_selectors(self):
        self.ws1.slug = "research-lab"
        self.ws1.save()
        self.make_rule(
            project_field="backend_id",
            project_match="exact",
            project_pattern="{co_external_id}_2",
        )
        self.assertEqual(
            self.grants(), self.expected([self.roger, self.sarah], [self.ws2])
        )

        self.make_rule(
            name="regex",
            project_role=ProjectRole.MANAGER,
            project_match="regex",
            project_pattern=r"^{co_external_id}_[12]$",
        )
        self.assertEqual(
            self.grants(role=ProjectRole.MANAGER),
            self.expected([self.roger, self.sarah], [self.ws1, self.ws2]),
        )

        self.make_rule(
            name="slug",
            project_role=ProjectRole.ADMIN,
            project_field="slug",
            project_match="prefix",
            project_pattern="{co_short_name}-",
        )
        self.assertEqual(
            self.grants(role=ProjectRole.ADMIN),
            self.expected([self.roger, self.sarah], [self.ws1]),
        )

    def test_projects_in_other_organizations_are_never_selected(self):
        self.make_rule(project_match="regex", project_pattern=".*")
        self.assertNotIn(self.foreign.id, {pid for _, pid in self.grants()})
        self.assertIn(self.unrelated.id, {pid for _, pid in self.grants()})

    def test_new_and_changed_projects_are_picked_up(self):
        self.make_rule()
        ws3 = structure_factories.ProjectFactory(
            customer=self.customer, backend_id=f"{self.co.external_id}_3"
        )
        self.assertIn(("roger", ws3.id), self.grants())

        self.ws1.backend_id = "renamed"
        self.ws1.save()
        self.assertNotIn(("roger", self.ws1.id), self.grants())

        self.unrelated.backend_id = f"{self.co.external_id}_4"
        self.unrelated.save()
        self.assertIn(("roger", self.unrelated.id), self.grants())

    def test_co_identifier_placeholder(self):
        identifier = self.co.external_id.split("@")[0]
        ws = structure_factories.ProjectFactory(
            customer=self.customer, backend_id=f"{identifier}-x"
        )
        self.make_rule(project_match="prefix", project_pattern="{co_identifier}-")
        self.assertEqual(self.grants(), self.expected([self.roger, self.sarah], [ws]))


class MembershipTest(RuleTestBase):
    def test_member_removed_from_collaboration_loses_rule_grants(self):
        self.make_rule()
        body = dict(self.co_body)
        body["members"] = [
            m for m in body["members"] if m["value"] == self.sarah.uuid.hex
        ]
        self.push(body)
        self.assertEqual(
            self.grants(), self.expected([self.sarah], [self.ws1, self.ws2])
        )
        revoked = UserRole.objects.get(
            role=ProjectRole.MEMBER, user=self.roger, object_id=self.ws1.id
        )
        self.assertIn("No longer granted by SRAM project rule", revoked.revoke_reason)

    def test_manual_placeholder_grant_and_revocation_follow_through(self):
        self.make_rule()
        outsider = structure_factories.UserFactory()
        user_role = add_user(self.customer, outsider, self.co.role)
        self.assertIn((outsider.username, self.ws1.id), self.grants())

        user_role.revoke()
        self.assertNotIn((outsider.username, self.ws1.id), self.grants())

    def test_existing_project_role_is_not_duplicated_or_revoked(self):
        manual = add_user(self.ws1, self.roger, ProjectRole.MEMBER)
        rule = self.make_rule()
        self.assertEqual(
            UserRole.objects.filter(
                role=ProjectRole.MEMBER, user=self.roger, object_id=self.ws1.id
            ).count(),
            1,
        )
        rule.is_active = False
        rule.save()
        manual.refresh_from_db()
        self.assertTrue(manual.is_active)
        self.assertEqual(self.grants(), {("roger", self.ws1.id)})

    def test_deleting_the_group_revokes_rule_grants(self):
        self.make_rule()
        self.sbs.delete(self.sbs.lookup("Groups", self.co.external_id))
        self.assertEqual(self.grants(), set())

    def test_suspended_member_loses_rule_grants(self):
        self.make_rule()
        body = payloads.sram_user(
            username="roger",
            external_id=self.roger.sram_user.external_id,
            active=False,
        )
        self.sbs.provision("Users", body)
        self.assertEqual(
            self.grants(), self.expected([self.sarah], [self.ws1, self.ws2])
        )

    def test_rejected_grant_is_skipped(self):
        role = ProjectRole.MANAGER
        rule = self.make_rule(project_role=role)
        self.assertTrue(self.grants(role=role))
        role.is_active = False
        role.save()
        UserRole.objects.filter(role=role).update(is_active=False)
        rules.reconcile_rule(rule)
        self.assertEqual(self.grants(role=role), set())


class ConditionTest(RuleTestBase):
    def test_labels(self):
        self.make_rule(labels=["gpu"])
        self.assertEqual(self.grants(), set())

        body = dict(self.co_body)
        body[payloads.SRAM_GROUP_EXTENSION_URN] = dict(
            body[payloads.SRAM_GROUP_EXTENSION_URN], labels=["gpu", "hpc"]
        )
        self.push(body)
        self.assertEqual(
            self.grants(), self.expected([self.roger, self.sarah], [self.ws1, self.ws2])
        )

        body[payloads.SRAM_GROUP_EXTENSION_URN]["labels"] = ["hpc"]
        self.push(body)
        self.assertEqual(self.grants(), set())

    def test_group_short_name_and_collaboration_labels(self):
        admins = self.push(
            payloads.sram_group(
                urn="uuc:research:admins",
                display_name="Admins",
                member_ids=[self.sarah.uuid.hex],
            )
        )
        self.make_rule(
            name="admins",
            project_role=ProjectRole.MANAGER,
            source_kind="group",
            group_short_name_patterns=["adm*"],
            labels=["hpc"],
        )
        self.assertEqual(
            self.grants(role=ProjectRole.MANAGER),
            self.expected([self.sarah], [self.ws1, self.ws2]),
        )
        rule = models.SramProjectRule.objects.get(name="admins")
        grant = UserRole.objects.filter(role=ProjectRole.MANAGER).first()
        self.assertEqual(grant.source, rules.grant_source(rule, admins))

        # The group has no labels of its own; removing the collaboration's
        # label withdraws the grant.
        body = dict(self.co_body)
        body[payloads.SRAM_GROUP_EXTENSION_URN] = dict(
            body[payloads.SRAM_GROUP_EXTENSION_URN], labels=[]
        )
        self.push(body)
        self.assertEqual(self.grants(role=ProjectRole.MANAGER), set())

    def test_group_rule_ignores_collaborations_and_vice_versa(self):
        self.push(
            payloads.sram_group(
                urn="uuc:research:admins", member_ids=[self.sarah.uuid.hex]
            )
        )
        self.make_rule(source_kind="group")
        self.assertEqual(
            self.grants(), self.expected([self.sarah], [self.ws1, self.ws2])
        )

    def test_group_without_known_collaboration_selects_nothing(self):
        self.push(
            payloads.sram_group(
                urn="uuc:unknown:admins", member_ids=[self.sarah.uuid.hex]
            )
        )
        self.make_rule(source_kind="group")
        self.assertEqual(self.grants(), set())


@override_config(SCIM_INBOUND_ENABLED=True, SRAM_INTEGRATION_ENABLED=True)
class RuleApiTest(RuleTestBase):
    def setUp(self):
        super().setUp()
        self.client.credentials()
        self.client.force_authenticate(self.staff)

    def payload(self, **kwargs):
        data = {
            "name": "workspaces",
            "project_role": ProjectRole.MEMBER.uuid.hex,
            "source_kind": "co",
            "project_field": "backend_id",
            "project_match": "prefix",
            "project_pattern": "{co_external_id}_",
        }
        data.update(kwargs)
        return data

    def test_create_applies_retroactively_and_delete_revokes_only_its_grants(self):
        manual = add_user(self.unrelated, self.roger, ProjectRole.MEMBER)
        response = self.client.post(RULES_URL, self.payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["project_role_name"], ProjectRole.MEMBER.name)
        self.assertEqual(
            self.grants() - {("roger", self.unrelated.id)},
            self.expected([self.roger, self.sarah], [self.ws1, self.ws2]),
        )

        response = self.client.delete(response.data["url"])
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.grants(), {("roger", self.unrelated.id)})
        manual.refresh_from_db()
        self.assertTrue(manual.is_active)

    def test_update_reconciles(self):
        url = self.client.post(RULES_URL, self.payload(), format="json").data["url"]
        response = self.client.patch(
            url, {"project_pattern": "{co_external_id}_1"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.grants(), self.expected([self.roger, self.sarah], [self.ws1])
        )

    def test_validation(self):
        for data, field in (
            (self.payload(project_pattern="{nope}_"), "project_pattern"),
            (
                self.payload(project_match="regex", project_pattern="{co_identifier}("),
                "project_pattern",
            ),
            (
                self.payload(group_short_name_patterns=["admins"]),
                "group_short_name_patterns",
            ),
            (self.payload(labels="hpc"), "labels"),
            (
                self.payload(project_role=self.co.role.uuid.hex),
                "project_role",
            ),
        ):
            response = self.client.post(RULES_URL, data, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, data)
            self.assertIn(field, response.data)

    def test_preview(self):
        url = self.client.post(RULES_URL, self.payload(), format="json").data["url"]
        response = self.client.get(url + "preview/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        item = response.data[0]
        self.assertEqual(item["group"]["external_id"], self.co.external_id)
        self.assertEqual(
            sorted(p["backend_id"] for p in item["projects"]),
            sorted([self.ws1.backend_id, self.ws2.backend_id]),
        )
        self.assertEqual(
            sorted(u["username"] for u in item["users"]), ["roger", "sarah"]
        )

    def test_groups_listing(self):
        response = self.client.get(
            GROUPS_URL, {"customer_uuid": self.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        group = response.data[0]
        self.assertEqual(group["role_name"], self.co.role.name)
        self.assertEqual(group["member_count"], 2)
        self.assertEqual(group["customer_name"], self.customer.name)

    def test_non_staff_is_refused(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        self.assertEqual(
            self.client.get(RULES_URL).status_code, status.HTTP_403_FORBIDDEN
        )
        self.assertEqual(
            self.client.get(GROUPS_URL).status_code, status.HTTP_403_FORBIDDEN
        )

    @override_config(SRAM_INTEGRATION_ENABLED=False)
    def test_disabled_integration_hides_the_api(self):
        self.assertEqual(
            self.client.get(RULES_URL).status_code, status.HTTP_404_NOT_FOUND
        )
        self.assertEqual(
            self.client.get(GROUPS_URL).status_code, status.HTTP_404_NOT_FOUND
        )


class ResyncTest(RuleTestBase):
    def test_resync_restores_missing_grants(self):
        self.make_rule()
        UserRole.objects.filter(role=ProjectRole.MEMBER).delete()
        call_command("sram_resync", stdout=StringIO())
        self.assertEqual(
            self.grants(), self.expected([self.roger, self.sarah], [self.ws1, self.ws2])
        )
        self.assertTrue(Customer.objects.filter(pk=self.customer.pk).exists())


class ReviewFixesTest(RuleTestBase):
    def test_changing_the_role_revokes_the_old_one(self):
        rule = self.make_rule(project_role=ProjectRole.ADMIN)
        self.assertTrue(self.grants(role=ProjectRole.ADMIN))
        rule.project_role = ProjectRole.MEMBER
        rule.save()
        self.assertEqual(self.grants(role=ProjectRole.ADMIN), set())
        self.assertEqual(
            self.grants(), self.expected([self.roger, self.sarah], [self.ws1, self.ws2])
        )

    def test_overlapping_rule_takes_over_when_one_is_switched_off(self):
        first = self.make_rule(name="first")
        self.make_rule(name="second")
        expected = self.expected([self.roger, self.sarah], [self.ws1, self.ws2])
        self.assertEqual(self.grants(), expected)
        first.is_active = False
        first.save()
        self.assertEqual(self.grants(), expected)
        second = models.SramProjectRule.objects.get(name="second")
        self.assertTrue(
            UserRole.objects.filter(
                source__startswith=f"sram-rule:{second.uuid.hex}:", is_active=True
            ).exists()
        )

    def test_deleting_one_of_two_overlapping_rules_keeps_access(self):
        first = self.make_rule(name="first")
        self.make_rule(name="second")
        first.delete()
        self.assertEqual(
            self.grants(), self.expected([self.roger, self.sarah], [self.ws1, self.ws2])
        )

    def test_leaving_a_group_keeps_access_granted_through_the_collaboration(self):
        admins_body = payloads.sram_group(
            urn="uuc:research:admins", member_ids=[self.sarah.uuid.hex]
        )
        self.push(admins_body)
        self.make_rule(source_kind="any")
        admins_body["members"] = []
        self.push(admins_body)
        self.assertEqual(
            self.grants(), self.expected([self.roger, self.sarah], [self.ws1, self.ws2])
        )

    def test_regex_the_database_rejects_does_not_break_pushes(self):
        rule = self.make_rule(project_match="regex", project_pattern="(?P<x>a)")
        self.assertEqual(self.grants(), set())
        self.assertIsNotNone(rule.pk)
        self.push(self.co_body)
        self.assertEqual(self.grants(), set())

    def test_failing_selector_leaves_existing_grants_alone(self):
        rule = self.make_rule()
        before = self.grants()
        models.SramProjectRule.objects.filter(pk=rule.pk).update(
            project_match="regex", project_pattern="(?P<x>a)"
        )
        self.push(self.co_body)
        self.assertEqual(self.grants(), before)

    def test_deleting_a_collaboration_revokes_its_groups_rule_grants(self):
        self.push(
            payloads.sram_group(
                urn="uuc:research:admins", member_ids=[self.sarah.uuid.hex]
            )
        )
        self.make_rule(source_kind="group")
        self.assertTrue(self.grants())
        self.sbs.delete(self.sbs.lookup("Groups", self.co.external_id))
        self.assertEqual(self.grants(), set())


class FreezeTest(RuleTestBase):
    def test_nothing_is_maintained_while_sram_is_off(self):
        with override_config(SRAM_INTEGRATION_ENABLED=False):
            rule = self.make_rule()
            structure_factories.ProjectFactory(
                customer=self.customer, backend_id=f"{self.co.external_id}_5"
            )
            add_user(self.customer, structure_factories.UserFactory(), self.co.role)
            self.assertEqual(self.grants(), set())
            with self.assertRaises(CommandError):
                call_command("sram_resync", stdout=StringIO())

        call_command("sram_resync", stdout=StringIO())
        self.assertEqual(len({pid for _, pid in self.grants()}), 3)

        with override_config(SRAM_INTEGRATION_ENABLED=False):
            rule.delete()
            self.assertTrue(self.grants())
        out = StringIO()
        call_command("sram_resync", stdout=out)
        self.assertEqual(self.grants(), set())
        self.assertIn("Revoked", out.getvalue())


@override_config(SCIM_INBOUND_ENABLED=True, SRAM_INTEGRATION_ENABLED=True)
class PatternValidationTest(RuleTestBase):
    def setUp(self):
        super().setUp()
        self.client.credentials()
        self.client.force_authenticate(self.staff)

    def post(self, **kwargs):
        data = {
            "name": "x",
            "project_role": ProjectRole.MEMBER.uuid.hex,
            "project_pattern": "{co_external_id}_",
        }
        data.update(kwargs)
        return self.client.post(RULES_URL, data, format="json")

    def test_attribute_access_in_placeholder_is_a_validation_error(self):
        response = self.post(project_pattern="{co_external_id.foo}")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("project_pattern", response.data)

    def test_regex_the_database_rejects_is_a_validation_error(self):
        response = self.post(project_match="regex", project_pattern="(?P<x>a)")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("database", str(response.data["project_pattern"]))
