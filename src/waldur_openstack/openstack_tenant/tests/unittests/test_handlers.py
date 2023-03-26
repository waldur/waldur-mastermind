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


class SecurityGroupHandlerTest(BaseServicePropertyTest):
    def setUp(self):
        super().setUp()

    def test_security_group_create(self):
        openstack_security_group = openstack_factories.SecurityGroupFactory(
            tenant=self.tenant, state=StateMixin.States.CREATING
        )

        openstack_security_rule = openstack_factories.SecurityGroupRuleFactory(
            security_group=openstack_security_group
        )

        self.assertEqual(models.SecurityGroup.objects.count(), 0)

        openstack_security_group.set_ok()
        openstack_security_group.save()

        self.assertEqual(models.SecurityGroup.objects.count(), 1)
        self.assertTrue(
            models.SecurityGroup.objects.filter(
                settings=self.service_settings,
                backend_id=openstack_security_group.backend_id,
            ).exists()
        )
        security_group_property = models.SecurityGroup.objects.get(
            settings=self.service_settings,
            backend_id=openstack_security_group.backend_id,
        )

        self.assertTrue(
            security_group_property.rules.filter(
                backend_id=openstack_security_rule.backend_id
            ).exists()
        )

    def test_security_group_import(self):
        resource = openstack_factories.SecurityGroupFactory(tenant=self.tenant)
        self.assertTrue(
            models.SecurityGroup.objects.filter(
                settings=self.service_settings, backend_id=resource.backend_id
            ).exists()
        )

    def test_when_security_group_is_created_remote_group_is_filled(self):
        group1 = openstack_factories.SecurityGroupFactory(
            tenant=self.tenant, state=StateMixin.States.CREATING
        )
        group2 = openstack_factories.SecurityGroupFactory(tenant=self.tenant)

        openstack_security_rule = openstack_factories.SecurityGroupRuleFactory(
            security_group=group1, remote_group=group2
        )

        group1.set_ok()
        group1.save()

        self.assertTrue(
            models.SecurityGroupRule.objects.filter(
                backend_id=openstack_security_rule.backend_id,
                security_group__backend_id=group1.backend_id,
                remote_group__backend_id=group2.backend_id,
            ).exists()
        )

    def test_when_group_is_imported_remote_group_is_imported_too(self):
        group1 = openstack_factories.SecurityGroupFactory(tenant=self.tenant)
        group2 = openstack_factories.SecurityGroupFactory(tenant=self.tenant)
        rule = openstack_factories.SecurityGroupRuleFactory(
            security_group=group1, remote_group=group2
        )
        self.assertTrue(
            models.SecurityGroupRule.objects.filter(
                backend_id=rule.backend_id,
                security_group__backend_id=group1.backend_id,
                remote_group__backend_id=group2.backend_id,
            ).exists()
        )

    def test_rule_without_backend_id_is_skipped_when_group_is_imported(self):
        group1 = openstack_factories.SecurityGroupFactory(tenant=self.tenant)
        group2 = openstack_factories.SecurityGroupFactory(tenant=self.tenant)
        rule = openstack_factories.SecurityGroupRuleFactory(
            security_group=group1, remote_group=group2, backend_id=''
        )
        self.assertFalse(
            models.SecurityGroupRule.objects.filter(
                backend_id=rule.backend_id,
                security_group__backend_id=group1.backend_id,
                remote_group__backend_id=group2.backend_id,
            ).exists()
        )

    def test_rule_without_backend_id_is_synced_when_backend_id_is_assigned(self):
        group1 = openstack_factories.SecurityGroupFactory(tenant=self.tenant)
        group2 = openstack_factories.SecurityGroupFactory(tenant=self.tenant)
        rule = openstack_factories.SecurityGroupRuleFactory(
            security_group=group1, remote_group=group2, backend_id=''
        )
        rule.backend_id = 'valid_backend_id'
        rule.save()
        self.assertTrue(
            models.SecurityGroupRule.objects.filter(
                backend_id=rule.backend_id,
                security_group__backend_id=group1.backend_id,
                remote_group__backend_id=group2.backend_id,
            ).exists()
        )

    def test_when_rule_is_updated_remote_group_is_synced(self):
        group1 = openstack_factories.SecurityGroupFactory(tenant=self.tenant)
        group2 = openstack_factories.SecurityGroupFactory(tenant=self.tenant)
        rule = openstack_factories.SecurityGroupRuleFactory(security_group=group1)
        rule.remote_group = group2
        rule.save(update_fields=['remote_group'])
        self.assertTrue(
            models.SecurityGroupRule.objects.filter(
                backend_id=rule.backend_id,
                security_group__backend_id=group1.backend_id,
                remote_group__backend_id=group2.backend_id,
            ).exists()
        )

    def test_rule_without_backend_id_is_skipped_when_remote_group_is_changed(self):
        group1 = openstack_factories.SecurityGroupFactory(tenant=self.tenant)
        group2 = openstack_factories.SecurityGroupFactory(tenant=self.tenant)
        rule = openstack_factories.SecurityGroupRuleFactory(security_group=group1)
        rule.remote_group = group2
        rule.backend_id = ''
        rule.save()
        self.assertFalse(
            models.SecurityGroupRule.objects.filter(
                backend_id=rule.backend_id,
                security_group__backend_id=group1.backend_id,
                remote_group__backend_id=group2.backend_id,
            ).exists()
        )

    def test_security_group_update(self):
        openstack_security_group = openstack_factories.SecurityGroupFactory(
            tenant=self.tenant,
            name='New name',
            description='New description',
            state=StateMixin.States.UPDATING,
        )
        security_group = factories.SecurityGroupFactory(
            settings=self.service_settings,
            backend_id=openstack_security_group.backend_id,
        )

        openstack_security_group.set_ok()
        openstack_security_group.save()
        security_group.refresh_from_db()

        self.assertIn(openstack_security_group.name, security_group.name)
        self.assertIn(openstack_security_group.description, security_group.description)

    def test_security_group_rules_are_created_when_one_more_rule_is_added(self):
        openstack_security_group = openstack_factories.SecurityGroupFactory(
            tenant=self.tenant, state=StateMixin.States.CREATING
        )
        security_group = factories.SecurityGroupFactory(
            settings=self.service_settings,
            backend_id=openstack_security_group.backend_id,
        )
        openstack_factories.SecurityGroupRuleFactory(
            security_group=openstack_security_group
        )
        openstack_security_group.set_ok()
        openstack_security_group.save()

        self.assertEqual(
            security_group.rules.count(), 1, 'Security group rule has not been added'
        )
        self.assertEqual(
            security_group.rules.first().protocol,
            openstack_security_group.rules.first().protocol,
        )
        self.assertEqual(
            security_group.rules.first().from_port,
            openstack_security_group.rules.first().from_port,
        )
        self.assertEqual(
            security_group.rules.first().to_port,
            openstack_security_group.rules.first().to_port,
        )

    def test_security_group_is_deleted_when_openstack_security_group_is_deleted(self):
        openstack_security_group = openstack_factories.SecurityGroupFactory(
            tenant=self.tenant
        )
        openstack_security_group.delete()
        self.assertEqual(models.SecurityGroup.objects.count(), 0)

    def test_if_security_group_already_exists_duplicate_is_not_created(self):
        """
        Consider the following case: there are two objects:
        security group as a property and security group as a resource.
        Property has been created by pull_security_groups method.
        When resource switches state, property should be created too via signal handler.
        But as security group already exists as a property it should not be created twice,
        because otherwise it violates uniqueness constraint.
        """
        security_group = factories.SecurityGroupFactory(
            settings=self.service_settings,
            backend_id='backend_id',
        )
        openstack_security_group = openstack_factories.SecurityGroupFactory(
            tenant=self.tenant,
            state=StateMixin.States.CREATING,
            backend_id=security_group.backend_id,
        )
        openstack_security_group.set_ok()
        openstack_security_group.save()

        self.assertEqual(models.SecurityGroup.objects.count(), 1)

    def test_security_group_rules_are_deleted(self):
        # Arrange
        openstack_security_group = openstack_factories.SecurityGroupFactory(
            tenant=self.tenant, state=StateMixin.States.UPDATING
        )
        rule = openstack_factories.SecurityGroupRuleFactory(
            security_group=openstack_security_group
        )
        security_group = factories.SecurityGroupFactory(
            settings=self.service_settings,
            backend_id=openstack_security_group.backend_id,
        )

        # Act
        openstack_security_group.set_ok()
        openstack_security_group.save()
        rule.delete()

        # Assert
        self.assertEqual(
            security_group.rules.count(), 0, 'Security group rule has not been deleted'
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
            settings=self.service_settings, backend_id='VALID_BACKEND_ID'
        )
        openstack_floating_ip = openstack_factories.FloatingIPFactory(
            tenant=self.tenant,
            state=StateMixin.States.CREATING,
            backend_id='VALID_BACKEND_ID',
        )
        self.assertEqual(models.FloatingIP.objects.count(), 1)

        openstack_floating_ip.set_ok()
        openstack_floating_ip.save()

        self.assertEqual(models.FloatingIP.objects.count(), 1)

    def test_floating_ip_update(self):
        openstack_floating_ip = openstack_factories.FloatingIPFactory(
            tenant=self.tenant, name='New name', state=StateMixin.States.UPDATING
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

        new_password = 'new_password'
        new_username = 'new_username'

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
        NEW_EXTERNAL_NETWORK_ID = 'new_external_network_id'
        self.tenant.external_network_id = NEW_EXTERNAL_NETWORK_ID
        self.tenant.save()
        self.service_settings.refresh_from_db()
        self.assertEqual(
            self.service_settings.get_option('external_network_id'),
            NEW_EXTERNAL_NETWORK_ID,
        )

    def test_update_service_setting_internal_network_id_if_updated_scope(self):
        NEW_INTERNAL_NETWORK_ID = 'new_internal_network_id'
        self.tenant.internal_network_id = NEW_INTERNAL_NETWORK_ID
        self.tenant.save()
        self.service_settings.refresh_from_db()
        self.assertEqual(
            self.service_settings.get_option('internal_network_id'),
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
        tenant.service_settings.options['config_drive'] = True
        tenant.service_settings.save()

        # Assert
        service_settings.refresh_from_db()
        self.assertEqual(service_settings.options['config_drive'], True)


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
            name='New network name',
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
            name='New subnet name',
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
        self.assertEqual(service_settings.options['tenant_id'], tenant.backend_id)
        self.assertEqual(
            service_settings.options['availability_zone'], tenant.availability_zone
        )
        self.assertFalse('console_type' in service_settings.options)

    def test_copy_console_type_from_admin_settings_to_private_settings(self):
        shared_settings = openstack_factories.OpenStackServiceSettingsFactory()
        shared_settings.options['console_type'] = 'console_type'
        shared_settings.save()
        tenant = openstack_factories.TenantFactory(service_settings=shared_settings)
        private_settings = structure_models.ServiceSettings.objects.get(
            scope=tenant,
            type=apps.OpenStackTenantConfig.service_name,
        )
        self.assertTrue('console_type' in private_settings.options)
        self.assertEqual(
            shared_settings.options['console_type'],
            private_settings.options['console_type'],
        )

    def test_copy_config_drive_from_admin_settings_to_private_settings(self):
        shared_settings = openstack_factories.OpenStackServiceSettingsFactory()
        shared_settings.options['config_drive'] = True
        shared_settings.save()
        tenant = openstack_factories.TenantFactory(service_settings=shared_settings)
        private_settings = structure_models.ServiceSettings.objects.get(
            scope=tenant,
            type=apps.OpenStackTenantConfig.service_name,
        )
        self.assertTrue(private_settings.options['config_drive'])

    def test_copy_tenant_id_from_tenant_to_private_settings(self):
        shared_settings = openstack_factories.OpenStackServiceSettingsFactory()
        tenant = openstack_factories.TenantFactory(
            service_settings=shared_settings, backend_id=None
        )
        private_settings = structure_models.ServiceSettings.objects.get(
            scope=tenant,
            type=apps.OpenStackTenantConfig.service_name,
        )
        tenant.backend_id = 'VALID_BACKEND_ID'
        tenant.save()
        private_settings.refresh_from_db()
        self.assertTrue(private_settings.options['tenant_id'], tenant.backend_id)
