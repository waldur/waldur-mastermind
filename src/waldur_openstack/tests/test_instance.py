import uuid
from unittest import mock

import factory
from celery import Signature
from cinderclient import exceptions as cinder_exceptions
from ddt import data, ddt
from django.test import override_settings
from novaclient import exceptions as nova_exceptions
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.core.utils import serialize_instance
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.common import utils as common_utils
from waldur_mastermind.marketplace_openstack.utils import (
    delete_instance,
)
from waldur_openstack import executors, models, views
from waldur_openstack.exceptions import OpenStackBackendError
from waldur_openstack.models import Port
from waldur_openstack.tasks import LimitedPerTypeThrottleMixin
from waldur_openstack.tests import factories, fixtures, helpers
from waldur_openstack.tests.helpers import (
    override_openstack_settings,
)
from waldur_openstack.utils import volume_type_name_to_quota_name


class InstanceFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(user=self.fixture.owner)
        self.url = factories.InstanceFactory.get_list_url()

    def test_filter_instance_by_valid_volume_uuid(self):
        self.fixture.instance
        response = self.client.get(
            self.url, {"attach_volume_uuid": self.fixture.volume.uuid.hex}
        )
        self.assertEqual(len(response.data), 1)

    def test_filter_instance_by_invalid_volume_uuid(self):
        self.fixture.instance
        response = self.client.get(self.url, {"attach_volume_uuid": "invalid"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filter_instance_by_availability_zone(self):
        vm_az = self.fixture.instance_availability_zone
        vm = self.fixture.instance
        vm.availability_zone = vm_az
        vm.save()

        volume_az = self.fixture.volume_availability_zone
        volume = self.fixture.volume
        volume.availability_zone = volume_az
        volume.save()

        shared_settings = self.fixture.tenant.service_settings
        shared_settings.options = {
            "valid_availability_zones": {vm_az.name: volume_az.name}
        }
        shared_settings.save()

        response = self.client.get(self.url, {"attach_volume_uuid": volume.uuid.hex})
        self.assertEqual(len(response.data), 1)


@ddt
class InstanceCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.openstack_settings = self.tenant.service_settings
        self.openstack_settings.options = {"external_network_id": uuid.uuid4().hex}
        self.openstack_settings.save()
        self.project = self.fixture.project
        self.customer = self.fixture.customer
        self.image = self.fixture.image
        self.flavor = self.fixture.flavor
        self.subnet = self.fixture.subnet
        self.volume_type = self.fixture.volume_type

    def create_instance(self, post_data=None):
        user = self.fixture.owner
        view = views.MarketplaceInstanceViewSet.as_view({"post": "create"})
        response = common_utils.create_request(view, user, post_data)
        return response

    def get_valid_data(self, **extra):
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        default = {
            "service_settings": factories.SettingsFactory.get_url(
                self.openstack_settings
            ),
            "tenant": factories.TenantFactory.get_url(self.tenant),
            "project": structure_factories.ProjectFactory.get_url(self.project),
            "flavor": factories.FlavorFactory.get_url(self.flavor),
            "image": factories.ImageFactory.get_url(self.image),
            "name": "valid-name",
            "system_volume_size": self.image.min_disk,
            "ports": [{"subnet": subnet_url}],
        }
        default.update(extra)
        return default

    def test_instance_cannot_be_created_if_volume_exceeds_volume_type_quota(self):
        quota_name = volume_type_name_to_quota_name(self.volume_type.name)
        self.tenant.set_quota_limit(quota_name, 100)
        self.tenant.set_quota_usage(quota_name, 100)
        response = self.create_instance(
            self.get_valid_data(
                system_volume_type=factories.VolumeTypeFactory.get_url(
                    self.volume_type
                ),
                data_volume_type=factories.VolumeTypeFactory.get_url(self.volume_type),
            )
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_quotas_update(self):
        response = self.create_instance(self.get_valid_data())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        instance = models.Instance.objects.get(uuid=response.data["uuid"])
        Quotas = self.tenant.Quotas
        self.assertEqual(self.tenant.get_quota_usage(Quotas.ram), instance.ram)
        self.assertEqual(self.tenant.get_quota_usage(Quotas.storage), instance.disk)
        self.assertEqual(self.tenant.get_quota_usage(Quotas.vcpu), instance.cores)
        self.assertEqual(self.tenant.get_quota_usage(Quotas.instances), 1)

    def test_config_drive_is_persisted_on_create(self):
        response = self.create_instance(self.get_valid_data(config_drive=True))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        instance = models.Instance.objects.get(uuid=response.data["uuid"])
        self.assertIs(instance.config_drive, True)
        self.assertIs(response.data["config_drive"], True)

    def test_config_drive_defaults_to_null(self):
        response = self.create_instance(self.get_valid_data())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        instance = models.Instance.objects.get(uuid=response.data["uuid"])
        self.assertIsNone(instance.config_drive)
        self.assertIsNone(response.data["config_drive"])

    def test_project_quotas_updated_when_instance_is_created(self):
        response = self.create_instance(self.get_valid_data())
        instance = models.Instance.objects.get(uuid=response.data["uuid"])

        self.assertEqual(self.project.get_quota_usage("os_cpu_count"), instance.cores)
        self.assertEqual(self.project.get_quota_usage("os_ram_size"), instance.ram)
        self.assertEqual(self.project.get_quota_usage("os_storage_size"), instance.disk)

    def test_customer_quotas_updated_when_instance_is_created(self):
        response = self.create_instance(self.get_valid_data())
        instance = models.Instance.objects.get(uuid=response.data["uuid"])

        self.assertEqual(self.customer.get_quota_usage("os_cpu_count"), instance.cores)
        self.assertEqual(self.customer.get_quota_usage("os_ram_size"), instance.ram)
        self.assertEqual(
            self.customer.get_quota_usage("os_storage_size"), instance.disk
        )

    def test_project_quotas_updated_when_instance_is_deleted(self):
        response = self.create_instance(self.get_valid_data())
        instance = models.Instance.objects.get(uuid=response.data["uuid"])
        instance.delete()

        self.assertEqual(self.project.get_quota_usage("os_cpu_count"), 0)
        self.assertEqual(self.project.get_quota_usage("os_ram_size"), 0)
        self.assertEqual(self.project.get_quota_usage("os_storage_size"), 0)

    def test_customer_quotas_updated_when_instance_is_deleted(self):
        response = self.create_instance(self.get_valid_data())
        instance = models.Instance.objects.get(uuid=response.data["uuid"])
        instance.delete()

        self.assertEqual(self.customer.get_quota_usage("os_cpu_count"), 0)
        self.assertEqual(self.customer.get_quota_usage("os_ram_size"), 0)
        self.assertEqual(self.customer.get_quota_usage("os_storage_size"), 0)

    @data("instances")
    def test_quota_validation(self, quota_name):
        self.tenant.set_quota_limit(quota_name, 0)
        response = self.create_instance(self.get_valid_data())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_provision_instance(self):
        response = self.create_instance(self.get_valid_data())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_rescue_tagged_image_cannot_be_used_as_boot_image(self):
        # Either hw_rescue property is enough to mark an image as a rescue
        # image (matches Image.is_rescue_image). Such images are typically
        # ISO-only and won't boot a usable system disk.
        for field in ("hw_rescue_device", "hw_rescue_bus"):
            with self.subTest(field=field):
                rescue_image = factories.ImageFactory(
                    settings=self.openstack_settings,
                    **{field: "cdrom"},
                )
                rescue_image.tenants.add(self.tenant)
                response = self.create_instance(
                    self.get_valid_data(
                        image=factories.ImageFactory.get_url(rescue_image),
                    )
                )
                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST, response.data
                )
                self.assertIn("image", response.data)

    def test_user_can_define_fixed_ips(self):
        post_data = self.get_valid_data()
        fixed_ips = [{"ip_address": "192.168.0.1", "subnet_id": self.subnet.backend_id}]
        post_data["ports"][0]["fixed_ips"] = fixed_ips
        response = self.create_instance(post_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        instance = models.Instance.objects.get(uuid=response.data["uuid"])
        self.assertEqual(instance.ports.first().fixed_ips, fixed_ips)

    def _create_unattached_port(self, status_value="DOWN"):
        return factories.PortFactory(
            tenant=self.tenant,
            network=self.subnet.network,
            subnet=self.subnet,
            service_settings=self.openstack_settings,
            project=self.project,
            status=status_value,
        )

    def test_can_create_instance_with_existing_port_in_down_state(self):
        existing_port = self._create_unattached_port(status_value="DOWN")
        post_data = self.get_valid_data()
        post_data["ports"][0]["port"] = factories.PortFactory.get_url(existing_port)
        response = self.create_instance(post_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        instance = models.Instance.objects.get(uuid=response.data["uuid"])
        self.assertEqual(instance.ports.first(), existing_port)

    def test_can_create_instance_with_existing_port_without_status(self):
        existing_port = self._create_unattached_port(status_value=None)
        post_data = self.get_valid_data()
        post_data["ports"][0]["port"] = factories.PortFactory.get_url(existing_port)
        response = self.create_instance(post_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        instance = models.Instance.objects.get(uuid=response.data["uuid"])
        self.assertEqual(instance.ports.first(), existing_port)

    def test_cannot_create_instance_with_port_already_attached_to_another_instance(
        self,
    ):
        other_instance = factories.InstanceFactory(
            service_settings=self.openstack_settings,
            project=self.project,
            tenant=self.tenant,
        )
        existing_port = factories.PortFactory(
            instance=other_instance,
            tenant=self.tenant,
            network=self.subnet.network,
            subnet=self.subnet,
            service_settings=self.openstack_settings,
            project=self.project,
            status="ACTIVE",
        )
        post_data = self.get_valid_data()
        post_data["ports"][0]["port"] = factories.PortFactory.get_url(existing_port)
        response = self.create_instance(post_data)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertIn("ports", response.data)

    def test_user_can_define_instance_subnets(self):
        subnet = self.fixture.subnet
        data = self.get_valid_data(
            ports=[{"subnet": factories.SubNetFactory.get_url(subnet)}]
        )

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        instance = models.Instance.objects.get(uuid=response.data["uuid"])
        self.assertTrue(Port.objects.filter(subnet=subnet, instance=instance).exists())

    def test_port_security_enabled_is_persisted_during_instance_creation(self):
        subnet = self.fixture.subnet
        data = self.get_valid_data(
            ports=[
                {
                    "subnet": factories.SubNetFactory.get_url(subnet),
                    "port_security_enabled": False,
                }
            ]
        )

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        instance = models.Instance.objects.get(uuid=response.data["uuid"])
        port = instance.ports.first()
        self.assertIsNotNone(port)
        self.assertFalse(port.port_security_enabled)

    def test_port_security_enabled_defaults_to_true_during_instance_creation(self):
        subnet = self.fixture.subnet
        data = self.get_valid_data(
            ports=[{"subnet": factories.SubNetFactory.get_url(subnet)}]
        )

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        instance = models.Instance.objects.get(uuid=response.data["uuid"])
        port = instance.ports.first()
        self.assertIsNotNone(port)
        self.assertTrue(port.port_security_enabled)

    def test_security_groups_rejected_when_port_security_disabled(self):
        subnet = self.fixture.subnet
        security_group = factories.SecurityGroupFactory(
            tenant=self.tenant,
            service_settings=self.openstack_settings,
            project=self.project,
        )
        data = self.get_valid_data(
            ports=[
                {
                    "subnet": factories.SubNetFactory.get_url(subnet),
                    "port_security_enabled": False,
                }
            ],
            security_groups=[
                {"url": factories.SecurityGroupFactory.get_url(security_group)}
            ],
        )

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_assign_subnet_from_other_settings_to_instance(self):
        data = self.get_valid_data(
            ports=[{"subnet": factories.SubNetFactory.get_url()}]
        )
        response = self.create_instance(data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_define_instance_floating_ips(self):
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        floating_ip = self.fixture.floating_ip
        floating_ip.state = CoreStates.OK
        floating_ip.save()
        data = self.get_valid_data(
            floating_ips=[
                {
                    "subnet": subnet_url,
                    "url": factories.FloatingIPFactory.get_url(floating_ip),
                }
            ],
        )

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        instance = models.Instance.objects.get(uuid=response.data["uuid"])
        self.assertIn(floating_ip, instance.floating_ips.all())

    def test_service_settings_should_have_external_network_id(self):
        self.openstack_settings.options = {"external_network_id": "invalid"}
        self.openstack_settings.save()

        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        data = self.get_valid_data(floating_ips=[{"subnet": subnet_url}])

        response = self.create_instance(data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_assign_floating_ip_from_other_settings_to_instance(self):
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        floating_ip = factories.FloatingIPFactory()
        data = self.get_valid_data(
            floating_ips=[
                {
                    "subnet": subnet_url,
                    "url": factories.FloatingIPFactory.get_url(floating_ip),
                }
            ],
        )

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_assign_floating_ip_to_disconnected_subnet(self):
        disconnected_subnet = factories.SubNetFactory(tenant=self.fixture.tenant)
        disconnected_subnet_url = factories.SubNetFactory.get_url(disconnected_subnet)
        floating_ip = self.fixture.floating_ip
        data = self.get_valid_data(
            floating_ips=[
                {
                    "subnet": disconnected_subnet_url,
                    "url": factories.FloatingIPFactory.get_url(floating_ip),
                }
            ],
        )

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_use_floating_ip_assigned_to_other_instance(self):
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        port = factories.PortFactory(subnet=self.subnet)
        floating_ip = factories.FloatingIPFactory(
            tenant=self.tenant,
            runtime_state="ACTIVE",
            port=port,
        )
        data = self.get_valid_data(
            floating_ips=[
                {
                    "subnet": subnet_url,
                    "url": factories.FloatingIPFactory.get_url(floating_ip),
                }
            ],
        )

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("floating_ips", response.data)

    def test_user_can_assign_floating_ip_by_address(self):
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        floating_ip = factories.FloatingIPFactory(
            tenant=self.tenant, state=CoreStates.OK, runtime_state="DOWN"
        )
        data = self.get_valid_data(
            floating_ips=[
                {"subnet": subnet_url, "ip_address": floating_ip.address},
            ],
        )

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        instance = models.Instance.objects.get(uuid=response.data["uuid"])
        self.assertIn(floating_ip, instance.floating_ips.all())

    def test_user_cannot_assign_floating_ip_by_invalid_address(self):
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        data = self.get_valid_data(
            floating_ips=[
                {"subnet": subnet_url, "ip_address": "not a valid ip"},
            ],
        )
        response = self.create_instance(data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ip_address", response.data["floating_ips"][0])

    def test_user_cannot_assign_floating_ip_by_address_and_url(self):
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        floating_ip = factories.FloatingIPFactory(tenant=self.tenant)
        data = self.get_valid_data(
            floating_ips=[
                {
                    "subnet": subnet_url,
                    "url": factories.FloatingIPFactory.get_url(floating_ip),
                    "ip_address": floating_ip.address,
                }
            ],
        )

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("non_field_errors", response.data["floating_ips"][0])
        self.assertIn(
            "Please specify floating IP URL or IP address, not both",
            str(response.data),
        )

    def test_user_cannot_use_floating_ip_in_use_by_address(self):
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        port = factories.PortFactory(subnet=self.subnet)
        floating_ip = factories.FloatingIPFactory(
            tenant=self.tenant,
            runtime_state="ACTIVE",
            port=port,
        )
        data = self.get_valid_data(
            floating_ips=[
                {"subnet": subnet_url, "ip_address": floating_ip.address},
            ],
        )

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("floating_ips", response.data)

    def test_user_can_assign_active_floating_ip(self):
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        floating_ip = factories.FloatingIPFactory(
            tenant=self.tenant, runtime_state="ACTIVE", state=CoreStates.OK
        )
        data = self.get_valid_data(
            floating_ips=[
                {
                    "subnet": subnet_url,
                    "url": factories.FloatingIPFactory.get_url(floating_ip),
                }
            ],
        )

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_user_can_allocate_floating_ip(self):
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        self.fixture.floating_ip.runtime_state = "ACTIVE"
        self.fixture.floating_ip.save()
        data = self.get_valid_data(
            floating_ips=[{"subnet": subnet_url}],
        )

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        instance = models.Instance.objects.get(uuid=response.data["uuid"])
        self.assertEqual(instance.floating_ips.count(), 1)

    def test_user_cannot_allocate_floating_ip_if_quota_limit_is_reached(self):
        self.tenant.set_quota_limit(self.tenant.Quotas.floating_ip_count, 0)
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        data = self.get_valid_data(
            floating_ips=[{"subnet": subnet_url}],
        )

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_can_not_create_instance_without_ports(self):
        data = self.get_valid_data()
        del data["ports"]

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_can_not_create_instance_with_empty_ports_list(self):
        data = self.get_valid_data()
        data["ports"] = []

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_can_create_instance_with_multiple_ports(self):
        data = self.get_valid_data()
        second_subnet = factories.SubNetFactory(
            network=self.fixture.network,
            tenant=self.fixture.tenant,
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            state=CoreStates.OK,
            backend_id=factory.Sequence(lambda n: "subnet_%s" % n),
        )
        data["ports"].append({"subnet": factories.SubNetFactory.get_url(second_subnet)})

        response = self.create_instance(data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        instance = models.Instance.objects.get(uuid=response.data["uuid"])
        self.assertEqual(instance.ports.count(), 2)
        self.assertTrue(
            instance.ports.filter(subnet=self.fixture.subnet, backend_id=None).exists(),
        )
        self.assertTrue(
            instance.ports.filter(subnet=second_subnet, backend_id=None).exists(),
        )

    def test_show_volume_type_in_instance_serializer(self):
        instance = factories.InstanceFactory()
        volume_type = factories.VolumeTypeFactory(
            settings=instance.tenant.service_settings
        )
        volume_type.tenants.add(self.tenant)
        factories.VolumeFactory(
            service_settings=instance.service_settings,
            project=instance.project,
            instance=instance,
            type=volume_type,
            name="test-volume",
        )
        url = factories.InstanceFactory.get_url(instance)
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        serialized_volume = [
            volume
            for volume in response.data["volumes"]
            if volume["name"] == "test-volume"
        ][0]
        self.assertEqual(serialized_volume["type_name"], volume_type.name)

    def test_user_can_define_instance_availability_zone(self):
        zone = self.fixture.instance_availability_zone
        data = self.get_valid_data(
            availability_zone=factories.InstanceAvailabilityZoneFactory.get_url(zone)
        )

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        instance = models.Instance.objects.get(uuid=response.data["uuid"])
        self.assertEqual(instance.availability_zone, zone)

    def test_availability_zone_should_be_available(self):
        zone = self.fixture.instance_availability_zone
        zone.available = False
        zone.save()
        data = self.get_valid_data(
            availability_zone=factories.InstanceAvailabilityZoneFactory.get_url(zone)
        )

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_availability_zone_should_be_related_to_the_same_service_settings(self):
        zone = factories.InstanceAvailabilityZoneFactory()
        data = self.get_valid_data(
            availability_zone=factories.InstanceAvailabilityZoneFactory.get_url(zone)
        )

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_volume_AZ_should_be_matched_with_instance_AZ(self):
        # Arrange
        vm_az = self.fixture.instance_availability_zone
        volume_az = self.fixture.volume_availability_zone

        shared_ss = self.fixture.tenant.service_settings
        shared_ss.options = {"valid_availability_zones": {vm_az.name: volume_az.name}}
        shared_ss.save()

        vm_az_url = factories.InstanceAvailabilityZoneFactory.get_url(vm_az)
        data = self.get_valid_data(availability_zone=vm_az_url)

        # Act
        response = self.create_instance(data)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        instance = models.Instance.objects.get(uuid=response.data["uuid"])

        self.assertEqual(instance.availability_zone, vm_az)
        self.assertEqual(instance.volumes.first().availability_zone, volume_az)
        self.assertEqual(instance.volumes.last().availability_zone, volume_az)

    @override_openstack_settings(REQUIRE_AVAILABILITY_ZONE=True)
    def test_when_availability_zone_is_mandatory_and_exists_validation_fails(self):
        self.fixture.instance_availability_zone
        data = self.get_valid_data()

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_openstack_settings(REQUIRE_AVAILABILITY_ZONE=True)
    def test_when_availability_zone_is_mandatory_and_does_not_exist_validation_succeeds(
        self,
    ):
        data = self.get_valid_data()

        response = self.create_instance(data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("kt-experimental-ubuntu-18.04", "vm_name")
    def test_not_create_instance_with_invalid_name(self, name):
        data = self.get_valid_data()
        data["name"] = name
        response = self.create_instance(data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("test", "vm-name", "vm", "VM")
    def test_create_instance_with_valid_name(self, name):
        data = self.get_valid_data()
        data["name"] = name
        response = self.create_instance(data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


@ddt
class InstanceUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.instance = self.fixture.instance
        self.url = factories.InstanceFactory.get_url(self.instance)
        self.client.force_authenticate(user=self.fixture.owner)

    @data("kt-experimental-ubuntu-18.04", "vm_name")
    def test_update_instance_with_invalid_name(self, name):
        response = self.client.put(self.url, {"name": name})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("test", "vm-name", "vm", "VM")
    def test_update_instance_with_valid_name(self, name):
        response = self.client.put(self.url, {"name": name})
        self.assertEqual(response.status_code, status.HTTP_200_OK)


@override_settings(task_always_eager=True)
class InstanceDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.mocked_keystone = mock.patch("keystoneclient.v3.client.Client").start()()
        self.mocked_nova = mock.patch("novaclient.v2.client.Client").start()()
        self.mocked_neutron = mock.patch("neutronclient.v2_0.client.Client").start()()
        self.mocked_cinder = mock.patch("cinderclient.v3.client.Client").start()()
        self.mocked_glance = mock.patch("glanceclient.v2.client.Client").start()()
        fixtures.mock_session()
        self.instance = factories.InstanceFactory(
            state=CoreStates.OK,
            runtime_state=models.Instance.RuntimeStates.SHUTOFF,
            backend_id="VALID_ID",
        )
        self.instance.increase_backend_quotas_usage()
        self.mocked_nova.servers.get.side_effect = nova_exceptions.NotFound(code=404)
        self.tenant = self.instance.tenant

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def mock_volumes(self, delete_data_volume=True):
        self.data_volume = self.instance.volumes.get(bootable=False)
        self.data_volume.backend_id = "DATA_VOLUME_ID"
        self.data_volume.state = CoreStates.OK
        self.data_volume.save()
        self.data_volume.increase_backend_quotas_usage()

        self.system_volume = self.instance.volumes.get(bootable=True)
        self.system_volume.backend_id = "SYSTEM_VOLUME_ID"
        self.system_volume.state = CoreStates.OK
        self.system_volume.save()
        self.system_volume.increase_backend_quotas_usage()

        def get_volume(backend_id):
            if not delete_data_volume and backend_id == self.data_volume.backend_id:
                mocked_volume = mock.Mock()
                mocked_volume.status = "available"
                return mocked_volume
            raise cinder_exceptions.NotFound(code=404)

        self.mocked_cinder.volumes.get.side_effect = get_volume

    def delete_instance(self, query_params=None):
        delete_instance(self.instance, query_params, is_async=False)

    def assert_quota_usage(self, scope, name, value):
        self.assertEqual(scope.get_quota_usage(name), value)

    def test_nova_methods_are_called_if_instance_is_deleted_with_volumes(self):
        self.mock_volumes(True)
        self.delete_instance()

        self.mocked_nova.servers.delete.assert_called_once_with(
            self.instance.backend_id
        )
        self.mocked_nova.servers.get.assert_called_once_with(self.instance.backend_id)

    def test_database_models_deleted(self):
        self.mock_volumes(True)
        self.delete_instance()

        self.assertFalse(models.Instance.objects.filter(id=self.instance.id).exists())
        for volume in self.instance.volumes.all():
            self.assertFalse(models.Volume.objects.filter(id=volume.id).exists())

    def test_quotas_updated_if_instance_is_deleted_with_volumes(self):
        self.mock_volumes(True)
        self.delete_instance()

        self.instance.service_settings.refresh_from_db()

        for scope in (
            self.instance.service_settings,
            self.tenant,
        ):
            self.assert_quota_usage(scope, "instances", 0)
            self.assert_quota_usage(scope, "vcpu", 0)
            self.assert_quota_usage(scope, "ram", 0)

            self.assert_quota_usage(scope, "volumes", 0)
            self.assert_quota_usage(scope, "storage", 0)

    def test_backend_methods_are_called_if_instance_is_deleted_without_volumes(self):
        self.mock_volumes(False)
        self.delete_instance({"delete_volumes": False})

        nova = self.mocked_nova
        nova.volumes.delete_server_volume.assert_called_once_with(
            self.instance.backend_id, self.data_volume.backend_id
        )

        nova.servers.delete.assert_called_once_with(self.instance.backend_id)
        nova.servers.get.assert_called_once_with(self.instance.backend_id)

    def test_system_volume_is_deleted_but_data_volume_exists(self):
        self.mock_volumes(False)
        self.delete_instance({"delete_volumes": False})

        self.assertFalse(models.Instance.objects.filter(id=self.instance.id).exists())
        self.assertTrue(models.Volume.objects.filter(id=self.data_volume.id).exists())
        self.assertFalse(
            models.Volume.objects.filter(id=self.system_volume.id).exists()
        )

    def test_quotas_updated_if_instance_is_deleted_without_volumes(self):
        self.mock_volumes(False)
        self.delete_instance({"delete_volumes": False})

        tenant = self.instance.tenant
        tenant.refresh_from_db()

        self.assert_quota_usage(tenant, "instances", 0)
        self.assert_quota_usage(tenant, "vcpu", 0)
        self.assert_quota_usage(tenant, "ram", 0)

        self.assert_quota_usage(tenant, "volumes", 1)
        self.assert_quota_usage(tenant, "storage", self.data_volume.size)

    def test_neutron_methods_are_called_if_instance_is_deleted_with_floating_ips(self):
        self.mock_volumes(False)
        fixture = fixtures.OpenStackFixture()
        port = factories.PortFactory.create(
            instance=self.instance, subnet=fixture.subnet
        )
        floating_ip = factories.FloatingIPFactory.create(port=port, tenant=self.tenant)
        self.delete_instance(
            {
                "release_floating_ips": True,
                "delete_volumes": False,
            }
        )
        self.mocked_neutron.delete_floatingip.assert_called_once_with(
            floating_ip.backend_id
        )

    def test_neutron_methods_are_not_called_if_instance_does_not_have_any_floating_ips_yet(
        self,
    ):
        self.mock_volumes(False)
        self.delete_instance(
            {
                "release_floating_ips": True,
                "delete_volumes": False,
            }
        )
        self.assertEqual(self.mocked_neutron.delete_floatingip.call_count, 0)

    def test_neutron_methods_are_not_called_if_user_did_not_ask_for_floating_ip_removal_explicitly(
        self,
    ):
        self.mock_volumes(False)
        self.mocked_neutron.show_floatingip.return_value = {
            "floatingip": {"status": "DOWN"}
        }
        fixture = fixtures.OpenStackFixture()
        port = factories.PortFactory.create(
            instance=self.instance, subnet=fixture.subnet
        )
        factories.FloatingIPFactory.create(port=port, tenant=self.tenant)
        self.delete_instance({"release_floating_ips": False, "delete_volumes": False})
        self.assertEqual(self.mocked_neutron.delete_floatingip.call_count, 0)

    def test_incomplete_instance_deletion_executor_produces_celery_signature(self):
        # Arrange
        self.instance.backend_id = None
        self.instance.save()

        # Act
        serialized_instance = serialize_instance(self.instance)
        signature = executors.InstanceDeleteExecutor.get_task_signature(
            self.instance, serialized_instance
        )

        # Assert
        self.assertIsInstance(signature, Signature)


class InstanceDisabledActionsTest(test.APITestCase):
    """Tests to verify that create and destroy actions are disabled for the instance endpoint."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(self.fixture.staff)

    def test_instance_create_action_is_not_allowed(self):
        url = factories.InstanceFactory.get_list_url()
        data = {"name": "Test instance"}

        response = self.client.post(url, data)

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_instance_destroy_action_is_not_allowed(self):
        url = factories.InstanceFactory.get_url(self.fixture.instance)

        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


class InstanceUpdatePortsTest(test.APITestCase):
    action_name = "update_ports"

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(user=self.fixture.admin)
        self.instance = self.fixture.instance
        self.url = factories.InstanceFactory.get_url(
            self.instance, action=self.action_name
        )

    def test_user_can_update_instance_ports(self):
        # instance had 2 ports
        ip_to_keep = factories.PortFactory(
            instance=self.instance, subnet=self.fixture.subnet
        )
        ip_to_delete = factories.PortFactory(instance=self.instance)
        # instance should be connected to new subnet
        subnet_to_connect = factories.SubNetFactory(tenant=self.fixture.tenant)

        response = self.client.post(
            self.url,
            data={
                "ports": [
                    {"subnet": factories.SubNetFactory.get_url(self.fixture.subnet)},
                    {"subnet": factories.SubNetFactory.get_url(subnet_to_connect)},
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertTrue(self.instance.ports.filter(pk=ip_to_keep.pk).exists())
        self.assertFalse(self.instance.ports.filter(pk=ip_to_delete.pk).exists())
        self.assertTrue(self.instance.ports.filter(subnet=subnet_to_connect).exists())

    def test_changed_pinned_address_replaces_the_port(self):
        # Rows are matched on (instance, subnet), so a re-declaration keeping
        # the subnet but naming a different address used to be dropped: the
        # caller got a success and the old address stayed. Declarative callers
        # cannot see that their change did nothing.
        existing = factories.PortFactory(
            instance=self.instance,
            subnet=self.fixture.subnet,
            backend_id="already-created",
            fixed_ips=[
                {"subnet_id": self.fixture.subnet.backend_id, "ip_address": "10.0.0.10"}
            ],
        )

        response = self.client.post(
            self.url,
            data={
                "ports": [
                    {
                        "subnet": factories.SubNetFactory.get_url(self.fixture.subnet),
                        "fixed_ips": [
                            {
                                "subnet_id": self.fixture.subnet.backend_id,
                                "ip_address": "10.0.0.99",
                            }
                        ],
                    }
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        # The old row is gone, so push_instance_ports will delete the backend
        # port still holding the old address before creating the replacement.
        self.assertFalse(models.Port.objects.filter(pk=existing.pk).exists())
        port = self.instance.ports.get(subnet=self.fixture.subnet)
        self.assertEqual(
            port.fixed_ips,
            [
                {
                    "subnet_id": self.fixture.subnet.backend_id,
                    "ip_address": "10.0.0.99",
                }
            ],
        )
        self.assertFalse(port.backend_id, "the replacement must be pushed afresh")

    def test_unchanged_pinned_address_keeps_the_port(self):
        # The replacement above costs a backend port rebuild, so it must happen
        # only on an actual change — re-running an unchanged declaration is the
        # common case for declarative automation.
        existing = factories.PortFactory(
            instance=self.instance,
            subnet=self.fixture.subnet,
            backend_id="already-created",
            fixed_ips=[
                {"subnet_id": self.fixture.subnet.backend_id, "ip_address": "10.0.0.10"}
            ],
        )

        response = self.client.post(
            self.url,
            data={
                "ports": [
                    {
                        "subnet": factories.SubNetFactory.get_url(self.fixture.subnet),
                        "fixed_ips": [
                            {
                                "subnet_id": self.fixture.subnet.backend_id,
                                "ip_address": "10.0.0.10",
                            }
                        ],
                    }
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        existing.refresh_from_db()
        self.assertEqual(existing.backend_id, "already-created")

    def test_declaration_without_fixed_ips_keeps_an_allocated_port(self):
        # No fixed_ips means "allocate one", which an already-allocated port
        # satisfies. Treating it as a change would rebuild every unpinned port
        # on every update.
        existing = factories.PortFactory(
            instance=self.instance,
            subnet=self.fixture.subnet,
            backend_id="already-created",
            fixed_ips=[
                {"subnet_id": self.fixture.subnet.backend_id, "ip_address": "10.0.0.10"}
            ],
        )

        response = self.client.post(
            self.url,
            data={
                "ports": [
                    {"subnet": factories.SubNetFactory.get_url(self.fixture.subnet)}
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        existing.refresh_from_db()
        self.assertEqual(existing.backend_id, "already-created")

    def test_user_cannot_add_port_from_different_settings(self):
        subnet = factories.SubNetFactory()

        response = self.client.post(
            self.url,
            data={
                "ports": [
                    {"subnet": factories.SubNetFactory.get_url(subnet)},
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self.instance.ports.filter(subnet=subnet).exists())

    def _shared_network_from_another_tenant(self):
        """A network owned by another tenant and shared into ours by RBAC."""
        other_tenant = factories.TenantFactory(
            service_settings=self.fixture.tenant.service_settings
        )
        network = factories.NetworkFactory(tenant=other_tenant)
        subnet = factories.SubNetFactory(network=network, tenant=other_tenant)
        factories.NetworkRBACPolicyFactory(
            network=network, target_tenant=self.fixture.tenant
        )
        return subnet

    def test_rbac_share_grants_access_only_to_the_shared_network(self):
        """One shared network must not expose the owner's other networks.

        The allowance used to be computed from the *owning tenant* of any shared
        network, so sharing one network let the target tenant reach every other
        network that tenant owned.
        """
        shared_subnet = self._shared_network_from_another_tenant()
        other_tenant = shared_subnet.tenant
        unshared_network = factories.NetworkFactory(tenant=other_tenant)
        unshared_subnet = factories.SubNetFactory(
            network=unshared_network, tenant=other_tenant
        )

        response = self.client.post(
            self.url,
            data={
                "ports": [
                    {"subnet": factories.SubNetFactory.get_url(unshared_subnet)},
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self.instance.ports.filter(subnet=unshared_subnet).exists())

    def test_subnet_of_an_rbac_shared_network_is_still_accepted(self):
        shared_subnet = self._shared_network_from_another_tenant()

        response = self.client.post(
            self.url,
            data={
                "ports": [
                    {"subnet": factories.SubNetFactory.get_url(shared_subnet)},
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertTrue(self.instance.ports.filter(subnet=shared_subnet).exists())

    def test_port_on_a_shared_network_belongs_to_the_instance_tenant(self):
        # The port used to inherit the subnet's tenant, which on a shared network
        # is the network owner. push_instance_ports then created it in Neutron
        # under that project while attaching it with a session scoped to the
        # instance's tenant, so nova could not see the port and the attach 404ed
        # on every run.
        shared_subnet = self._shared_network_from_another_tenant()

        response = self.client.post(
            self.url,
            data={
                "ports": [
                    {"subnet": factories.SubNetFactory.get_url(shared_subnet)},
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        port = self.instance.ports.get(subnet=shared_subnet)
        self.assertNotEqual(shared_subnet.tenant, self.instance.tenant)
        self.assertEqual(port.tenant, self.instance.tenant)
        self.assertEqual(port.project, self.instance.project)

    def test_user_cannot_pin_an_address_on_another_tenants_network(self):
        """Neutron reserves this to the network owner; Waldur creates ports as admin."""
        subnet = self._shared_network_from_another_tenant()

        response = self.client.post(
            self.url,
            data={
                "ports": [
                    {
                        "subnet": factories.SubNetFactory.get_url(subnet),
                        "fixed_ips": [
                            {"ip_address": "10.90.0.50", "subnet_id": subnet.backend_id}
                        ],
                    }
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self.instance.ports.filter(subnet=subnet).exists())

    def test_user_can_attach_to_a_shared_network_without_pinning(self):
        """Letting OpenStack allocate is what Neutron permits to the recipient."""
        subnet = self._shared_network_from_another_tenant()

        response = self.client.post(
            self.url,
            data={"ports": [{"subnet": factories.SubNetFactory.get_url(subnet)}]},
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertTrue(self.instance.ports.filter(subnet=subnet).exists())

    def test_staff_may_pin_an_address_on_another_tenants_network(self):
        subnet = self._shared_network_from_another_tenant()
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.post(
            self.url,
            data={
                "ports": [
                    {
                        "subnet": factories.SubNetFactory.get_url(subnet),
                        "fixed_ips": [
                            {"ip_address": "10.90.0.50", "subnet_id": subnet.backend_id}
                        ],
                    }
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_user_cannot_connect_instance_to_one_subnet_twice(self):
        response = self.client.post(
            self.url,
            data={
                "ports": [
                    {"subnet": factories.SubNetFactory.get_url(self.fixture.subnet)},
                    {"subnet": factories.SubNetFactory.get_url(self.fixture.subnet)},
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            self.instance.ports.filter(subnet=self.fixture.subnet).exists()
        )

    def test_fixed_ips_are_saved_when_updating_ports(self):
        subnet = factories.SubNetFactory(tenant=self.fixture.tenant)
        fixed_ips = [{"ip_address": "192.168.0.10", "subnet_id": subnet.backend_id}]

        response = self.client.post(
            self.url,
            data={
                "ports": [
                    {
                        "subnet": factories.SubNetFactory.get_url(subnet),
                        "fixed_ips": fixed_ips,
                    },
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        port = self.instance.ports.filter(subnet=subnet).first()
        self.assertIsNotNone(port)
        self.assertEqual(port.fixed_ips, fixed_ips)

    def test_existing_port_is_reused_when_updating_ports(self):
        existing_port = factories.PortFactory(
            tenant=self.fixture.tenant,
            network=self.fixture.subnet.network,
            subnet=self.fixture.subnet,
            service_settings=self.fixture.tenant.service_settings,
            project=self.fixture.tenant.project,
            status="DOWN",
        )

        response = self.client.post(
            self.url,
            data={
                "ports": [
                    {
                        "port": factories.PortFactory.get_url(existing_port),
                        "subnet": factories.SubNetFactory.get_url(self.fixture.subnet),
                    },
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        existing_port.refresh_from_db()
        self.assertEqual(existing_port.instance, self.instance)
        # No extra port should be created for the same subnet
        self.assertEqual(
            self.instance.ports.filter(subnet=self.fixture.subnet).count(), 1
        )
        self.assertEqual(
            self.instance.ports.filter(subnet=self.fixture.subnet).first().pk,
            existing_port.pk,
        )

    def test_existing_active_port_attached_to_same_instance_is_accepted(self):
        existing_port = factories.PortFactory(
            instance=self.instance,
            tenant=self.fixture.tenant,
            network=self.fixture.subnet.network,
            subnet=self.fixture.subnet,
            service_settings=self.fixture.tenant.service_settings,
            project=self.fixture.tenant.project,
            status="ACTIVE",
            device_owner="compute:nova",
        )

        response = self.client.post(
            self.url,
            data={
                "ports": [
                    {
                        "port": factories.PortFactory.get_url(existing_port),
                        "subnet": factories.SubNetFactory.get_url(self.fixture.subnet),
                    },
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        existing_port.refresh_from_db()
        self.assertEqual(existing_port.instance, self.instance)

    def test_port_without_status_can_be_referenced(self):
        existing_port = factories.PortFactory(
            tenant=self.fixture.tenant,
            network=self.fixture.subnet.network,
            subnet=self.fixture.subnet,
            service_settings=self.fixture.tenant.service_settings,
            project=self.fixture.tenant.project,
            status=None,
        )

        response = self.client.post(
            self.url,
            data={
                "ports": [
                    {
                        "port": factories.PortFactory.get_url(existing_port),
                        "subnet": factories.SubNetFactory.get_url(self.fixture.subnet),
                    },
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        existing_port.refresh_from_db()
        self.assertEqual(existing_port.instance, self.instance)

    def test_port_with_infrastructure_device_owner_is_rejected(self):
        existing_port = factories.PortFactory(
            tenant=self.fixture.tenant,
            network=self.fixture.subnet.network,
            subnet=self.fixture.subnet,
            service_settings=self.fixture.tenant.service_settings,
            project=self.fixture.tenant.project,
            status="ACTIVE",
            device_owner="network:router_interface",
        )

        response = self.client.post(
            self.url,
            data={
                "ports": [
                    {
                        "port": factories.PortFactory.get_url(existing_port),
                        "subnet": factories.SubNetFactory.get_url(self.fixture.subnet),
                    },
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ports", response.data)

    def test_port_attached_to_other_instance_is_rejected(self):
        other_instance = factories.InstanceFactory(
            service_settings=self.fixture.tenant.service_settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
        )
        existing_port = factories.PortFactory(
            instance=other_instance,
            tenant=self.fixture.tenant,
            network=self.fixture.subnet.network,
            subnet=self.fixture.subnet,
            service_settings=self.fixture.tenant.service_settings,
            project=self.fixture.tenant.project,
            status="ACTIVE",
        )

        response = self.client.post(
            self.url,
            data={
                "ports": [
                    {
                        "port": factories.PortFactory.get_url(existing_port),
                        "subnet": factories.SubNetFactory.get_url(self.fixture.subnet),
                    },
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ports", response.data)
        existing_port.refresh_from_db()
        self.assertEqual(existing_port.instance, other_instance)


class InstanceUpdateFloatingIPsTest(test.APITestCase):
    action_name = "update_floating_ips"

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.fixture.tenant.service_settings.options = {
            "external_network_id": uuid.uuid4().hex
        }
        self.fixture.tenant.service_settings.save()
        self.client.force_authenticate(user=self.fixture.admin)
        self.instance = self.fixture.instance
        factories.PortFactory.create(instance=self.instance, subnet=self.fixture.subnet)
        self.url = factories.InstanceFactory.get_url(
            self.instance, action=self.action_name
        )
        self.subnet_url = factories.SubNetFactory.get_url(self.fixture.subnet)

    def test_user_can_update_instance_floating_ips(self):
        self.fixture.floating_ip.state = CoreStates.OK
        self.fixture.floating_ip.save()
        floating_ip_url = factories.FloatingIPFactory.get_url(self.fixture.floating_ip)
        data = {
            "floating_ips": [
                {"subnet": self.subnet_url, "url": floating_ip_url},
            ]
        }

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(self.instance.floating_ips.count(), 1)
        self.assertIn(self.fixture.floating_ip, self.instance.floating_ips)

    def test_when_floating_ip_is_attached_action_details_are_updated(self):
        self.fixture.floating_ip.state = CoreStates.OK
        self.fixture.floating_ip.save()
        floating_ip_url = factories.FloatingIPFactory.get_url(self.fixture.floating_ip)
        data = {
            "floating_ips": [
                {"subnet": self.subnet_url, "url": floating_ip_url},
            ]
        }

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.instance.refresh_from_db()
        self.assertEqual(
            self.instance.action_details,
            {
                "message": "Attached floating IPs: %s."
                % self.fixture.floating_ip.address,
                "attached": [self.fixture.floating_ip.address],
                "detached": [],
            },
        )

    def test_when_floating_ip_is_detached_action_details_are_updated(self):
        self.fixture.floating_ip.port = self.instance.ports.first()
        self.fixture.floating_ip.save()

        self.client.post(self.url, data={"floating_ips": []})

        self.instance.refresh_from_db()
        self.assertEqual(
            self.instance.action_details,
            {
                "message": "Detached floating IPs: %s."
                % self.fixture.floating_ip.address,
                "attached": [],
                "detached": [self.fixture.floating_ip.address],
            },
        )

    def test_user_can_not_assign_floating_ip_used_by_other_instance(self):
        port = factories.PortFactory(subnet=self.fixture.subnet)
        floating_ip = factories.FloatingIPFactory(
            tenant=self.fixture.tenant,
            runtime_state="DOWN",
            port=port,
        )
        floating_ip_url = factories.FloatingIPFactory.get_url(floating_ip)
        data = {
            "floating_ips": [
                {"subnet": self.subnet_url, "url": floating_ip_url},
            ]
        }

        response = self.client.post(self.url, data=data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("floating_ips", response.data)

    def test_user_cannot_add_floating_ip_via_subnet_that_is_not_connected_to_instance(
        self,
    ):
        subnet_url = factories.SubNetFactory.get_url()
        data = {"floating_ips": [{"subnet": subnet_url}]}

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_remove_floating_ip_from_instance(self):
        self.fixture.floating_ip.port = self.instance.ports.first()
        self.fixture.floating_ip.save()
        data = {"floating_ips": []}

        response = self.client.post(self.url, data=data)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(self.instance.floating_ips.count(), 0)

    def test_free_floating_ip_is_used_for_allocation(self):
        external_network_id = self.fixture.tenant.service_settings.options[
            "external_network_id"
        ]
        self.fixture.floating_ip.backend_network_id = external_network_id
        self.fixture.floating_ip.save()
        data = {"floating_ips": [{"subnet": self.subnet_url}]}

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertIn(self.fixture.floating_ip, self.instance.floating_ips)

    def test_user_cannot_use_same_subnet_twice(self):
        data = {
            "floating_ips": [{"subnet": self.subnet_url}, {"subnet": self.subnet_url}]
        }
        response = self.client.post(self.url, data=data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class InstanceBackupTest(test.APITestCase):
    action_name = "backup"

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(self.fixture.owner)

    def test_backup_can_be_created_for_instance_with_2_volumes(self):
        url = factories.InstanceFactory.get_url(self.fixture.instance, action="backup")
        payload = self.get_payload()
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            models.Backup.objects.get(name=payload["name"]).snapshots.count(), 2
        )

    def test_backup_can_be_created_for_instance_only_with_system_volume(self):
        instance = self.fixture.instance
        instance.volumes.filter(bootable=False).delete()
        url = factories.InstanceFactory.get_url(instance, action="backup")
        payload = self.get_payload()
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            models.Backup.objects.get(name=payload["name"]).snapshots.count(), 1
        )

    def test_backup_can_be_created_for_instance_with_3_volumes(self):
        instance = self.fixture.instance
        instance.volumes.add(
            factories.VolumeFactory(
                service_settings=instance.service_settings,
                project=instance.project,
            )
        )
        url = factories.InstanceFactory.get_url(instance, action="backup")
        payload = self.get_payload()
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            models.Backup.objects.get(name=payload["name"]).snapshots.count(), 3
        )

    def test_user_cannot_backup_unstable_instance(self):
        instance = self.fixture.instance
        instance.state = CoreStates.UPDATING
        instance.save()
        url = factories.InstanceFactory.get_url(instance, action="backup")

        response = self.client.post(url, data={"name": "test backup"})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def get_payload(self):
        return {"name": "backup_name"}


@ddt
class InstanceActionsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.fixture.tenant.service_settings.options = {
            "external_network_id": uuid.uuid4().hex,
            "tenant_id": self.fixture.tenant.id,
        }
        self.fixture.tenant.service_settings.save()
        self.instance = self.fixture.instance

        self.url = factories.InstanceFactory.get_url(self.instance, action=self.action)
        self.mock_path = mock.patch(
            "waldur_openstack.backend.OpenStackBackend.%s" % self.backend_method
        )
        self.mock_console = self.mock_path.start()
        self.mock_console.return_value = self.backend_return_value

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()


@ddt
class InstanceConsoleTest(InstanceActionsTest):
    action = "console"
    backend_method = "get_console_url"
    backend_return_value = "url"

    @data("staff")
    def test_action_available_to_staff(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.mock_console.assert_called_once_with(self.instance)

    @data("admin", "manager", "owner")
    @helpers.override_openstack_settings(
        ALLOW_CUSTOMER_USERS_OPENSTACK_CONSOLE_ACCESS=False
    )
    def test_action_not_available_for_users_if_this_is_disabled_in_settings(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("staff", "admin", "manager", "owner")
    def test_action_available_for_users(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("user")
    @helpers.override_openstack_settings(
        ALLOW_CUSTOMER_USERS_OPENSTACK_CONSOLE_ACCESS=True
    )
    def test_action_not_available_for_other_users(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_error_is_propagated_correctly(self):
        self.mock_console.side_effect = OpenStackBackendError("Invalid request.")
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue("Invalid request." in response.data)


@ddt
class InstanceConsoleLogTest(InstanceActionsTest):
    action = "console_log"
    backend_method = "get_console_output"
    backend_return_value = "openstack-vm login: "

    @data("staff", "admin", "manager", "owner")
    def test_action_available_for_staff_and_users_associated_with_project(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.mock_console.assert_called_once_with(self.instance, None)

    @data("user")
    def test_action_not_available_for_users_unassociated_with_project(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_error_is_propagated_correctly(self):
        self.mock_console.side_effect = OpenStackBackendError("Invalid request.")
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue("Invalid request." in response.data)


@ddt
class InstancePlacementAllocationsTest(InstanceActionsTest):
    """The diagnostic endpoint that surfaces what Placement allocated to an
    instance. Audience is sysadmin-scope (staff, support, service-provider
    owner of the OpenStack ServiceSettings' customer) — the response carries
    fleet-topology data (resource provider UUIDs and names) that project
    members must not see, even in opaque form."""

    action = "placement_allocations"
    backend_method = "get_instance_placement_allocations"
    backend_return_value = [
        {
            "resource_provider_uuid": "rp-uuid-1",
            "resource_provider_name": "compute01",
            "resources": {"VCPU": 1, "MEMORY_MB": 1024, "DISK_GB": 10},
        }
    ]

    # ---- allowed audiences (sysadmin-scope only) ----

    @data("staff", "global_support", "owner")
    def test_action_allowed_for_sysadmin_audiences(self, user):
        # `owner` in OpenStackFixture holds CUSTOMER.OWNER on the customer
        # that owns the ServiceSettings — i.e. the service-provider owner
        # in production setups. Should see the full payload.
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = response.data[0]
        self.assertEqual(row["resource_provider_uuid"], "rp-uuid-1")
        self.assertEqual(row["resource_provider_name"], "compute01")
        self.assertEqual(
            row["resources"], {"VCPU": 1, "MEMORY_MB": 1024, "DISK_GB": 10}
        )

    # ---- denied audiences ----

    @data("admin", "manager", "member")
    def test_action_denied_for_project_roles(self, user):
        # Project-level roles must NOT see Placement topology — neither
        # the compute hostname nor the opaque resource_provider_uuid.
        # The data is sysadmin-scope.
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.mock_console.assert_not_called()

    @data("user")
    def test_action_denied_for_unrelated_user(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        # Unrelated users are filtered out by the queryset before the
        # permission check, so they get 404 (not 403).
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.mock_console.assert_not_called()

    # ---- backend behaviors (verified via staff caller) ----

    def test_empty_allocations_returns_empty_list(self):
        # Placement returns 200 with empty allocations dict for unknown
        # consumers — should surface as an empty list, not a 404 or 500.
        self.mock_console.return_value = []
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_backend_error_is_propagated(self):
        self.mock_console.side_effect = OpenStackBackendError("Placement down.")
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Placement down.", response.data)


@ddt
class InstanceRescueActionTest(test.APITestCase):
    """Rescue / unrescue actions on InstanceViewSet [WAL-8603]."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.instance = self.fixture.instance
        self.instance.state = CoreStates.OK
        self.instance.runtime_state = models.Instance.RuntimeStates.ACTIVE
        self.instance.save()
        self.rescue_url = factories.InstanceFactory.get_url(
            self.instance, action="rescue"
        )
        self.unrescue_url = factories.InstanceFactory.get_url(
            self.instance, action="unrescue"
        )

        # Stub out the executors so we don't actually push tasks.
        self.rescue_exec = mock.patch(
            "waldur_openstack.executors.InstanceRescueExecutor.execute"
        ).start()
        self.unrescue_exec = mock.patch(
            "waldur_openstack.executors.InstanceUnrescueExecutor.execute"
        ).start()

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    # ---- helpers ----------------------------------------------------------

    def _make_rescue_image(self, hw_rescue_device="cdrom", hw_rescue_bus="ide"):
        image = factories.ImageFactory(settings=self.fixture.settings)
        image.hw_rescue_device = hw_rescue_device
        image.hw_rescue_bus = hw_rescue_bus
        image.save()
        image.tenants.add(self.fixture.tenant)
        return image

    def _make_volume_backed(self):
        # Bootable volume on the instance flips the BFV-detection check.
        factories.VolumeFactory(instance=self.instance, bootable=True)

    # ---- rescue happy path ------------------------------------------------

    def test_rescue_without_image_for_image_backed_instance(self):
        # Image-backed (no bootable volume): rescue without explicit image
        # is allowed; Nova will use the boot image. The fixture's instance
        # comes with a system volume by default, so drop it here to model
        # an image-backed instance.
        self.instance.volumes.update(bootable=False)
        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.post(self.rescue_url, data={})
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.rescue_exec.assert_called_once()

    def test_rescue_with_valid_rescue_image(self):
        image = self._make_rescue_image()
        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.post(
            self.rescue_url,
            data={"rescue_image": factories.ImageFactory.get_url(image)},
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        call_kwargs = self.rescue_exec.call_args.kwargs
        self.assertEqual(call_kwargs["rescue_image_ref"], image.backend_id)

    # ---- BFV safety -------------------------------------------------------

    def test_volume_backed_rescue_requires_explicit_image(self):
        self._make_volume_backed()
        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.post(self.rescue_url, data={})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.rescue_exec.assert_not_called()

    def test_volume_backed_rescue_rejects_non_tagged_image(self):
        self._make_volume_backed()
        # Plain image, no hw_rescue_* properties set.
        plain_image = factories.ImageFactory(settings=self.fixture.settings)
        plain_image.tenants.add(self.fixture.tenant)
        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.post(
            self.rescue_url,
            data={"rescue_image": factories.ImageFactory.get_url(plain_image)},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.rescue_exec.assert_not_called()

    def test_volume_backed_rescue_accepts_tagged_image(self):
        self._make_volume_backed()
        image = self._make_rescue_image()
        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.post(
            self.rescue_url,
            data={"rescue_image": factories.ImageFactory.get_url(image)},
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    # ---- cross-tenant -----------------------------------------------------

    def test_cross_tenant_rescue_image_rejected(self):
        # Image not associated with the instance's tenant.
        other_image = factories.ImageFactory(settings=self.fixture.settings)
        other_image.hw_rescue_device = "cdrom"
        other_image.save()
        # No tenants.add — so it's not visible to the instance's tenant.
        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.post(
            self.rescue_url,
            data={"rescue_image": factories.ImageFactory.get_url(other_image)},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.rescue_exec.assert_not_called()

    # ---- runtime-state preconditions --------------------------------------

    def test_rescue_rejected_from_shutoff(self):
        self.instance.runtime_state = models.Instance.RuntimeStates.SHUTOFF
        self.instance.save()
        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.post(self.rescue_url, data={})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.rescue_exec.assert_not_called()

    def test_unrescue_rejected_from_active(self):
        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.post(self.unrescue_url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.unrescue_exec.assert_not_called()

    def test_unrescue_succeeds_from_rescue(self):
        self.instance.runtime_state = models.Instance.RuntimeStates.RESCUE
        self.instance.save()
        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.post(self.unrescue_url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.unrescue_exec.assert_called_once()

    # ---- permissions ------------------------------------------------------

    @data("user")
    def test_unrelated_user_cannot_rescue(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.post(self.rescue_url, data={})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.rescue_exec.assert_not_called()


class ImageRescueFilterTest(test.APITestCase):
    """is_rescue_image filter on the openstack-images list."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(user=self.fixture.staff)
        # Pre-existing fixture image is tied to the tenant via the fixture; we
        # only need to assert filtering, not permission scoping here.
        self.tenant = self.fixture.tenant
        self.url = factories.ImageFactory.get_list_url()

        self.tagged_with_device = factories.ImageFactory(settings=self.fixture.settings)
        self.tagged_with_device.hw_rescue_device = "cdrom"
        self.tagged_with_device.save()
        self.tagged_with_device.tenants.add(self.tenant)

        self.tagged_with_bus = factories.ImageFactory(settings=self.fixture.settings)
        self.tagged_with_bus.hw_rescue_bus = "usb"
        self.tagged_with_bus.save()
        self.tagged_with_bus.tenants.add(self.tenant)

        self.plain = factories.ImageFactory(settings=self.fixture.settings)
        self.plain.tenants.add(self.tenant)

    def _list_uuids(self, **params):
        params.setdefault("tenant_uuid", self.tenant.uuid.hex)
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["uuid"] for row in response.data}

    def test_no_filter_returns_all(self):
        uuids = self._list_uuids()
        self.assertIn(self.tagged_with_device.uuid.hex, uuids)
        self.assertIn(self.tagged_with_bus.uuid.hex, uuids)
        self.assertIn(self.plain.uuid.hex, uuids)

    def test_is_rescue_image_true_returns_only_tagged(self):
        uuids = self._list_uuids(is_rescue_image="true")
        self.assertIn(self.tagged_with_device.uuid.hex, uuids)
        self.assertIn(self.tagged_with_bus.uuid.hex, uuids)
        self.assertNotIn(self.plain.uuid.hex, uuids)

    def test_is_rescue_image_false_excludes_tagged(self):
        uuids = self._list_uuids(is_rescue_image="false")
        self.assertNotIn(self.tagged_with_device.uuid.hex, uuids)
        self.assertNotIn(self.tagged_with_bus.uuid.hex, uuids)
        self.assertIn(self.plain.uuid.hex, uuids)


@ddt
class InstanceRetrieveTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.instance = self.fixture.instance
        self.url = factories.InstanceFactory.get_url(self.instance)

    @data("staff", "global_support")
    def test_field_hypervisor_hostname_is_available(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue("hypervisor_hostname" in response.json())

    @data("admin", "manager", "owner")
    def test_field_hypervisor_hostname_is_not_available(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse("hypervisor_hostname" in response.json())


class MaxConcurrentProvisionTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()

    def test_settings_limit_is_used_if_it_is_available(self):
        openstack_settings = self.fixture.tenant.service_settings
        openstack_settings.options["max_concurrent_provision_instance"] = 10
        openstack_settings.save()
        self.assertEqual(
            10, LimitedPerTypeThrottleMixin().get_limit(self.fixture.instance)
        )

    @override_openstack_settings(MAX_CONCURRENT_PROVISION={"OpenStack.Instance": 5})
    def test_plugin_settings_limit_is_used_if_it_is_available(self):
        self.assertEqual(
            5, LimitedPerTypeThrottleMixin().get_limit(self.fixture.instance)
        )


class InstanceUpdateBlockedIfOfferingIsUnavailableTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.instance = self.fixture.instance

    def test_start_action_blocked_when_can_be_managed_false(self):
        url = factories.InstanceFactory.get_url(self.instance, action="start")
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        self.instance.can_be_managed = False
        self.instance.save()
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_request_allowed_when_can_be_managed_false(self):
        url = factories.InstanceFactory.get_url(self.instance)
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.instance.can_be_managed = False
        self.instance.save()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_update_blocked_when_can_be_managed_false(self):
        url = factories.InstanceFactory.get_url(self.instance)
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.put(url, {"name": "VM"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.instance.can_be_managed = False
        self.instance.save()
        response = self.client.put(url, {"name": "VM"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
