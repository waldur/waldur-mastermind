from __future__ import unicode_literals

import collections
import datetime

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status, test
from rest_framework.reverse import reverse
from six.moves import mock

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.structure import tasks
from waldur_core.structure.models import CustomerRole, ProjectRole, CustomerPermission
from waldur_core.structure.tests import factories

User = get_user_model()

TestRole = collections.namedtuple('TestRole', ['user', 'customer', 'role'])


class CustomerPermissionBaseTest(test.APITransactionTestCase):
    all_roles = (
        # user customer role
        TestRole('first', 'first', 'owner'),
        TestRole('second', 'second', 'owner'),
    )

    role_map = {
        'owner': CustomerRole.OWNER,
    }

    def setUp(self):
        self.users = {
            'staff': factories.UserFactory(is_staff=True),
            'first': factories.UserFactory(),
            'first_manager': factories.UserFactory(),
            'first_admin': factories.UserFactory(),
            'second': factories.UserFactory(),
            'no_role': factories.UserFactory(),
        }

        self.customers = {
            'first': factories.CustomerFactory(),
            'second': factories.CustomerFactory(),
        }

        customer = self.customers['first']
        project = factories.ProjectFactory(customer=customer)

        for user, customer, role in self.all_roles:
            self.customers[customer].add_user(self.users[user], self.role_map[role])

        project.add_user(self.users['first_admin'], ProjectRole.ADMINISTRATOR)

    # Helper methods
    def _get_permission_url(self, user, customer, role):
        permission = CustomerPermission.objects.get(
            user=self.users[user],
            role=self.role_map[role],
            customer=self.customers[customer],
        )
        return 'http://testserver' + reverse('customer_permission-detail', kwargs={'pk': permission.pk})


class CustomerPermissionListTest(CustomerPermissionBaseTest):

    def test_anonymous_user_cannot_list_customer_permissions(self):
        response = self.client.get(reverse('customer_permission-list'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_cannot_list_roles_of_customer_he_is_not_affiliated(self):
        self.assert_user_access_to_permission_list(user='no_role', customer='first', should_see=False)
        self.assert_user_access_to_permission_list(user='no_role', customer='second', should_see=False)

    def test_customer_owner_can_list_roles_of_his_customer(self):
        self.assert_user_access_to_permission_list(user='first', customer='first', should_see=True)

    def test_project_admin_can_list_roles_of_his_customer(self):
        self.assert_user_access_to_permission_list(user='first_admin', customer='first', should_see=True)

    def test_staff_can_list_roles_of_any_customer(self):
        self.assert_user_access_to_permission_list(user='staff', customer='first', should_see=True)
        self.assert_user_access_to_permission_list(user='staff', customer='second', should_see=True)

    def test_customer_owner_cannot_list_roles_of_another_customer(self):
        self.assert_user_access_to_permission_list(user='first', customer='second', should_see=False)

    def test_project_admin_cannot_list_roles_of_another_customer(self):
        self.assert_user_access_to_permission_list(user='first_admin', customer='second', should_see=False)

    def assert_user_access_to_permission_list(self, user, customer, should_see):
        self.client.force_authenticate(user=self.users[user])

        response = self.client.get(reverse('customer_permission-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        expected_urls = {
            r: self._get_permission_url(*r)
            for r in self.all_roles
            if r.customer == customer
        }

        actual_urls = set([role['url'] for role in response.data])

        for role, role_url in expected_urls.items():
            if should_see:
                self.assertIn(
                    role_url, actual_urls,
                    '{0} user does not see privilege '
                    'he is supposed to see: {1}'.format(user, role),
                )
            else:
                self.assertNotIn(
                    role_url, actual_urls,
                    '{0} user sees privilege '
                    'he is not supposed to see: {1}'.format(user, role),
                )


class CustomerPermissionGrantTest(CustomerPermissionBaseTest):
    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=True)
    def test_customer_owner_can_grant_new_role_within_his_customer(self):
        self.assert_user_access_to_permission_granting(
            login_user='first',
            affected_user='no_role',
            affected_customer='first',
            expected_status=status.HTTP_201_CREATED,
        )

    def test_customer_owner_cannot_grant_existing_role_within_his_customer(self):
        self.assert_user_access_to_permission_granting(
            login_user='staff',
            affected_user='first',
            affected_customer='first',
            expected_status=status.HTTP_400_BAD_REQUEST,
            expected_payload={
                'non_field_errors': ['The fields customer and user must make a unique set.'],
            }
        )

    def test_customer_owner_cannot_grant_role_within_another_customer(self):
        self.assert_user_access_to_permission_granting(
            login_user='first',
            affected_user='no_role',
            affected_customer='second',
            expected_status=status.HTTP_400_BAD_REQUEST,
            expected_payload={
                'customer': ['Invalid hyperlink - Object does not exist.'],
            }
        )

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=False)
    def test_customer_owner_can_not_grant_new_role_within_his_customer_if_settings_are_tweaked(self):
        self.assert_user_access_to_permission_granting(
            login_user='first',
            affected_user='no_role',
            affected_customer='first',
            expected_status=status.HTTP_403_FORBIDDEN,
        )

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=True)
    def test_project_admin_cannot_grant_role_within_his_customer(self):
        self.assert_user_access_to_permission_granting(
            login_user='first_admin',
            affected_user='no_role',
            affected_customer='first',
            expected_status=status.HTTP_403_FORBIDDEN,
            expected_payload={
                'detail': 'You do not have permission to perform this action.',
            }
        )

    def test_staff_can_grant_new_role_within_any_customer(self):
        self.assert_user_access_to_permission_granting(
            login_user='staff',
            affected_user='no_role',
            affected_customer='first',
            expected_status=status.HTTP_201_CREATED,
        )
        self.assert_user_access_to_permission_granting(
            login_user='staff',
            affected_user='no_role',
            affected_customer='second',
            expected_status=status.HTTP_201_CREATED,
        )

    def test_staff_cannot_grant_permission_if_customer_quota_exceeded(self):
        affected_customer = 'first'
        self.customers[affected_customer].set_quota_limit('nc_user_count', 0)

        self.assert_user_access_to_permission_granting(
            login_user='staff',
            affected_user='no_role',
            affected_customer=affected_customer,
            expected_status=status.HTTP_409_CONFLICT,
        )

    def test_staff_cannot_grant_existing_role_within_any_customer(self):
        self.assert_user_access_to_permission_granting(
            login_user='staff',
            affected_user='first',
            affected_customer='first',
            expected_status=status.HTTP_400_BAD_REQUEST,
            expected_payload={
                'non_field_errors': ['The fields customer and user must make a unique set.'],
            }
        )
        self.assert_user_access_to_permission_granting(
            login_user='staff',
            affected_user='second',
            affected_customer='second',
            expected_status=status.HTTP_400_BAD_REQUEST,
            expected_payload={
                'non_field_errors': ['The fields customer and user must make a unique set.'],
            }
        )

    def assert_user_access_to_permission_granting(self, login_user, affected_user, affected_customer,
                                                  expected_status, expected_payload=None):
        self.client.force_authenticate(user=self.users[login_user])

        data = {
            'customer': factories.CustomerFactory.get_url(self.customers[affected_customer]),
            'user': factories.UserFactory.get_url(self.users[affected_user]),
            'role': 'owner',
        }

        response = self.client.post(reverse('customer_permission-list'), data)
        self.assertEqual(response.status_code, expected_status)
        if expected_payload is not None:
            self.assertDictContainsSubset(expected_payload, response.data)


class CustomerPermissionRevokeTest(CustomerPermissionBaseTest):
    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=True)
    def test_customer_owner_can_revoke_role_within_his_customer(self):
        self.assert_user_access_to_permission_revocation(
            login_user='first',
            affected_user='first',
            affected_customer='first',
            expected_status=status.HTTP_204_NO_CONTENT,
        )

    def test_customer_owner_cannot_revoke_role_within_another_customer(self):
        self.assert_user_access_to_permission_revocation(
            login_user='first',
            affected_user='second',
            affected_customer='second',
            expected_status=status.HTTP_404_NOT_FOUND,
        )

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=False)
    def test_customer_owner_can_not_revoke_role_within_his_customer_if_settings_are_tweaked(self):
        self.assert_user_access_to_permission_revocation(
            login_user='first',
            affected_user='first',
            affected_customer='first',
            expected_status=status.HTTP_403_FORBIDDEN,
        )

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=True)
    def test_project_admin_cannot_revoke_role_within_his_customer(self):
        self.assert_user_access_to_permission_revocation(
            login_user='first_admin',
            affected_user='first',
            affected_customer='first',
            expected_status=status.HTTP_403_FORBIDDEN,
            expected_payload={
                'detail': 'You do not have permission to perform this action.',
            }
        )

    def test_staff_can_revoke_role_within_any_customer(self):
        self.assert_user_access_to_permission_revocation(
            login_user='staff',
            affected_user='first',
            affected_customer='first',
            expected_status=status.HTTP_204_NO_CONTENT,
        )
        self.assert_user_access_to_permission_revocation(
            login_user='staff',
            affected_user='second',
            affected_customer='second',
            expected_status=status.HTTP_204_NO_CONTENT,
        )

    def assert_user_access_to_permission_revocation(self, login_user, affected_user, affected_customer,
                                                    expected_status, expected_payload=None):
        self.client.force_authenticate(user=self.users[login_user])

        url = self._get_permission_url(affected_user, affected_customer, 'owner')

        response = self.client.delete(url)
        self.assertEqual(response.status_code, expected_status)
        if expected_payload is not None:
            self.assertDictContainsSubset(expected_payload, response.data)


class CustomerPermissionFilterTest(test.APITransactionTestCase):
    def setUp(self):
        staff_user = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff_user)

        self.users = {
            'first': factories.UserFactory(),
            'second': factories.UserFactory(),
        }

        self.customers = {
            'first': factories.CustomerFactory(),
            'second': factories.CustomerFactory(),
        }

        for customer in self.customers:
            self.customers[customer].add_user(self.users['first'], CustomerRole.OWNER)
            self.customers[customer].add_user(self.users['second'], CustomerRole.OWNER)

    def test_staff_user_can_filter_roles_within_customer_by_customer_uuid(self):
        response = self.client.get(reverse('customer_permission-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for customer in self.customers:
            response = self.client.get(reverse('customer_permission-list'),
                                       data={'customer': self.customers[customer].uuid})
            self.assertEqual(response.status_code, status.HTTP_200_OK)

            customer_url = self._get_customer_url(self.customers[customer])

            for permission in response.data:
                self.assertEqual(customer_url, permission['customer'])

    def test_staff_user_can_filter_roles_within_customer_by_username(self):
        response = self.client.get(reverse('customer_permission-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for user in self.users:
            self._ensure_matching_entries_in('username', self.users[user].username)
            self._ensure_non_matching_entries_not_in('username', self.users[user].username)

    def test_staff_user_can_filter_roles_within_customer_by_native_name(self):
        response = self.client.get(reverse('customer_permission-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for user in self.users:
            self._ensure_matching_entries_in('native_name', self.users[user].native_name)
            self._ensure_non_matching_entries_not_in('native_name', self.users[user].native_name)

    def test_staff_user_can_filter_roles_within_customer_by_full_name(self):
        response = self.client.get(reverse('customer_permission-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for user in self.users:
            self._ensure_matching_entries_in('full_name', self.users[user].full_name)
            self._ensure_non_matching_entries_not_in('full_name', self.users[user].full_name)

    def test_staff_user_can_filter_roles_within_customer_by_role_name(self):
        response = self.client.get(reverse('customer_permission-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(reverse('customer_permission-list'),
                                   data={'role': 'owner'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for permission in response.data:
            self.assertEqual('owner', permission['role'])

    def test_staff_user_cannot_filter_roles_within_customer_by_role_pk(self):
        response = self.client.get(reverse('customer_permission-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(reverse('customer_permission-list'),
                                   data={'role': '1'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_staff_user_can_see_required_fields_in_filtration_response(self):
        response = self.client.get(reverse('customer_permission-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for customer in self.customers:
            response = self.client.get(reverse('customer_permission-list'),
                                       data={'customer': self.customers[customer].uuid})
            self.assertEqual(response.status_code, status.HTTP_200_OK)

            required_fields = ('url', 'user_native_name', 'user_full_name', 'user_username')

            for permission in response.data:
                for field in required_fields:
                    self.assertIn(field, permission)

    # Helper methods
    def _ensure_matching_entries_in(self, field, value):
        response = self.client.get(reverse('customer_permission-list'),
                                   data={field: value})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for permission in response.data:
            self.assertEqual(value, permission['user_' + field])

    def _ensure_non_matching_entries_not_in(self, field, value):
        user = factories.UserFactory()

        customer = factories.CustomerFactory()
        customer.add_user(user, CustomerRole.OWNER)

        response = self.client.get(reverse('customer_permission-list'),
                                   data={field: getattr(user, field)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for permission in response.data:
            self.assertNotEqual(value, permission['user_' + field])

    def _get_customer_url(self, customer):
        return 'http://testserver' + reverse('customer-detail', kwargs={'uuid': customer.uuid})


class CustomerPermissionExpirationTest(test.APITransactionTestCase):
    def setUp(self):
        permission = factories.CustomerPermissionFactory()
        self.user = permission.user
        self.customer = permission.customer
        self.url = factories.CustomerPermissionFactory.get_url(permission)

    def test_user_can_not_update_permission_expiration_time_for_himself(self):
        self.client.force_authenticate(user=self.user)

        expiration_time = timezone.now() + datetime.timedelta(days=100)
        response = self.client.put(self.url, {
            'expiration_time': expiration_time
        })
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_update_permission_expiration_time_for_any_user(self):
        staff_user = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff_user)

        expiration_time = timezone.now() + datetime.timedelta(days=100)
        response = self.client.put(self.url, {
            'expiration_time': expiration_time
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['expiration_time'], expiration_time, response.data)

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=True)
    def test_owner_can_update_permission_expiration_time_for_other_owner_in_same_customer(self):
        owner = factories.UserFactory()
        self.customer.add_user(owner, CustomerRole.OWNER)
        self.client.force_authenticate(user=owner)

        expiration_time = timezone.now() + datetime.timedelta(days=100)
        response = self.client.put(self.url, {
            'expiration_time': expiration_time
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['expiration_time'], expiration_time, response.data)

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=False)
    def test_owner_can_not_update_permission_expiration_time_for_other_owner_if_settings_are_tweaked(self):
        owner = factories.UserFactory()
        self.customer.add_user(owner, CustomerRole.OWNER)
        self.client.force_authenticate(user=owner)

        expiration_time = timezone.now() + datetime.timedelta(days=100)
        response = self.client.put(self.url, {
            'expiration_time': expiration_time
        })
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_can_not_set_permission_expiration_time_lower_than_current(self):
        staff_user = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff_user)

        expiration_time = timezone.now() - datetime.timedelta(days=100)
        response = self.client.put(self.url, {
            'expiration_time': expiration_time
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_grant_permissions_with_greater_expiration_time(self):
        expiration_time = timezone.now() + datetime.timedelta(days=100)
        permission = factories.CustomerPermissionFactory(expiration_time=expiration_time)
        self.client.force_authenticate(user=permission.user)
        response = self.client.post(factories.CustomerPermissionFactory.get_list_url(), {
            'customer': factories.CustomerFactory.get_url(customer=permission.customer),
            'user': factories.UserFactory.get_url(),
            'role': CustomerRole.OWNER,
            'expiration_time': expiration_time + datetime.timedelta(days=1),
        })
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_can_set_expiration_time_when_role_is_created(self):
        staff_user = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff_user)

        expiration_time = timezone.now() + datetime.timedelta(days=100)
        response = self.client.post(factories.CustomerPermissionFactory.get_list_url(), {
            'customer': factories.CustomerFactory.get_url(),
            'user': factories.UserFactory.get_url(),
            'role': factories.CustomerPermissionFactory.role,
            'expiration_time': expiration_time,
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['expiration_time'], expiration_time, response.data)

    def test_task_revokes_expired_permissions(self):
        expired_permission = factories.CustomerPermissionFactory(
            expiration_time=timezone.now() - datetime.timedelta(days=100))
        not_expired_permission = factories.CustomerPermissionFactory(
            expiration_time=timezone.now() + datetime.timedelta(days=100))
        tasks.check_expired_permissions()

        self.assertFalse(expired_permission.customer.has_user(
            expired_permission.user, expired_permission.role))
        self.assertTrue(not_expired_permission.customer.has_user(
            not_expired_permission.user, not_expired_permission.role))

    def test_when_expiration_time_is_updated_event_is_emitted(self):
        staff_user = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff_user)
        expiration_time = timezone.now() + datetime.timedelta(days=100)

        with mock.patch('logging.LoggerAdapter.info') as mocked_info:
            self.client.put(self.url, {'expiration_time': expiration_time})
            (args, kwargs) = mocked_info.call_args_list[-1]
            event_type = kwargs['extra']['event_type']
            event_message = args[0]
            self.assertEqual(event_type, 'role_updated')
            self.assertTrue(staff_user.full_name in event_message)


class CustomerPermissionCreatedByTest(test.APITransactionTestCase):
    def test_user_which_granted_permission_is_stored(self):
        staff_user = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff_user)

        user = factories.UserFactory()
        customer = factories.CustomerFactory()

        data = {
            'customer': factories.CustomerFactory.get_url(customer),
            'user': factories.UserFactory.get_url(user),
            'role': CustomerRole.OWNER,
        }

        response = self.client.post(reverse('customer_permission-list'), data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        permission = CustomerPermission.objects.get(pk=response.data['pk'])
        self.assertEqual(permission.created_by, staff_user)
