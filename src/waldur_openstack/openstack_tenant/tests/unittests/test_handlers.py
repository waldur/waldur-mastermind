from django.test import TestCase

from waldur_core.core.models import StateMixin
from waldur_core.structure import models as structure_models
from waldur_openstack.openstack.models import Tenant
from waldur_openstack.openstack.tests import factories as openstack_factories
from waldur_openstack.openstack_tenant import apps, models
from waldur_openstack.openstack_tenant.tests import factories


class BaseServicePropertyTest(TestCase):
    def setUp(self):
        self.tenant = openstack_factories.TenantFactory()
        self.service_settings = structure_models.ServiceSettings.objects.get(
            scope=self.tenant, type=apps.OpenStackTenantConfig.service_name
        )


class FloatingIPHandlerTest(BaseServicePropertyTest):
    def setUp(self):
        super().setUp()

    def test_floating_ip_create(self):
        openstack_floating_ip = openstack_factories.FloatingIPFactory(
            tenant=self.tenant, state=StateMixin.States.CREATING
        )
        self.assertEqual(models.FloatingIP.objects.count(), 0)

        openstack_floating_ip.set_ok()
        openstack_floating_ip.save()

        self.assertEqual(models.FloatingIP.objects.count(), 1)

    def test_floating_ip_is_not_created_if_it_already_exists(self):
        factories.FloatingIPFactory(
            settings=self.service_settings, backend_id="VALID_BACKEND_ID"
        )
        openstack_floating_ip = openstack_factories.FloatingIPFactory(
            tenant=self.tenant,
            state=StateMixin.States.CREATING,
            backend_id="VALID_BACKEND_ID",
        )
        self.assertEqual(models.FloatingIP.objects.count(), 1)

        openstack_floating_ip.set_ok()
        openstack_floating_ip.save()

        self.assertEqual(models.FloatingIP.objects.count(), 1)

    def test_floating_ip_update(self):
        openstack_floating_ip = openstack_factories.FloatingIPFactory(
            tenant=self.tenant, name="New name", state=StateMixin.States.UPDATING
        )
        floating_ip = factories.FloatingIPFactory(
            settings=self.service_settings,
            backend_id=openstack_floating_ip.backend_id,
        )

        openstack_floating_ip.set_ok()
        openstack_floating_ip.save()
        floating_ip.refresh_from_db()

        self.assertEqual(openstack_floating_ip.name, floating_ip.name)
        self.assertEqual(openstack_floating_ip.address, floating_ip.address)
        self.assertEqual(openstack_floating_ip.runtime_state, floating_ip.runtime_state)
        self.assertEqual(
            openstack_floating_ip.backend_network_id, floating_ip.backend_network_id
        )

    def test_floating_ip_delete(self):
        openstack_floating_ip = openstack_factories.FloatingIPFactory(
            tenant=self.tenant
        )
        factories.FloatingIPFactory(
            settings=self.service_settings, backend_id=openstack_floating_ip.backend_id
        )

        openstack_floating_ip.delete()
        self.assertEqual(models.FloatingIP.objects.count(), 0)


class TenantChangeCredentialsTest(TestCase):
    def test_service_settings_password_and_username_are_updated_when_tenant_user_password_changes(
        self,
    ):
        tenant = openstack_factories.TenantFactory()
        service_settings = structure_models.ServiceSettings.objects.first()
        service_settings.scope = tenant
        service_settings.password = tenant.user_password
        service_settings.save()

        new_password = "new_password"
        new_username = "new_username"

        tenant.user_password = new_password
        tenant.user_username = new_username
        tenant.save()
        service_settings.refresh_from_db()
        self.assertEqual(service_settings.password, new_password)
        self.assertEqual(service_settings.username, new_username)


class UpdateTenantSettingsTest(TestCase):
    def setUp(self) -> None:
        self.tenant: Tenant = openstack_factories.TenantFactory()
        self.service_settings = structure_models.ServiceSettings.objects.get(
            scope=self.tenant, type=apps.OpenStackTenantConfig.service_name
        )

    def test_update_service_setting_external_network_id_if_updated_scope(self):
        NEW_EXTERNAL_NETWORK_ID = "new_external_network_id"
        self.tenant.external_network_id = NEW_EXTERNAL_NETWORK_ID
        self.tenant.save()
        self.service_settings.refresh_from_db()
        self.assertEqual(
            self.service_settings.get_option("external_network_id"),
            NEW_EXTERNAL_NETWORK_ID,
        )

    def test_update_service_setting_internal_network_id_if_updated_scope(self):
        NEW_INTERNAL_NETWORK_ID = "new_internal_network_id"
        self.tenant.internal_network_id = NEW_INTERNAL_NETWORK_ID
        self.tenant.save()
        self.service_settings.refresh_from_db()
        self.assertEqual(
            self.service_settings.get_option("internal_network_id"),
            NEW_INTERNAL_NETWORK_ID,
        )

    def test_mark_settings_as_erred_if_tenant_was_not_created(self):
        # Arrange
        self.tenant.state = StateMixin.States.CREATING
        self.tenant.save()
        self.service_settings.state = StateMixin.States.CREATING
        self.service_settings.save()

        # Act
        self.tenant.set_erred()
        self.tenant.save()

        # Assert
        self.service_settings.refresh_from_db()
        self.assertEqual(self.service_settings.state, StateMixin.States.ERRED)


class ConfigDriveUpdateTest(TestCase):
    def test_service_settings_config_drive_is_updated(self):
        # Arrange
        tenant = openstack_factories.TenantFactory()
        service_settings = structure_models.ServiceSettings.objects.get(
            scope=tenant, type=apps.OpenStackTenantConfig.service_name
        )

        # Act
        tenant.service_settings.options["config_drive"] = True
        tenant.service_settings.save()

        # Assert
        service_settings.refresh_from_db()
        self.assertEqual(service_settings.options["config_drive"], True)


class NetworkHandlerTest(BaseServicePropertyTest):
    def test_network_create(self):
        openstack_network = openstack_factories.NetworkFactory(
            tenant=self.tenant, state=StateMixin.States.CREATING
        )
        self.assertEqual(models.Network.objects.count(), 0)

        openstack_network.set_ok()
        openstack_network.save()
        self.assertTrue(
            models.Network.objects.filter(
                backend_id=openstack_network.backend_id
            ).exists()
        )

    def test_network_update(self):
        openstack_network = openstack_factories.NetworkFactory(
            tenant=self.tenant,
            name="New network name",
            state=StateMixin.States.UPDATING,
        )
        network = factories.NetworkFactory(
            settings=self.service_settings,
            backend_id=openstack_network.backend_id,
        )

        openstack_network.set_ok()
        openstack_network.save()
        network.refresh_from_db()

        self.assertEqual(openstack_network.name, network.name)
        self.assertEqual(openstack_network.is_external, network.is_external)
        self.assertEqual(openstack_network.type, network.type)
        self.assertEqual(openstack_network.segmentation_id, network.segmentation_id)
        self.assertEqual(openstack_network.backend_id, network.backend_id)

    def test_network_delete(self):
        openstack_network = openstack_factories.NetworkFactory(tenant=self.tenant)
        openstack_network.delete()
        self.assertEqual(models.Network.objects.count(), 0)


class SubNetHandlerTest(BaseServicePropertyTest):
    def setUp(self):
        super().setUp()

        self.openstack_network = openstack_factories.NetworkFactory(tenant=self.tenant)
        self.network = models.Network.objects.get(
            settings=self.service_settings, backend_id=self.openstack_network.backend_id
        )

    def test_subnet_create(self):
        openstack_subnet = openstack_factories.SubNetFactory(
            network=self.openstack_network, state=StateMixin.States.CREATING
        )
        self.assertEqual(models.SubNet.objects.count(), 0)

        openstack_subnet.set_ok()
        openstack_subnet.save()

        self.assertTrue(
            models.SubNet.objects.filter(
                backend_id=openstack_subnet.backend_id
            ).exists()
        )

    def test_subnet_update(self):
        openstack_subnet = openstack_factories.SubNetFactory(
            network=self.openstack_network,
            name="New subnet name",
            state=StateMixin.States.UPDATING,
        )
        subnet = factories.SubNetFactory(
            network=self.network,
            settings=self.service_settings,
            backend_id=openstack_subnet.backend_id,
        )

        openstack_subnet.set_ok()
        openstack_subnet.save()
        subnet.refresh_from_db()

        self.assertEqual(openstack_subnet.name, subnet.name)
        self.assertEqual(openstack_subnet.cidr, subnet.cidr)
        self.assertEqual(openstack_subnet.gateway_ip, subnet.gateway_ip)
        self.assertEqual(openstack_subnet.allocation_pools, subnet.allocation_pools)
        self.assertEqual(openstack_subnet.ip_version, subnet.ip_version)
        self.assertEqual(openstack_subnet.enable_dhcp, subnet.enable_dhcp)
        self.assertEqual(openstack_subnet.dns_nameservers, subnet.dns_nameservers)

    def test_subnet_delete(self):
        openstack_subnet = openstack_factories.SubNetFactory(
            network__tenant=self.tenant
        )
        openstack_subnet.delete()
        self.assertEqual(models.SubNet.objects.count(), 0)


class CreateServiceFromTenantTest(TestCase):
    def test_service_is_created_on_tenant_creation(self):
        tenant = openstack_factories.TenantFactory()

        self.assertTrue(
            structure_models.ServiceSettings.objects.filter(scope=tenant).exists()
        )
        service_settings = structure_models.ServiceSettings.objects.get(
            scope=tenant,
            type=apps.OpenStackTenantConfig.service_name,
        )
        self.assertEqual(service_settings.name, tenant.name)
        self.assertEqual(service_settings.customer, tenant.project.customer)
        self.assertEqual(service_settings.username, tenant.user_username)
        self.assertEqual(service_settings.password, tenant.user_password)
        self.assertEqual(service_settings.domain, tenant.service_settings.domain)
        self.assertEqual(
            service_settings.backend_url,
            tenant.service_settings.backend_url,
        )
        self.assertEqual(service_settings.type, apps.OpenStackTenantConfig.service_name)
        self.assertEqual(service_settings.options["tenant_id"], tenant.backend_id)
        self.assertEqual(
            service_settings.options["availability_zone"], tenant.availability_zone
        )
        self.assertFalse("console_type" in service_settings.options)

    def test_copy_console_type_from_admin_settings_to_private_settings(self):
        shared_settings = openstack_factories.OpenStackServiceSettingsFactory()
        shared_settings.options["console_type"] = "console_type"
        shared_settings.save()
        tenant = openstack_factories.TenantFactory(service_settings=shared_settings)
        private_settings = structure_models.ServiceSettings.objects.get(
            scope=tenant,
            type=apps.OpenStackTenantConfig.service_name,
        )
        self.assertTrue("console_type" in private_settings.options)
        self.assertEqual(
            shared_settings.options["console_type"],
            private_settings.options["console_type"],
        )

    def test_copy_config_drive_from_admin_settings_to_private_settings(self):
        shared_settings = openstack_factories.OpenStackServiceSettingsFactory()
        shared_settings.options["config_drive"] = True
        shared_settings.save()
        tenant = openstack_factories.TenantFactory(service_settings=shared_settings)
        private_settings = structure_models.ServiceSettings.objects.get(
            scope=tenant,
            type=apps.OpenStackTenantConfig.service_name,
        )
        self.assertTrue(private_settings.options["config_drive"])

    def test_copy_tenant_id_from_tenant_to_private_settings(self):
        shared_settings = openstack_factories.OpenStackServiceSettingsFactory()
        tenant = openstack_factories.TenantFactory(
            service_settings=shared_settings, backend_id=None
        )
        private_settings = structure_models.ServiceSettings.objects.get(
            scope=tenant,
            type=apps.OpenStackTenantConfig.service_name,
        )
        tenant.backend_id = "VALID_BACKEND_ID"
        tenant.save()
        private_settings.refresh_from_db()
        self.assertTrue(private_settings.options["tenant_id"], tenant.backend_id)
