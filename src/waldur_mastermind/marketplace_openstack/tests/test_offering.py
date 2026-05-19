import uuid
from unittest import mock

from ddt import data, ddt
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_TENANT_OFFERING,
    BillingTypes,
    OfferingStates,
)
from waldur_mastermind.marketplace.management.commands.load_categories import (
    load_category,
)
from waldur_mastermind.marketplace.tests import (
    factories as marketplace_factories,
)
from waldur_mastermind.marketplace_openstack import (
    STORAGE_MODE_DYNAMIC,
    STORAGE_MODE_FIXED,
)
from waldur_openstack import models as openstack_models
from waldur_openstack.tests import factories as openstack_factories
from waldur_openstack.tests import fixtures as openstack_fixtures
from waldur_openstack.tests.factories import VolumeTypeFactory
from waldur_openstack.tests.fixtures import OpenStackFixture
from waldur_openstack.tests.unittests.test_backend import BaseBackendTestCase
from waldur_openstack.utils import volume_type_name_to_quota_name

from ...marketplace.enums import OPENSTACK_VOLUME_OFFERING
from .utils import BaseOpenStackTest, override_plugin_settings


class PlanComponentsTest(test.APITestCase):
    prices = {
        "cores": 10,
        "ram": 100,
        "storage": 1000,
    }
    quotas = prices

    def setUp(self):
        super().setUp()
        self.category = load_category("vpc")
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)

    def test_plan_components_are_validated(self):
        response = self.create_offering()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = marketplace_models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertEqual(offering.plans.first().components.count(), 3)

    def test_plan_components_have_parent(self):
        response = self.create_offering()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = marketplace_models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertEqual(3, offering.components.exclude(parent=None).count())

    def test_plan_without_components_is_valid(self):
        response = self.create_offering(False)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def create_offering(self, components=True):
        fixture = structure_fixtures.ProjectFixture()
        url = marketplace_factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(fixture.owner)
        marketplace_factories.ServiceProviderFactory(customer=fixture.customer)
        payload = {
            "name": "offering",
            "category": marketplace_factories.CategoryFactory.get_url(self.category),
            "customer": structure_factories.CustomerFactory.get_url(fixture.customer),
            "type": OPENSTACK_TENANT_OFFERING,
            "service_attributes": {
                "backend_url": "http://example.com/",
                "username": "root",
                "password": "secret",
                "tenant_name": "admin",
                "external_network_id": uuid.uuid4(),
            },
            "plans": [
                {
                    "name": "small",
                    "description": "CPU 1",
                    "unit": UnitPriceMixin.Units.PER_DAY,
                    "unit_price": 1010100,
                }
            ],
        }
        if components:
            payload["plans"][0]["prices"] = self.prices
        with mock.patch("waldur_core.structure.models.ServiceSettings.get_backend"):
            return self.client.post(url, payload)


@ddt
class OpenStackResourceOfferingTest(BaseOpenStackTest):
    @data(OPENSTACK_INSTANCE_OFFERING, OPENSTACK_VOLUME_OFFERING)
    def test_offering_is_created_when_tenant_creation_is_completed(self, offering_type):
        tenant = self.trigger_offering_creation()

        offering = marketplace_models.Offering.objects.get(type=offering_type)

        self.assertEqual(offering.scope, tenant)
        self.assertEqual(offering.customer, tenant.project.customer)

    @data(OPENSTACK_INSTANCE_OFFERING, OPENSTACK_VOLUME_OFFERING)
    @override_plugin_settings(AUTOMATICALLY_CREATE_PRIVATE_OFFERING=False)
    def test_offering_is_not_created_if_feature_is_disabled(self, offering_type):
        self.trigger_offering_creation()

        self.assertRaises(
            marketplace_models.Offering.DoesNotExist,
            lambda: marketplace_models.Offering.objects.get(type=offering_type),
        )

    @data(OPENSTACK_INSTANCE_OFFERING, OPENSTACK_VOLUME_OFFERING)
    def test_offering_is_not_created_if_tenant_is_not_created_via_marketplace(
        self, offering_type
    ):
        fixture = OpenStackFixture()
        tenant = openstack_models.Tenant.objects.create(
            service_settings=fixture.settings,
            project=fixture.project,
            state=CoreStates.CREATING,
        )

        tenant.set_ok()
        tenant.save()

        self.assertRaises(
            ObjectDoesNotExist,
            marketplace_models.Offering.objects.get,
            type=offering_type,
        )

    @data(OPENSTACK_INSTANCE_OFFERING, OPENSTACK_VOLUME_OFFERING)
    def test_offering_is_archived_when_tenant_is_deleted(self, offering_type):
        tenant = self.trigger_offering_creation()
        tenant.delete()
        offering = marketplace_models.Offering.objects.get(type=offering_type)
        self.assertEqual(offering.state, OfferingStates.ARCHIVED)

    @data(OPENSTACK_INSTANCE_OFFERING, OPENSTACK_VOLUME_OFFERING)
    def test_recreates_offering_when_existing_one_is_archived(self, offering_type):
        from waldur_mastermind.marketplace_openstack import utils

        tenant = self.trigger_offering_creation()
        offering = marketplace_models.Offering.objects.get(
            type=offering_type, scope=tenant
        )
        offering.state = OfferingStates.ARCHIVED
        offering.save(update_fields=["state"])

        utils.create_offerings_for_volume_and_instance(tenant)

        live_offerings = marketplace_models.Offering.objects.filter(
            type=offering_type, scope=tenant
        ).exclude(state=OfferingStates.ARCHIVED)
        self.assertEqual(live_offerings.count(), 1)

    def trigger_offering_creation(self):
        fixture = OpenStackFixture()
        tenant = openstack_models.Tenant.objects.create(
            service_settings=fixture.settings,
            project=fixture.project,
            state=CoreStates.CREATING,
        )
        resource = marketplace_factories.ResourceFactory(scope=tenant)
        marketplace_factories.OrderFactory(resource=resource)

        tenant.set_ok()
        tenant.save()
        return tenant


class OfferingComponentForVolumeTypeTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING,
            scope=self.fixture.settings,
            plugin_options={"storage_mode": STORAGE_MODE_DYNAMIC},
        )
        self.volume_type = self.fixture.volume_type

    def test_offering_component_for_volume_type_is_created(self):
        component = marketplace_models.OfferingComponent.objects.get(
            scope=self.volume_type
        )
        self.assertEqual(component.offering, self.offering)
        self.assertEqual(
            component.billing_type,
            BillingTypes.LIMIT,
        )
        self.assertEqual(component.name, "Storage (%s)" % self.volume_type.name)
        self.assertEqual(
            component.type, volume_type_name_to_quota_name(self.volume_type.name)
        )

    def test_offering_component_for_volume_type_is_not_created_if_storage_mode_is_fixed(
        self,
    ):
        self.offering.plugin_options = {"storage_mode": STORAGE_MODE_FIXED}
        self.offering.save()

        new_volume_type = VolumeTypeFactory(settings=self.fixture.settings)

        self.assertFalse(
            marketplace_models.OfferingComponent.objects.filter(
                scope=new_volume_type
            ).exists()
        )

    def test_offering_component_name_is_updated(self):
        self.volume_type.name = "new name"
        self.volume_type.save()
        component = marketplace_models.OfferingComponent.objects.get(
            scope=self.volume_type
        )
        self.assertEqual(component.name, "Storage (%s)" % self.volume_type.name)

    def test_offering_component_is_deleted(self):
        self.volume_type.delete()
        self.assertRaises(
            marketplace_models.OfferingComponent.DoesNotExist,
            marketplace_models.OfferingComponent.objects.get,
            scope=self.volume_type,
        )

    def set_storage_mode(self, storage_mode):
        url = marketplace_factories.OfferingFactory.get_url(
            self.offering, "update_integration"
        )
        new_options = {
            "plugin_options": {"storage_mode": storage_mode},
        }

        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(url, new_options)

    def test_switch_from_fixed_to_dynamic_billing(self):
        self.offering.plugin_options = {"storage_mode": STORAGE_MODE_FIXED}
        response = self.set_storage_mode(STORAGE_MODE_DYNAMIC)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.plugin_options["storage_mode"], STORAGE_MODE_DYNAMIC
        )

    def test_switch_from_dynamic_to_fixed_billing(self):
        self.offering.plugin_options = {"storage_mode": STORAGE_MODE_DYNAMIC}
        response = self.set_storage_mode(STORAGE_MODE_FIXED)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.plugin_options["storage_mode"], STORAGE_MODE_FIXED
        )


class OfferingCreateTest(BaseBackendTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.customer_url = structure_factories.CustomerFactory.get_url(
            customer=self.fixture.customer
        )
        marketplace_factories.ServiceProviderFactory(customer=self.fixture.customer)
        self.category_url = marketplace_factories.CategoryFactory.get_url()
        self.url = marketplace_factories.OfferingFactory.get_list_url()
        patcher = mock.patch(
            "waldur_mastermind.marketplace_openstack.views.TenantCreateExecutor"
        )
        patcher.start()

    def tearDown(self):
        mock.patch.stopall()

    def test_create_offering(self):
        payload = self._get_payload()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            marketplace_models.Offering.objects.filter(name="TEST").exists()
        )
        self.assertTrue(
            marketplace_models.OfferingComponent.objects.filter(
                offering__name="TEST"
            ).exists()
        )
        component = marketplace_models.OfferingComponent.objects.get(
            offering__name="TEST", type="cores"
        )
        self.assertEqual(component.article_code, "artcode1")
        self.assertEqual(component.min_value, 1)
        self.assertEqual(component.max_value, 100)
        self.assertEqual(component.max_available_limit, 200)

    def _get_payload(self):
        return {
            "name": "TEST",
            "category": self.category_url,
            "customer": self.customer_url,
            "type": OPENSTACK_TENANT_OFFERING,
            "service_attributes": {
                "backend_url": "https://193.0.0.1:5000/v3/",
                "username": "admin",
                "password": "password",
                "tenant_name": "admin",
                "external_network_id": "admin",
            },
            "shared": True,
            "attributes": {},
            "plugin_options": {"storage_mode": "fixed"},
            "components": [
                {
                    "type": "cores",
                    "name": "Cores",
                    "measured_unit": "cores",
                    "billing_type": "limit",
                    "limit_period": None,
                    "article_code": "artcode1",
                    "min_value": 1,
                    "max_value": 100,
                    "max_available_limit": 200,
                },
                {
                    "type": "ram",
                    "name": "RAM",
                    "measured_unit": "GB",
                    "billing_type": "limit",
                    "limit_period": None,
                    "article_code": "artcode2",
                    "min_value": 1024,
                    "max_value": 102400,
                    "max_available_limit": 204800,
                },
                {
                    "type": "storage",
                    "name": "Storage",
                    "measured_unit": "GB",
                    "billing_type": "limit",
                    "limit_period": None,
                    "article_code": "artcode3",
                    "min_value": 1024,
                    "max_value": 102400,
                    "max_available_limit": 204800,
                },
            ],
        }

    def test_create_offering_with_limits(self):
        payload = self._get_payload()
        payload.pop("components")
        payload["limits"] = {
            "cores": {"min": 1, "max": 100, "max_available_limit": 200},
            "ram": {"min": 1024, "max": 102400, "max_available_limit": 204800},
            "storage": {"min": 1024, "max": 102400, "max_available_limit": 204800},
        }
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            marketplace_models.Offering.objects.filter(name="TEST").exists()
        )
        self.assertTrue(
            marketplace_models.OfferingComponent.objects.filter(
                offering__name="TEST"
            ).exists()
        )
        component = marketplace_models.OfferingComponent.objects.get(
            offering__name="TEST", type="cores"
        )
        self.assertEqual(component.min_value, 1)
        self.assertEqual(component.max_value, 100)
        self.assertEqual(component.max_available_limit, 200)


@ddt
class OfferingUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING, scope=self.fixture.settings
        )
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="cores",
            article_code="article_code",
        )
        self.url = marketplace_factories.OfferingFactory.get_url(
            self.offering, "update_offering_component"
        )

    def test_update_article_code(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url,
            {
                "uuid": self.component.uuid.hex,
                "article_code": "new_article_code",
            },
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.component.refresh_from_db()
        self.assertEqual(self.component.article_code, "new_article_code")

    def test_validate_extra_components(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url,
            {
                "type": "extra",
                "name": "extra",
                "measured_unit": "hours",
                "billing_type": "fixed",
            },
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OfferingDetailsTest(test.APITestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING, scope=self.fixture.settings
        )
        marketplace_factories.OfferingComponentFactory(
            offering=self.offering, type="cores"
        )
        marketplace_factories.OfferingComponentFactory(
            offering=self.offering, type="ram"
        )
        marketplace_factories.OfferingComponentFactory(
            offering=self.offering, type="storage"
        )
        marketplace_factories.OfferingComponentFactory(
            offering=self.offering, type="gigabytes_ssd"
        )
        self.url = marketplace_factories.OfferingFactory.get_url(offering=self.offering)

    def test_when_storage_mode_is_fixed_offering_components_are_filtered(self):
        self.offering.plugin_options["storage_mode"] = STORAGE_MODE_FIXED
        self.offering.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        actual_types = {component["type"] for component in response.data["components"]}
        expected_types = {"cores", "ram", "storage"}
        self.assertEqual(actual_types, expected_types)

    def test_when_storage_mode_is_dynamic_offering_components_are_filtered(self):
        self.offering.plugin_options["storage_mode"] = STORAGE_MODE_DYNAMIC
        self.offering.save()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        actual_types = {component["type"] for component in response.data["components"]}
        expected_types = {"cores", "ram", "gigabytes_ssd"}
        self.assertEqual(actual_types, expected_types)


@ddt
class OfferingNameTest(test.APITestCase):
    def setUp(self):
        self.fixture = OpenStackFixture()

    @data(OPENSTACK_INSTANCE_OFFERING, OPENSTACK_VOLUME_OFFERING)
    def test_renaming_openstack_tenant_should_also_rename_linked_private_offerings(
        self, offering_type
    ):
        offering = marketplace_factories.OfferingFactory(
            type=offering_type,
            scope=self.fixture.tenant,
        )
        self.fixture.tenant.name = "new_name"
        self.fixture.tenant.save()
        offering.refresh_from_db()
        self.assertTrue("new_name" in offering.name)


class RouterExternalIPTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.UserFixture()
        self.router = openstack_factories.RouterFactory(fixed_ips=["100.100.100.1"])
        self.external_ips = [
            {
                "floating_ip": "100.100.100.0/24",
                "external_ip": "200.200.200.0/24",
            }
        ]
        self.offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING,
            secret_options={"ipv4_external_ip_mapping": self.external_ips},
        )
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering, scope=self.router.tenant
        )
        self.url = openstack_factories.RouterFactory.get_url(self.router)
        self.client.force_authenticate(self.fixture.staff)

    def test_external_ips_has_been_added(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["offering_external_ips"], ["200.200.200.1"])

    def test_external_ips_has_not_been_added(self):
        self.router.fixed_ips = ["1.100.100.1"]
        self.router.save()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["offering_external_ips"], [])


class InstanceExternalIPTest(test.APITestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.instance = self.fixture.instance
        self.external_ips = [
            {
                "floating_ip": "100.100.100.0/24",
                "external_ip": "200.200.200.0/24",
            }
        ]
        self.parent_offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING,
            secret_options={"ipv4_external_ip_mapping": self.external_ips},
        )
        self.offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING,
            parent=self.parent_offering,
        )
        self.resource = marketplace_factories.ResourceFactory(offering=self.offering)
        self.resource.scope = self.fixture.instance
        self.resource.save()

        self.url = marketplace_factories.ResourceFactory.get_url(
            self.resource, "details"
        )
        self.client.force_authenticate(self.fixture.staff)

    def test_external_ips_has_been_added(self):
        floating_ip = openstack_factories.FloatingIPFactory(
            port=self.fixture.port,
            address="100.100.100.1",
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            state=CoreStates.OK,
        )
        floating_ip.refresh_from_db()
        self.assertEqual(floating_ip.external_address, "200.200.200.1")
        self.parent_offering.secret_options["ipv4_external_ip_mapping"] = [
            {
                "floating_ip": "100.100.100.0/24",
                "external_ip": "250.250.250.0/24",
            }
        ]
        self.parent_offering.save()
        floating_ip.refresh_from_db()
        self.assertEqual(floating_ip.external_address, "250.250.250.1")

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["external_address"], {"250.250.250.1"})

    def test_external_ips_has_not_been_added(self):
        floating_ip = openstack_factories.FloatingIPFactory(
            port=self.fixture.port,
            address="1.100.100.1",
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            state=CoreStates.OK,
        )
        floating_ip.refresh_from_db()
        self.assertEqual(floating_ip.external_address, None)

    def test_filter(self):
        openstack_factories.FloatingIPFactory(
            port=self.fixture.port,
            address="100.100.100.1",
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            state=CoreStates.OK,
        )
        openstack_factories.FloatingIPFactory(
            port=self.fixture.port,
            address="100.100.100.2",
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            state=CoreStates.OK,
        )
        marketplace_factories.ResourceFactory()
        url = marketplace_factories.ResourceFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        response = self.client.get(url, {"query": "200.200.200.2"})
        self.assertEqual(len(response.data), 1)


class ImportedFloatingIPExternalMappingTest(test.APITestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.parent_offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING,
            secret_options={
                "ipv4_external_ip_mapping": [
                    {
                        "floating_ip": "100.100.100.0/24",
                        "external_ip": "200.200.200.0/24",
                    }
                ]
            },
        )
        self.offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING,
            parent=self.parent_offering,
        )
        self.resource = marketplace_factories.ResourceFactory(offering=self.offering)

    def test_imported_floating_ip_gets_external_address_on_creation(self):
        # Simulate creating a floating IP via import (created=True signal)
        floating_ip = openstack_factories.FloatingIPFactory(
            port=self.fixture.port,
            address="100.100.100.50",
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            state=CoreStates.OK,
        )

        # Create marketplace resource for the instance
        marketplace_factories.ResourceFactory(
            offering=self.offering,
            scope=self.fixture.instance,
        )

        # Trigger the signal manually as if the floating IP was just imported
        from waldur_mastermind.marketplace_openstack.handlers import (
            update_floating_ip_external_addresses,
        )

        update_floating_ip_external_addresses(
            sender=floating_ip.__class__, instance=floating_ip, created=True
        )

        floating_ip.refresh_from_db()
        self.assertEqual(floating_ip.external_address, "200.200.200.50")

    def test_imported_floating_ip_without_matching_network_gets_no_external_address(
        self,
    ):
        # Simulate creating a floating IP via import with non-matching IP
        floating_ip = openstack_factories.FloatingIPFactory(
            port=self.fixture.port,
            address="192.168.1.50",  # Different network
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            state=CoreStates.OK,
        )

        # Create marketplace resource for the instance
        marketplace_factories.ResourceFactory(
            offering=self.offering,
            scope=self.fixture.instance,
        )

        # Trigger the signal manually as if the floating IP was just imported
        from waldur_mastermind.marketplace_openstack.handlers import (
            update_floating_ip_external_addresses,
        )

        update_floating_ip_external_addresses(
            sender=floating_ip.__class__, instance=floating_ip, created=True
        )

        floating_ip.refresh_from_db()
        self.assertEqual(floating_ip.external_address, None)


class UpdateSecretOptionsTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.UserFixture()
        self.secret_options = {
            "ipv4_external_ip_mapping": [
                {
                    "floating_ip": "100.100.100.0/24",
                    "external_ip": "200.200.200.0/24",
                }
            ]
        }
        self.offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING,
        )
        self.url = marketplace_factories.OfferingFactory.get_url(
            self.offering, "update_integration"
        )
        self.client.force_authenticate(self.fixture.staff)

    def test_update_ipv4_external_ip_mapping(self):
        response = self.client.post(
            self.url, data={"secret_options": self.secret_options}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.secret_options, self.secret_options)


class OfferingPluginOptionsMaxSecurityGroupsTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING,
            customer=self.fixture.customer,
        )
        self.url = marketplace_factories.OfferingFactory.get_url(
            self.offering, "update_integration"
        )
        self.client.force_authenticate(self.fixture.staff)

    def test_offering_max_security_groups_integration(self):
        """Test that max_security_groups can be set and retrieved through plugin_options"""
        plugin_options = {
            "max_instances": 5,
            "max_volumes": 10,
            "max_security_groups": 20,
        }

        response = self.client.post(self.url, data={"plugin_options": plugin_options})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.plugin_options["max_instances"], 5)
        self.assertEqual(self.offering.plugin_options["max_volumes"], 10)
        self.assertEqual(self.offering.plugin_options["max_security_groups"], 20)

    def test_offering_max_security_groups_serializer_validation(self):
        """Test that max_security_groups is properly validated by the serializer"""
        # Test with invalid (negative) value
        plugin_options = {
            "max_security_groups": -1,
        }

        response = self.client.post(self.url, data={"plugin_options": plugin_options})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Test with valid value
        plugin_options = {
            "max_security_groups": 25,
        }

        response = self.client.post(self.url, data={"plugin_options": plugin_options})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.plugin_options["max_security_groups"], 25)


class ConfigDriveDefaultPublicFieldTest(test.APITestCase):
    """config_drive_default reflects the provider service_settings.options value."""

    def _create_offering(self, options=None):
        offering = marketplace_factories.OfferingFactory(state=OfferingStates.ACTIVE)
        if options is not None:
            offering.scope = structure_factories.ServiceSettingsFactory(options=options)
            offering.save()
        return offering

    def test_returns_true_when_provider_enables_config_drive(self):
        offering = self._create_offering(options={"config_drive": True})
        url = marketplace_factories.OfferingFactory.get_public_url(offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIs(response.data["config_drive_default"], True)

    def test_returns_false_when_provider_disables_config_drive(self):
        offering = self._create_offering(options={"config_drive": False})
        url = marketplace_factories.OfferingFactory.get_public_url(offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIs(response.data["config_drive_default"], False)

    def test_returns_false_when_option_unset(self):
        offering = self._create_offering(options={})
        url = marketplace_factories.OfferingFactory.get_public_url(offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIs(response.data["config_drive_default"], False)

    def test_returns_false_when_offering_has_no_service_settings(self):
        offering = self._create_offering(options=None)
        url = marketplace_factories.OfferingFactory.get_public_url(offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIs(response.data["config_drive_default"], False)
