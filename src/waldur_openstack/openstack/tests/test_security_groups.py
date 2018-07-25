from ddt import ddt, data
from mock import patch
from rest_framework import test, status

from .. import models
from . import factories, fixtures


class BaseSecurityGroupTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()


@ddt
class SecurityGroupCreateTest(BaseSecurityGroupTest):

    def setUp(self):
        super(SecurityGroupCreateTest, self).setUp()
        self.url = factories.TenantFactory.get_url(self.fixture.tenant, 'create_security_group')

    @data('staff', 'owner', 'admin', 'manager')
    def test_user_with_access_can_create_security_group(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        data = {
            'name': 'https',
            'rules': [
                {
                    'protocol': 'tcp',
                    'from_port': 100,
                    'to_port': 8001,
                    'cidr': '11.11.1.2/24',
                }
            ]
        }
        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(models.SecurityGroup.objects.count(), 1)
        self.assertEqual(models.SecurityGroupRule.objects.count(), 1)

    def test_can_not_create_security_group_with_invalid_protocol(self):
        self.client.force_authenticate(self.fixture.staff)

        data = {
            'name': 'https',
            'rules': [
                {
                    'protocol': 'invalid',
                    'from_port': 100,
                    'to_port': 8001,
                    'cidr': '11.11.1.2/24',
                }
            ]
        }
        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(models.SecurityGroup.objects.count(), 0)
        self.assertEqual(models.SecurityGroupRule.objects.count(), 0)

    def test_can_not_create_security_group_with_invalid_port(self):
        self.client.force_authenticate(self.fixture.staff)

        data = {
            'name': 'https',
            'rules': [
                {
                    'protocol': 'icmp',
                    'from_port': 8001,
                    'to_port': 8001,
                    'cidr': '11.11.1.2/24',
                }
            ]
        }
        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(models.SecurityGroup.objects.count(), 0)
        self.assertEqual(models.SecurityGroupRule.objects.count(), 0)

    def test_can_not_create_security_group_with_invalid_cidr(self):
        self.client.force_authenticate(self.fixture.staff)

        data = {
            'name': 'https',
            'rules': [
                {
                    'protocol': 'tcp',
                    'from_port': 8001,
                    'to_port': 8001,
                    'cidr': '300.300.300.300/100',
                }
            ]
        }
        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(models.SecurityGroup.objects.count(), 0)
        self.assertEqual(models.SecurityGroupRule.objects.count(), 0)


@ddt
class SecurityGroupUpdateTest(BaseSecurityGroupTest):

    def setUp(self):
        super(SecurityGroupUpdateTest, self).setUp()
        self.security_group = factories.SecurityGroupFactory(
            service_project_link=self.fixture.openstack_spl,
            tenant=self.fixture.tenant,
            state=models.SecurityGroup.States.OK)
        self.url = factories.SecurityGroupFactory.get_url(self.security_group)

    @data('staff', 'owner', 'admin', 'manager')
    def test_user_with_access_can_update_security_group(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        data = {'name': 'new_name'}
        response = self.client.patch(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.security_group.refresh_from_db()
        self.assertEqual(self.security_group.name, data['name'])

    @data('user')
    def test_user_without_access_cannot_update_security_group(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.patch(self.url, data={'name': 'new_name'})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_security_group_can_not_be_updated_in_unstable_state(self):
        self.client.force_authenticate(self.fixture.admin)
        self.security_group.state = models.SecurityGroup.States.ERRED
        self.security_group.save()

        response = self.client.patch(self.url, data={'name': 'new_name'})

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @data('patch', 'put')
    def test_default_security_group_name_can_not_be_updated(self, method):
        self.client.force_authenticate(self.fixture.staff)
        self.security_group.name = 'default'
        self.security_group.save()

        update = getattr(self.client, method)
        response = update(self.url, data={'name': 'new_name'})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data('patch', 'put')
    def test_security_group_name_can_not_become_default(self, method):
        self.client.force_authenticate(self.fixture.staff)
        self.security_group.name = 'ssh'
        self.security_group.save()

        update = getattr(self.client, method)
        response = update(self.url, data={'name': 'default'})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('name' in response.data)


class SecurityGroupSetRulesTest(BaseSecurityGroupTest):

    def setUp(self):
        super(SecurityGroupSetRulesTest, self).setUp()
        self.security_group = factories.SecurityGroupFactory(
            service_project_link=self.fixture.openstack_spl,
            tenant=self.fixture.tenant,
            state=models.SecurityGroup.States.OK)
        self.url = factories.SecurityGroupFactory.get_url(self.security_group, action='set_rules')

    def test_security_group_rules_can_not_be_added_if_quota_is_over_limit(self):
        self.client.force_authenticate(self.fixture.admin)
        self.fixture.tenant.set_quota_limit('security_group_rule_count', 0)

        data = [
            {
                'protocol': 'udp',
                'from_port': 100,
                'to_port': 8001,
                'cidr': '11.11.1.2/24',
            }
        ]
        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.security_group.refresh_from_db()
        self.assertEqual(self.security_group.rules.count(), 0)

    def test_security_group_update_starts_calls_executor(self):
        self.client.force_authenticate(self.fixture.admin)

        execute_method = 'waldur_openstack.openstack.executors.PushSecurityGroupRulesExecutor.execute'
        with patch(execute_method) as mocked_execute:
            response = self.client.post(self.url, data=[])

            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
            mocked_execute.assert_called_once_with(self.security_group)

    def test_user_can_remove_rule_from_security_group(self):
        rule_to_remain = factories.SecurityGroupRuleFactory(security_group=self.security_group)
        rule_to_delete = factories.SecurityGroupRuleFactory(security_group=self.security_group)
        self.client.force_authenticate(self.fixture.admin)

        response = self.client.post(self.url, data=[{'id': rule_to_remain.id}])

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        exist_rules = self.security_group.rules.all()
        self.assertIn(rule_to_remain, exist_rules)
        self.assertNotIn(rule_to_delete, exist_rules)

    def test_user_can_add_new_security_group_rule_and_leave_existing(self):
        exist_rule = factories.SecurityGroupRuleFactory(security_group=self.security_group)
        self.client.force_authenticate(self.fixture.admin)
        new_rule_data = {
            'protocol': 'udp',
            'from_port': 100,
            'to_port': 8001,
            'cidr': '11.11.1.2/24',
        }

        response = self.client.post(self.url, data=[{'id': exist_rule.id}, new_rule_data])

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(self.security_group.rules.count(), 2)
        self.assertTrue(self.security_group.rules.filter(id=exist_rule.id).exists())
        self.assertTrue(self.security_group.rules.filter(**new_rule_data).exists())

    def test_rule_is_not_created_if_port_range_is_invalid(self):
        self.client.force_authenticate(self.fixture.admin)

        data = [
            {
                'protocol': 'udp',
                'from_port': 125,
                'to_port': 25,
                'cidr': '11.11.1.2/24',
            }
        ]
        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.security_group.refresh_from_db()
        self.assertEqual(self.security_group.rules.count(), 0)

    def test_rule_is_not_updated_if_port_range_is_invalid(self):
        rule = factories.SecurityGroupRuleFactory(
            security_group=self.security_group, from_port=2222, to_port=2222)
        self.client.force_authenticate(self.fixture.admin)

        data = [
            {
                'protocol': rule.protocol,
                'from_port': 125,
                'to_port': 25,
                'cidr': rule.cidr,
            }
        ]
        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        rule.refresh_from_db()
        self.assertEqual(rule.from_port, 2222)
        self.assertEqual(rule.to_port, 2222)

    def test_rule_is_updated_if_port_range_is_valid(self):
        rule = factories.SecurityGroupRuleFactory(
            security_group=self.security_group, from_port=2222, to_port=2222)
        self.client.force_authenticate(self.fixture.admin)

        data = [
            {
                'id': rule.id,
                'protocol': rule.protocol,
                'from_port': 125,
                'to_port': 225,
                'cidr': rule.cidr,
            }
        ]
        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        rule.refresh_from_db()
        self.assertEqual(rule.from_port, 125)
        self.assertEqual(rule.to_port, 225)


@ddt
class SecurityGroupDeleteTest(BaseSecurityGroupTest):

    def setUp(self):
        super(SecurityGroupDeleteTest, self).setUp()
        self.security_group = factories.SecurityGroupFactory(
            service_project_link=self.fixture.openstack_spl,
            tenant=self.fixture.tenant,
            state=models.SecurityGroup.States.OK)
        self.url = factories.SecurityGroupFactory.get_url(self.security_group)

    @data('admin', 'manager', 'staff', 'owner')
    def test_project_administrator_can_delete_security_group(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        with patch('waldur_openstack.openstack.executors.SecurityGroupDeleteExecutor.execute') as mocked_execute:
            response = self.client.delete(self.url)
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

            mocked_execute.assert_called_once_with(self.security_group, force=False, async=True)

    def test_security_group_can_be_deleted_from_erred_state(self):
        self.security_group.state = models.SecurityGroup.States.ERRED
        self.security_group.save()

        self.client.force_authenticate(self.fixture.admin)
        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_default_security_group_name_can_not_be_deleted(self):
        self.client.force_authenticate(self.fixture.staff)
        self.security_group.name = 'default'
        self.security_group.save()

        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class SecurityGroupRetrieveTest(BaseSecurityGroupTest):

    def setUp(self):
        super(SecurityGroupRetrieveTest, self).setUp()
        self.security_group = factories.SecurityGroupFactory(
            service_project_link=self.fixture.openstack_spl,
            tenant=self.fixture.tenant,
        )
        self.url = factories.SecurityGroupFactory.get_url(self.security_group)

    @data('admin', 'manager', 'staff', 'owner')
    def test_user_can_access_security_groups_of_project_instances_he_has_role_in(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('user')
    def test_user_cannot_access_security_groups_of_instances_not_connected_to_him(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
