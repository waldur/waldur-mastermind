"""Provider project group scenarios agreed for the directory writer.

Each class follows one section of the scenario list: several resources in one
project, different projects, termination, membership, idempotence, then the
onboarding and review scenarios.
"""

import datetime
from unittest import mock

from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone
from rest_framework import status
from rest_framework.reverse import reverse

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.permissions.models import UserRole
from waldur_core.structure import utils as structure_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import (
    models,
    posix_ids,
    project_groups,
    tasks,
    utils,
)
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    OPENSTACK_INSTANCE_OFFERING,
    OfferingUserStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.test_provider_project_groups import (
    LIST_URL,
    ProjectGroupsTestCase,
)
from waldur_mastermind.marketplace.views import _render_glauth_toml

POOL_LIST_URL = reverse("marketplace-posix-id-pool-list")


def run_backfill_inline():
    """Run the backfill task where the code queues it."""
    return mock.patch.object(
        tasks.backfill_provider_project_groups,
        "delay",
        side_effect=lambda uuid: project_groups.backfill(
            models.ServiceProvider.objects.get(uuid=uuid)
        ),
    )


class ScenarioTestCase(ProjectGroupsTestCase):
    def item(self, project=None, **params):
        params.setdefault("service_provider_uuid", self.provider.uuid.hex)
        params.setdefault("project_uuid", (project or self.project).uuid.hex)
        items = self.list_groups(**params)
        self.assertEqual(len(items), 1, items)
        return items[0]

    def offering_names(self, item):
        return sorted(o["name"] for o in item["offerings"])

    def update_pool(self, **data):
        self.client.force_authenticate(self.staff)
        with self.captureOnCommitCallbacks(execute=True), run_backfill_inline():
            return self.client.patch(
                factories.PosixIdPoolFactory.get_url(self.pool), data, format="json"
            )

    def set_switch(self, enabled):
        with self.captureOnCommitCallbacks(execute=True), run_backfill_inline():
            self.provider.account_options = (
                {"project_groups_enabled": True} if enabled else {}
            )
            self.provider.save()

    def add_member(self, username, project=None, offering=None, **kwargs):
        user = structure_factories.UserFactory()
        (project or self.project).add_user(user, ProjectRole.MEMBER)
        kwargs.setdefault("state", OfferingUserStates.OK)
        factories.OfferingUserFactory(
            offering=offering or self.offering, user=user, username=username, **kwargs
        )
        return user


class SeveralResourcesInOneProjectTest(ScenarioTestCase):
    def test_two_resources_of_one_offering(self):
        self.make_resource()
        self.make_resource()
        item = self.item()
        self.assertEqual(item["gid"], 20001)
        self.assertEqual(self.offering_names(item), [self.offering.name])

    def test_resources_on_two_offerings(self):
        self.make_resource()
        self.make_resource(offering=self.other_offering)
        item = self.item()
        self.assertEqual(item["gid"], 20001)
        self.assertEqual(
            self.offering_names(item),
            sorted([self.offering.name, self.other_offering.name]),
        )
        self.assertEqual(models.ServiceProviderProjectGroup.objects.count(), 1)

    def test_terminating_one_of_two_resources_of_one_offering(self):
        first = self.make_resource()
        self.make_resource()
        self.terminate(first)
        item = self.item()
        self.assertTrue(item["in_use"])
        self.assertEqual(self.offering_names(item), [self.offering.name])
        self.assertEqual(item["gid"], 20001)

    def test_terminating_the_resource_on_one_of_two_offerings(self):
        first = self.make_resource()
        self.make_resource(offering=self.other_offering)
        self.terminate(first)
        item = self.item()
        self.assertTrue(item["in_use"])
        self.assertEqual(self.offering_names(item), [self.other_offering.name])
        self.assertEqual(self.list_groups(offering_uuid=self.offering.uuid.hex), [])
        self.assertEqual(
            len(self.list_groups(offering_uuid=self.other_offering.uuid.hex)), 1
        )


class DifferentProjectsTest(ScenarioTestCase):
    def test_projects_of_one_and_of_different_customers_skip_pinned_gids(self):
        for gid in (20001, 20002, 20003):
            self.adopt(project=structure_factories.ProjectFactory().uuid.hex, gid=gid)
        same_customer = structure_factories.ProjectFactory(
            customer=self.project.customer
        )
        self.make_resource()
        self.make_resource(project=same_customer)
        other_customer = structure_factories.ProjectFactory()
        self.make_resource(project=other_customer)
        self.assertEqual(self.group_of().gid, 20004)
        self.assertEqual(self.group_of(same_customer).gid, 20005)
        self.assertEqual(self.group_of(other_customer).gid, 20006)

    def test_slug_collision_is_suffixed_deterministically(self):
        # Slugs are generated unique but are editable and not constrained.
        first = structure_factories.ProjectFactory()
        second = structure_factories.ProjectFactory()
        third = structure_factories.ProjectFactory()
        for project in (first, second, third):
            project.slug = "Shared-Name"
            project.save()
            self.make_resource(project=project)
        self.assertEqual(
            [self.group_of(p).name for p in (first, second, third)],
            ["shared-name", "shared-name-2", "shared-name-3"],
        )

    def test_collision_with_an_adopted_name_is_case_insensitive(self):
        self.adopt(
            project=structure_factories.ProjectFactory().uuid.hex,
            gid=20001,
            name="alpha",
        )
        self.project.slug = "ALPHA"
        self.project.save()
        self.make_resource()
        self.assertEqual(self.group_of().name, "alpha-2")

    def test_projects_of_another_provider_never_appear(self):
        other_provider = factories.ServiceProviderFactory(
            account_options={"project_groups_enabled": True}
        )
        factories.PosixIdPoolFactory(service_provider=other_provider)
        other_offering = factories.OfferingFactory(
            customer=other_provider.customer, type=BASIC_OFFERING
        )
        project = structure_factories.ProjectFactory()
        self.make_resource(offering=other_offering, project=project)
        self.assertFalse(
            models.ServiceProviderProjectGroup.objects.filter(
                service_provider=self.provider
            ).exists()
        )
        self.assertEqual(
            self.list_groups(service_provider_uuid=self.provider.uuid.hex), []
        )
        self.assertEqual(
            self.list_groups(provider_offering_uuid=self.offering.uuid.hex), []
        )

    def test_offering_without_posix_accounts_gives_no_group(self):
        offering = self.make_offering(plugin_options={"enable_posix_account": False})
        self.make_resource(offering=offering)
        self.assertFalse(models.ServiceProviderProjectGroup.objects.exists())

    def test_provider_without_a_pool_gives_a_group_without_gid(self):
        self.pool.delete()
        self.make_resource()
        item = self.item()
        self.assertIsNone(item["gid"])
        self.assertTrue(item["in_use"])


class TerminationTest(ScenarioTestCase):
    def test_last_resource_terminated_then_new_resource(self):
        resource = self.make_resource()
        self.terminate(resource)
        item = self.item()
        self.assertFalse(item["in_use"])
        self.assertEqual(item["offerings"], [])
        self.assertEqual(item["gid"], 20001)

        other = structure_factories.ProjectFactory()
        self.make_resource(project=other)
        self.assertEqual(self.group_of(other).gid, 20002)

        self.make_resource()
        item = self.item()
        self.assertTrue(item["in_use"])
        self.assertEqual(item["gid"], 20001)

    def test_soft_deleted_project_keeps_its_group_and_gid(self):
        self.make_resource()
        self.project.delete()
        item = self.item(project=self.project)
        self.assertFalse(item["in_use"])
        self.assertEqual(item["offerings"], [])
        self.assertEqual(item["gid"], 20001)
        other = structure_factories.ProjectFactory()
        self.make_resource(project=other)
        self.assertEqual(self.group_of(other).gid, 20002)

    def test_hard_deleted_project_leaves_its_group_behind(self):
        self.make_resource()
        group = self.group_of()
        models.Resource.objects.filter(project=self.project).delete()
        models.Order.objects.filter(project=self.project).delete()
        type(self.project).objects.filter(pk=self.project.pk).delete()
        group.refresh_from_db()
        self.assertIsNone(group.project)
        [item] = self.list_groups(service_provider_uuid=self.provider.uuid.hex)
        self.assertEqual(item["gid"], 20001)
        self.assertIsNone(item["project_uuid"])
        self.assertFalse(item["in_use"])
        self.assertEqual(item["members"], [])

    def test_transitional_states_count_as_in_use(self):
        resource = self.make_resource()
        for state in (
            ResourceStates.CREATING,
            ResourceStates.OK,
            ResourceStates.UPDATING,
            ResourceStates.TERMINATING,
            ResourceStates.ERRED,
        ):
            models.Resource.objects.filter(pk=resource.pk).update(state=state)
            item = self.item()
            self.assertTrue(item["in_use"], state)
            self.assertEqual(
                len(self.list_groups(offering_uuid=self.offering.uuid.hex)), 1
            )
        self.terminate(models.Resource.objects.get(pk=resource.pk))
        self.assertFalse(self.item()["in_use"])


class MembershipTest(ScenarioTestCase):
    def setUp(self):
        super().setUp()
        self.make_resource()

    def members(self):
        return self.item()["members"]

    def test_member_added_and_removed(self):
        user = self.add_member("bob")
        self.assertEqual(self.members(), ["bob"])
        self.project.remove_user(user)
        self.assertEqual(self.members(), [])

    def test_expired_role_drops_the_member(self):
        user = self.add_member("bob")
        UserRole.objects.filter(user=user).update(
            expiration_time=timezone.now() - datetime.timedelta(minutes=1)
        )
        self.assertEqual(self.members(), [])

    def test_member_without_an_account_is_not_listed(self):
        user = structure_factories.UserFactory()
        self.project.add_user(user, ProjectRole.MEMBER)
        factories.OfferingUserFactory(offering=self.offering, user=user, username="")
        self.assertEqual(self.members(), [])

    def test_username_change_shows_the_new_name(self):
        user = self.add_member("bob")
        models.OfferingUser.objects.filter(user=user).update(username="robert")
        self.assertEqual(self.members(), ["robert"])

    def test_accounts_leaving_the_live_set(self):
        deleting = self.add_member("deleting")
        models.OfferingUser.objects.filter(user=deleting).update(
            state=OfferingUserStates.DELETION_REQUESTED
        )
        restricted = self.add_member("restricted", is_restricted=True)
        partly = self.add_member("partly", is_restricted=True)
        factories.OfferingUserFactory(
            offering=self.other_offering,
            user=partly,
            username="partly",
            state=OfferingUserStates.OK,
        )
        inactive = self.add_member("inactive")
        inactive.is_active = False
        inactive.save()
        self.add_member("requested", state=OfferingUserStates.CREATION_REQUESTED)
        self.add_member("erred", state=OfferingUserStates.ERROR_CREATING)
        self.assertEqual(self.members(), ["erred", "partly", "requested"])
        self.assertTrue(restricted)

    def test_project_moved_to_another_customer(self):
        self.add_member("bob")
        before = self.item()
        structure_utils.move_project(
            self.project,
            structure_factories.CustomerFactory(),
            preserve_permissions=True,
        )
        after = self.item()
        for key in ("name", "gid", "in_use", "members"):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(after["customer_uuid"], self.project.customer.uuid.hex)

        structure_utils.move_project(
            self.project, structure_factories.CustomerFactory()
        )
        after = self.item()
        self.assertEqual((after["name"], after["gid"]), (before["name"], before["gid"]))
        self.assertEqual(after["members"], [])


class IdempotenceTest(ScenarioTestCase):
    def test_backfill_twice_changes_nothing(self):
        self.provider.account_options = {}
        self.provider.save()
        self.make_resource()
        self.make_resource(project=structure_factories.ProjectFactory())
        self.provider.account_options = {"project_groups_enabled": True}
        self.provider.save()
        self.assertEqual(len(project_groups.backfill(self.provider)), 2)
        snapshot = list(
            models.ServiceProviderProjectGroup.objects.values_list(
                "name", "gid", "modified"
            )
        )
        identities = models.PosixIdentity.objects.count()
        self.assertEqual(project_groups.backfill(self.provider), [])
        self.assertEqual(
            list(
                models.ServiceProviderProjectGroup.objects.values_list(
                    "name", "gid", "modified"
                )
            ),
            snapshot,
        )
        self.assertEqual(models.PosixIdentity.objects.count(), identities)


class OnboardingTest(ScenarioTestCase):
    """Adopt the directory's existing groups, then enable."""

    def setUp(self):
        super().setUp()
        self.provider.account_options = {}
        self.provider.save()
        self.projects = [structure_factories.ProjectFactory() for _ in range(5)]
        for project in self.projects:
            self.make_resource(project=project, state=ResourceStates.OK)

    def test_adopt_then_enable(self):
        for project, name, gid in zip(
            self.projects[:3], ("alpha", "beta", "gamma"), (20001, 20002, 20003)
        ):
            response = self.adopt(project=project.uuid.hex, gid=gid, name=name)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            Event.objects.filter(
                event_type=EventType.MARKETPLACE_PROVIDER_PROJECT_GROUP_GID_UPDATED
            ).count(),
            3,
        )
        self.set_switch(True)
        gids = [self.group_of(p).gid for p in self.projects]
        self.assertEqual(gids, [20001, 20002, 20003, 20004, 20005])
        self.assertEqual(
            models.PosixIdentity.objects.filter(
                pool=self.pool, released_at__isnull=True, gid__gte=20001
            ).count(),
            5,
        )
        self.assertEqual(project_groups.backfill(self.provider), [])

    def test_enable_first_then_fix_with_set_gid(self):
        self.set_switch(True)
        group = self.group_of(self.projects[0])
        self.assertEqual(group.gid, 20001)
        response = self.set_gid(group, gid=20150)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        project = structure_factories.ProjectFactory()
        self.make_resource(project=project)
        self.assertEqual(self.group_of(project).gid, 20006)
        self.assertFalse(
            models.ServiceProviderProjectGroup.objects.filter(gid=20001).exists()
        )


class PoolAndSwitchOrderTest(ScenarioTestCase):
    def test_switch_on_without_group_range_then_range_added(self):
        self.pool.min_group_gid = self.pool.max_group_gid = None
        self.pool.next_group_gid = None
        self.pool.save()
        self.make_resource()
        self.assertEqual(self.group_of().gid, 30000)
        response = self.update_pool(min_group_gid=20001, max_group_gid=20200)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.group_of().gid, 30000)
        project = structure_factories.ProjectFactory()
        self.make_resource(project=project)
        self.assertEqual(self.group_of(project).gid, 20001)

    def test_pool_without_gids_then_range_added(self):
        self.pool.min_gid = self.pool.max_gid = self.pool.next_gid = None
        self.pool.min_group_gid = self.pool.max_group_gid = None
        self.pool.next_group_gid = None
        self.pool.save()
        self.make_resource()
        self.assertIsNone(self.group_of().gid)
        response = self.update_pool(min_group_gid=20001, max_group_gid=20200)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.group_of().gid, 20001)

    def test_pool_created_later_numbers_waiting_groups(self):
        self.pool.delete()
        self.make_resource()
        self.client.force_authenticate(self.staff)
        with self.captureOnCommitCallbacks(execute=True), run_backfill_inline():
            response = self.client.post(
                POOL_LIST_URL,
                {
                    "service_provider": self.provider.uuid.hex,
                    "min_gid": 30000,
                    "max_gid": 30999,
                    "min_group_gid": 20001,
                    "max_group_gid": 20200,
                },
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self.group_of().gid, 20001)

    def test_switch_preview_warns_without_a_group_range(self):
        self.pool.min_group_gid = self.pool.max_group_gid = None
        self.pool.next_group_gid = None
        self.pool.save()
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            factories.ServiceProviderFactory.get_url(
                self.provider, "account_options_preview"
            ),
            {"account_options": {"project_groups_enabled": True}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data["warnings"]), 1)

    def test_switch_off_and_on(self):
        self.make_resource()
        self.set_switch(False)
        missed = structure_factories.ProjectFactory()
        self.make_resource(project=missed)
        self.assertFalse(
            models.ServiceProviderProjectGroup.objects.filter(project=missed).exists()
        )
        self.assertEqual(len(self.list_groups()), 1)
        self.set_switch(True)
        self.assertEqual(self.group_of(missed).gid, 20002)


class ExhaustedRangeTest(ScenarioTestCase):
    def test_full_group_range(self):
        self.pool.max_group_gid = 20003
        self.pool.save()
        for gid in (20001, 20002, 20003):
            self.adopt(project=structure_factories.ProjectFactory().uuid.hex, gid=gid)
        self.make_resource()
        self.assertIsNone(self.group_of().gid)
        self.assertEqual(
            sorted(
                models.ServiceProviderProjectGroup.objects.exclude(
                    project=self.project
                ).values_list("gid", flat=True)
            ),
            [20001, 20002, 20003],
        )
        stats = posix_ids.get_pool_stats(self.pool)
        self.assertEqual(stats["group_gid"]["used"], 3)
        self.assertEqual(stats["group_gid"]["utilization"], 100)
        self.client.force_authenticate(self.staff)
        pool_data = self.client.get(
            factories.PosixIdPoolFactory.get_url(self.pool)
        ).data
        self.assertEqual(pool_data["group_gid_used"], 3)
        self.assertEqual(pool_data["group_gid_utilization"], 100)
        self.assertEqual(pool_data["gid_used"], 0)

        response = self.update_pool(max_group_gid=20010)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.group_of().gid, 20004)


class OrderApprovalTest(ScenarioTestCase):
    def make_ordered_resource(self, state):
        self.provider.account_options = {}
        self.provider.save()
        resource = self.make_resource()
        order = factories.OrderFactory(
            resource=resource,
            offering=self.offering,
            project=self.project,
            type=OrderTypes.CREATE,
            state=state,
        )
        self.provider.account_options = {"project_groups_enabled": True}
        self.provider.save()
        return resource, order

    def set_order_state(self, order, state):
        with self.captureOnCommitCallbacks(execute=True):
            order.state = state
            order.save()

    def test_pending_order_takes_no_gid_until_approved(self):
        resource, order = self.make_ordered_resource(OrderStates.PENDING_PROVIDER)
        project_groups.ensure_group_for_resource(resource)
        self.assertFalse(models.ServiceProviderProjectGroup.objects.exists())
        self.set_order_state(order, OrderStates.EXECUTING)
        self.assertEqual(self.group_of().gid, 20001)

    def test_rejected_order_never_takes_a_gid(self):
        resource, order = self.make_ordered_resource(OrderStates.PENDING_CONSUMER)
        self.set_order_state(order, OrderStates.REJECTED)
        self.terminate(resource)
        self.assertFalse(models.ServiceProviderProjectGroup.objects.exists())
        self.assertFalse(models.PosixIdentity.objects.exists())


class MovesTest(ScenarioTestCase):
    def test_move_resource_between_projects(self):
        resource = self.make_resource()
        target = structure_factories.ProjectFactory()
        with self.captureOnCommitCallbacks(execute=True):
            utils.move_resource(resource, target)
        self.assertEqual(self.group_of(target).gid, 20002)
        self.assertFalse(self.item()["in_use"])
        self.assertTrue(self.item(project=target)["in_use"])

    def test_move_offering_to_another_provider(self):
        self.make_resource()
        target = factories.ServiceProviderFactory(
            account_options={"project_groups_enabled": True}
        )
        factories.PosixIdPoolFactory(
            service_provider=target,
            min_group_gid=50001,
            max_group_gid=50100,
            next_group_gid=50001,
        )
        with self.captureOnCommitCallbacks(execute=True):
            utils.move_offering(self.offering, target.customer)
        moved = models.ServiceProviderProjectGroup.objects.get(
            service_provider=target, project=self.project
        )
        self.assertEqual(moved.gid, 50001)
        self.assertFalse(self.item()["in_use"])
        self.assertEqual(self.group_of().gid, 20001)


class PoolDeletionTest(ScenarioTestCase):
    def test_pool_holding_group_gids_cannot_be_deleted(self):
        self.make_resource()
        with self.assertRaises(ProtectedError), transaction.atomic():
            self.pool.delete()
        self.client.force_authenticate(self.staff)
        response = self.client.delete(factories.PosixIdPoolFactory.get_url(self.pool))
        self.assertNotEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(models.PosixIdPool.objects.filter(pk=self.pool.pk).exists())

    def test_new_pool_reserves_gids_groups_already_carry(self):
        self.pool.delete()
        models.ServiceProviderProjectGroup.objects.create(
            service_provider=self.provider,
            project=structure_factories.ProjectFactory(),
            name="legacy",
            gid=20001,
        )
        factories.PosixIdPoolFactory(
            service_provider=self.provider,
            min_gid=30000,
            max_gid=30999,
            next_gid=30000,
            min_group_gid=20001,
            max_group_gid=20200,
            next_group_gid=20001,
        )
        self.make_resource()
        self.assertEqual(self.group_of().gid, 20002)


class PermissionsMatrixTest(ScenarioTestCase):
    def setUp(self):
        super().setUp()
        self.make_resource()
        self.agent = structure_factories.UserFactory()
        self.other_offering.add_user(self.agent, OfferingRole.MANAGER)

    def test_agent_token_lists_through_sibling_offerings(self):
        for params in (
            {"provider_offering_uuid": self.other_offering.uuid.hex},
            {"provider_offering_uuid": self.offering.uuid.hex},
            {"offering_uuid": self.offering.uuid.hex},
        ):
            self.assertEqual(len(self.list_groups(user=self.agent, **params)), 1)

    def test_outsiders_see_nothing(self):
        other_manager = structure_factories.UserFactory()
        factories.OfferingFactory(type=BASIC_OFFERING).add_user(
            other_manager, OfferingRole.MANAGER
        )
        consumer_owner = structure_factories.UserFactory()
        self.project.customer.add_user(consumer_owner, CustomerRole.OWNER)
        project_manager = structure_factories.UserFactory()
        self.project.add_user(project_manager, ProjectRole.MANAGER)
        for user in (other_manager, consumer_owner, project_manager):
            self.assertEqual(
                self.list_groups(
                    user=user, provider_offering_uuid=self.offering.uuid.hex
                ),
                [],
            )
            self.client.force_authenticate(user)
            response = self.client.get(
                reverse(
                    "marketplace-service-provider-project-group-detail",
                    kwargs={"uuid": self.group_of().uuid.hex},
                )
            )
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_only_owner_and_staff_write(self):
        service_manager = structure_factories.UserFactory()
        self.provider.add_user(service_manager, ServiceProviderRole.MANAGER)
        group = self.group_of()
        for user in (self.agent, service_manager):
            response = self.set_gid(group, user=user, gid=20100)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            response = self.adopt(
                user=user,
                project=structure_factories.ProjectFactory().uuid.hex,
                gid=20100,
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.set_gid(group, user=self.staff, gid=20100)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class GlauthUnchangedTest(ScenarioTestCase):
    def test_providers_without_groups_render_as_before(self):
        self.provider.account_options = {}
        self.provider.save()
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
        rendered = _render_glauth_toml(self.offering)
        with mock.patch.object(
            utils,
            "_provider_project_groups_for_offering",
            return_value=([], {}),
        ):
            self.assertEqual(_render_glauth_toml(self.offering), rendered)
        self.assertNotIn(
            "provider_project",
            [g["kind"] for g in utils.build_glauth_tree(self.offering)["groups"]],
        )


class RangeChangesTest(ScenarioTestCase):
    def test_unused_tail_can_be_cut(self):
        self.make_resource()
        response = self.update_pool(max_group_gid=20050)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_outside_range_pin_does_not_block_unrelated_updates(self):
        self.adopt(gid=25000, allow_outside_range=True)
        response = self.update_pool(description="renamed")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self.update_pool(max_gid=30500)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_offering_pool_overlapping_the_group_range_is_refused(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            POOL_LIST_URL,
            {"offering": self.offering.uuid.hex, "min_gid": 20150, "max_gid": 20250},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_offering_level_pools_only_give_no_gid(self):
        self.pool.delete()
        factories.PosixIdPoolFactory(offering=self.offering)
        self.make_resource()
        self.assertIsNone(self.group_of().gid)

    def test_enable_posix_account_switched_off_on_one_offering(self):
        self.make_resource()
        self.make_resource(offering=self.other_offering)
        self.other_offering.plugin_options = {"enable_posix_account": False}
        self.other_offering.save()
        item = self.item()
        self.assertEqual(self.offering_names(item), [self.offering.name])
        self.assertEqual(item["gid"], 20001)


class NamesTest(ScenarioTestCase):
    def test_non_ascii_project_name(self):
        project = structure_factories.ProjectFactory(name="Проект")
        project.slug = ""
        project.save()
        self.make_resource(project=project)
        self.assertEqual(self.group_of(project).name, f"p{project.uuid.hex[:8]}")

    def test_slug_starting_with_a_digit(self):
        self.project.slug = "2024-project"
        self.project.save()
        self.make_resource()
        self.assertEqual(self.group_of().name, f"p{self.project.uuid.hex[:8]}")

    def test_long_slug_is_cut_and_suffixed_within_32(self):
        first = structure_factories.ProjectFactory()
        second = structure_factories.ProjectFactory()
        for project in (first, second):
            project.slug = "a" * 50
            project.save()
            self.make_resource(project=project)
        self.assertEqual(self.group_of(first).name, "a" * 32)
        self.assertEqual(self.group_of(second).name, "a" * 30 + "-2")

    def test_adopt_names_outside_the_rule_are_refused(self):
        for name in ("Alpha", "al pha", "al,pha", "al+pha", "-alpha", "a" * 33):
            response = self.adopt(gid=20001, name=name)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, name)
        self.assertFalse(models.ServiceProviderProjectGroup.objects.exists())

    def test_name_collapse_race_is_suffixed(self):
        models.ServiceProviderProjectGroup.objects.create(
            service_provider=self.provider,
            project=structure_factories.ProjectFactory(),
            name=self.project.slug,
        )
        real_free_name = project_groups._free_name
        calls = []

        def stale_then_real(provider, project):
            calls.append(1)
            # The first lookup ran before the other group was committed.
            return (
                project.slug if len(calls) == 1 else real_free_name(provider, project)
            )

        with mock.patch.object(project_groups, "_free_name", stale_then_real):
            group, created = project_groups.get_or_create_group(
                self.provider, self.project
            )
        self.assertTrue(created)
        self.assertEqual(group.name, f"{self.project.slug}-2")

    def test_adopt_racing_auto_create_is_refused_cleanly(self):
        real = project_groups.get_or_create_group

        def created_meanwhile(provider, project, name=None):
            real(provider, project)
            return real(provider, project, name=name)

        with mock.patch.object(
            project_groups, "get_or_create_group", created_meanwhile
        ):
            response = self.adopt(gid=20100)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class AdoptEdgeCasesTest(ScenarioTestCase):
    def test_taken_name(self):
        self.adopt(
            project=structure_factories.ProjectFactory().uuid.hex,
            gid=20001,
            name="alpha",
        )
        response = self.adopt(gid=20002, name="alpha")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_gid_held_in_an_offering_pool(self):
        offering_pool = factories.PosixIdPoolFactory(
            offering=self.other_offering,
            min_uid=None,
            max_uid=None,
            next_uid=None,
            min_gid=40000,
            max_gid=40999,
            next_gid=40000,
        )
        models.PosixIdentity.objects.create(
            pool=offering_pool, user=structure_factories.UserFactory(), gid=40001
        )
        response = self.adopt(gid=40001, allow_outside_range=True)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_released_non_recyclable_gid_may_be_pinned(self):
        self.adopt(gid=20005)
        self.set_gid(self.group_of(), gid=20006)
        other = structure_factories.ProjectFactory()
        response = self.adopt(project=other.uuid.hex, gid=20005)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_project_without_resources(self):
        response = self.adopt(gid=20001)
        self.assertFalse(response.data["in_use"])
        self.assertEqual(response.data["offerings"], [])


class UnrelatedOfferingTest(ScenarioTestCase):
    """Offerings that are not the provider's never reach project-group code."""

    def assert_no_group(self, offering):
        with mock.patch.object(
            models.Offering, "service_provider", new_callable=mock.PropertyMock
        ) as service_provider:
            resource = self.make_resource(offering=offering)
            with self.captureOnCommitCallbacks(execute=True):
                order = factories.OrderFactory(
                    resource=resource,
                    offering=offering,
                    project=self.project,
                    type=OrderTypes.CREATE,
                    state=OrderStates.PENDING_PROVIDER,
                )
                order.state = OrderStates.EXECUTING
                order.save()
        service_provider.assert_not_called()
        self.assertFalse(models.ServiceProviderProjectGroup.objects.exists())

    def test_project_scoped_offering_without_customer(self):
        offering = factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING,
            customer=None,
            project=self.project,
            shared=False,
        )
        self.assert_no_group(offering)

    def test_project_scoped_offering_of_a_consumer_organization(self):
        offering = factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING,
            customer=self.project.customer,
            project=self.project,
            shared=False,
        )
        self.assert_no_group(offering)

    def test_openstack_offering_at_the_provider_with_the_switch_on(self):
        offering = factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING, customer=self.provider.customer
        )
        self.assert_no_group(offering)
        with mock.patch.object(posix_ids, "provider_pool") as provider_pool:
            target = structure_factories.ProjectFactory()
            resource = models.Resource.objects.get(offering=offering)
            with self.captureOnCommitCallbacks(execute=True):
                utils.move_resource(resource, target)
                utils.move_offering(offering, structure_factories.CustomerFactory())
        provider_pool.assert_not_called()
        self.assertFalse(models.ServiceProviderProjectGroup.objects.exists())

    def test_qualifying_type_without_customer_or_provider(self):
        for customer in (None, structure_factories.CustomerFactory()):
            offering = factories.OfferingFactory(
                type=BASIC_OFFERING, customer=customer, project=self.project
            )
            resource = self.make_resource(offering=offering)
            self.assertEqual(resource.state, ResourceStates.CREATING)
        self.assertFalse(models.ServiceProviderProjectGroup.objects.exists())


class CustomerlessOfferingTest(ScenarioTestCase):
    def test_resource_on_an_offering_without_customer_is_ignored(self):
        offering = factories.OfferingFactory(
            type=BASIC_OFFERING, customer=None, project=self.project, shared=False
        )
        resource = self.make_resource(offering=offering)
        self.assertEqual(resource.state, ResourceStates.CREATING)
        self.assertFalse(models.ServiceProviderProjectGroup.objects.exists())
        tree = utils.build_glauth_tree(offering)
        self.assertFalse([g for g in tree["groups"] if g["kind"] == "provider_project"])


class ProjectVisibilityTest(ScenarioTestCase):
    """Who sees which GIDs a project holds, and where."""

    def setUp(self):
        super().setUp()
        self.make_resource()
        self.member = self.add_member("bob")

    def rollup(self, user):
        self.client.force_authenticate(user)
        return self.client.get(
            reverse("marketplace-project-posix-group-list"),
            {"project_uuid": self.project.uuid.hex},
        )

    def test_project_members_and_customer_owners_see_the_provider_group(self):
        customer_owner = structure_factories.UserFactory()
        self.project.customer.add_user(customer_owner, CustomerRole.OWNER)
        for user in (self.member, customer_owner):
            response = self.rollup(user)
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            [row] = [r for r in response.data if r["kind"] == "provider_project_group"]
            self.assertEqual(row["gid"], 20001)
            self.assertEqual(row["group_name"], self.project.slug)
            self.assertEqual(row["service_provider_uuid"], self.provider.uuid.hex)
            self.assertEqual(row["provider_name"], self.provider.customer.name)
            self.assertTrue(row["in_use"])
            self.assertEqual(
                row["offerings"],
                [{"uuid": self.offering.uuid.hex, "name": self.offering.name}],
            )
            self.assertEqual(row["members"], ["bob"])
            self.assertEqual(row["member_count"], 1)
            self.assertIsNone(row["offering_uuid"])

    def test_outsiders_cannot_read_the_rollup(self):
        response = self.rollup(structure_factories.UserFactory())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unused_group_without_gid_is_listed(self):
        project = structure_factories.ProjectFactory()
        models.ServiceProviderProjectGroup.objects.create(
            service_provider=self.provider, project=project, name="waiting"
        )
        self.client.force_authenticate(self.staff)
        response = self.client.get(
            reverse("marketplace-project-posix-group-list"),
            {"project_uuid": project.uuid.hex},
        )
        [row] = response.data
        self.assertIsNone(row["gid"])
        self.assertFalse(row["in_use"])

    def test_account_page_lists_provider_groups_of_the_member(self):
        offering_user = models.OfferingUser.objects.get(user=self.member)
        self.client.force_authenticate(self.staff)
        response = self.client.get(
            factories.OfferingUserFactory.get_url(offering_user) + "posix_groups/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        [row] = [r for r in response.data if r["kind"] == "provider_project_group"]
        self.assertEqual(row["gid"], 20001)
        self.assertEqual(row["group_name"], self.project.slug)
        self.assertEqual(row["service_provider_name"], self.provider.customer.name)
        self.assertEqual(row["project_uuid"], self.project.uuid.hex)
        self.assertTrue(row["project_accessible"])

    def test_restricted_account_is_not_listed_as_member(self):
        offering_user = models.OfferingUser.objects.get(user=self.member)
        offering_user.is_restricted = True
        offering_user.save()
        rows = utils.get_offering_user_posix_groups(offering_user, viewer=self.staff)
        self.assertEqual([r for r in rows if r["kind"] == "provider_project_group"], [])

    def test_provider_table_fields(self):
        [item] = self.list_groups()
        self.assertEqual(item["service_provider_name"], self.provider.customer.name)
        for key in (
            "customer_name",
            "project_name",
            "project_slug",
            "offerings",
            "members",
            "in_use",
            "created",
            "modified",
        ):
            self.assertIn(key, item)


class SearchTest(ScenarioTestCase):
    def test_query_and_customer_filters(self):
        self.make_resource()
        other = structure_factories.ProjectFactory(name="Climate modelling")
        self.make_resource(project=other)

        def names(**params):
            return sorted(item["name"] for item in self.list_groups(**params))

        self.assertEqual(names(query="climate"), [other.slug])
        self.assertEqual(names(query=self.project.slug.upper()), [self.project.slug])
        self.assertEqual(names(query=other.customer.name), [other.slug])
        self.assertEqual(names(query="20002"), [other.slug])
        self.assertEqual(names(query="no-such-group"), [])
        self.assertEqual(
            names(customer_uuid=self.project.customer.uuid.hex), [self.project.slug]
        )


class ServiceManagerVisibilityTest(ScenarioTestCase):
    def test_customer_manager_of_the_provider_lists_its_groups(self):
        self.make_resource()
        manager = structure_factories.UserFactory()
        self.provider.add_user(manager, ServiceProviderRole.MANAGER)
        items = self.list_groups(
            user=manager, service_provider_uuid=self.provider.uuid.hex
        )
        self.assertEqual(len(items), 1)
        # Service managers read but do not pin.
        response = self.set_gid(self.group_of(), user=manager, gid=20100)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class AdoptScopeTest(ScenarioTestCase):
    def owner_adopt(self, project_ref, **data):
        self.client.force_authenticate(self.owner)
        return self.client.post(
            LIST_URL,
            {
                "service_provider": self.provider.uuid.hex,
                "project": project_ref,
                **data,
            },
            format="json",
        )

    def test_owner_cannot_adopt_for_an_unrelated_project(self):
        stranger = structure_factories.ProjectFactory()
        response = self.owner_adopt(stranger.uuid.hex, gid=20001)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("no resource or order", str(response.data["project"]))
        self.assertFalse(models.ServiceProviderProjectGroup.objects.exists())

    def test_owner_may_adopt_with_a_terminated_resource_or_a_pending_order(self):
        provider_off = self.provider.account_options
        self.provider.account_options = {}
        self.provider.save()
        with_terminated = structure_factories.ProjectFactory()
        self.make_resource(project=with_terminated, state=ResourceStates.TERMINATED)
        with_order = structure_factories.ProjectFactory()
        factories.OrderFactory(
            project=with_order,
            offering=self.offering,
            state=OrderStates.PENDING_PROVIDER,
        )
        self.provider.account_options = provider_off
        self.provider.save()
        for project, gid in ((with_terminated, 20001), (with_order, 20002)):
            response = self.owner_adopt(project.uuid.hex, gid=gid)
            self.assertEqual(
                response.status_code, status.HTTP_201_CREATED, response.data
            )

    def test_staff_may_adopt_for_any_project(self):
        response = self.adopt(
            project=structure_factories.ProjectFactory().uuid.hex, gid=20001
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_non_owner_gets_403_before_any_project_lookup(self):
        outsider = structure_factories.UserFactory()
        self.client.force_authenticate(outsider)
        response = self.client.post(
            LIST_URL,
            {
                "service_provider": self.provider.uuid.hex,
                "project": "no-such-slug",
                "gid": 20001,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_project_slug_is_accepted_when_unique_among_adoptable(self):
        self.make_resource()
        self.group_of().delete()
        response = self.owner_adopt(self.project.slug, gid=20050)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self.group_of().gid, 20050)

    def test_ambiguous_or_unknown_slug_asks_for_the_uuid(self):
        self.provider.account_options = {}
        self.provider.save()
        first = structure_factories.ProjectFactory()
        second = structure_factories.ProjectFactory()
        for project in (first, second):
            project.slug = "twin"
            project.save()
            self.make_resource(project=project)
        response = self.owner_adopt("twin", gid=20001)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("UUID", str(response.data["project"]))
        response = self.owner_adopt("unknown-slug", gid=20001)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("UUID", str(response.data["project"]))

    def test_import_resolves_slugs_and_reports_per_entry(self):
        self.provider.account_options = {}
        self.provider.save()
        self.make_resource()
        stranger = structure_factories.ProjectFactory()
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            LIST_URL + "import_groups/",
            {
                "service_provider": self.provider.uuid.hex,
                "groups": [
                    {"project": self.project.slug, "gid": 20001},
                    {"project": stranger.uuid.hex, "gid": 20002},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(list(response.data["groups"]), [1])
        self.assertFalse(models.ServiceProviderProjectGroup.objects.exists())

    def test_adoptable_projects_lists_what_the_owner_may_adopt(self):
        self.make_resource()
        structure_factories.ProjectFactory()
        self.client.force_authenticate(self.owner)
        response = self.client.get(
            LIST_URL + "adoptable_projects/",
            {"service_provider_uuid": self.provider.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        [item] = response.data
        self.assertEqual(item["uuid"], self.project.uuid.hex)
        self.assertEqual(item["group_name"], self.project.slug)
        manager = structure_factories.UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)
        self.client.force_authenticate(manager)
        response = self.client.get(
            LIST_URL + "adoptable_projects/",
            {"service_provider_uuid": self.provider.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ReadableMessagesTest(ScenarioTestCase):
    def test_conflict_names_the_holding_group(self):
        self.make_resource()
        other = structure_factories.ProjectFactory()
        response = self.adopt(project=other.uuid.hex, gid=20001)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            str(response.data["gid"]),
            f"20001 is already used by project group {self.project.slug}.",
        )

    def test_outside_range_names_the_option(self):
        response = self.adopt(gid=25000)
        self.assertIn("Set allow_outside_range", str(response.data["gid"]))

    def test_existing_group_points_to_change_gid(self):
        self.make_resource()
        response = self.adopt(gid=20100)
        self.assertIn("Change GID", str(response.data["project"]))

    def test_pool_update_names_range_values_and_holders(self):
        self.make_resource()
        response = self.update_pool(min_group_gid=20100, max_group_gid=20200)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        message = str(response.data)
        self.assertIn("project group range", message)
        self.assertIn(f"20001 (project group {self.project.slug})", message)


class AgentAccountKeyTest(ScenarioTestCase):
    """The directory writer keys accounts on the person's Waldur username."""

    def test_offering_manager_sees_user_username_and_uuid(self):
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()
        user = self.add_member("bob")
        agent = structure_factories.UserFactory()
        self.offering.add_user(agent, OfferingRole.MANAGER)
        self.client.force_authenticate(agent)
        response = self.client.get(
            reverse("marketplace-offering-user-list"),
            {
                "offering_uuid": self.offering.uuid.hex,
                "field": ["username", "user_username", "user_uuid"],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data,
            [
                {
                    "username": "bob",
                    "user_username": user.username,
                    "user_uuid": user.uuid.hex,
                }
            ],
        )
