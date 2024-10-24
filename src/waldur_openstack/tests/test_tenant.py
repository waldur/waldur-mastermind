import itertools
from unittest.mock import patch

from ddt import data, ddt
from django.conf import settings
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core.utils import is_uuid_like, make_random_password
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack import executors, models, tasks
from waldur_openstack.tests.helpers import override_openstack_settings

from . import factories, fixtures


@override_openstack_settings(TENANT_CREDENTIALS_VISIBLE=True)
class BaseTenantActionsTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.fixture.settings.shared = True
        self.fixture.settings.save()


class TenantGetTest(BaseTenantActionsTest):
    def setUp(self):
        super().setUp()
        self.fixture.settings.backend_url = "https://waldur.com/"
        self.fixture.settings.save()

    @override_openstack_settings(TENANT_CREDENTIALS_VISIBLE=False)
    def test_user_name_and_password_and_access_url_are_not_returned_if_credentials_are_not_visible(
        self,
    ):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(factories.TenantFactory.get_url(self.fixture.tenant))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("user_username", response.data)
        self.assertNotIn("user_password", response.data)
        self.assertNotIn("access_url", response.data)

    def test_user_name_and_password_and_access_url_are_returned_if_credentials_are_visible(
        self,
    ):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(factories.TenantFactory.get_url(self.fixture.tenant))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.fixture.tenant.user_username, response.data["user_username"]
        )
        self.assertEqual(
            self.fixture.tenant.user_password, response.data["user_password"]
        )
        self.assertEqual(
            self.fixture.tenant.get_access_url(), response.data["access_url"]
        )


@ddt
class TenantCreateTest(BaseTenantActionsTest):
    def setUp(self):
        super().setUp()
        self.valid_data = {
            "name": "Test tenant",
            "service_settings": factories.SettingsFactory.get_url(
                self.fixture.settings
            ),
            "project": structure_factories.ProjectFactory.get_url(self.fixture.project),
        }
        self.url = factories.TenantFactory.get_list_url()
        self.fixture.settings.backend_url = "https://waldur.com/"
        self.fixture.settings.shared = False
        self.fixture.settings.save()

    @data("admin", "manager", "staff", "owner")
    def test_authorized_user_can_create_tenant(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(self.url, data=self.valid_data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            models.Tenant.objects.filter(name=self.valid_data["name"]).exists()
        )

    @data("admin", "manager", "owner")
    def test_cannot_create_tenant_with_shared_service_settings(self, user):
        self.fixture.settings.shared = True
        self.fixture.settings.save()
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(self.url, data=self.valid_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            models.Tenant.objects.filter(name=self.valid_data["name"]).exists()
        )

    @data("global_support", "user")
    def test_unathorized_user_cannot_create_tenant(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(self.url, data=self.valid_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            models.Tenant.objects.filter(name=self.valid_data["name"]).exists()
        )

    def test_cannot_create_tenant_with_service_settings_username(self):
        self.fixture.settings.username = "admin"
        self.fixture.settings.save()
        self.assert_can_not_create_tenant(
            "user_username",
            {"user_username": self.fixture.settings.username},
        )

    def test_cannot_create_tenant_with_blacklisted_username(self):
        self.fixture.settings.options["blacklisted_usernames"] = ["admin"]
        self.assert_can_not_create_tenant("user_username", {"user_username": "admin"})

    def test_cannot_create_tenant_with_duplicated_username(self):
        self.fixture.tenant.user_username = "username"
        self.fixture.tenant.save()

        self.assert_can_not_create_tenant(
            "user_username", {"user_username": self.fixture.tenant.user_username}
        )

    def test_cannot_create_tenant_with_duplicated_tenant_name_in_same_project(self):
        self.assert_can_not_create_tenant(
            "name",
            {
                "name": self.fixture.tenant.name,
                "service_settings": factories.SettingsFactory.get_url(
                    self.fixture.settings
                ),
                "project": structure_factories.ProjectFactory.get_url(
                    self.fixture.project
                ),
            },
        )

    empty_domains = list(
        itertools.combinations_with_replacement((None, "", "default"), 2)
    )

    @data(("same", "same"), *empty_domains)
    def test_can_not_create_tenant_with_same_tenant_name_in_other_service_with_same_or_empty_domain(
        self, pair
    ):
        (domain1, domain2) = pair
        self.fixture.settings.domain = domain1
        self.fixture.settings.save()

        other_fixture = fixtures.OpenStackFixture()
        service_settings = other_fixture.settings
        service_settings.backend_url = self.fixture.settings.backend_url
        service_settings.domain = domain2
        service_settings.save()

        self.assert_can_not_create_tenant(
            "name",
            {
                "name": self.fixture.tenant.name,
                "service_settings": factories.SettingsFactory.get_url(
                    other_fixture.settings
                ),
                "project": structure_factories.ProjectFactory.get_url(
                    other_fixture.project
                ),
            },
        )

    def test_can_create_tenant_with_same_tenant_name_in_other_service_with_same_url_but_other_domain(
        self,
    ):
        self.fixture.settings.domain = "first"
        self.fixture.settings.save()

        other_fixture = fixtures.OpenStackFixture()
        service_settings = other_fixture.settings
        service_settings.backend_url = self.fixture.settings.backend_url
        service_settings.domain = "second"
        service_settings.save()

        self.assert_can_create_tenant(
            {
                "name": self.fixture.tenant.name,
                "service_settings": factories.SettingsFactory.get_url(
                    other_fixture.settings
                ),
                "project": structure_factories.ProjectFactory.get_url(
                    other_fixture.project
                ),
            }
        )

    def assert_can_create_tenant(self, payload):
        response = self.create_tenant(payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def assert_can_not_create_tenant(self, error_field, payload):
        response = self.create_tenant(payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(error_field in response.data)

    def create_tenant(self, payload):
        payload.setdefault("name", "Test tenant")
        payload.setdefault(
            "service_settings",
            factories.SettingsFactory.get_url(self.fixture.settings),
        )
        payload.setdefault(
            "project",
            structure_factories.ProjectFactory.get_url(self.fixture.project),
        )
        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(self.url, data=payload)

    @override_openstack_settings(TENANT_CREDENTIALS_VISIBLE=False)
    def test_user_name_and_password_are_autogenerated_if_credentials_are_not_visible(
        self,
    ):
        self.client.force_authenticate(self.fixture.staff)
        payload = self.valid_data.copy()
        payload["user_username"] = "random"
        payload["user_password"] = "12345678secret"

        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        tenant = models.Tenant.objects.get(name=payload["name"])
        self.assertIsNotNone(tenant.user_username)
        self.assertIsNotNone(tenant.user_password)
        self.assertNotEqual(tenant.user_username, payload["user_username"])
        self.assertNotEqual(tenant.user_password, payload["user_password"])

    def test_user_can_set_username_if_autogeneration_is_disabled(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self.valid_data.copy()
        payload["user_username"] = "random"

        response = self.client.post(self.url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        tenant = models.Tenant.objects.get(name=payload["name"])
        self.assertIsNotNone(tenant.user_username)
        self.assertIsNotNone(tenant.user_password)
        self.assertEqual(tenant.user_username, payload["user_username"])

    @override_openstack_settings(
        DEFAULT_SECURITY_GROUPS=(
            {
                "name": "allow-all",
                "description": "Security group for any access",
                "rules": (
                    {
                        "protocol": "icmp",
                        "cidr": "0.0.0.0/0",
                        "icmp_type": -1,
                        "icmp_code": -1,
                    },
                    {
                        "protocol": "tcp",
                        "cidr": "0.0.0.0/0",
                        "from_port": 1,
                        "to_port": 65535,
                    },
                ),
            },
            {
                "name": "ssh",
                "description": "Security group for secure shell access",
                "rules": (
                    {
                        "protocol": "tcp",
                        "cidr": "0.0.0.0/0",
                        "from_port": 22,
                        "to_port": 22,
                    },
                ),
            },
        )
    )
    def test_default_security_groups_are_created(self):
        expected_security_groups = settings.WALDUR_OPENSTACK["DEFAULT_SECURITY_GROUPS"]
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(self.url, data=self.valid_data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        tenant = models.Tenant.objects.get(name=self.valid_data["name"])
        self.assertEqual(len(expected_security_groups), tenant.security_groups.count())
        allow_all = expected_security_groups[0]
        tenant_sg = tenant.security_groups.get(name="allow-all")
        expected_icmp_rule = [
            rule for rule in allow_all["rules"] if rule["protocol"] == "icmp"
        ][0]
        icmp_rule = tenant_sg.rules.get(protocol="icmp")
        self.assertEqual(expected_icmp_rule["icmp_type"], icmp_rule.from_port)
        self.assertEqual(expected_icmp_rule["icmp_code"], icmp_rule.to_port)

    @override_openstack_settings(
        DEFAULT_SECURITY_GROUPS=(
            {
                "description": "Security group for any access",
                "rules": (
                    {
                        "protocol": "icmp",
                        "cidr": "0.0.0.0/0",
                        "icmp_type": -1,
                        "icmp_code": -1,
                    },
                    {
                        "protocol": "tcp",
                        "cidr": "0.0.0.0/0",
                        "from_port": 1,
                        "to_port": 65535,
                    },
                ),
            },
        )
    )
    def test_tenant_is_not_created_if_configured_security_groups_have_no_name(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(self.url, data=self.valid_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_openstack_settings(
        DEFAULT_SECURITY_GROUPS=(
            {
                "name": "allow-all",
                "description": "Security group for any access",
            },
        )
    )
    def test_tenant_is_not_created_if_configured_security_groups_rules_are_not_present(
        self,
    ):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(self.url, data=self.valid_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_task_id(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, data=self.valid_data)
        self.assertTrue(
            is_uuid_like(models.Tenant.objects.get(uuid=response.data["uuid"]).task_id)
        )


@ddt
class TenantUpdateTest(BaseTenantActionsTest):
    def test_user_cannot_update_username_even_if_credentials_autogeneration_is_disabled(
        self,
    ):
        self.client.force_authenticate(self.fixture.staff)
        payload = dict(name=self.fixture.tenant.name, user_username="new_username")

        response = self.client.put(self.get_url(), payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotEqual(response.data["user_username"], payload["user_username"])

    def test_cannot_update_tenant_with_duplicated_tenant_name(self):
        other_tenant = factories.TenantFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
        )
        payload = dict(name=other_tenant.name)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.put(self.get_url(), payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(models.Tenant.objects.filter(name=payload["name"]).count(), 1)

    @data("staff", "admin", "manager", "owner")
    def test_if_authorized_user_can_update_tenant(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self.get_url(), dict(name="new valid tenant name"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def get_url(self):
        return factories.TenantFactory.get_url(self.fixture.tenant)


@patch("waldur_openstack.executors.TenantPushQuotasExecutor.execute")
class TenantQuotasTest(BaseTenantActionsTest):
    def test_non_staff_user_cannot_set_tenant_quotas(self, mocked_task):
        self.client.force_authenticate(user=structure_factories.UserFactory())
        response = self.client.post(self.get_url())

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(mocked_task.called)

    def test_staff_can_set_tenant_quotas(self, mocked_task):
        self.client.force_authenticate(self.fixture.staff)
        quotas_data = {"security_group_count": 100, "security_group_rule_count": 100}
        response = self.client.post(self.get_url(), data=quotas_data)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mocked_task.assert_called_once_with(self.tenant, quotas=quotas_data)

    def get_url(self):
        return factories.TenantFactory.get_url(self.tenant, "set_quotas")


@patch("waldur_openstack.executors.TenantPullExecutor.execute")
class TenantPullTest(BaseTenantActionsTest):
    def test_staff_can_pull_tenant(self, mocked_task):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.get_url())
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mocked_task.assert_called_once_with(self.tenant)

    def test_it_should_not_be_possible_to_pull_tenant_without_backend_id(
        self, mocked_task
    ):
        # Arrange
        self.tenant.backend_id = ""
        self.tenant.save()

        # Act
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.get_url())

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(0, mocked_task.call_count)

    def get_url(self):
        return factories.TenantFactory.get_url(self.tenant, "pull")


@patch("waldur_openstack.executors.TenantPullQuotasExecutor.execute")
class TenantPullQuotasTest(BaseTenantActionsTest):
    def test_staff_can_pull_tenant_quotas(self, mocked_task):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.get_url())
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mocked_task.assert_called_once_with(self.tenant)

    def get_url(self):
        return factories.TenantFactory.get_url(self.tenant, "pull_quotas")


@ddt
@patch("waldur_openstack.executors.TenantDeleteExecutor.execute")
class TenantDeleteTest(BaseTenantActionsTest):
    @data("staff", "owner", "admin", "manager")
    def test_can_delete_tenant_from_private_settings(self, user, mocked_task):
        self.fixture.settings.shared = False
        self.fixture.settings.save()

        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.get_url())

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mocked_task.assert_called_once_with(self.tenant, is_async=True, force=False)

    @data("admin", "manager")
    def test_cannot_delete_tenant_from_shared_settings(self, user, mocked_task):
        self.fixture.settings.shared = True
        self.fixture.settings.save()
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.get_url())

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(mocked_task.call_count, 0)

    def test_manager_can_delete_tenant_from_shared_settings_with_permission_from_settings(
        self, mocked_task
    ):
        # Arrange
        ProjectRole.MANAGER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.fixture.settings.shared = True
        self.fixture.settings.save()

        # Act
        self.client.force_authenticate(user=self.fixture.manager)
        response = self.client.delete(self.get_url())

        # Assert
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mocked_task.assert_called_once_with(self.tenant, is_async=True, force=False)

    def test_admin_can_delete_tenant_from_shared_settings_with_permission_from_settings(
        self, mocked_task
    ):
        # Arrange
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.fixture.settings.shared = True
        self.fixture.settings.save()

        # Act
        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.delete(self.get_url())

        # Assert
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mocked_task.assert_called_once_with(self.tenant, is_async=True, force=False)

    @data("global_support")
    def test_cannot_delete_tenant(self, user, mocked_task):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.get_url())

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(mocked_task.call_count, 0)

    def test_user_can_delete_tenant_if_project_has_been_soft_deleted(self, mocked_task):
        self.fixture.settings.shared = False
        self.fixture.settings.save()

        self.fixture.project.is_removed = True
        self.fixture.project.save()

        self.client.force_authenticate(self.fixture.owner)

        response = self.client.delete(self.get_url())

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mocked_task.assert_called_once_with(self.tenant, is_async=True, force=False)

    def get_url(self):
        return factories.TenantFactory.get_url(self.tenant)


@patch("waldur_openstack.executors.FloatingIPCreateExecutor.execute")
class TenantCreateFloatingIPTest(BaseTenantActionsTest):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.fixture.owner)
        self.url = factories.TenantFactory.get_url(self.tenant, "create_floating_ip")

    def test_that_floating_ip_count_quota_increases_when_floating_ip_is_created(
        self, mocked_task
    ):
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.tenant.floating_ips.count(), 1)
        self.assertTrue(mocked_task.called)

    def test_that_floating_ip_count_quota_exceeds_limit_if_too_many_ips_are_created(
        self, mocked_task
    ):
        self.tenant.set_quota_limit("floating_ip_count", 0)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.tenant.floating_ips.count(), 0)
        self.assertFalse(mocked_task.called)

    def test_user_cannot_create_floating_ip_if_external_network_is_not_defined_for_tenant(
        self, mocked_task
    ):
        self.tenant.external_network_id = ""
        self.tenant.save()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.tenant.floating_ips.count(), 0)
        self.assertFalse(mocked_task.called)


@patch("waldur_openstack.executors.NetworkCreateExecutor.execute")
class TenantCreateNetworkTest(BaseTenantActionsTest):
    quota_name = "network_count"

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.fixture.owner)
        self.url = factories.TenantFactory.get_url(self.tenant, "create_network")
        self.request_data = {"name": "test_network_name"}

    def test_that_network_quota_is_increased_when_network_is_created(self, mocked_task):
        response = self.client.post(self.url, self.request_data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.tenant.networks.count(), 1)
        self.assertEqual(self.tenant.get_quota_usage(self.quota_name), 1)
        self.assertTrue(mocked_task.called)

    def test_that_network_is_not_created_when_quota_exceeds_set_limit(
        self, mocked_task
    ):
        self.tenant.set_quota_limit(self.quota_name, 0)
        response = self.client.post(self.url, self.request_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.tenant.networks.count(), 0)
        self.assertEqual(self.tenant.get_quota_usage(self.quota_name), 0)
        self.assertFalse(mocked_task.called)


@ddt
class TenantChangePasswordTest(BaseTenantActionsTest):
    def setUp(self):
        super().setUp()
        self.tenant = self.fixture.tenant
        self.url = factories.TenantFactory.get_url(
            self.tenant, action="change_password"
        )
        self.new_password = make_random_password()[:50]

    @data("owner", "staff", "admin", "manager")
    def test_user_can_change_tenant_user_password(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(self.url, {"user_password": self.new_password})

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.user_password, self.new_password)

    @data("global_support", "customer_support", "member")
    def test_user_cannot_change_tenant_user_password(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(self.url, {"user_password": self.new_password})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_cannot_set_password_if_it_consists_only_with_digits(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, {"user_password": 682992000})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_set_password_with_length_less_than_8_characters(self):
        request_data = {"user_password": make_random_password()[:7]}

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, request_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_set_password_if_it_matches_to_the_old_one(self):
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.post(
            self.url, {"user_password": self.fixture.tenant.user_password}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_change_password_if_tenant_is_not_in_OK_state(self):
        self.tenant.state = self.tenant.States.ERRED
        self.tenant.save()

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            self.url, {"user_password": self.fixture.tenant.user_password}
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_user_can_set_an_empty_password(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, {"user_password": ""})

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)


@ddt
class TenantExecutorTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.network = self.fixture.network
        self.subnet = self.fixture.subnet
        self.subnet.network = self.network
        self.subnet.save()

        self.service_settings = self.fixture.settings
        self.service_settings.options = {"external_network_id": "external_network_id"}
        self.service_settings.save()

    def test_if_skip_connection_extnet_is_true_task_does_not_exists(self):
        chain = executors.TenantCreateExecutor.get_task_signature(
            self.tenant, "openstack.tenant:1", skip_connection_extnet=True
        )
        self.assertEqual(
            len(
                [
                    t.args
                    for t in chain.tasks
                    if "connect_tenant_to_external_network" in t.args
                ]
            ),
            0,
        )

    def test_if_skip_connection_extnet_is_false_task_exists(self):
        chain = executors.TenantCreateExecutor.get_task_signature(
            self.tenant, "openstack.tenant:1", skip_connection_extnet=False
        )
        self.assertEqual(
            len(
                [
                    t.args
                    for t in chain.tasks
                    if "connect_tenant_to_external_network" in t.args
                ]
            ),
            1,
        )


class TenantTasksTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant

    def test_mark_as_erred_old_tenants_in_deleting_state(self):
        with freeze_time("2022-01-01"):
            self.tenant.state = models.Tenant.States.DELETING
            self.tenant.save()
            tasks.mark_as_erred_old_tenants_in_deleting_state()
            self.tenant.refresh_from_db()
            self.assertEqual(self.tenant.state, models.Tenant.States.DELETING)

        with freeze_time("2022-01-02"):
            tasks.mark_as_erred_old_tenants_in_deleting_state()
            self.tenant.refresh_from_db()
            self.assertEqual(self.tenant.state, models.Tenant.States.ERRED)
