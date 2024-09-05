from django.test import TestCase

from waldur_core.core.models import StateMixin
from waldur_core.structure import models as structure_models
from waldur_openstack.openstack.models import Tenant
from waldur_openstack.openstack.tests import factories as openstack_factories
from waldur_openstack.openstack_tenant import apps


class BaseServicePropertyTest(TestCase):
    def setUp(self):
        self.tenant = openstack_factories.TenantFactory()
        self.service_settings = structure_models.ServiceSettings.objects.get(
            scope=self.tenant, type=apps.OpenStackTenantConfig.service_name
        )


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
