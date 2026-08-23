from constance.test.unittest import override_config
from ddt import data, ddt
from rest_framework import status, test

from waldur_core.media.utils import dummy_image
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.utils import get_permissions
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, utils
from waldur_mastermind.marketplace.enums import (
    SUPPORT_OFFERING,
    BillingTypes,
    OfferingStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import fixtures

from . import factories


@ddt
class ServiceProviderGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.service_provider = self.fixture.service_provider
        # Create consent so users are visible to service providers
        self.fixture.user_offering_consent

    @data("staff", "owner", "user", "customer_support", "admin", "manager")
    def test_service_provider_should_be_visible_to_all_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ServiceProviderFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_service_provider_should_be_visible_to_unauthenticated_users_by_default(
        self,
    ):
        url = factories.ServiceProviderFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_service_provider_should_be_invisible_to_unauthenticated_users_when_offerings_are_public(
        self,
    ):
        url = factories.ServiceProviderFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @data("staff", "offering_owner")
    def test_service_provider_api_secret_code_is_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        CustomerRole.OWNER.add_permission(
            PermissionEnum.GET_SERVICE_PROVIDER_API_SECRET_CODE
        )
        url = factories.ServiceProviderFactory.get_url(
            self.service_provider, "api_secret_code"
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue("api_secret_code" in response.data.keys())

    @data("user", "customer_support", "admin", "manager")
    def test_service_provider_api_secret_code_is_invisible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ServiceProviderFactory.get_url(
            self.service_provider, "api_secret_code"
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_projects_are_visible(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_SERVICE_PROVIDER_USERS)
        self.fixture.resource
        self.fixture.manager
        self.client.force_authenticate(self.fixture.service_owner)
        url = factories.ServiceProviderFactory.get_url(
            self.fixture.service_provider, "users"
        )
        response = self.client.get(url)
        self.assertEqual(response.json()[0]["projects_count"], 1)


@ddt
class ServiceProviderRegisterTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data("staff")
    def test_staff_can_register_a_service_provider(self, user):
        response = self.create_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.ServiceProvider.objects.filter(customer=self.customer).exists()
        )

    @data("user", "customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_register_an_service_provider(self, user):
        response = self.create_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_can_register_service_provider_with_settings_enabled(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.REGISTER_SERVICE_PROVIDER)
        response = self.create_service_provider("owner")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("owner")
    def test_owner_can_not_register_service_provider_with_settings_disabled(self, user):
        response = self.create_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("user", "customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_register_service_provider_with_settings_disabled(
        self, user
    ):
        response = self.create_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def create_service_provider(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ServiceProviderFactory.get_list_url()

        payload = {
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
        }

        return self.client.post(url, payload)


@ddt
class ServiceProviderUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data("staff", "owner")
    def test_authorized_user_can_update_service_provider(self, user):
        response, service_provider = self.update_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(service_provider.enable_notifications)
        self.assertTrue(
            models.ServiceProvider.objects.filter(customer=self.customer).exists()
        )

    @data("user", "customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_update_service_provider(self, user):
        response, service_provider = self.update_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def update_service_provider(self, user, payload=None, **kwargs):
        if not payload:
            payload = {"enable_notifications": False}

        service_provider = factories.ServiceProviderFactory(customer=self.customer)
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ServiceProviderFactory.get_url(service_provider)

        response = self.client.patch(url, payload, **kwargs)
        service_provider.refresh_from_db()

        return response, service_provider

    @data("staff", "owner")
    def test_generate_api_secret_code(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        CustomerRole.OWNER.add_permission(
            PermissionEnum.GENERATE_SERVICE_PROVIDER_API_SECRET_CODE
        )

        service_provider = factories.ServiceProviderFactory(customer=self.customer)
        url = factories.ServiceProviderFactory.get_url(
            service_provider, "api_secret_code"
        )
        old_secret_code = service_provider.api_secret_code
        response = self.client.post(url)
        service_provider.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotEqual(service_provider.api_secret_code, old_secret_code)

    @data("user", "customer_support", "admin", "manager")
    def test_not_generate_api_secret_code(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        service_provider = factories.ServiceProviderFactory(customer=self.customer)
        url = factories.ServiceProviderFactory.get_url(
            service_provider, "api_secret_code"
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_upload_service_provider_image(self):
        payload = {"image": dummy_image()}
        response, service_provider = self.update_service_provider(
            "staff", payload=payload, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(service_provider.image)

        url = factories.ServiceProviderFactory.get_url(service_provider)
        response = self.client.patch(url, {"image": None})
        service_provider.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(service_provider.image)


@ddt
class ServiceProviderDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.service_provider = factories.ServiceProviderFactory(customer=self.customer)

    @data("staff", "owner")
    def test_authorized_user_can_delete_service_provider(self, user):
        response = self.delete_service_provider(user)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(
            models.ServiceProvider.objects.filter(customer=self.customer).exists()
        )

    def test_service_provider_could_not_be_deleted_if_it_has_active_offerings(self):
        factories.OfferingFactory(customer=self.customer, state=OfferingStates.ACTIVE)
        response = self.delete_service_provider("staff")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(
            models.ServiceProvider.objects.filter(customer=self.customer).exists()
        )

    def test_service_provider_is_deleted_if_it_has_archived_offering(self):
        factories.OfferingFactory(customer=self.customer, state=OfferingStates.ARCHIVED)
        response = self.delete_service_provider("staff")
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(
            models.ServiceProvider.objects.filter(customer=self.customer).exists()
        )

    def test_service_provider_is_deleted_if_it_has_only_child_offerings(self):
        parent = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        factories.OfferingFactory(
            customer=self.customer,
            state=OfferingStates.ACTIVE,
            parent=parent,
        )
        response = self.delete_service_provider("staff")
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(
            models.ServiceProvider.objects.filter(customer=self.customer).exists()
        )

    @data("user", "customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_delete_service_provider(self, user):
        response = self.delete_service_provider(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            models.ServiceProvider.objects.filter(customer=self.customer).exists()
        )

    def delete_service_provider(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.ServiceProviderFactory.get_url(self.service_provider)
        response = self.client.delete(url)
        return response


class CustomerSerializerTest(test.APITestCase):
    def test_service_provider_is_not_defined(self):
        customer = structure_factories.CustomerFactory()
        self.assertFalse(self.get_value(customer))

    def test_service_provider_is_defined(self):
        customer = factories.ServiceProviderFactory().customer
        self.assertTrue(self.get_value(customer))

    def get_value(self, customer):
        user = structure_factories.UserFactory(is_staff=True)
        url = structure_factories.CustomerFactory.get_url(customer)

        self.client.force_login(user)
        response = self.client.get(url)
        return response.data["is_service_provider"]


class ServiceProviderNotificationTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.fixture.owner
        self.service_provider = factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            type=SUPPORT_OFFERING,
            name="First",
        )
        self.component = factories.OfferingComponentFactory(
            billing_type=BillingTypes.USAGE, offering=offering
        )

        self.resource = factories.ResourceFactory(
            offering=offering, state=ResourceStates.OK, name="My resource"
        )

    def test_get_customer_if_usages_are_not_exist(self):
        self.assertEqual(len(utils.get_info_about_missing_usage_reports()), 1)
        self.assertEqual(
            utils.get_info_about_missing_usage_reports()[0]["customer"],
            self.fixture.customer,
        )

    def test_do_not_get_customer_if_usages_are_exist(self):
        factories.ComponentUsageFactory(
            resource=self.resource, component=self.component
        )
        self.assertEqual(len(utils.get_info_about_missing_usage_reports()), 0)


class ConsumerProjectListTest(test.APITestCase):
    def setUp(self) -> None:
        self.mp_fixture = fixtures.MarketplaceFixture()

        self.consumer_project = self.mp_fixture.project
        self.consumable_resource = self.mp_fixture.resource
        self.url = factories.ServiceProviderFactory.get_url(
            self.mp_fixture.service_provider, action="projects"
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_SERVICE_PROVIDER_PROJECTS)

    def test_service_provider_can_view_project_with_purchased_resource(self):
        self.client.force_login(self.mp_fixture.offering_owner)
        response = self.client.get(self.url)

        self.assertEqual(200, response.status_code)
        self.assertIn(
            self.consumer_project.uuid.hex, [item["uuid"] for item in response.data]
        )


class ConsumerSshKeyListTest(test.APITestCase):
    def setUp(self) -> None:
        self.mp_fixture = fixtures.MarketplaceFixture()

        self.consumer_project = self.mp_fixture.project
        self.consumable_resource = self.mp_fixture.resource
        self.admin = self.mp_fixture.admin
        self.ssh_key = structure_factories.SshPublicKeyFactory(
            user=self.admin,
            is_shared=True,
        )
        self.url = factories.ServiceProviderFactory.get_url(
            self.mp_fixture.service_provider, action="keys"
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_SERVICE_PROVIDER_KEYS)

    def test_service_provider_can_view_ssh_keys_from_project_with_purchased_resource(
        self,
    ):
        self.client.force_login(self.mp_fixture.offering_owner)
        response = self.client.get(self.url)

        self.assertEqual(200, response.status_code)
        self.assertIn(self.ssh_key.uuid.hex, [item["uuid"] for item in response.data])


class ConsumerProjectPermissionListTest(test.APITestCase):
    def setUp(self) -> None:
        self.mp_fixture = fixtures.MarketplaceFixture()

        self.consumer_project = self.mp_fixture.project
        self.consumable_resource = self.mp_fixture.resource
        self.admin = self.mp_fixture.admin
        self.permission = get_permissions(self.consumer_project, self.admin).get()
        self.url = factories.ServiceProviderFactory.get_url(
            self.mp_fixture.service_provider, action="project_permissions"
        )
        CustomerRole.OWNER.add_permission(
            PermissionEnum.LIST_SERVICE_PROVIDER_PROJECT_PERMISSIONS
        )

    def test_service_provider_can_view_project_permissions_in_project_with_purchased_resource(
        self,
    ):
        self.client.force_login(self.mp_fixture.offering_owner)
        response = self.client.get(self.url)

        self.assertEqual(200, response.status_code)
        self.assertEqual(len(response.data), 1)


class ConsumerUserListTest(test.APITestCase):
    def setUp(self) -> None:
        self.mp_fixture = fixtures.MarketplaceFixture()

        self.consumer_project = self.mp_fixture.project
        self.consumable_resource = self.mp_fixture.resource
        self.admin = self.mp_fixture.admin
        self.url = factories.ServiceProviderFactory.get_url(
            self.mp_fixture.service_provider, action="users"
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_SERVICE_PROVIDER_USERS)
        # Create consent so users are visible to service providers
        self.mp_fixture.user_offering_consent

    def test_service_provider_can_view_users_in_project_with_purchased_resource(self):
        self.client.force_login(self.mp_fixture.offering_owner)
        response = self.client.get(self.url)

        self.assertEqual(200, response.status_code)
        self.assertIn(self.admin.uuid.hex, [item["uuid"] for item in response.data])

    def test_disabled_users_are_excluded(self):
        # Arrange
        self.admin.is_active = False
        self.admin.save()

        # Act
        self.client.force_login(self.mp_fixture.offering_owner)
        response = self.client.get(self.url)

        # Assert
        self.assertEqual(200, response.status_code)
        self.assertNotIn(self.admin.uuid.hex, [item["uuid"] for item in response.data])


class SetOfferingUsersTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.MarketplaceFixture()

        self.consumer_project = self.fixture.project
        self.consumable_resource = self.fixture.resource
        self.offering = self.fixture.offering
        self.admin = self.fixture.admin
        self.url = factories.ServiceProviderFactory.get_url(
            self.fixture.service_provider,
            action="set_offerings_username",
        )
        CustomerRole.OWNER.add_permission(
            PermissionEnum.SET_SERVICE_PROVIDER_OFFERINGS_USERNAME
        )

    def test_offering_user_creation(self):
        self.assertEqual(
            0,
            models.OfferingUser.objects.filter(
                user=self.admin, offering=self.offering
            ).count(),
        )
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.post(
            self.url,
            {
                "user_uuid": self.admin.uuid,
                "username": "SET_OFFERING_USERNAME",
            },
        )

        self.assertEqual(201, response.status_code)
        self.assertEqual(
            1,
            models.OfferingUser.objects.filter(
                user=self.admin, offering=self.offering
            ).count(),
        )
        offering_user = models.OfferingUser.objects.get(
            user=self.admin, offering=self.offering
        )
        self.assertEqual("SET_OFFERING_USERNAME", offering_user.username)

    def test_offering_user_update(self):
        models.OfferingUser.objects.create(
            offering=self.offering,
            user=self.admin,
            username="ADMIN_OLD",
        )
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.post(
            self.url,
            {
                "user_uuid": self.admin.uuid,
                "username": "ADMIN_NEW",
            },
        )

        self.assertEqual(201, response.status_code)
        self.assertEqual(
            1,
            models.OfferingUser.objects.filter(
                user=self.admin, offering=self.offering
            ).count(),
        )
        offering_user = models.OfferingUser.objects.get(
            user=self.admin, offering=self.offering
        )
        self.assertEqual("ADMIN_NEW", offering_user.username)

    def test_posix_attributes_are_populated(self):
        # The action is the entry point of the Terraform provisioning flow, so
        # an account it materialises must come out complete: without the POSIX
        # projection the site agent has nothing to write into the directory.
        factories.PosixIdPoolFactory(
            service_provider=self.fixture.service_provider,
            min_uid=100000,
            max_uid=100099,
            next_uid=100000,
            min_gid=200000,
            max_gid=200099,
            next_gid=200000,
        )
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.post(
            self.url,
            {"user_uuid": self.admin.uuid, "username": "alice"},
        )

        self.assertEqual(201, response.status_code)
        offering_user = models.OfferingUser.objects.get(
            user=self.admin, offering=self.offering
        )
        self.assertEqual(offering_user.backend_metadata["uidnumber"], 100000)
        self.assertEqual(offering_user.backend_metadata["primarygroup"], 200000)
        self.assertEqual(offering_user.backend_metadata["homeDir"], "/home/alice")
        self.assertEqual(offering_user.backend_metadata["loginShell"], "/bin/bash")

    def test_pinned_home_directory_survives_a_repeated_call(self):
        # The provisioning flow re-runs this action; re-deriving the home
        # directory when the username has not changed would undo an override.
        factories.PosixIdPoolFactory(
            service_provider=self.fixture.service_provider,
            min_uid=100000,
            max_uid=100099,
            next_uid=100000,
            min_gid=200000,
            max_gid=200099,
            next_gid=200000,
        )
        self.client.force_login(self.fixture.offering_owner)
        payload = {"user_uuid": self.admin.uuid, "username": "alice"}
        self.client.post(self.url, payload)

        offering_user = models.OfferingUser.objects.get(
            user=self.admin, offering=self.offering
        )
        offering_user.backend_metadata["homeDir"] = "/data/alice"
        offering_user.save()

        self.client.post(self.url, payload)
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.backend_metadata["homeDir"], "/data/alice")

    def test_overridden_home_directory_survives_a_username_change(self):
        # The PATCH path preserves an operator's override; this path must agree.
        factories.PosixIdPoolFactory(
            service_provider=self.fixture.service_provider,
            min_uid=100000,
            max_uid=100099,
            next_uid=100000,
            min_gid=200000,
            max_gid=200099,
            next_gid=200000,
        )
        self.client.force_login(self.fixture.offering_owner)
        self.client.post(self.url, {"user_uuid": self.admin.uuid, "username": "alice"})
        offering_user = models.OfferingUser.objects.get(
            user=self.admin, offering=self.offering
        )
        offering_user.backend_metadata["homeDir"] = "/data/alice"
        offering_user.save()

        self.client.post(self.url, {"user_uuid": self.admin.uuid, "username": "alice2"})
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, "alice2")
        self.assertEqual(offering_user.backend_metadata["homeDir"], "/data/alice")

    def test_derived_home_directory_follows_a_username_change(self):
        factories.PosixIdPoolFactory(
            service_provider=self.fixture.service_provider,
            min_uid=100000,
            max_uid=100099,
            next_uid=100000,
            min_gid=200000,
            max_gid=200099,
            next_gid=200000,
        )
        self.client.force_login(self.fixture.offering_owner)
        self.client.post(self.url, {"user_uuid": self.admin.uuid, "username": "alice"})
        self.client.post(self.url, {"user_uuid": self.admin.uuid, "username": "alice2"})
        offering_user = models.OfferingUser.objects.get(
            user=self.admin, offering=self.offering
        )
        self.assertEqual(offering_user.backend_metadata["homeDir"], "/home/alice2")

    def test_an_exhausted_pool_leaves_no_account_half_applied(self):
        # The allocator raises 409; the action must be all-or-nothing rather than
        # applying the offerings it got through first.
        second_offering = factories.OfferingFactory(
            customer=self.fixture.customer, name="Cluster B"
        )
        factories.ResourceFactory(
            offering=second_offering, project=self.consumer_project
        )
        factories.PosixIdPoolFactory(
            service_provider=self.fixture.service_provider,
            min_uid=100000,
            max_uid=100000,
            next_uid=100001,
            min_gid=200000,
            max_gid=200000,
            next_gid=200001,
        )
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.post(
            self.url, {"user_uuid": self.admin.uuid, "username": "alice"}
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertFalse(models.OfferingUser.objects.filter(user=self.admin).exists())

    def test_posix_attributes_are_shared_across_the_providers_offerings(self):
        factories.PosixIdPoolFactory(
            service_provider=self.fixture.service_provider,
            min_uid=100000,
            max_uid=100099,
            next_uid=100000,
            min_gid=200000,
            max_gid=200099,
            next_gid=200000,
        )
        second_offering = factories.OfferingFactory(
            customer=self.fixture.customer, name="Cluster B"
        )
        factories.ResourceFactory(
            offering=second_offering, project=self.consumer_project
        )
        self.client.force_login(self.fixture.offering_owner)
        self.client.post(
            self.url,
            {"user_uuid": self.admin.uuid, "username": "alice"},
        )

        values = {
            offering_user.backend_metadata["uidnumber"]
            for offering_user in models.OfferingUser.objects.filter(user=self.admin)
        }
        self.assertEqual(len(values), 1)


class ServiceProviderUserCustomersTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.service_provider = factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )
        self.url = factories.ServiceProviderFactory.get_url(
            self.service_provider, "user_customers"
        )
        CustomerRole.OWNER.add_permission(
            PermissionEnum.LIST_SERVICE_PROVIDER_USER_CUSTOMERS
        )

    def test_get_user_customers_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    def test_user_uuid(self):
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            type=SUPPORT_OFFERING,
            name="First",
        )

        resource = factories.ResourceFactory(
            offering=offering, state=ResourceStates.OK, name="My resource"
        )
        resource.project.add_user(self.fixture.user, ProjectRole.ADMIN)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"user_uuid": self.fixture.user.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ServiceProviderProjectServiceAccountsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.service_provider = self.fixture.service_provider
        self.url = factories.ServiceProviderFactory.get_url(
            self.service_provider, "project_service_accounts"
        )
        CustomerRole.OWNER.add_permission(
            PermissionEnum.LIST_SERVICE_PROVIDER_SERVICE_ACCOUNTS
        )

        self.resource = self.fixture.resource
        self.service_account = factories.ProjectServiceAccountFactory(
            project=self.resource.project,
            username="test-svc-username",
        )

        self.service_account_hidden = factories.ProjectServiceAccountFactory(
            project=structure_factories.ProjectFactory(),
            username="test-svc-username-2",
        )

    def test_get_project_service_accounts(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

        service_account = response.json()[0]
        self.assertEqual(service_account["username"], self.service_account.username)
        self.assertEqual(
            service_account["project_uuid"], self.resource.project.uuid.hex
        )

    def test_filter_project_service_accounts(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        new_resource = factories.ResourceFactory(
            name="New resource", offering=self.fixture.offering, state=ResourceStates.OK
        )
        new_project = new_resource.project

        new_service_account = factories.ProjectServiceAccountFactory(
            project=new_project,
            username="test-svc-new-username",
        )
        url = f"{self.url}?project_uuid={new_project.uuid.hex}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

        service_account = response.json()[0]
        self.assertEqual(service_account["username"], new_service_account.username)
        self.assertEqual(service_account["project_uuid"], new_project.uuid.hex)


class ServiceProviderCourseAccountsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.service_provider = self.fixture.service_provider
        self.url = factories.ServiceProviderFactory.get_url(
            self.service_provider, "course_accounts"
        )
        CustomerRole.OWNER.add_permission(
            PermissionEnum.LIST_SERVICE_PROVIDER_COURSE_ACCOUNTS
        )

        self.resource = self.fixture.resource
        self.course_account = factories.CourseAccountFactory(
            project=self.resource.project,
        )

        self.course_account_hidden = factories.CourseAccountFactory(
            project=structure_factories.ProjectFactory(),
        )

    def test_get_course_accounts(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

        course_account = response.json()[0]
        self.assertEqual(course_account["username"], self.course_account.user.username)
        self.assertEqual(course_account["project_uuid"], self.resource.project.uuid.hex)

    def test_filter_course_accounts(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        new_resource = factories.ResourceFactory(
            name="New resource", offering=self.fixture.offering, state=ResourceStates.OK
        )
        new_project = new_resource.project

        new_course_account = factories.CourseAccountFactory(
            project=new_project,
        )
        url = f"{self.url}?project_uuid={new_project.uuid.hex}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

        course_account = response.json()[0]
        self.assertEqual(course_account["username"], new_course_account.user.username)
        self.assertEqual(course_account["project_uuid"], new_project.uuid.hex)


class ServiceProviderUsersGDPRFilteringTest(test.APITestCase):
    """Test GDPR-aware attribute filtering on service provider users endpoint."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.service_provider = self.fixture.service_provider
        self.offering = self.fixture.offering
        # Enable service_provider_can_create_offering_user for the offering
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()

        # Create user with profile data
        self.user = structure_factories.UserFactory(
            email="testuser@example.com",
            phone_number="+1234567890",
            organization="Test Org",
            affiliations=["Affiliation1"],
        )

        # Create resource linking user to offering
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            state=ResourceStates.OK,
        )
        self.resource.project.add_user(self.user, ProjectRole.ADMIN)

        # Create OfferingUser
        models.OfferingUser.objects.create(user=self.user, offering=self.offering)

        # Grant permission
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_SERVICE_PROVIDER_USERS)

        self.url = (
            f"/api/marketplace-service-providers/{self.service_provider.uuid}/users/"
        )

    def test_default_attributes_exposed_without_config(self):
        """When no attribute config exists, default attributes are exposed."""
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should have at least one user
        self.assertGreater(len(response.data), 0)

        # Find our test user
        user_data = next(
            (u for u in response.data if u["uuid"] == str(self.user.uuid)), None
        )
        self.assertIsNotNone(user_data)

        # Default attributes should be exposed
        self.assertIn("username", user_data)
        self.assertIn("full_name", user_data)
        self.assertIn("email", user_data)

    def test_restricted_attributes_hidden_when_not_in_config(self):
        """Attributes not in the offering config are hidden."""
        # Create attribute config that only exposes username, full_name, email (defaults)
        # phone_number, organization, affiliations are disabled by default
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_phone_number=False,
            expose_organization=False,
            expose_affiliations=False,
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_data = next(
            (u for u in response.data if u["uuid"] == str(self.user.uuid)), None
        )
        self.assertIsNotNone(user_data)

        # Exposed attributes should be present
        self.assertIn("username", user_data)
        self.assertIn("full_name", user_data)
        self.assertIn("email", user_data)

        # Restricted attributes should be hidden
        self.assertNotIn("phone_number", user_data)
        self.assertNotIn("organization", user_data)
        self.assertNotIn("affiliations", user_data)

    def test_extended_attributes_exposed_when_in_config(self):
        """Extended attributes are exposed when configured."""
        # Create attribute config with extended fields enabled
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_phone_number=True,
            expose_organization=True,
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_data = next(
            (u for u in response.data if u["uuid"] == str(self.user.uuid)), None
        )
        self.assertIsNotNone(user_data)

        # All configured attributes should be present
        self.assertIn("username", user_data)
        self.assertIn("email", user_data)
        self.assertIn("phone_number", user_data)
        self.assertIn("organization", user_data)

    def test_union_of_multiple_offerings(self):
        """When multiple offerings exist, uses union (least restrictive)."""
        # Create first offering config - exposes phone_number but not organization
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_phone_number=True,
            expose_organization=False,
        )

        # Create second offering with different config - exposes organization but not phone
        offering2 = factories.OfferingFactory(
            customer=self.service_provider.customer,
            state=OfferingStates.ACTIVE,
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        models.OfferingUserAttributeConfig.objects.create(
            offering=offering2,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_phone_number=False,
            expose_organization=True,
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_data = next(
            (u for u in response.data if u["uuid"] == str(self.user.uuid)), None
        )
        self.assertIsNotNone(user_data)

        # Common attributes should be exposed
        self.assertIn("username", user_data)
        self.assertIn("full_name", user_data)
        self.assertIn("email", user_data)

        # Both phone_number and organization are in the union
        self.assertIn("phone_number", user_data)
        self.assertIn("organization", user_data)

    def test_uuid_and_projects_count_always_present(self):
        """Non-GDPR fields like uuid and projects_count are always present."""
        # Minimal config - only expose username
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=False,
            expose_email=False,
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_data = next(
            (u for u in response.data if u["uuid"] == str(self.user.uuid)), None
        )
        self.assertIsNotNone(user_data)

        # These fields should always be present regardless of GDPR config
        self.assertIn("uuid", user_data)
        self.assertIn("projects_count", user_data)

    def test_mixed_config_and_no_config_uses_union(self):
        """When one offering has config and another uses defaults, union is applied."""
        # First offering has config with organization enabled
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_phone_number=False,
            expose_organization=True,
        )

        # Second offering has NO config - will use defaults (username, full_name, email)
        factories.OfferingFactory(
            customer=self.service_provider.customer,
            state=OfferingStates.ACTIVE,
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        # No OfferingUserAttributeConfig created for offering2

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_data = next(
            (u for u in response.data if u["uuid"] == str(self.user.uuid)), None
        )
        self.assertIsNotNone(user_data)

        # Union of config1 and defaults: username, full_name, email, organization
        self.assertIn("username", user_data)
        self.assertIn("full_name", user_data)
        self.assertIn("email", user_data)

        # organization is in config1 - exposed via union
        self.assertIn("organization", user_data)

    def test_first_last_name_filtered_with_full_name(self):
        """first_name and last_name are filtered together with full_name via USER_ATTRIBUTE_EXTRA_FIELDS."""
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=False,  # Disable full_name → also hides first/last name
            expose_email=False,
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_data = next(
            (u for u in response.data if u["uuid"] == str(self.user.uuid)), None
        )
        self.assertIsNotNone(user_data)

        # first_name and last_name are linked to full_name via USER_ATTRIBUTE_EXTRA_FIELDS
        # When expose_full_name=False, all three should be filtered out
        self.assertNotIn("first_name", user_data)
        self.assertNotIn("last_name", user_data)
        self.assertNotIn("full_name", user_data)

    def test_affiliations_field_filtering(self):
        """Test that affiliations field is properly filtered."""
        # Config with affiliations disabled
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_affiliations=False,
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_data = next(
            (u for u in response.data if u["uuid"] == str(self.user.uuid)), None
        )
        self.assertIsNotNone(user_data)

        self.assertNotIn("affiliations", user_data)

    def test_affiliations_field_exposed_when_enabled(self):
        """Test that affiliations field is exposed when enabled."""
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_affiliations=True,
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_data = next(
            (u for u in response.data if u["uuid"] == str(self.user.uuid)), None
        )
        self.assertIsNotNone(user_data)

        self.assertIn("affiliations", user_data)
        self.assertEqual(user_data["affiliations"], ["Affiliation1"])

    def test_offerings_without_offering_user_feature_are_ignored(self):
        """Offerings without service_provider_can_create_offering_user are ignored."""
        # Config for main offering with phone enabled
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_phone_number=True,
        )

        # Create second offering WITHOUT service_provider_can_create_offering_user
        offering2 = factories.OfferingFactory(
            customer=self.service_provider.customer,
            state=OfferingStates.ACTIVE,
            plugin_options={"service_provider_can_create_offering_user": False},
        )
        # This config should be IGNORED since the offering doesn't have the feature
        models.OfferingUserAttributeConfig.objects.create(
            offering=offering2,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_phone_number=False,  # Would restrict phone if not ignored
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_data = next(
            (u for u in response.data if u["uuid"] == str(self.user.uuid)), None
        )
        self.assertIsNotNone(user_data)

        # phone_number should be exposed because offering2 is ignored
        self.assertIn("phone_number", user_data)

    def test_staff_user_sees_is_active_field(self):
        """Staff users can see the is_active field."""
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_data = next(
            (u for u in response.data if u["uuid"] == str(self.user.uuid)), None
        )
        self.assertIsNotNone(user_data)

        # Staff users should see is_active
        self.assertIn("is_active", user_data)

    def test_non_staff_user_cannot_see_is_active_field(self):
        """Non-staff users cannot see the is_active field."""
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_data = next(
            (u for u in response.data if u["uuid"] == str(self.user.uuid)), None
        )
        self.assertIsNotNone(user_data)

        # Non-staff users should NOT see is_active
        self.assertNotIn("is_active", user_data)

    def test_all_offerings_with_same_config_shows_all_fields(self):
        """When all offerings have same permissive config, all fields are shown."""
        # Both offerings expose everything
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_phone_number=True,
            expose_organization=True,
            expose_affiliations=True,
        )

        offering2 = factories.OfferingFactory(
            customer=self.service_provider.customer,
            state=OfferingStates.ACTIVE,
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        models.OfferingUserAttributeConfig.objects.create(
            offering=offering2,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_phone_number=True,
            expose_organization=True,
            expose_affiliations=True,
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_data = next(
            (u for u in response.data if u["uuid"] == str(self.user.uuid)), None
        )
        self.assertIsNotNone(user_data)

        # All configured fields should be present
        self.assertIn("username", user_data)
        self.assertIn("full_name", user_data)
        self.assertIn("email", user_data)
        self.assertIn("phone_number", user_data)
        self.assertIn("organization", user_data)
        self.assertIn("affiliations", user_data)

    def test_multiple_users_all_filtered_consistently(self):
        """All users in response have same fields filtered."""
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_phone_number=False,
        )

        # Create another user
        user2 = structure_factories.UserFactory(
            email="user2@example.com",
            phone_number="+9876543210",
        )
        self.resource.project.add_user(user2, ProjectRole.MEMBER)
        models.OfferingUser.objects.create(user=user2, offering=self.offering)

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Both users should have same fields filtered
        for user_data in response.data:
            self.assertIn("username", user_data)
            self.assertIn("email", user_data)
            self.assertNotIn("phone_number", user_data)

    def test_active_isds_exposed_when_enabled(self):
        """active_isds field is exposed when expose_active_isds=True in config."""
        self.user.active_isds = ["isd:puhuri", "isd:fenix"]
        self.user.save()

        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_active_isds=True,
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_data = next(
            (u for u in response.data if u["uuid"] == str(self.user.uuid)), None
        )
        self.assertIsNotNone(user_data)
        self.assertIn("active_isds", user_data)
        self.assertEqual(user_data["active_isds"], ["isd:puhuri", "isd:fenix"])

    def test_active_isds_hidden_when_not_enabled(self):
        """active_isds field is hidden when expose_active_isds=False."""
        self.user.active_isds = ["isd:puhuri"]
        self.user.save()

        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_active_isds=False,
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_data = next(
            (u for u in response.data if u["uuid"] == str(self.user.uuid)), None
        )
        self.assertIsNotNone(user_data)
        self.assertNotIn("active_isds", user_data)

    def test_organization_registry_code_exposed_when_enabled(self):
        """organization_registry_code is exposed when enabled in config."""
        self.user.organization_registry_code = "12345678"
        self.user.save()

        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_organization_registry_code=True,
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_data = next(
            (u for u in response.data if u["uuid"] == str(self.user.uuid)), None
        )
        self.assertIsNotNone(user_data)
        self.assertIn("organization_registry_code", user_data)
        self.assertEqual(user_data["organization_registry_code"], "12345678")

    def test_organization_registry_code_hidden_when_not_enabled(self):
        """organization_registry_code is hidden when not enabled in config."""
        self.user.organization_registry_code = "12345678"
        self.user.save()

        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_username=True,
            expose_full_name=True,
            expose_email=True,
            expose_organization_registry_code=False,
        )

        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_data = next(
            (u for u in response.data if u["uuid"] == str(self.user.uuid)), None
        )
        self.assertIsNotNone(user_data)
        self.assertNotIn("organization_registry_code", user_data)


@ddt
class ServiceProviderEndpointAllowedDomainsFieldTest(test.APITestCase):
    """Tests that allowed_domains is staff-only writable."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.service_provider = factories.ServiceProviderFactory(customer=self.customer)

    def _update(self, user, payload):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.ServiceProviderFactory.get_url(self.service_provider)
        response = self.client.patch(url, payload, format="json")
        self.service_provider.refresh_from_db()
        return response

    @data("staff")
    def test_authorized_user_can_set_allowed_domains(self, user):
        response = self._update(
            user, {"allowed_domains": ["example.com", "provider.org"]}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.service_provider.allowed_domains,
            ["example.com", "provider.org"],
        )

    @data("staff")
    def test_authorized_user_can_clear_allowed_domains(self, user):
        self.service_provider.allowed_domains = ["example.com"]
        self.service_provider.save()

        response = self._update(user, {"allowed_domains": []})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.service_provider.allowed_domains, [])

    @data("owner")
    def test_non_staff_cannot_change_allowed_domains(self, user):
        """Non-staff users can see the field but cant change (read_only)."""
        self.service_provider.allowed_domains = ["original.com"]
        self.service_provider.save()

        response = self._update(user, {"allowed_domains": ["attacker.com"]})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.service_provider.allowed_domains,
            ["original.com"],
        )

    def test_allowed_domains_visible_in_response_for_owner(self):
        self.service_provider.allowed_domains = ["example.com"]
        self.service_provider.save()

        self.client.force_authenticate(self.fixture.owner)
        url = factories.ServiceProviderFactory.get_url(self.service_provider)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("allowed_domains", response.data)
        self.assertEqual(response.data["allowed_domains"], ["example.com"])

    @data(["api.example.com", "api.provider.org", "example.com", "provider.org"])
    def test_staff_can_set_valid_subdomain(self, domain_list):
        for domain in domain_list:
            response = self._update("staff", {"allowed_domains": [domain]})
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            self.assertEqual(self.service_provider.allowed_domains, [domain])

    @data(
        [
            "localhost",
            "127.0.0.1",
            "localhost:8000",
            "127.0.0.1",
            "https://example.com/scim",
        ]
    )
    def test_invalid_domain_is_rejected(self, domain_list):
        for domain in domain_list:
            response = self._update("staff", {"allowed_domains": [domain]})
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OfferingEndpointDomainValidationTest(test.APITestCase):
    """Tests that OfferingAccessEndpoints can only be created under allowed domains."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        # Grant ADD_OFFERING_ENDPOINT permission to the offering owner role
        CustomerRole.OWNER.add_permission(PermissionEnum.ADD_OFFERING_ENDPOINT)
        self.url = factories.OfferingFactory.get_url(self.offering, "add_endpoint")

    def _add_endpoint(self, user, endpoint_url):
        self.client.force_authenticate(user)
        return self.client.post(
            self.url,
            {"name": "Test Endpoint", "url": endpoint_url},
            format="json",
        )

    def test_endpoint_can_be_added_when_no_domain_restriction_set(self):
        """When allowed_domains is empty, any domain is allowed."""
        self.fixture.service_provider.allowed_domains = []
        self.fixture.service_provider.save()

        response = self._add_endpoint(
            self.fixture.service_owner,
            "https://any-domain.example.com/api",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_endpoint_with_allowed_domain_is_accepted(self):
        self.fixture.service_provider.allowed_domains = ["provider.org"]
        self.fixture.service_provider.save()

        response = self._add_endpoint(
            self.fixture.service_owner,
            "https://provider.org/scim",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_endpoint_with_allowed_subdomain_is_accepted(self):
        """Subdomains of allowed domains should be permitted."""
        self.fixture.service_provider.allowed_domains = ["provider.org"]
        self.fixture.service_provider.save()

        response = self._add_endpoint(
            self.fixture.service_owner,
            "https://api.provider.org/scim",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_endpoint_with_disallowed_domain_is_rejected(self):
        self.fixture.service_provider.allowed_domains = ["provider.org"]
        self.fixture.service_provider.save()

        response = self._add_endpoint(
            self.fixture.service_owner,
            "https://attacker.com/steal-tokens",
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_endpoint_domain_rejection_includes_useful_message(self):
        self.fixture.service_provider.allowed_domains = ["provider.org"]
        self.fixture.service_provider.save()

        response = self._add_endpoint(
            self.fixture.service_owner,
            "https://attacker.com/endpoint",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attacker.com", str(response.data))
        self.assertIn("provider.org", str(response.data))

    def test_endpoint_with_multiple_allowed_domains(self):
        self.fixture.service_provider.allowed_domains = [
            "provider.org",
            "secondary.net",
        ]
        self.fixture.service_provider.save()

        response = self._add_endpoint(
            self.fixture.service_owner,
            "https://secondary.net/api",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_endpoint_with_nested_subdomain_is_accepted(self):
        """Nested subdomains of allowed domains should be permitted."""
        self.fixture.service_provider.allowed_domains = ["somedomain.test.com"]
        self.fixture.service_provider.save()

        response = self._add_endpoint(
            self.fixture.service_owner,
            "https://opentest.somedomain.test.com/api",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_partial_domain_match_is_rejected(self):
        """'fakeprovider.org' must not match allowed domain 'provider.org'."""
        self.fixture.service_provider.allowed_domains = ["provider.org"]
        self.fixture.service_provider.save()

        response = self._add_endpoint(
            self.fixture.service_owner,
            "https://fakeprovider.org",
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
