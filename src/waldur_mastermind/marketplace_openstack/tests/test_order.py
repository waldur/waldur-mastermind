from unittest import mock

from ddt import data, ddt
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers as rf_serializers
from rest_framework import status, test
from rest_framework.request import Request

from waldur_core.core.enums import CoreStates
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_TENANT_OFFERING,
    OPENSTACK_VOLUME_OFFERING,
    BillingTypes,
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.utils import (
    create_offering_components,
    validate_order,
)
from waldur_mastermind.marketplace_openstack import handlers
from waldur_mastermind.marketplace_openstack.processors import InstanceDeleteProcessor
from waldur_mastermind.marketplace_openstack.tests.utils import BaseOpenStackTest
from waldur_mastermind.marketplace_openstack.utils import map_limits_to_quotas
from waldur_openstack import models as openstack_models
from waldur_openstack.exceptions import OpenStackBackendError
from waldur_openstack.tests import factories as openstack_factories
from waldur_openstack.tests import (
    fixtures as openstack_fixtures,
)
from waldur_openstack.tests.helpers import override_openstack_settings

from .. import (
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_MODE_DYNAMIC,
    STORAGE_MODE_FIXED,
    STORAGE_TYPE,
)


class TenantGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING
        )
        self.order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            offering=self.offering,
            attributes=dict(user_username="admin", user_userpassword="secret"),
        )

    def get_order(self):
        self.client.force_login(self.fixture.manager)
        url = marketplace_factories.OrderFactory.get_url(self.order)
        return self.client.get(url)

    @override_openstack_settings(TENANT_CREDENTIALS_VISIBLE=True)
    def test_secret_attributes_are_rendered(self):
        response = self.get_order()
        self.assertTrue("user_username" in response.data["attributes"])

    @override_openstack_settings(TENANT_CREDENTIALS_VISIBLE=False)
    def test_secret_attributes_are_not_rendered(self):
        response = self.get_order()
        self.assertFalse("user_username" in response.data["attributes"])


@ddt
class TenantCreateTest(BaseOpenStackTest):
    def setUp(self):
        super().setUp()
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.offering = marketplace_factories.OfferingFactory(
            scope=self.fixture.settings,
            type=OPENSTACK_TENANT_OFFERING,
            state=OfferingStates.ACTIVE,
            plugin_options={"storage_mode": STORAGE_MODE_DYNAMIC},
        )
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        create_offering_components(self.offering)

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.MEMBER.add_permission(PermissionEnum.CREATE_ORDER)

    @data("staff", "owner", "manager", "admin")
    def test_order_is_created(self, user):
        response = self.create_order(user=user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @override_openstack_settings(TENANT_CREDENTIALS_VISIBLE=True)
    def test_mandatory_attributes_are_checked(self):
        response = self.create_order(dict(user_username=None))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue("user_username" in response.data)

    def test_limits_are_not_checked_if_offering_components_limits_are_not_defined(self):
        response = self.create_order(
            limits={"cores": 2, "ram": 1024 * 10, "storage": 1024 * 1024 * 10}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_limits_are_checked_against_offering_components(self):
        self.offering.components.filter(type=CORES_TYPE).update(max_value=10)
        self.offering.components.filter(type=RAM_TYPE).update(max_value=1024 * 10)
        self.offering.components.filter(type=STORAGE_TYPE).update(
            max_value=1024 * 1024 * 10
        )

        response = self.create_order(
            limits={"cores": 20, "ram": 1024 * 100, "storage": 1024 * 1024 * 100}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_max_available_limit_is_checked_against_offering_components(self):
        self.offering.components.filter(type=CORES_TYPE).update(max_value=50)
        self.offering.components.filter(type=RAM_TYPE).update(max_value=1024 * 10)
        self.offering.components.filter(type=STORAGE_TYPE).update(
            max_value=1024 * 1024 * 10
        )
        self.offering.components.filter(type=CORES_TYPE).update(max_available_limit=35)

        response = self.create_order(
            limits={"cores": 40, "ram": 1024 * 5, "storage": 1024 * 1024 * 5}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def create_plan_component(self, type, price):
        return marketplace_factories.PlanComponentFactory(
            component=self.offering.components.get(type=type),
            plan=self.plan,
            price=price,
        )

    def test_cost_estimate_is_calculated_using_limits(self):
        self.create_plan_component(CORES_TYPE, 1)
        self.create_plan_component(RAM_TYPE, 0.5)
        self.create_plan_component(STORAGE_TYPE, 0.1)

        response = self.create_order(
            limits={"cores": 20, "ram": 1024 * 100, "storage": 1024 * 10000}
        )
        expected = 20 * 1 + 100 * 0.5 + 10000 * 0.1
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(float(response.data["cost"]), expected)

    def test_cost_estimate_is_calculated_using_dynamic_storage(self):
        self.create_plan_component(CORES_TYPE, 1)
        self.create_plan_component(RAM_TYPE, 0.5)
        marketplace_models.OfferingComponent.objects.create(
            offering=self.offering,
            type="gigabytes_llvm",
            billing_type=BillingTypes.LIMIT,
        )
        self.create_plan_component("gigabytes_llvm", 0.1)

        response = self.create_order(
            limits={"cores": 20, "ram": 1024 * 100, "gigabytes_llvm": 10000}
        )

        expected = 20 * 1 + 100 * 0.5 + 10000 * 0.1
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(float(response.data["cost"]), expected)

    def create_order(self, add_attributes=None, user="staff", limits=None):
        project_url = structure_factories.ProjectFactory.get_url(self.fixture.project)
        offering_url = marketplace_factories.OfferingFactory.get_public_url(
            self.offering
        )
        plan_url = marketplace_factories.PlanFactory.get_public_url(self.plan)

        attributes = dict(
            name="My first VPC",
            description="Database cluster",
            user_username="admin_user",
        )
        if add_attributes:
            attributes.update(add_attributes)

        payload = {
            "project": project_url,
            "offering": offering_url,
            "plan": plan_url,
            "attributes": attributes,
        }
        if not limits:
            limits = {"cores": 2, "ram": 1024 * 10, "storage": 1024 * 1024 * 10}
        payload["limits"] = limits

        self.client.force_login(getattr(self.fixture, user))
        url = marketplace_factories.OrderFactory.get_list_url()
        return self.client.post(url, payload)

    def test_when_order_is_approved_openstack_tenant_is_created(self):
        # Arrange
        attributes = dict(
            name="My first VPC",
            description="Database cluster",
            user_username="admin_user",
        )
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            offering=self.offering,
            attributes=attributes,
            plan=self.plan,
            state=OrderStates.EXECUTING,
            limits={"cores": 2, "ram": 1024 * 10, "storage": 1024 * 1024 * 10},
        )

        marketplace_utils.process_order(order, self.fixture.staff)

        # Assert
        order.refresh_from_db()
        self.assertTrue(isinstance(order.resource.scope, openstack_models.Tenant))

    def test_order_set_state_done(self):
        tenant = openstack_factories.TenantFactory()
        resource = marketplace_factories.ResourceFactory(scope=tenant)

        order = marketplace_factories.OrderFactory(resource=resource)
        order.set_state_executing()
        order.save()

        order.review_by_consumer()
        order.save()

        tenant.state = CoreStates.CREATING
        tenant.save()

        tenant.state = CoreStates.OK
        tenant.save()

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.DONE)

        order.resource.refresh_from_db()
        self.assertEqual(order.resource.state, ResourceStates.OK)

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.DONE)

    def test_volume_type_limits_are_propagated(self):
        marketplace_models.OfferingComponent.objects.create(
            offering=self.offering,
            type="gigabytes_llvm",
            billing_type=BillingTypes.LIMIT,
        )

        response = self.create_order(
            limits={"cores": 2, "ram": 1024 * 10, "gigabytes_llvm": 10}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        order = marketplace_models.Order.objects.get(uuid=response.data["uuid"])
        marketplace_utils.process_order(order, self.fixture.staff)

        tenant: openstack_models.Tenant = order.resource.scope
        self.assertEqual(tenant.get_quota_limit("gigabytes_llvm"), 10)

    def test_volume_type_limits_are_initialized_with_zero_by_default(self):
        openstack_factories.VolumeTypeFactory(settings=self.offering.scope, name="llvm")
        openstack_factories.VolumeTypeFactory(settings=self.offering.scope, name="ssd")
        openstack_factories.VolumeTypeFactory(settings=self.offering.scope, name="rbd")

        response = self.create_order(
            limits={"cores": 2, "ram": 1024 * 10, "gigabytes_llvm": 10}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        order = marketplace_models.Order.objects.get(uuid=response.data["uuid"])
        marketplace_utils.process_order(order, self.fixture.staff)

        tenant: openstack_models.Tenant = order.resource.scope
        self.assertEqual(tenant.get_quota_limit("gigabytes_llvm"), 10)
        self.assertEqual(tenant.get_quota_limit("gigabytes_ssd"), 0)
        self.assertEqual(tenant.get_quota_limit("gigabytes_rbd"), 0)

        # quota_limits is the dict the tenant-create executor hands to
        # push_tenant_quotas, so a zeroed volume type must reach Cinder as 0.
        pushed = tenant.quota_limits
        self.assertEqual(pushed["gigabytes_llvm"], 10)
        self.assertEqual(pushed["gigabytes_ssd"], 0)
        self.assertEqual(pushed["gigabytes_rbd"], 0)

    def test_volume_type_limits_zero_is_preserved(self):
        openstack_factories.VolumeTypeFactory(settings=self.offering.scope, name="ssd")
        openstack_factories.VolumeTypeFactory(settings=self.offering.scope, name="rbd")

        response = self.create_order(
            limits={
                "cores": 2,
                "ram": 1024 * 10,
                "gigabytes_ssd": 1,
                "gigabytes_rbd": 0,
            }
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        order = marketplace_models.Order.objects.get(uuid=response.data["uuid"])
        marketplace_utils.process_order(order, self.fixture.staff)

        tenant: openstack_models.Tenant = order.resource.scope
        self.assertEqual(tenant.get_quota_limit("gigabytes_ssd"), 1)
        self.assertEqual(tenant.get_quota_limit("gigabytes_rbd"), 0)

        pushed = tenant.quota_limits
        self.assertEqual(pushed["gigabytes_ssd"], 1)
        self.assertEqual(pushed["gigabytes_rbd"], 0)

    def test_create_pushes_the_same_quotas_as_an_update_with_the_same_limits(self):
        openstack_factories.VolumeTypeFactory(settings=self.offering.scope, name="ssd")
        openstack_factories.VolumeTypeFactory(settings=self.offering.scope, name="rbd")
        limits = {"cores": 2, "ram": 1024 * 10, "gigabytes_ssd": 0, "gigabytes_rbd": 10}

        response = self.create_order(limits=limits)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        order = marketplace_models.Order.objects.get(uuid=response.data["uuid"])
        marketplace_utils.process_order(order, self.fixture.staff)

        tenant: openstack_models.Tenant = order.resource.scope
        created = tenant.quota_limits
        updated = map_limits_to_quotas(limits, self.offering, is_create=False)

        for name, value in updated.items():
            self.assertEqual(created[name], value, name)

    def test_volume_type_limits_unset_in_fixed_storage_mode(self):
        self.offering.plugin_options["storage_mode"] = STORAGE_MODE_FIXED
        self.offering.save()

        openstack_factories.VolumeTypeFactory(settings=self.offering.scope, name="ssd")

        response = self.create_order(
            limits={
                "cores": 2,
                "ram": 1024 * 10,
                "storage": 1024 * 10,
            }
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        order = marketplace_models.Order.objects.get(uuid=response.data["uuid"])
        marketplace_utils.process_order(order, self.fixture.staff)

        tenant: openstack_models.Tenant = order.resource.scope
        self.assertEqual(tenant.get_quota_limit("gigabytes_ssd"), -1)


class TenantMutateTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING
        )
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.tenant,
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
        )


class TenantDeleteTest(TenantMutateTest):
    def setUp(self):
        super().setUp()
        self.order = marketplace_factories.OrderFactory(
            resource=self.resource,
            project=self.fixture.project,
            state=OrderStates.EXECUTING,
            type=OrderTypes.TERMINATE,
        )

    def test_deletion_is_scheduled(self):
        self.trigger_deletion()
        self.assertEqual(self.order.state, OrderStates.EXECUTING)
        self.assertEqual(self.resource.state, ResourceStates.TERMINATING)
        self.assertEqual(self.tenant.state, CoreStates.DELETION_SCHEDULED)

    def test_deletion_is_completed(self):
        self.trigger_deletion()
        self.tenant.delete()

        self.order.refresh_from_db()
        self.resource.refresh_from_db()

        self.assertEqual(self.order.state, OrderStates.DONE)
        self.assertEqual(self.resource.state, ResourceStates.TERMINATED)
        self.assertRaises(ObjectDoesNotExist, self.tenant.refresh_from_db)

    def trigger_deletion(self):
        marketplace_utils.process_order(self.order, self.fixture.staff)

        self.order.refresh_from_db()
        self.resource.refresh_from_db()
        self.tenant.refresh_from_db()


class InstanceCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.service_settings = self.fixture.tenant.service_settings
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.MEMBER.add_permission(PermissionEnum.CREATE_ORDER)

    def test_instance_order_via_api_does_not_require_plan(self):
        """
        Test that private offerings (like OpenStack instances) can be created without a plan.
        Private offerings (shared=False) are typically auto-created and inherit plans from parent resources.
        """
        tenant_offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING,
            scope=self.service_settings,
            state=OfferingStates.ACTIVE,
            customer=self.fixture.customer,
        )

        # Create private instance offering (shared=False, no plan required)
        instance_offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING,
            scope=self.tenant,
            parent=tenant_offering,
            state=OfferingStates.ACTIVE,
            shared=False,
            customer=self.fixture.customer,
            project=self.fixture.project,
        )

        subnet_url = openstack_factories.SubNetFactory.get_url(self.fixture.subnet)
        attributes = {
            "flavor": openstack_factories.FlavorFactory.get_url(self.fixture.flavor),
            "image": openstack_factories.ImageFactory.get_url(self.fixture.image),
            "name": "virtual-machine",
            "system_volume_size": self.fixture.image.min_disk,
            "ports": [{"subnet": subnet_url}],
            "ssh_public_key": structure_factories.SshPublicKeyFactory.get_url(
                structure_factories.SshPublicKeyFactory(user=self.fixture.manager)
            ),
        }

        # Create order WITHOUT plan - should succeed for private offerings
        self.client.force_authenticate(self.fixture.owner)
        url = marketplace_factories.OrderFactory.get_list_url()
        payload = {
            "project": structure_factories.ProjectFactory.get_url(self.fixture.project),
            "offering": marketplace_factories.OfferingFactory.get_public_url(
                instance_offering
            ),
            "attributes": attributes,
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        order = marketplace_models.Order.objects.get(uuid=response.data["uuid"])
        self.assertIsNone(order.plan)

    def test_instance_is_created_when_order_is_processed(self):
        order = self.trigger_instance_creation()
        self.assertEqual(order.state, OrderStates.EXECUTING, order.error_message)
        self.assertTrue(
            openstack_models.Instance.objects.filter(name="virtual-machine").exists()
        )

    def test_availability_zone_is_passed_to_plugin(self):
        availability_zone = openstack_factories.InstanceAvailabilityZoneFactory(
            tenant=self.tenant
        )
        az_url = openstack_factories.InstanceAvailabilityZoneFactory.get_url(
            availability_zone
        )
        order = self.trigger_instance_creation(availability_zone=az_url)
        self.assertEqual(order.resource.scope.availability_zone, availability_zone)

    def test_metadata_is_passed_to_plugin(self):
        order = self.trigger_instance_creation(metadata={"env": "prod"})
        self.assertEqual(order.state, OrderStates.EXECUTING, order.error_message)
        self.assertEqual(order.resource.scope.metadata, {"env": "prod"})

    def test_invalid_metadata_erreds_the_order(self):
        order = self.trigger_instance_creation(metadata={"env": 1})
        self.assertEqual(order.state, OrderStates.ERRED)

    def test_request_payload_is_validated(self):
        order = self.trigger_instance_creation(system_volume_size=100)
        self.assertEqual(order.state, OrderStates.ERRED)

    def test_instance_state_is_synchronized_when_it_is_done(self):
        order = self.trigger_instance_creation()
        instance = order.resource.scope

        instance.begin_creating()
        instance.save()

        instance.set_ok()
        instance.save()

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.DONE)

        order.resource.refresh_from_db()
        self.assertEqual(order.resource.state, ResourceStates.OK)

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.DONE)

    def test_instance_state_is_synchronized_when_it_is_erred(self):
        order = self.trigger_instance_creation()
        instance = order.resource.scope

        instance.begin_creating()
        instance.save()

        instance.set_erred()
        instance.save()

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.ERRED)

        order.resource.refresh_from_db()
        self.assertEqual(order.resource.state, ResourceStates.ERRED)

    def test_instance_state_is_synchronized_when_it_is_switched_from_scheduled_to_erred(
        self,
    ):
        order = self.trigger_instance_creation()
        instance = order.resource.scope

        self.assertEqual(instance.state, CoreStates.CREATION_SCHEDULED)

        instance.set_erred()
        instance.save()

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.ERRED)

        order.resource.refresh_from_db()
        self.assertEqual(order.resource.state, ResourceStates.ERRED)

    def test_create_resource_of_volume_if_instance_created(self):
        order = self.trigger_instance_creation()
        instance = order.resource.scope
        volume = instance.volumes.first()
        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=volume).exists()
        )

    def test_create_resource_of_volume_skipped_on_unrelated_save(self):
        order = self.trigger_instance_creation()
        resource = order.resource
        resource.refresh_from_db()
        volume_resource_count = marketplace_models.Resource.objects.filter(
            offering__type=OPENSTACK_VOLUME_OFFERING
        ).count()

        resource.set_state_erred()
        with mock.patch(
            "waldur_mastermind.marketplace_openstack.handlers.utils.get_offering"
        ) as get_offering_mock:
            handlers.create_resource_of_volume_if_instance_created(
                sender=marketplace_models.Resource,
                instance=resource,
                created=False,
            )
            get_offering_mock.assert_not_called()

        self.assertEqual(
            marketplace_models.Resource.objects.filter(
                offering__type=OPENSTACK_VOLUME_OFFERING
            ).count(),
            volume_resource_count,
        )

    def test_parent_resource_is_linked(self):
        tenant_resource = marketplace_factories.ResourceFactory(
            scope=self.fixture.tenant
        )
        order = self.trigger_instance_creation()
        self.assertEqual(order.resource.parent, tenant_resource)

    def trigger_instance_creation(self, **kwargs):
        subnet_url = openstack_factories.SubNetFactory.get_url(self.fixture.subnet)
        attributes = {
            "flavor": openstack_factories.FlavorFactory.get_url(self.fixture.flavor),
            "image": openstack_factories.ImageFactory.get_url(self.fixture.image),
            "name": "virtual-machine",
            "system_volume_size": self.fixture.image.min_disk,
            "ports": [{"subnet": subnet_url}],
            "ssh_public_key": structure_factories.SshPublicKeyFactory.get_url(
                structure_factories.SshPublicKeyFactory(user=self.fixture.manager)
            ),
        }
        attributes.update(kwargs)

        offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING, scope=self.tenant
        )
        marketplace_factories.OfferingFactory(
            type=OPENSTACK_VOLUME_OFFERING, scope=self.tenant
        )
        order = marketplace_factories.OrderFactory(
            offering=offering,
            attributes=attributes,
            project=self.fixture.project,
            state=OrderStates.EXECUTING,
        )

        marketplace_utils.process_order(order, self.fixture.owner)

        order.refresh_from_db()
        return order


class InstancePreFlightCheckTest(test.APITestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)

    def _build_order(self, pre_flight_check_enabled=True):
        subnet_url = openstack_factories.SubNetFactory.get_url(self.fixture.subnet)
        attributes = {
            "flavor": openstack_factories.FlavorFactory.get_url(self.fixture.flavor),
            "image": openstack_factories.ImageFactory.get_url(self.fixture.image),
            "name": "virtual-machine",
            "system_volume_size": self.fixture.image.min_disk,
            "ports": [{"subnet": subnet_url}],
            "ssh_public_key": structure_factories.SshPublicKeyFactory.get_url(
                structure_factories.SshPublicKeyFactory(user=self.fixture.manager)
            ),
        }
        plugin_options = (
            {"pre_flight_check_enabled": True} if pre_flight_check_enabled else {}
        )
        offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING,
            scope=self.tenant,
            plugin_options=plugin_options,
        )
        marketplace_factories.OfferingFactory(
            type=OPENSTACK_VOLUME_OFFERING, scope=self.tenant
        )
        order = marketplace_factories.OrderFactory(
            offering=offering,
            attributes=attributes,
            project=self.fixture.project,
            state=OrderStates.EXECUTING,
        )
        url = marketplace_factories.OrderFactory.get_url(order)
        request = Request(test.APIRequestFactory().post(url))
        request.user = self.fixture.owner
        return order, request

    @mock.patch("waldur_mastermind.marketplace_openstack.processors.OpenStackBackend")
    def test_order_is_rejected_when_no_candidates(self, mock_backend):
        mock_backend.return_value.get_allocation_candidates.return_value = {
            "allocation_requests": [],
            "provider_summaries": {},
        }
        order, request = self._build_order()

        with self.assertRaises(rf_serializers.ValidationError):
            validate_order(order, request)

        mock_backend.return_value.get_allocation_candidates.assert_called_once_with(
            resources={"VCPU": 2, "MEMORY_MB": 2048}
        )

    @mock.patch("waldur_mastermind.marketplace_openstack.processors.OpenStackBackend")
    def test_order_proceeds_when_candidates_exist(self, mock_backend):
        mock_backend.return_value.get_allocation_candidates.return_value = {
            "allocation_requests": [{"allocations": {}}],
            "provider_summaries": {},
        }
        order, request = self._build_order()

        validate_order(order, request)

        mock_backend.return_value.get_allocation_candidates.assert_called_once()

    @mock.patch("waldur_mastermind.marketplace_openstack.processors.OpenStackBackend")
    def test_order_proceeds_when_placement_unavailable(self, mock_backend):
        mock_backend.return_value.get_allocation_candidates.side_effect = (
            OpenStackBackendError("Placement endpoint not found")
        )
        order, request = self._build_order()

        with self.assertLogs(
            "waldur_mastermind.marketplace_openstack.processors", level="WARNING"
        ) as logs:
            validate_order(order, request)

        self.assertTrue(
            any("Placement is unavailable" in message for message in logs.output)
        )

    @mock.patch("waldur_mastermind.marketplace_openstack.processors.OpenStackBackend")
    def test_check_is_skipped_when_option_disabled(self, mock_backend):
        order, request = self._build_order(pre_flight_check_enabled=False)

        validate_order(order, request)

        mock_backend.return_value.get_allocation_candidates.assert_not_called()


class InstanceDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.instance = self.fixture.instance
        self.offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING
        )
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.instance, offering=self.offering
        )
        self.order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            state=OrderStates.EXECUTING,
            resource=self.resource,
            type=OrderTypes.TERMINATE,
        )

    def test_order_is_valid(self):
        self.resource.scope = None
        self.resource.save()
        url = marketplace_factories.OrderFactory.get_url(self.order, "terminate")
        request = test.APIRequestFactory().post(url)
        request.user = self.fixture.user
        validate_order(self.order, request)

    def test_deletion_is_scheduled(self):
        self.trigger_deletion()
        self.assertEqual(self.order.state, OrderStates.EXECUTING)
        self.assertEqual(self.resource.state, ResourceStates.TERMINATING)
        self.assertEqual(
            self.instance.state,
            CoreStates.DELETION_SCHEDULED,
        )

    @mock.patch("waldur_openstack.executors.InstanceDeleteExecutor.execute")
    def test_cancel_of_volume_deleting(self, execute):
        self.order.attributes = {"delete_volumes": False}
        self.order.save()
        self.trigger_deletion()
        self.assertFalse(execute.call_args[1]["delete_volumes"])

    @mock.patch("waldur_openstack.executors.InstanceDeleteExecutor.execute")
    def test_cancel_of_floating_ips_deleting(self, execute):
        self.order.attributes = {"release_floating_ips": False}
        self.order.save()
        self.trigger_deletion()
        self.assertFalse(execute.call_args[1]["release_floating_ips"])

    def test_deletion_is_completed(self):
        self.trigger_deletion()
        self.instance.delete()

        self.order.refresh_from_db()
        self.resource.refresh_from_db()

        self.assertEqual(self.order.state, OrderStates.DONE)
        self.assertEqual(self.resource.state, ResourceStates.TERMINATED)
        self.assertRaises(ObjectDoesNotExist, self.instance.refresh_from_db)

    @mock.patch("waldur_openstack.executors.InstanceDeleteExecutor.execute")
    def test_deletion_is_scheduled_when_instance_has_backups(self, execute):
        openstack_factories.BackupFactory(instance=self.instance)
        self.trigger_deletion()
        execute.assert_called_once()

    @mock.patch("waldur_openstack.executors.InstanceDeleteExecutor.execute")
    def test_deletion_is_scheduled_when_instance_has_snapshots(self, execute):
        openstack_factories.SnapshotFactory(
            tenant=self.instance.tenant,
            project=self.instance.project,
            source_volume=self.instance.volumes.first(),
        )
        self.trigger_deletion()
        execute.assert_called_once()

    @mock.patch("waldur_openstack.executors.InstanceDeleteExecutor.execute")
    def test_deletion_is_scheduled_for_active_instance(self, execute):
        self.instance.state = CoreStates.OK
        self.instance.runtime_state = openstack_models.Instance.RuntimeStates.ACTIVE
        self.instance.save()
        self.trigger_deletion()
        execute.assert_called_once()

    def test_terminate_api_accepts_instance_with_backups(self):
        self.resource.state = ResourceStates.OK
        self.resource.save()
        self.order.state = OrderStates.DONE
        self.order.save()
        openstack_factories.BackupFactory(instance=self.instance)
        url = marketplace_factories.ResourceFactory.get_url(self.resource, "terminate")
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(url, {"attributes": {}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_terminate_api_accepts_instance_with_snapshots(self):
        self.resource.state = ResourceStates.OK
        self.resource.save()
        self.order.state = OrderStates.DONE
        self.order.save()
        openstack_factories.SnapshotFactory(
            tenant=self.instance.tenant,
            project=self.instance.project,
            source_volume=self.instance.volumes.first(),
        )
        url = marketplace_factories.ResourceFactory.get_url(self.resource, "terminate")
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(url, {"attributes": {}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_terminate_api_accepts_active_instance(self):
        self.resource.state = ResourceStates.OK
        self.resource.save()
        self.instance.state = CoreStates.OK
        self.instance.runtime_state = openstack_models.Instance.RuntimeStates.ACTIVE
        self.instance.save()
        self.order.state = OrderStates.DONE
        self.order.save()

        url = marketplace_factories.ResourceFactory.get_url(self.resource, "terminate")
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            url,
            {
                "attributes": {
                    "delete_volumes": True,
                    "release_floating_ips": True,
                }
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_termination_should_not_be_triggered_if_termination_is_already_in_progress(
        self,
    ):
        self.resource.state = ResourceStates.OK
        self.resource.save()
        self.order.state = OrderStates.DONE
        self.order.save()
        url = marketplace_factories.ResourceFactory.get_url(self.resource, "terminate")
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            url,
            {
                "attributes": {
                    "delete_volumes": True,
                    "release_floating_ips": True,
                }
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.client.post(
            url,
            {
                "attributes": {
                    "delete_volumes": True,
                    "release_floating_ips": True,
                }
            },
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn(b"OK", response.rendered_content)
        self.assertIn(b"Erred", response.rendered_content)
        self.assertIn(b"pending consumer approval", response.rendered_content)

    def trigger_deletion(self):
        InstanceDeleteProcessor(self.order).process_order(self.fixture.staff)

        self.order.refresh_from_db()
        self.resource.refresh_from_db()
        self.instance.refresh_from_db()

    def test_validate_order_allows_active_instance_with_snapshots(self):
        self.instance.state = CoreStates.OK
        self.instance.runtime_state = openstack_models.Instance.RuntimeStates.ACTIVE
        self.instance.save()
        openstack_factories.SnapshotFactory(
            tenant=self.instance.tenant,
            project=self.instance.project,
            source_volume=self.instance.volumes.first(),
        )
        url = marketplace_factories.OrderFactory.get_url(self.order, "terminate")
        request = test.APIRequestFactory().post(url)
        request.user = self.fixture.user
        validate_order(self.order, request)


class VolumeCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.service_settings = self.fixture.tenant.service_settings

    def test_volume_is_created_when_order_is_processed(self):
        order = self.trigger_volume_creation()
        self.assertEqual(order.state, OrderStates.EXECUTING)
        self.assertTrue(openstack_models.Volume.objects.filter(name="Volume").exists())

    def test_availability_zone_is_passed_to_plugin(self):
        availability_zone = openstack_factories.VolumeAvailabilityZoneFactory(
            tenant=self.fixture.tenant
        )
        az_url = openstack_factories.VolumeAvailabilityZoneFactory.get_url(
            availability_zone
        )
        order = self.trigger_volume_creation(availability_zone=az_url)
        self.assertEqual(order.resource.scope.availability_zone, availability_zone)

    def test_request_payload_is_validated(self):
        order = self.trigger_volume_creation(size=100)
        self.assertEqual(order.state, OrderStates.ERRED)

    def test_volume_state_is_synchronized(self):
        order = self.trigger_volume_creation()
        instance = order.resource.scope

        instance.begin_creating()
        instance.save()

        instance.set_ok()
        instance.save()

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.DONE)

    def trigger_volume_creation(self, **kwargs):
        attributes = {
            "image": openstack_factories.ImageFactory.get_url(self.fixture.image),
            "name": "Volume",
            "size": 10 * 1024,
        }
        attributes.update(kwargs)

        offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_VOLUME_OFFERING, scope=self.fixture.tenant
        )

        order = marketplace_factories.OrderFactory(
            offering=offering,
            attributes=attributes,
            project=self.fixture.project,
            state=OrderStates.EXECUTING,
        )
        marketplace_utils.process_order(order, self.fixture.staff)

        order.refresh_from_db()
        return order


class VolumeDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()

        self.volume = self.fixture.volume
        self.volume.runtime_state = "available"
        self.volume.save()

        self.offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_VOLUME_OFFERING
        )
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.volume, offering=self.offering
        )
        self.order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            state=OrderStates.EXECUTING,
            resource=self.resource,
            type=OrderTypes.TERMINATE,
        )

    def test_deletion_is_scheduled(self):
        self.trigger_deletion()
        self.assertEqual(self.order.state, OrderStates.EXECUTING)
        self.assertEqual(self.resource.state, ResourceStates.TERMINATING)
        self.assertEqual(self.volume.state, CoreStates.DELETION_SCHEDULED)

    def test_deletion_is_completed(self):
        self.trigger_deletion()
        self.volume.delete()

        self.order.refresh_from_db()
        self.resource.refresh_from_db()

        self.assertEqual(self.order.state, OrderStates.DONE)
        self.assertEqual(self.resource.state, ResourceStates.TERMINATED)
        self.assertRaises(ObjectDoesNotExist, self.volume.refresh_from_db)

    def trigger_deletion(self):
        marketplace_utils.process_order(self.order, self.fixture.staff)

        self.order.refresh_from_db()
        self.resource.refresh_from_db()
        self.volume.refresh_from_db()


class TenantUpdateLimitTestBase(test.APITestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING
        )
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            plan=self.plan,
            state=ResourceStates.OK,
        )
        tenant = self.fixture.tenant
        self.mock_get_backend = mock.MagicMock()
        tenant.get_backend = self.mock_get_backend
        self.resource.scope = tenant
        self.resource.save()
        self.quotas = {
            "network_count": 100,
            "cores": 4,
            "ram": 1024,
            "storage": 1024,
            "snapshots": 50,
            "instances": 30,
            "floating_ip_count": 50,
            "subnet_count": 100,
            "volumes": 50,
            "security_group_rule_count": 100,
            "security_group_count": 100,
        }


class TenantUpdateLimitTest(TenantUpdateLimitTestBase):
    def setUp(self):
        super().setUp()
        self.order = marketplace_factories.OrderFactory(
            type=OrderTypes.UPDATE,
            resource=self.resource,
            plan=self.resource.plan,
            offering=self.offering,
            limits=self.quotas,
            attributes={"old_limits": self.resource.limits},
            state=OrderStates.EXECUTING,
        )
        self.offering.plugin_options["storage_mode"] = STORAGE_MODE_DYNAMIC
        self.offering.save()

    def test_resource_limits_have_been_updated_if_backend_does_not_raise_exception(
        self,
    ):
        self.resource.set_state_updating()
        self.resource.save()
        marketplace_utils.process_order(self.order, self.fixture.staff)
        self.order.refresh_from_db()
        self.assertEqual(
            self.order.state,
            OrderStates.DONE,
            self.order.error_message,
        )
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits, self.quotas)

    def test_resource_limits_have_been_not_updated_if_backend_raises_exception(self):
        self.offering.plugin_options["storage_mode"] = STORAGE_MODE_FIXED
        self.offering.save()

        self.mock_get_backend().push_tenant_quotas = mock.Mock(
            side_effect=Exception("foo")
        )
        marketplace_utils.process_order(self.order, self.fixture.staff)
        self.resource.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.ERRED)
        self.assertEqual(self.order.error_message, "foo")

    def test_volume_type_quotas_are_propagated(self):
        del self.quotas["storage"]
        self.quotas["gigabytes_lvmdriver-1"] = 10
        self.quotas["gigabytes_backup"] = 30
        marketplace_utils.process_order(self.order, self.fixture.staff)
        _, quotas = self.mock_get_backend().push_tenant_quotas.call_args[0]
        self.assertTrue("gigabytes_lvmdriver-1" in quotas)
        self.assertEqual(quotas["storage"], 40 * 1024)


class TenantUpdateLimitValidationTest(TenantUpdateLimitTestBase):
    def setUp(self):
        super().setUp()
        create_offering_components(self.offering)
        self.offering.components.filter(type="cores").update(
            max_value=20,
            min_value=2,
        )

    def update_limits(self, user, resource, limits=None):
        defaults = {"cores": 10, "ram": 10240, "storage": 102400}
        defaults.update(limits or {})
        self.client.force_authenticate(user)
        url = marketplace_factories.ResourceFactory.get_url(resource, "update_limits")
        payload = {"limits": defaults}
        return self.client.post(url, payload)

    def test_validation_if_requested_available_limits(self):
        response = self.update_limits(self.fixture.staff, self.resource)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_validation_if_requested_unavailable_limits(self):
        response = self.update_limits(self.fixture.staff, self.resource, {"foo": 1})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validation_if_value_limit_in_confines(self):
        response = self.update_limits(self.fixture.staff, self.resource)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_validation_if_value_limit_more_max(self):
        response = self.update_limits(self.fixture.staff, self.resource, {"cores": 30})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validation_if_value_limit_less_min(self):
        response = self.update_limits(self.fixture.staff, self.resource, {"cores": 1})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
