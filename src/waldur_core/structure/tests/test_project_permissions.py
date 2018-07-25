from __future__ import unicode_literals

import collections
import datetime

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test
from six.moves import mock

from waldur_core.structure import tasks
from waldur_core.structure.models import ProjectRole, CustomerRole, ProjectPermission
from waldur_core.structure.tests import factories, fixtures

User = get_user_model()

TestRole = collections.namedtuple('TestRole', ['user', 'project', 'role'])


class ProjectPermissionBaseTest(test.APITransactionTestCase):
    all_roles = (
        #           user      project     role
        TestRole('admin1', 'project11', 'admin'),
        TestRole('admin2', 'project11', 'admin'),
        TestRole('admin3', 'project12', 'admin'),
        TestRole('admin4', 'project13', 'admin'),
        TestRole('admin5', 'project21', 'admin'),
        TestRole('project_manager1', 'project11', 'manager'),
        TestRole('project_manager2', 'project11', 'manager'),
        TestRole('project_manager3', 'project12', 'manager'),
        TestRole('project_manager4', 'project13', 'manager'),
        TestRole('project_manager5', 'project21', 'manager'),
    )

    role_map = {
        'admin': ProjectRole.ADMINISTRATOR,
        'manager': ProjectRole.MANAGER,
    }

    def setUp(self):
        customers = {
            'customer1': factories.CustomerFactory(),
            'customer2': factories.CustomerFactory(),
        }

        self.projects = {
            'project11': factories.ProjectFactory(customer=customers['customer1']),
            'project12': factories.ProjectFactory(customer=customers['customer1']),
            'project13': factories.ProjectFactory(customer=customers['customer1']),
            'project21': factories.ProjectFactory(customer=customers['customer2']),
        }

        self.users = {
            'owner1': factories.UserFactory(),
            'owner2': factories.UserFactory(),
            'manager1': factories.UserFactory(),
            'manager2': factories.UserFactory(),
            'manager3': factories.UserFactory(),
            'admin1': factories.UserFactory(),
            'admin2': factories.UserFactory(),
            'admin3': factories.UserFactory(),
            'admin4': factories.UserFactory(),
            'admin5': factories.UserFactory(),
            'project_manager1': factories.UserFactory(),
            'project_manager2': factories.UserFactory(),
            'project_manager3': factories.UserFactory(),
            'project_manager4': factories.UserFactory(),
            'project_manager5': factories.UserFactory(),
            'no_role': factories.UserFactory(),
            'staff': factories.UserFactory(is_staff=True),
        }

        customers['customer1'].add_user(self.users['owner1'], CustomerRole.OWNER)
        customers['customer2'].add_user(self.users['owner2'], CustomerRole.OWNER)

        for user, project, role in self.all_roles:
            self.projects[project].add_user(self.users[user], role)

    # Helper methods
    def _get_permission_url(self, user, project, role):
        permission = ProjectPermission.objects.get(
            user=self.users[user],
            role=self.role_map[role],
            project=self.projects[project],
        )
        return 'http://testserver' + reverse('project_permission-detail', kwargs={'pk': permission.pk})


class ProjectPermissionListTest(ProjectPermissionBaseTest):
    def test_anonymous_user_cannot_list_project_permissions(self):
        response = self.client.get(reverse('project_permission-list'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_cannot_list_roles_of_project_he_is_not_affiliated(self):
        for project in self.projects.keys():
            self.assert_user_access_to_permission_list(user='no_role', project=project, should_see=False)

    def test_customer_owner_can_list_roles_of_his_customers_project(self):
        self.assert_user_access_to_permission_list(user='owner1', project='project11', should_see=True)
        self.assert_user_access_to_permission_list(user='owner1', project='project12', should_see=True)
        self.assert_user_access_to_permission_list(user='owner1', project='project13', should_see=True)

    def test_customer_owner_cannot_list_roles_of_another_customers_project(self):
        self.assert_user_access_to_permission_list(user='owner1', project='project21', should_see=False)

    def test_project_admin_can_list_roles_of_his_project(self):
        self.assert_user_access_to_permission_list(user='admin1', project='project11', should_see=True)

    def test_project_admin_cannot_list_roles_of_another_project(self):
        self.assert_user_access_to_permission_list(user='admin2', project='project12', should_see=False)
        self.assert_user_access_to_permission_list(user='admin2', project='project13', should_see=False)
        self.assert_user_access_to_permission_list(user='admin2', project='project21', should_see=False)

    def test_staff_can_list_roles_of_any_project(self):
        for project in self.projects.keys():
            self.assert_user_access_to_permission_list(user='staff', project=project, should_see=True)

    def assert_user_access_to_permission_list(self, user, project, should_see):
        self.client.force_authenticate(user=self.users[user])

        response = self.client.get(reverse('project_permission-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        expected_urls = {
            r: self._get_permission_url(*r)
            for r in self.all_roles
            if r.project == project
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


class ProjectPermissionGrantTest(ProjectPermissionBaseTest):
    def test_customer_owner_can_grant_new_role_within_his_customers_project(self):
        self.assert_user_access_to_permission_granting(
            login_user='owner1',
            affected_user='no_role',
            affected_project='project11',
            expected_status=status.HTTP_201_CREATED,
        )

    def test_customer_owner_can_grant_new_role_within_his_customers_project_for_himself(self):
        self.assert_user_access_to_permission_granting(
            login_user='owner1',
            affected_user='owner1',
            affected_project='project11',
            expected_status=status.HTTP_201_CREATED,
        )

    def test_customer_owner_cannot_grant_existing_role_within_his_project(self):
        self.assert_user_access_to_permission_granting(
            login_user='owner1',
            affected_user='admin1',
            affected_project='project11',
            expected_status=status.HTTP_400_BAD_REQUEST,
            expected_payload={
                'non_field_errors': ['The fields project and user must make a unique set.'],
            }
        )

    def test_customer_owner_cannot_grant_role_within_his_project_if_user_already_has_role(self):
        self.assert_user_access_to_permission_granting(
            login_user='owner1',
            affected_user='admin1',
            role='manager',
            affected_project='project11',
            expected_status=status.HTTP_400_BAD_REQUEST,
            expected_payload={
                'non_field_errors': ['The fields project and user must make a unique set.'],
            }
        )

    def test_customer_owner_cannot_grant_role_within_another_customers_project(self):
        self.assert_user_access_to_permission_granting(
            login_user='owner1',
            affected_user='no_role',
            affected_project='project21',
            expected_status=status.HTTP_400_BAD_REQUEST,
            expected_payload={
                'project': ['Invalid hyperlink - Object does not exist.'],
            }
        )

    def test_project_manager_can_grant_new_admin_role_within_his_project(self):
        self.assert_user_access_to_permission_granting(
            login_user='project_manager1',
            affected_user='no_role',
            affected_project='project11',
            expected_status=status.HTTP_201_CREATED,
            role='admin',
        )

    def test_project_manager_cannot_grant_new_manager_role_within_his_project(self):
        self.assert_user_access_to_permission_granting(
            login_user='project_manager1',
            affected_user='no_role',
            affected_project='project11',
            expected_status=status.HTTP_403_FORBIDDEN,
            role='manager',
            expected_payload={
                'detail': 'You do not have permission to perform this action.',
            }
        )

    def test_project_manager_cannot_grant_new_admin_role_within_not_his_project(self):
        self.assert_user_access_to_permission_granting(
            login_user='project_manager1',
            affected_user='no_role',
            affected_project='project12',
            expected_status=status.HTTP_400_BAD_REQUEST,
            role='admin',
        )

    def test_project_admin_cannot_grant_new_role_within_his_project(self):
        self.assert_user_access_to_permission_granting(
            login_user='admin1',
            affected_user='no_role',
            affected_project='project11',
            expected_status=status.HTTP_403_FORBIDDEN,
            expected_payload={
                'detail': 'You do not have permission to perform this action.',
            }
        )

    def test_project_manager_cannot_grant_existing_role_within_his_project(self):
        self.assert_user_access_to_permission_granting(
            login_user='project_manager1',
            affected_user='admin1',
            affected_project='project11',
            expected_status=status.HTTP_400_BAD_REQUEST,
            expected_payload={
                'non_field_errors': ['The fields project and user must make a unique set.'],
            }
        )

    def test_project_manager_cannot_grant_role_to_himself(self):
        self.assert_user_access_to_permission_granting(
            login_user='project_manager1',
            affected_user='project_manager1',
            affected_project='project11',
            expected_status=status.HTTP_400_BAD_REQUEST,
        )

    def test_project_admin_cannot_grant_role_within_another_project(self):
        self.assert_user_access_to_permission_granting(
            login_user='admin1',
            affected_user='no_role',
            affected_project='project13',
            expected_status=status.HTTP_400_BAD_REQUEST,
            expected_payload={
                'project': ['Invalid hyperlink - Object does not exist.'],
            }
        )

    def test_staff_can_grant_new_role_within_any_project(self):
        for project in self.projects.keys():
            self.assert_user_access_to_permission_granting(
                login_user='staff',
                affected_user='no_role',
                affected_project=project,
                expected_status=status.HTTP_201_CREATED,
            )

    def test_staff_cannot_grant_new_role_if_customer_quota_were_exceeded(self):
        project = 'project11'
        self.projects[project].customer.set_quota_limit('nc_user_count', 0)
        self.assert_user_access_to_permission_granting(
            login_user='staff',
            affected_user='no_role',
            affected_project=project,
            expected_status=status.HTTP_409_CONFLICT,
        )

    def test_staff_cannot_grant_existing_role_within_any_project(self):
        for user, project, role in self.all_roles:
            self.assert_user_access_to_permission_granting(
                login_user='staff',
                affected_user=user,
                affected_project=project,
                expected_status=status.HTTP_400_BAD_REQUEST,
                role=role,
                expected_payload={
                    'non_field_errors': ['The fields project and user must make a unique set.'],
                }
            )

    def assert_user_access_to_permission_granting(self, login_user, affected_user, affected_project,
                                                  expected_status, expected_payload=None, role='admin'):
        self.client.force_authenticate(user=self.users[login_user])

        data = {
            'project': factories.ProjectFactory.get_url(self.projects[affected_project]),
            'user': factories.UserFactory.get_url(self.users[affected_user]),
            'role': role,
        }

        response = self.client.post(reverse('project_permission-list'), data)
        self.assertEqual(response.status_code, expected_status, response.data)
        if expected_payload is not None:
            self.assertDictContainsSubset(expected_payload, response.data)


class ProjectPermissionRevokeTest(ProjectPermissionBaseTest):
    def test_customer_owner_can_revoke_role_within_his_customers_project(self):
        self.assert_user_access_to_permission_revocation(
            login_user='owner1',
            affected_user='admin1',
            affected_project='project11',
            expected_status=status.HTTP_204_NO_CONTENT,
        )

    def test_customer_owner_can_revoke_role_within_his_customers_project_for_himself(self):
        self.projects['project11'].add_user(self.users['owner1'], ProjectRole.ADMINISTRATOR)
        self.assert_user_access_to_permission_revocation(
            login_user='owner1',
            affected_user='owner1',
            affected_project='project11',
            expected_status=status.HTTP_204_NO_CONTENT,
        )

    def test_customer_owner_cannot_revoke_role_within_another_customers_project(self):
        self.assert_user_access_to_permission_revocation(
            login_user='owner1',
            affected_user='admin5',
            affected_project='project21',
            expected_status=status.HTTP_404_NOT_FOUND,
        )

    def test_project_manager_can_revoke_admin_role_within_his_project(self):
        self.assert_user_access_to_permission_revocation(
            login_user='project_manager1',
            affected_user='admin1',
            affected_project='project11',
            expected_status=status.HTTP_204_NO_CONTENT,
        )

    def test_project_manager_cannot_revoke_manager_role_within_his_project(self):
        self.assert_user_access_to_permission_revocation(
            login_user='project_manager1',
            affected_user='project_manager1',
            affected_project='project11',
            expected_status=status.HTTP_403_FORBIDDEN,
            role='manager',
            expected_payload={
                'detail': 'You do not have permission to perform this action.',
            }
        )

    def test_project_admin_cannot_revoke_role_within_his_project(self):
        self.assert_user_access_to_permission_revocation(
            login_user='admin1',
            affected_user='admin2',
            affected_project='project11',
            expected_status=status.HTTP_403_FORBIDDEN,
            expected_payload={
                'detail': 'You do not have permission to perform this action.',
            }
        )

    def test_project_admin_cannot_revoke_role_within_within_another_project(self):
        for user, project, _ in self.all_roles:
            if project == 'project11':
                continue

            self.assert_user_access_to_permission_revocation(
                login_user='admin1',
                affected_user='admin5',
                affected_project='project21',
                expected_status=status.HTTP_404_NOT_FOUND,
            )

    def test_staff_can_revoke_role_within_any_project(self):
        for user, project, role in self.all_roles:
            self.assert_user_access_to_permission_revocation(
                login_user='staff',
                affected_user=user,
                affected_project=project,
                expected_status=status.HTTP_204_NO_CONTENT,
                role=role,
            )

    def assert_user_access_to_permission_revocation(self, login_user, affected_user, affected_project,
                                                    expected_status, expected_payload=None, role='admin'):
        self.client.force_authenticate(user=self.users[login_user])

        url = self._get_permission_url(affected_user, affected_project, role)

        response = self.client.delete(url)
        self.assertEqual(response.status_code, expected_status)
        if expected_payload is not None:
            self.assertDictContainsSubset(expected_payload, response.data)


class ProjectPermissionFilterTest(test.APITransactionTestCase):

    def setUp(self):
        fixture = fixtures.ProjectFixture()
        self.staff = fixture.staff
        self.customer = fixture.customer
        self.admin = fixture.admin
        self.manager = fixture.manager
        self.project = fixture.project
        self.url = reverse('project_permission-list')

    def test_user_can_filter_permission_by_project(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get(self.url, {'project': self.project.uuid.hex})
        self.assertEqual(len(response.data), 2)

    def test_user_can_filter_permission_by_user(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get(self.url, {'user': self.admin.uuid.hex})
        self.assertEqual(len(response.data), 1)

    def test_user_can_filter_permission_by_customer(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url, {'customer': self.customer.uuid.hex})
        self.assertEqual(len(response.data), 2)

    def test_user_can_filter_permission_by_empty_customer(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url, {'customer': factories.CustomerFactory().uuid.hex})
        self.assertEqual(len(response.data), 0)


class ProjectPermissionExpirationTest(test.APITransactionTestCase):
    def setUp(self):
        permission = factories.ProjectPermissionFactory()
        self.user = permission.user
        self.project = permission.project
        self.url = reverse('project_permission-detail', kwargs={'pk': permission.pk})

    def test_user_can_not_update_permission_expiration_time(self):
        self.client.force_authenticate(user=self.user)

        expiration_time = timezone.now() + datetime.timedelta(days=100)
        response = self.client.put(self.url, {
            'expiration_time': expiration_time
        })
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_update_permission_expiration_time(self):
        staff_user = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff_user)

        expiration_time = timezone.now() + datetime.timedelta(days=100)
        response = self.client.put(self.url, {
            'expiration_time': expiration_time
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['expiration_time'], expiration_time, response.data)

    def test_owner_can_update_permission_for_himself(self):
        owner = factories.UserFactory()
        self.project.customer.add_user(owner, CustomerRole.OWNER)
        self.client.force_authenticate(user=owner)

        expiration_time = timezone.now() + datetime.timedelta(days=100)
        response = self.client.put(self.url, {
            'expiration_time': expiration_time
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['expiration_time'], expiration_time, response.data)

    def test_user_can_set_permission_expiration_time_lower_than_current(self):
        staff_user = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff_user)

        expiration_time = timezone.now() - datetime.timedelta(days=100)
        response = self.client.put(self.url, {
            'expiration_time': expiration_time
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_set_expiration_time_role_when_role_is_created(self):
        staff_user = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff_user)

        expiration_time = timezone.now() + datetime.timedelta(days=100)
        response = self.client.post(factories.ProjectPermissionFactory.get_list_url(), {
            'project': factories.ProjectFactory.get_url(),
            'user': factories.UserFactory.get_url(),
            'role': factories.ProjectPermissionFactory.role,
            'expiration_time': expiration_time,
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['expiration_time'], expiration_time, response.data)

    def test_user_cannot_grant_permissions_with_greater_expiration_time(self):
        expiration_time = timezone.now() + datetime.timedelta(days=100)
        permission = factories.ProjectPermissionFactory(
            role=ProjectRole.MANAGER,
            expiration_time=expiration_time)
        self.client.force_authenticate(user=permission.user)
        response = self.client.post(factories.ProjectPermissionFactory.get_list_url(), {
            'project': factories.ProjectFactory.get_url(project=permission.project),
            'user': factories.UserFactory.get_url(),
            'role': factories.ProjectPermissionFactory.role,
            'expiration_time': expiration_time + datetime.timedelta(days=1),
        })
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_task_revokes_expired_permissions(self):
        expired_permission = factories.ProjectPermissionFactory(
            expiration_time=timezone.now() - datetime.timedelta(days=100))
        not_expired_permission = factories.ProjectPermissionFactory(
            expiration_time=timezone.now() + datetime.timedelta(days=100))
        tasks.check_expired_permissions()

        self.assertFalse(expired_permission.project.has_user(
            expired_permission.user, expired_permission.role))
        self.assertTrue(not_expired_permission.project.has_user(
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


class ProjectPermissionCreatedByTest(test.APITransactionTestCase):
    def test_user_which_granted_permission_is_stored(self):
        staff_user = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff_user)

        user = factories.UserFactory()
        project = factories.ProjectFactory()

        data = {
            'project': factories.ProjectFactory.get_url(project),
            'user': factories.UserFactory.get_url(user),
            'role': ProjectRole.ADMINISTRATOR,
        }

        response = self.client.post(reverse('project_permission-list'), data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        permission = ProjectPermission.objects.get(pk=response.data['pk'])
        self.assertEqual(permission.created_by, staff_user)


class GetProjectUsersTest(test.APITransactionTestCase):

    def setUp(self):
        fixture = fixtures.ProjectFixture()
        self.project = fixture.project
        self.admin = fixture.admin
        self.manager = fixture.manager

    def test_get_users_by_default_returns_both_managers_and_admins(self):
        users = list(self.project.get_users())
        self.assertListEqual(users, [self.admin, self.manager])

    def test_get_users_by_returns_admins(self):
        users = list(self.project.get_users(ProjectRole.ADMINISTRATOR))
        self.assertListEqual(users, [self.admin])

    def test_get_users_by_returns_managers(self):
        users = list(self.project.get_users(ProjectRole.MANAGER))
        self.assertListEqual(users, [self.manager])
