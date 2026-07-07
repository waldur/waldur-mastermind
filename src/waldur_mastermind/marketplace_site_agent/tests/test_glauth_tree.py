from django.contrib.contenttypes.models import ContentType
from rest_framework import test

from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.permissions.models import Role, UserRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_site_agent.tests.fixtures import GlauthUserFixture


class GlauthTreeFixture(GlauthUserFixture):
    """Augment GlauthUserFixture with offering-custom roles + assignments."""

    def __init__(self, *, configure_role_maps=True):
        super().__init__()
        self.resource_ct = ContentType.objects.get_for_model(
            marketplace_models.Resource
        )
        self.rp_ct = ContentType.objects.get_for_model(
            marketplace_models.ResourceProject
        )

        self.resource_role = Role.objects.create(
            name="ClusterAdmin",
            content_type=self.resource_ct,
            is_system_role=False,
        )
        self.rp_role = Role.objects.create(
            name="ProjectMember",
            content_type=self.rp_ct,
            is_system_role=False,
        )

        # Resource-scope role grant.
        UserRole.objects.create(
            user=self.manager,
            role=self.resource_role,
            content_type=self.resource_ct,
            object_id=self.resource.id,
        )

        # Sub-project + role grant.
        self.rp_a = marketplace_models.ResourceProject.objects.create(
            resource=self.resource, name="team-x"
        )
        UserRole.objects.create(
            user=self.manager,
            role=self.rp_role,
            content_type=self.rp_ct,
            object_id=self.rp_a.id,
        )

        if configure_role_maps:
            self.offering.plugin_options = {
                **self.offering.plugin_options,
                "resource_role_map": {"ClusterAdmin": "admin"},
                "resource_project_role_map": {"ProjectMember": "member"},
                "resource_role_group_template": "${resource_slug}_${role_name}",
                "resource_project_role_group_template": (
                    "${resource_slug}_${rp_uuid_short}_${role_name}"
                ),
            }
            # The offering already has a pool (from the parent fixture). Push its
            # GID high-water mark up so the lazily-allocated role groups land in a
            # distinct high band, above the seeded project-group GIDs.
            pool = self.offering.posix_pool
            pool.max_gid = 159999
            pool.next_gid = 60000
            pool.save(update_fields=["max_gid", "next_gid"])
            self.offering.save()


class GlauthTreeOfferingEndpointTest(test.APITestCase):
    def setUp(self):
        self.fixture = GlauthTreeFixture()
        self.url = marketplace_factories.OfferingFactory.get_url(
            self.fixture.offering, "glauth_tree"
        )

    def test_endpoint_requires_offering_manager(self):
        # Random consumer-side user must not see the integration tree.
        self.client.force_login(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404)

    def test_endpoint_stamps_integration_status(self):
        self.client.force_login(self.fixture.offering_owner)
        self.assertEqual(
            0,
            marketplace_models.IntegrationStatus.objects.filter(
                offering=self.fixture.offering,
                agent_type=marketplace_models.IntegrationStatus.AgentTypes.GLAUTH_SYNC,
            ).count(),
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        status_row = marketplace_models.IntegrationStatus.objects.get(
            offering=self.fixture.offering,
            agent_type=marketplace_models.IntegrationStatus.AgentTypes.GLAUTH_SYNC,
        )
        self.assertIsNotNone(status_row.last_request_timestamp)
        self.assertEqual(
            status_row.status,
            marketplace_models.IntegrationStatus.States.ACTIVE,
        )

    def test_tree_contains_resource_and_rp_role_groups(self):
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        tree = response.data

        # Project-mapped groups (gid 6001, 6002) still present.
        project_groups = [g for g in tree["groups"] if g["kind"] == "project"]
        self.assertEqual(sorted(g["gid"] for g in project_groups), [6001, 6002])

        # One resource-scope role group.
        resource_groups = [g for g in tree["groups"] if g["kind"] == "resource_role"]
        self.assertEqual(len(resource_groups), 1)
        self.assertEqual(resource_groups[0]["role"], "ClusterAdmin")
        self.assertGreaterEqual(resource_groups[0]["gid"], 60000)
        self.assertIn(self.fixture.manager.username, resource_groups[0]["members"])
        self.assertEqual(resource_groups[0]["scope"]["type"], "resource")
        self.assertEqual(
            resource_groups[0]["scope"]["uuid"], self.fixture.resource.uuid.hex
        )

        # One resource-project-scope role group.
        rp_groups = [g for g in tree["groups"] if g["kind"] == "resource_project_role"]
        self.assertEqual(len(rp_groups), 1)
        self.assertEqual(rp_groups[0]["role"], "ProjectMember")
        self.assertGreaterEqual(rp_groups[0]["gid"], 60000)
        self.assertEqual(rp_groups[0]["scope"]["type"], "resource_project")
        self.assertEqual(rp_groups[0]["scope"]["uuid"], self.fixture.rp_a.uuid.hex)

    def test_user_memberships_link_to_role_groups(self):
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.get(self.url)
        users_by_name = {u["username"]: u for u in response.data["users"]}
        manager_user = users_by_name[self.fixture.offering_user.username]
        membership_gids = {m["gid"] for m in manager_user["memberships"]}
        role_group_gids = {
            g["gid"]
            for g in response.data["groups"]
            if g["kind"] != "project" and self.fixture.manager.username in g["members"]
        }
        self.assertTrue(role_group_gids.issubset(membership_gids))

    def test_gid_is_stable_across_regenerations(self):
        self.client.force_login(self.fixture.offering_owner)
        first = self.client.get(self.url).data
        second = self.client.get(self.url).data
        first_gids = {
            (g["kind"], g["scope"]["uuid"], g["role"]): g["gid"]
            for g in first["groups"]
            if g["kind"] != "project"
        }
        second_gids = {
            (g["kind"], g["scope"]["uuid"], g["role"]): g["gid"]
            for g in second["groups"]
            if g["kind"] != "project"
        }
        self.assertEqual(first_gids, second_gids)

    def test_role_groups_empty_when_role_map_not_configured(self):
        # Wipe the role maps the fixture set up.
        offering = self.fixture.offering
        offering.plugin_options = {
            **offering.plugin_options,
            "resource_role_map": {},
            "resource_project_role_map": {},
        }
        offering.save()
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.get(self.url)
        kinds = {g["kind"] for g in response.data["groups"]}
        # Only the legacy project-mapped groups remain.
        self.assertEqual(kinds, {"project"})

    def test_role_outside_role_map_is_skipped(self):
        # Add another role and assignment, but don't include it in the map.
        unmapped = Role.objects.create(
            name="Auditor",
            content_type=self.fixture.resource_ct,
            is_system_role=False,
        )
        UserRole.objects.create(
            user=self.fixture.manager,
            role=unmapped,
            content_type=self.fixture.resource_ct,
            object_id=self.fixture.resource.id,
        )
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.get(self.url)
        resource_roles = [
            g["role"] for g in response.data["groups"] if g["kind"] == "resource_role"
        ]
        self.assertNotIn("Auditor", resource_roles)
        self.assertIn("ClusterAdmin", resource_roles)


class GlauthTreeResourceEndpointTest(test.APITestCase):
    """The same JSON, scoped to one resource."""

    def setUp(self):
        self.fixture = GlauthTreeFixture()
        # The action is registered on ConsumerResourceViewSet but inherited by
        # ProviderResourceViewSet; the offering side is the natural caller, so
        # we test via the provider URL.
        self.url = marketplace_factories.ResourceFactory.get_provider_resource_url(
            self.fixture.resource, "glauth_tree"
        )

    def test_endpoint_returns_tree_scoped_to_resource(self):
        self.client.force_login(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        tree = response.data
        # The single resource should be in every non-project group.
        for g in tree["groups"]:
            if g["kind"] in ("resource_role", "resource_project_role"):
                if g["kind"] == "resource_role":
                    self.assertEqual(g["scope"]["uuid"], self.fixture.resource.uuid.hex)
                else:
                    self.assertEqual(
                        g["scope"]["resource_uuid"], self.fixture.resource.uuid.hex
                    )


class GlauthRoleAwareTomlTest(test.APITestCase):
    """The TOML endpoint emits the same role groups + per-user otherGroups."""

    def setUp(self):
        self.fixture = GlauthTreeFixture()
        self.toml_url = marketplace_factories.OfferingFactory.get_url(
            self.fixture.offering, "glauth_users_config"
        )

    def test_role_groups_appear_in_toml(self):
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.get(self.toml_url)
        self.assertEqual(response.status_code, 200)
        body = response.data
        # The role-group [[groups]] block + name template appears.
        self.assertIn(f'name = "{self.fixture.resource.slug}_admin"', body)
        rp_uuid_short = self.fixture.rp_a.uuid.hex[:8]
        self.assertIn(
            f'name = "{self.fixture.resource.slug}_{rp_uuid_short}_member"',
            body,
        )

    def test_user_otherGroups_includes_role_gids(self):
        self.client.force_login(self.fixture.offering_owner)
        body = self.client.get(self.toml_url).data
        # Pull manager's [[users]] block.
        import re

        manager_block = re.search(
            r"\[\[users\]\][^\[]+name = \""
            + re.escape(self.fixture.manager.username)
            + r"\"[^\[]+otherGroups = \[(?P<gids>[^\]]*)\]",
            body,
            re.DOTALL,
        )
        self.assertIsNotNone(manager_block, body)
        gids = {
            int(s.strip()) for s in manager_block.group("gids").split(",") if s.strip()
        }
        # The project gids (6001) and at least two role gids (>= 60000) should be there.
        self.assertIn(6001, gids)
        role_gids = {g for g in gids if g >= 60000}
        self.assertEqual(len(role_gids), 2)

    def test_no_role_groups_when_role_map_empty(self):
        offering = self.fixture.offering
        offering.plugin_options = {
            **offering.plugin_options,
            "resource_role_map": {},
            "resource_project_role_map": {},
        }
        offering.save()
        self.client.force_login(self.fixture.offering_owner)
        body = self.client.get(self.toml_url).data
        self.assertNotIn('_admin"', body)
        self.assertNotIn('_member"', body)


class GlauthMultiUserUidGidPropagationTest(test.APITestCase):
    """uid/gid propagation for several team members sharing role assignments.

    Two users are granted the same resource-scope role and the same
    resource-project-scope role. Each must receive its own distinct POSIX uid and
    personal-group gid (allocated from the offering pool), while the role groups
    — keyed per (offering, scope, role), not per user — must be shared: one gid,
    both usernames as members, and both users carrying that gid in their
    ``otherGroups`` membership rollup.
    """

    def setUp(self):
        self.fixture = GlauthTreeFixture()
        self.offering = self.fixture.offering
        self.url = marketplace_factories.OfferingFactory.get_url(
            self.offering, "glauth_tree"
        )

        # Second team member: real project membership, then the same resource +
        # resource-project role grants the manager already holds.
        self.user2 = structure_factories.UserFactory()
        self.fixture.project.add_user(self.user2, ProjectRole.MEMBER)
        self.offering_user2, _ = marketplace_models.OfferingUser.objects.get_or_create(
            offering=self.offering,
            user=self.user2,
            defaults={"username": self.user2.username},
        )
        marketplace_utils.setup_linux_related_data(self.offering_user2, self.offering)
        self.offering_user2.save()

        self.user2_resource_role = UserRole.objects.create(
            user=self.user2,
            role=self.fixture.resource_role,
            content_type=self.fixture.resource_ct,
            object_id=self.fixture.resource.id,
        )
        UserRole.objects.create(
            user=self.user2,
            role=self.fixture.rp_role,
            content_type=self.fixture.rp_ct,
            object_id=self.fixture.rp_a.id,
        )

    def _tree(self):
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_distinct_uid_and_personal_gid_per_user(self):
        users = {u["username"]: u for u in self._tree()["users"]}
        self.assertIn(self.fixture.manager.username, users)
        self.assertIn(self.user2.username, users)
        m = users[self.fixture.manager.username]
        u2 = users[self.user2.username]
        # UIDs are allocated sequentially from the offering pool (uid namespace
        # untouched by the fixture): manager 1001, second member 1002.
        self.assertEqual({m["uidnumber"], u2["uidnumber"]}, {1001, 1002})
        # Personal groups draw from the same offering GID pool as the role groups
        # and must stay distinct per user. The manager's group was allocated at
        # the pool's gid_start (2001) before the fixture pushed the GID
        # high-water mark up to seed role groups; the second member's is drawn
        # from wherever the pool pointer sits when its account is created.
        self.assertEqual(m["personal_group"], 2001)
        self.assertNotEqual(m["personal_group"], u2["personal_group"])

    def test_role_group_gid_is_shared_across_members(self):
        role_groups = [g for g in self._tree()["groups"] if g["kind"] != "project"]
        # One resource-role group and one resource-project-role group.
        self.assertEqual(
            {g["kind"] for g in role_groups},
            {"resource_role", "resource_project_role"},
        )
        for g in role_groups:
            # Both team members belong to the single shared role group.
            self.assertIn(self.fixture.manager.username, g["members"])
            self.assertIn(self.user2.username, g["members"])
            self.assertGreaterEqual(g["gid"], 60000)

    def test_both_users_carry_role_gids_in_memberships(self):
        tree = self._tree()
        users = {u["username"]: u for u in tree["users"]}
        role_gids = {g["gid"] for g in tree["groups"] if g["kind"] != "project"}
        self.assertEqual(len(role_gids), 2)
        for username in (self.fixture.manager.username, self.user2.username):
            membership_gids = {m["gid"] for m in users[username]["memberships"]}
            self.assertTrue(
                role_gids.issubset(membership_gids),
                f"{username} missing role gids: {role_gids - membership_gids}",
            )

    def test_role_revocation_shrinks_membership_but_keeps_gid_stable(self):
        before = self._tree()
        resource_group_before = next(
            g for g in before["groups"] if g["kind"] == "resource_role"
        )
        gid_before = resource_group_before["gid"]
        self.assertIn(self.user2.username, resource_group_before["members"])

        # Real revocation: deactivate only user2's resource-scope role.
        self.user2_resource_role.revoke()

        after = self._tree()
        resource_group_after = next(
            g for g in after["groups"] if g["kind"] == "resource_role"
        )
        # The group survives (manager still holds the role) with a stable gid.
        self.assertEqual(resource_group_after["gid"], gid_before)
        self.assertIn(self.fixture.manager.username, resource_group_after["members"])
        self.assertNotIn(self.user2.username, resource_group_after["members"])

        # user2 drops the resource-role gid but keeps the resource-project-role
        # gid it still holds.
        users_after = {u["username"]: u for u in after["users"]}
        u2_gids = {m["gid"] for m in users_after[self.user2.username]["memberships"]}
        self.assertNotIn(gid_before, u2_gids)
        rp_group = next(
            g for g in after["groups"] if g["kind"] == "resource_project_role"
        )
        self.assertIn(rp_group["gid"], u2_gids)
