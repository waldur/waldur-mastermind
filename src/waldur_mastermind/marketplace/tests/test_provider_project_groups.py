"""One POSIX group per project using a service provider, from a group GID range."""

import tomllib
from unittest import mock

from django.core.management import call_command
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, project_groups, tasks, utils
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    OfferingUserStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.views import _render_glauth_toml

LIST_URL = reverse("marketplace-service-provider-project-group-list")


def detail_url(group, action=None):
    url = reverse(
        "marketplace-service-provider-project-group-detail",
        kwargs={"uuid": group.uuid.hex},
    )
    return url + f"{action}/" if action else url


class ProjectGroupsTestCase(test.APITestCase):
    def setUp(self):
        self.provider = factories.ServiceProviderFactory(
            account_options={"project_groups_enabled": True}
        )
        self.pool = factories.PosixIdPoolFactory(
            service_provider=self.provider,
            min_gid=30000,
            max_gid=30999,
            next_gid=30000,
            min_group_gid=20001,
            max_group_gid=20200,
            next_group_gid=20001,
        )
        self.offering = self.make_offering()
        self.other_offering = self.make_offering()
        self.project = structure_factories.ProjectFactory()
        self.owner = structure_factories.UserFactory()
        self.provider.customer.add_user(self.owner, CustomerRole.OWNER)
        self.staff = structure_factories.UserFactory(is_staff=True)

    def make_offering(self, **kwargs):
        return factories.OfferingFactory(
            customer=self.provider.customer, type=BASIC_OFFERING, **kwargs
        )

    def make_resource(self, offering=None, project=None, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return factories.ResourceFactory(
                offering=offering or self.offering,
                project=project or self.project,
                **kwargs,
            )

    def terminate(self, resource):
        with self.captureOnCommitCallbacks(execute=True):
            resource.state = ResourceStates.TERMINATED
            resource.save()

    def group_of(self, project=None):
        return models.ServiceProviderProjectGroup.objects.get(
            service_provider=self.provider, project=project or self.project
        )

    def adopt(self, user=None, **data):
        # Staff may adopt for any project; owners only for projects related to
        # the provider, which AdoptScopeTest covers.
        self.client.force_authenticate(user or self.staff)
        payload = {
            "service_provider": self.provider.uuid.hex,
            "project": self.project.uuid.hex,
            **data,
        }
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(LIST_URL, payload, format="json")

    def set_gid(self, group, user=None, **data):
        self.client.force_authenticate(user or self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(detail_url(group, "set_gid"), data, format="json")

    def list_groups(self, user=None, **params):
        self.client.force_authenticate(user or self.staff)
        response = self.client.get(LIST_URL, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data


class GroupRangeValidationTest(ProjectGroupsTestCase):
    def update_pool(self, pool, **data):
        self.client.force_authenticate(self.staff)
        return self.client.patch(
            factories.PosixIdPoolFactory.get_url(pool), data, format="json"
        )

    def test_group_range_is_exposed_on_the_pool(self):
        response = self.update_pool(self.pool, max_group_gid=20300)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["min_group_gid"], 20001)
        self.assertEqual(response.data["max_group_gid"], 20300)
        self.assertEqual(response.data["next_group_gid"], 20001)

    def test_group_range_overlapping_the_pools_gid_range_is_refused(self):
        response = self.update_pool(self.pool, min_group_gid=30500, max_group_gid=31500)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.pool.refresh_from_db()
        self.assertEqual(self.pool.min_group_gid, 20001)

    def test_group_range_overlapping_another_pools_gid_range_is_refused(self):
        factories.PosixIdPoolFactory(
            offering=self.other_offering,
            min_uid=None,
            max_uid=None,
            next_uid=None,
            min_gid=40000,
            max_gid=40999,
            next_gid=40000,
        )
        response = self.update_pool(self.pool, min_group_gid=40500, max_group_gid=41000)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_gid_range_overlapping_another_pools_group_range_is_refused(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            reverse("marketplace-posix-id-pool-list"),
            {
                "offering": self.other_offering.uuid.hex,
                "min_gid": 20100,
                "max_gid": 20150,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_shrinking_the_group_range_below_an_allocated_gid_is_refused(self):
        self.make_resource()
        self.assertEqual(self.group_of().gid, 20001)
        response = self.update_pool(self.pool, min_group_gid=20100, max_group_gid=20200)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_group_gids_do_not_block_changes_to_the_gid_range(self):
        self.make_resource()
        response = self.update_pool(self.pool, max_gid=30500)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class GroupCreationTest(ProjectGroupsTestCase):
    def test_first_resource_creates_one_group_named_after_the_slug(self):
        self.make_resource()
        group = self.group_of()
        self.assertEqual(group.name, self.project.slug)
        self.assertEqual(group.gid, 20001)
        identity = models.PosixIdentity.objects.get(
            pool=self.pool, gid=20001, released_at__isnull=True
        )
        self.assertEqual(identity.consumer, group)

    def test_more_resources_on_any_offering_do_not_create_another_group(self):
        self.make_resource()
        self.make_resource()
        self.make_resource(offering=self.other_offering)
        self.assertEqual(
            models.ServiceProviderProjectGroup.objects.filter(
                project=self.project
            ).count(),
            1,
        )
        self.assertEqual(self.group_of().gid, 20001)

    def test_next_project_gets_the_next_gid(self):
        self.make_resource()
        other = structure_factories.ProjectFactory()
        self.make_resource(project=other)
        self.assertEqual(self.group_of(other).gid, 20002)

    def test_without_a_group_range_the_gid_comes_from_the_gid_range(self):
        self.pool.min_group_gid = self.pool.max_group_gid = None
        self.pool.next_group_gid = None
        self.pool.save()
        self.make_resource()
        self.assertEqual(self.group_of().gid, 30000)

    def test_without_a_pool_the_group_has_no_gid(self):
        self.pool.delete()
        self.make_resource()
        self.assertIsNone(self.group_of().gid)

    def test_nothing_is_created_while_the_switch_is_off(self):
        self.provider.account_options = {}
        self.provider.save()
        self.make_resource()
        self.assertFalse(models.ServiceProviderProjectGroup.objects.exists())

    def test_offerings_without_posix_accounts_do_not_count(self):
        offering = self.make_offering(plugin_options={"enable_posix_account": False})
        self.make_resource(offering=offering)
        self.assertFalse(models.ServiceProviderProjectGroup.objects.exists())

    def test_terminating_the_last_resource_keeps_the_group_and_its_gid(self):
        first = self.make_resource()
        second = self.make_resource(offering=self.other_offering)
        self.terminate(first)
        [item] = self.list_groups()
        self.assertTrue(item["in_use"])

        self.terminate(second)
        [item] = self.list_groups()
        self.assertFalse(item["in_use"])
        self.assertEqual(item["offerings"], [])
        self.assertEqual(item["gid"], 20001)

        # Another project does not inherit the unused group's GID.
        other = structure_factories.ProjectFactory()
        self.make_resource(project=other)
        self.assertEqual(self.group_of(other).gid, 20002)

        # The project coming back gets its group and GID back.
        self.make_resource()
        self.assertEqual(self.group_of().gid, 20001)
        self.assertEqual(
            models.ServiceProviderProjectGroup.objects.filter(
                project=self.project
            ).count(),
            1,
        )

    def test_a_slug_change_does_not_rename_the_group(self):
        self.make_resource()
        original = self.group_of().name
        self.project.slug = "renamed-project"
        self.project.save()
        self.make_resource(offering=self.other_offering)
        self.assertEqual(self.group_of().name, original)

    def test_deleting_a_group_never_recycles_its_gid(self):
        self.make_resource()
        self.group_of().delete()
        identity = models.PosixIdentity.objects.get(pool=self.pool, gid=20001)
        self.assertIsNotNone(identity.released_at)
        self.assertFalse(identity.recyclable)


class PinningTest(ProjectGroupsTestCase):
    def test_adopt_pins_a_group_for_a_project_without_resources(self):
        response = self.adopt(gid=20003)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        group = self.group_of()
        self.assertEqual(group.gid, 20003)
        self.assertEqual(response.data["gid"], 20003)
        self.assertFalse(response.data["in_use"])
        identity = models.PosixIdentity.objects.get(
            pool=self.pool, gid=20003, released_at__isnull=True
        )
        self.assertEqual(identity.consumer, group)
        event = Event.objects.get(
            event_type=EventType.MARKETPLACE_PROVIDER_PROJECT_GROUP_GID_UPDATED
        )
        self.assertEqual(event.context["new_gid"], 20003)
        self.assertNotIn("old_gid", event.context)

    def test_adopt_with_a_custom_name(self):
        response = self.adopt(gid=20003, name="legacy_group")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self.group_of().name, "legacy_group")

    def test_a_gid_held_by_another_consumer_is_refused(self):
        other = structure_factories.ProjectFactory()
        self.make_resource(project=other)
        response = self.adopt(gid=20001)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            models.ServiceProviderProjectGroup.objects.filter(
                project=self.project
            ).exists()
        )
        self.assertFalse(
            Event.objects.filter(
                event_type=EventType.MARKETPLACE_PROVIDER_PROJECT_GROUP_GID_UPDATED
            ).exists()
        )

    def test_a_gid_held_as_a_users_primary_gid_is_refused(self):
        user = structure_factories.UserFactory()
        models.PosixIdentity.objects.create(
            pool=self.pool, user=user, uid=None, gid=30010
        )
        response = self.adopt(gid=30010, allow_outside_range=True)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_gid_outside_the_group_range_needs_the_flag(self):
        response = self.adopt(gid=25000)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(models.ServiceProviderProjectGroup.objects.exists())

        response = self.adopt(gid=25000, allow_outside_range=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self.group_of().gid, 25000)

    def test_a_gid_inside_another_pools_range_is_refused(self):
        factories.PosixIdPoolFactory(
            offering=self.other_offering,
            min_uid=None,
            max_uid=None,
            next_uid=None,
            min_gid=40000,
            max_gid=40999,
            next_gid=40000,
        )
        response = self.adopt(gid=40500, allow_outside_range=True)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_allocation_skips_pinned_gids(self):
        for gid in (20001, 20002, 20003):
            project = structure_factories.ProjectFactory()
            self.adopt(project=project.uuid.hex, gid=gid)
        self.make_resource()
        self.assertEqual(self.group_of().gid, 20004)

    def test_a_project_with_a_group_cannot_be_adopted_again(self):
        self.make_resource()
        response = self.adopt(gid=20100)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.group_of().gid, 20001)

    def test_override_moves_the_group_and_never_reuses_the_old_gid(self):
        self.adopt(gid=20005)
        group = self.group_of()
        Event.objects.all().delete()

        response = self.set_gid(group, gid=20150)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        group.refresh_from_db()
        self.assertEqual(group.gid, 20150)

        old = models.PosixIdentity.objects.get(pool=self.pool, gid=20005)
        self.assertIsNotNone(old.released_at)
        self.assertFalse(old.recyclable)
        event = Event.objects.get(
            event_type=EventType.MARKETPLACE_PROVIDER_PROJECT_GROUP_GID_UPDATED
        )
        self.assertEqual(event.context["old_gid"], 20005)
        self.assertEqual(event.context["new_gid"], 20150)

        # The counter walks past the released value.
        gids = []
        for _ in range(5):
            project = structure_factories.ProjectFactory()
            self.make_resource(project=project)
            gids.append(self.group_of(project).gid)
        self.assertEqual(gids, [20001, 20002, 20003, 20004, 20006])

    def test_override_to_a_held_gid_changes_nothing(self):
        self.make_resource()
        other = structure_factories.ProjectFactory()
        self.make_resource(project=other)
        group = self.group_of()
        response = self.set_gid(group, gid=20002)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        group.refresh_from_db()
        self.assertEqual(group.gid, 20001)
        self.assertTrue(
            models.PosixIdentity.objects.filter(
                pool=self.pool, gid=20001, released_at__isnull=True
            ).exists()
        )

    def test_override_outside_the_range_needs_the_flag(self):
        self.make_resource()
        group = self.group_of()
        response = self.set_gid(group, gid=25000)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.set_gid(group, gid=25000, allow_outside_range=True)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_only_owners_pin(self):
        manager = structure_factories.UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)
        response = self.adopt(user=manager, gid=20003)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.make_resource()
        response = self.set_gid(self.group_of(), user=manager, gid=20100)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_import_adopts_several_groups(self):
        projects = [structure_factories.ProjectFactory() for _ in range(3)]
        self.client.force_authenticate(self.staff)
        payload = {
            "service_provider": self.provider.uuid.hex,
            "groups": [
                {"project": project.uuid.hex, "gid": 20001 + index}
                for index, project in enumerate(projects)
            ],
        }
        response = self.client.post(LIST_URL + "import_groups/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            sorted(item["gid"] for item in response.data), [20001, 20002, 20003]
        )

    def test_import_is_all_or_nothing(self):
        projects = [structure_factories.ProjectFactory() for _ in range(2)]
        self.client.force_authenticate(self.staff)
        payload = {
            "service_provider": self.provider.uuid.hex,
            "groups": [
                {"project": projects[0].uuid.hex, "gid": 20010},
                {"project": projects[1].uuid.hex, "gid": 20010},
            ],
        }
        response = self.client.post(LIST_URL + "import_groups/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(models.ServiceProviderProjectGroup.objects.exists())


class ListTest(ProjectGroupsTestCase):
    def add_member(self, username, offering=None, state=OfferingUserStates.OK):
        user = structure_factories.UserFactory()
        self.project.add_user(user, ProjectRole.MEMBER)
        factories.OfferingUserFactory(
            offering=offering or self.offering,
            user=user,
            username=username,
            state=state,
        )
        return user

    def test_item_carries_the_contract_fields(self):
        self.make_resource()
        [item] = self.list_groups()
        group = self.group_of()
        self.assertEqual(str(item["uuid"]), group.uuid.hex)
        self.assertEqual(item["name"], self.project.slug)
        self.assertEqual(item["gid"], 20001)
        self.assertTrue(item["in_use"])
        self.assertEqual(str(item["service_provider_uuid"]), self.provider.uuid.hex)
        self.assertEqual(str(item["project_uuid"]), self.project.uuid.hex)
        self.assertEqual(item["project_name"], self.project.name)
        self.assertEqual(item["project_slug"], self.project.slug)
        self.assertEqual(str(item["customer_uuid"]), self.project.customer.uuid.hex)
        self.assertEqual(item["customer_name"], self.project.customer.name)
        self.assertEqual(item["customer_slug"], self.project.customer.slug)
        self.assertEqual(
            item["offerings"],
            [{"uuid": self.offering.uuid.hex, "name": self.offering.name}],
        )
        self.assertIn("url", item)
        self.assertIn("created", item)
        self.assertIn("modified", item)

    def test_a_group_whose_project_is_gone_has_no_organization(self):
        models.ServiceProviderProjectGroup.objects.create(
            service_provider=self.provider, project=None, name="orphan", gid=20150
        )
        [item] = self.list_groups()
        self.assertIsNone(item["customer_uuid"])
        self.assertIsNone(item["customer_slug"])

    def test_members_are_the_live_provider_accounts_of_project_members(self):
        self.make_resource()
        self.add_member("bob")
        self.add_member("alice", offering=self.other_offering)
        self.add_member("gone", state=OfferingUserStates.DELETED)
        both = self.add_member("carol")
        factories.OfferingUserFactory(
            offering=self.other_offering, user=both, username="carol"
        )
        # An account at the provider of a user outside the project.
        factories.OfferingUserFactory(offering=self.offering, username="stranger")
        [item] = self.list_groups()
        self.assertEqual(item["members"], ["alice", "bob", "carol"])

    def test_a_member_who_leaves_the_project_disappears(self):
        self.make_resource()
        user = self.add_member("bob")
        self.project.remove_user(user)
        [item] = self.list_groups()
        self.assertEqual(item["members"], [])

    def test_filters(self):
        self.make_resource()
        other_project = structure_factories.ProjectFactory()
        resource = self.make_resource(
            project=other_project, offering=self.other_offering
        )
        self.terminate(resource)
        other_provider_group = models.ServiceProviderProjectGroup.objects.create(
            service_provider=factories.ServiceProviderFactory(),
            project=self.project,
            name="elsewhere",
        )

        def names(**params):
            return sorted(item["name"] for item in self.list_groups(**params))

        mine = sorted([self.project.slug, other_project.slug])
        self.assertEqual(names(service_provider_uuid=self.provider.uuid.hex), mine)
        self.assertEqual(names(provider_offering_uuid=self.offering.uuid.hex), mine)
        self.assertEqual(
            names(offering_uuid=self.offering.uuid.hex), [self.project.slug]
        )
        self.assertEqual(names(offering_uuid=self.other_offering.uuid.hex), [])
        self.assertEqual(
            names(
                service_provider_uuid=self.provider.uuid.hex,
                project_uuid=other_project.uuid.hex,
            ),
            [other_project.slug],
        )
        self.assertEqual(
            names(service_provider_uuid=self.provider.uuid.hex, in_use=False),
            [other_project.slug],
        )
        self.assertEqual(
            names(service_provider_uuid=self.provider.uuid.hex, in_use=True),
            [self.project.slug],
        )
        self.assertIn(other_provider_group.name, names())

    def test_soft_deleted_projects_stay_listed_unused(self):
        self.make_resource()
        self.project.delete()
        [item] = self.list_groups(service_provider_uuid=self.provider.uuid.hex)
        self.assertFalse(item["in_use"])

    def test_who_can_read(self):
        self.make_resource()
        offering_manager = structure_factories.UserFactory()
        self.offering.add_user(offering_manager, OfferingRole.MANAGER)
        service_manager = structure_factories.UserFactory()
        self.provider.add_user(service_manager, ServiceProviderRole.MANAGER)
        for user in (self.owner, offering_manager, service_manager, self.staff):
            self.assertEqual(
                len(
                    self.list_groups(
                        user=user, provider_offering_uuid=self.offering.uuid.hex
                    )
                ),
                1,
                user,
            )

        project_member = structure_factories.UserFactory()
        self.project.add_user(project_member, ProjectRole.ADMIN)
        other_manager = structure_factories.UserFactory()
        factories.OfferingFactory(type=BASIC_OFFERING).add_user(
            other_manager, OfferingRole.MANAGER
        )
        stranger = structure_factories.UserFactory()
        for user in (project_member, other_manager, stranger):
            self.assertEqual(self.list_groups(user=user), [], user)


class BackfillTest(ProjectGroupsTestCase):
    def setUp(self):
        super().setUp()
        self.provider.account_options = {}
        self.provider.save()
        self.make_resource()
        self.other_project = structure_factories.ProjectFactory()
        self.make_resource(project=self.other_project)
        self.provider.account_options = {"project_groups_enabled": True}
        self.provider.save()

    def test_dry_run_writes_nothing(self):
        call_command("backfill_provider_project_groups", "--dry-run")
        self.assertFalse(models.ServiceProviderProjectGroup.objects.exists())

    def test_backfill_creates_groups_and_skips_pinned_gids(self):
        self.adopt(project=self.other_project.uuid.hex, gid=20001)
        call_command(
            "backfill_provider_project_groups", provider=self.provider.uuid.hex
        )
        self.assertEqual(self.group_of().gid, 20002)
        self.assertEqual(self.group_of(self.other_project).gid, 20001)

    def test_enabling_the_switch_backfills(self):
        self.provider.account_options = {}
        self.provider.save()
        with mock.patch.object(
            tasks.backfill_provider_project_groups, "delay"
        ) as delay:
            with self.captureOnCommitCallbacks(execute=True):
                self.provider.account_options = {"project_groups_enabled": True}
                self.provider.save()
        delay.assert_called_once_with(self.provider.uuid.hex)

        tasks.backfill_provider_project_groups(self.provider.uuid.hex)
        self.assertEqual(
            models.ServiceProviderProjectGroup.objects.filter(
                service_provider=self.provider
            ).count(),
            2,
        )

    def test_switch_is_set_through_the_provider_api(self):
        self.client.force_authenticate(self.owner)
        response = self.client.patch(
            factories.ServiceProviderFactory.get_url(self.provider),
            {"account_options": {"project_groups_enabled": False}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.provider.refresh_from_db()
        self.assertFalse(self.provider.project_groups_enabled)
        self.assertEqual(
            response.data["account_options"]["project_groups_enabled"], False
        )


class GlauthTest(ProjectGroupsTestCase):
    def test_glauth_renders_the_provider_project_groups(self):
        self.make_resource()
        user = structure_factories.UserFactory()
        self.project.add_user(user, ProjectRole.MEMBER)
        factories.OfferingUserFactory(
            offering=self.offering,
            user=user,
            username="bob",
            state=OfferingUserStates.OK,
            backend_metadata={
                "uidnumber": 100001,
                "primarygroup": 100001,
                "loginShell": "/bin/bash",
                "homeDir": "/home/bob",
            },
        )
        tree = utils.build_glauth_tree(self.offering)
        [group] = [g for g in tree["groups"] if g["kind"] == "provider_project"]
        self.assertEqual(group["gid"], 20001)
        self.assertEqual(group["name"], self.project.slug)
        self.assertEqual(group["members"], ["bob"])
        [entry] = tree["users"]
        self.assertIn(20001, [m["gid"] for m in entry["memberships"]])

        config = tomllib.loads(_render_glauth_toml(self.offering))
        self.assertIn({"name": self.project.slug, "gidnumber": 20001}, config["groups"])
        [record] = [u for u in config["users"] if u["name"] == "bob"]
        self.assertIn(20001, record["otherGroups"])

    def test_offerings_without_a_live_resource_of_the_project_omit_the_group(self):
        self.make_resource()
        tree = utils.build_glauth_tree(self.other_offering)
        self.assertFalse([g for g in tree["groups"] if g["kind"] == "provider_project"])


class DescribeTest(ProjectGroupsTestCase):
    def test_describe_is_empty_for_no_groups(self):
        self.assertEqual(project_groups.describe([]), {})
