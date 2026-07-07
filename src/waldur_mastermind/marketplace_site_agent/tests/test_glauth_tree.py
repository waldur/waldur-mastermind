from django.contrib.contenttypes.models import ContentType
from rest_framework import test

from waldur_core.permissions.models import Role, UserRole
from waldur_mastermind.marketplace import models as marketplace_models
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
            if g["kind"] not in ("project", "personal")
            and self.fixture.manager.username in g["members"]
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
        # Only the legacy project-mapped groups and per-user personal groups
        # remain — no role groups.
        self.assertEqual(kinds, {"project", "personal"})

    def test_personal_groups_surface_in_tree(self):
        # Each user's personal group (name = username, gid = primarygroup) is
        # served by glauth as a posixGroup, so the tree must list it too.
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.get(self.url)
        offering_user = self.fixture.offering_user
        expected_gid = offering_user.backend_metadata["primarygroup"]
        personal = [g for g in response.data["groups"] if g["kind"] == "personal"]
        by_name = {g["name"]: g for g in personal}
        self.assertIn(offering_user.username, by_name)
        group = by_name[offering_user.username]
        self.assertEqual(group["gid"], expected_gid)
        self.assertIsNone(group["role"])
        self.assertEqual(group["members"], [offering_user.username])
        self.assertEqual(group["scope"]["type"], "user")
        self.assertEqual(group["scope"]["uuid"], self.fixture.manager.uuid.hex)

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

    def test_personal_group_not_double_emitted(self):
        # The personal group is emitted by the per-user record path; the tree
        # also carries it, so guard against the TOML writing it twice.
        self.client.force_login(self.fixture.offering_owner)
        body = self.client.get(self.toml_url).data
        personal_gid = self.fixture.offering_user.backend_metadata["primarygroup"]
        self.assertEqual(body.count(f"gidnumber = {personal_gid}\n"), 1)

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
