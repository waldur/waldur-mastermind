from unittest import mock
from unittest.mock import patch

from django.test import TestCase

from waldur_core.core import utils as core_utils
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack.openstack import apps

from .. import factories


@patch('waldur_core.core.tasks.BackendMethodTask.delay')
class SshKeysHandlersTest(TestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.ssh_key = structure_factories.SshPublicKeyFactory(user=self.user)
        self.tenant = factories.TenantFactory()

    def test_ssh_key_will_be_removed_if_user_lost_connection_to_tenant(
        self, mocked_task_call
    ):
        project = self.tenant.project
        perm = project.add_user(self.user, ProjectRole.ADMIN)
        perm.revoke()

        serialized_tenant = core_utils.serialize_instance(self.tenant)
        mocked_task_call.assert_called_once_with(
            serialized_tenant,
            'remove_ssh_key_from_tenant',
            self.ssh_key.name,
            self.ssh_key.fingerprint,
        )

    def test_ssh_key_will_not_be_removed_if_user_still_has_connection_to_tenant(
        self, mocked_task_call
    ):
        project = self.tenant.project
        perm = project.add_user(self.user, ProjectRole.ADMIN)
        project.customer.add_user(self.user, CustomerRole.OWNER)
        perm.revoke()

        self.assertEqual(mocked_task_call.call_count, 0)

    def test_ssh_key_will_be_deleted_from_tenant_on_user_deletion(
        self, mocked_task_call
    ):
        project = self.tenant.project
        project.add_user(self.user, ProjectRole.ADMIN)
        self.user.delete()

        serialized_tenant = core_utils.serialize_instance(self.tenant)
        mocked_task_call.assert_called_once_with(
            serialized_tenant,
            'remove_ssh_key_from_tenant',
            self.ssh_key.name,
            self.ssh_key.fingerprint,
        )

    def test_ssh_key_will_be_deleted_from_tenant_on_ssh_key_deletion(
        self, mocked_task_call
    ):
        project = self.tenant.project
        project.add_user(self.user, ProjectRole.ADMIN)
        self.ssh_key.delete()

        serialized_tenant = core_utils.serialize_instance(self.tenant)
        mocked_task_call.assert_called_once_with(
            serialized_tenant,
            'remove_ssh_key_from_tenant',
            self.ssh_key.name,
            self.ssh_key.fingerprint,
        )


class LogTenantQuotaUpdateTest(TestCase):
    def test_logger_called_on_quota_limit_update(self):
        tenant = factories.TenantFactory()
        tenant.set_quota_limit('vcpu', 10)

        with patch('waldur_openstack.openstack.handlers.event_logger') as logger_mock:
            tenant.set_quota_limit('vcpu', 20)

            logger_mock.openstack_tenant_quota.info.assert_called_once_with(
                mock.ANY,
                event_type='openstack_tenant_quota_limit_updated',
                event_context={
                    'quota_name': 'vcpu',
                    'tenant': tenant,
                    'limit': 20,
                    'old_limit': 10,
                },
            )


class UpdateServiceSettingsNameHandlerTest(TestCase):
    def test_settings_name_is_update_when_tenant_is_renamed(self):
        tenant = factories.TenantFactory()
        service_settings = structure_factories.ServiceSettingsFactory(
            scope=tenant, name=tenant.name, type=apps.OpenStackConfig.service_name
        )

        tenant.name = 'new name'
        tenant.save()

        service_settings.refresh_from_db()
        self.assertEqual(service_settings.name, tenant.name)
