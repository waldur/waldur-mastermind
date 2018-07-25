from __future__ import unicode_literals

from ddt import data, ddt
from django.test import TransactionTestCase
from django.urls import reverse
from mock_django import mock_signal_receiver
from rest_framework import status, test
from six.moves import mock

from waldur_core.quotas.tests import factories as quota_factories
from waldur_core.structure import executors, models, signals, views
from waldur_core.structure.models import CustomerRole, Project, ProjectRole
from waldur_core.structure.tests import factories, fixtures, models as test_models


class ProjectPermissionGrantTest(TransactionTestCase):
    def setUp(self):
        self.project = factories.ProjectFactory()
        self.user = factories.UserFactory()

    def test_add_user_returns_created_if_grant_didnt_exist_before(self):
        _, created = self.project.add_user(self.user, ProjectRole.ADMINISTRATOR)

        self.assertTrue(created, 'Project permission should have been reported as created')

    def test_add_user_returns_not_created_if_grant_existed_before(self):
        self.project.add_user(self.user, ProjectRole.ADMINISTRATOR)
        _, created = self.project.add_user(self.user, ProjectRole.ADMINISTRATOR)

        self.assertFalse(created, 'Project permission should have been reported as not created')

    def test_add_user_returns_membership(self):
        membership, _ = self.project.add_user(self.user, ProjectRole.ADMINISTRATOR)

        self.assertEqual(membership.user, self.user)
        self.assertEqual(membership.project, self.project)

    def test_add_user_returns_same_membership_for_consequent_calls_with_same_arguments(self):
        membership1, _ = self.project.add_user(self.user, ProjectRole.ADMINISTRATOR)
        membership2, _ = self.project.add_user(self.user, ProjectRole.ADMINISTRATOR)

        self.assertEqual(membership1, membership2)

    def test_add_user_emits_structure_role_granted_if_grant_didnt_exist_before(self):
        with mock_signal_receiver(signals.structure_role_granted) as receiver:
            self.project.add_user(self.user, ProjectRole.ADMINISTRATOR)

        receiver.assert_called_once_with(
            structure=self.project,
            user=self.user,
            role=ProjectRole.ADMINISTRATOR,
            created_by=None,

            sender=Project,
            signal=signals.structure_role_granted,
        )

    def test_add_user_doesnt_emit_structure_role_granted_if_grant_existed_before(self):
        self.project.add_user(self.user, ProjectRole.ADMINISTRATOR)

        with mock_signal_receiver(signals.structure_role_granted) as receiver:
            self.project.add_user(self.user, ProjectRole.ADMINISTRATOR)

        self.assertFalse(receiver.called, 'structure_role_granted should not be emitted')


class ProjectPermissionRevokeTest(TransactionTestCase):
    def setUp(self):
        self.project = factories.ProjectFactory()
        self.user = factories.UserFactory()
        self.removed_by = factories.UserFactory()

    def test_remove_user_emits_structure_role_revoked_for_each_role_user_had_in_project(self):
        self.project.add_user(self.user, ProjectRole.ADMINISTRATOR)
        self.project.add_user(self.user, ProjectRole.MANAGER)

        with mock_signal_receiver(signals.structure_role_revoked) as receiver:
            self.project.remove_user(self.user, removed_by=self.removed_by)

        calls = [
            mock.call(
                structure=self.project,
                user=self.user,
                removed_by=self.removed_by,
                role=ProjectRole.MANAGER,

                sender=Project,
                signal=signals.structure_role_revoked,
            ),

            mock.call(
                structure=self.project,
                user=self.user,
                removed_by=self.removed_by,
                role=ProjectRole.ADMINISTRATOR,

                sender=Project,
                signal=signals.structure_role_revoked,
            ),
        ]

        receiver.assert_has_calls(calls, any_order=True)

        self.assertEqual(
            receiver.call_count, 2,
            'Excepted exactly 2 signals emitted'
        )

    def test_remove_user_emits_structure_role_revoked_if_grant_existed_before(self):
        self.project.add_user(self.user, ProjectRole.MANAGER)

        with mock_signal_receiver(signals.structure_role_revoked) as receiver:
            self.project.remove_user(self.user, ProjectRole.MANAGER, self.removed_by)

        receiver.assert_called_once_with(
            structure=self.project,
            user=self.user,
            role=ProjectRole.MANAGER,

            sender=Project,
            signal=signals.structure_role_revoked,
            removed_by=self.removed_by,
        )

    def test_remove_user_doesnt_emit_structure_role_revoked_if_grant_didnt_exist_before(self):
        with mock_signal_receiver(signals.structure_role_revoked) as receiver:
            self.project.remove_user(self.user, ProjectRole.MANAGER)

        self.assertFalse(receiver.called, 'structure_role_remove should not be emitted')


class ProjectUpdateDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()

    # Update tests:
    def test_user_can_change_single_project_field(self):
        self.client.force_authenticate(self.fixture.staff)

        data = {'name': 'New project name'}
        response = self.client.patch(factories.ProjectFactory.get_url(self.fixture.project), data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('New project name', response.data['name'])
        self.assertTrue(Project.objects.filter(name=data['name']).exists())

    # Delete tests:
    def test_user_can_delete_project_belonging_to_the_customer_he_owns(self):
        self.client.force_authenticate(self.fixture.owner)

        project = self.fixture.project
        response = self.client.delete(factories.ProjectFactory.get_url(project))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Project.objects.filter(pk=project.pk).exists())


class ProjectCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()

    def test_staff_can_create_any_project(self):
        self.client.force_authenticate(self.fixture.owner)
        data = self._get_valid_project_payload(self.fixture.customer)

        response = self.client.post(factories.ProjectFactory.get_list_url(), data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Project.objects.filter(name=data['name']).exists())

    def test_owner_can_create_project_belonging_to_the_customer_he_owns(self):
        self.client.force_authenticate(self.fixture.owner)
        data = self._get_valid_project_payload(self.fixture.customer)

        response = self.client.post(factories.ProjectFactory.get_list_url(), data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Project.objects.filter(name=data['name']).exists())

    def test_owner_cannot_create_project_not_belonging_to_the_customer_he_owns(self):
        self.client.force_authenticate(self.fixture.owner)
        data = self._get_valid_project_payload(factories.CustomerFactory())
        data['name'] = 'unique name 2'

        response = self.client.post(factories.ProjectFactory.get_list_url(), data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Project.objects.filter(name=data['name']).exists())

    def test_owner_cannot_create_project_if_customer_quota_were_exceeded(self):
        self.fixture.customer.set_quota_limit('nc_project_count', 0)
        data = self._get_valid_project_payload(self.fixture.customer)
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.post(factories.ProjectFactory.get_list_url(), data)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_customer_support_cannot_create_project(self):
        self.client.force_authenticate(self.fixture.customer_support)
        data = self._get_valid_project_payload(self.fixture.customer)

        response = self.client.post(factories.ProjectFactory.get_list_url(), data)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Project.objects.filter(name=data['name']).exists())

    def test_user_can_specify_certifications(self):
        self.client.force_authenticate(self.fixture.owner)
        data = self._get_valid_project_payload(self.fixture.customer)
        certificate = factories.ServiceCertificationFactory()
        data['certifications'] = [{"url": factories.ServiceCertificationFactory.get_url(certificate)}]

        response = self.client.post(factories.ProjectFactory.get_list_url(), data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Project.objects.filter(name=data['name'], customer=self.fixture.customer).exists())
        self.assertTrue(models.ServiceCertification.objects.filter(projects__name=data['name'],
                                                                   name=certificate.name).exists())

    def _get_valid_project_payload(self, customer):
        return {
            'name': 'New project name',
            'customer': factories.CustomerFactory.get_url(customer),
        }


class ProjectApiPermissionTest(test.APITransactionTestCase):
    forbidden_combinations = (
        # User role, Project
        ('admin', 'manager'),
        ('admin', 'inaccessible'),
        ('manager', 'admin'),
        ('manager', 'inaccessible'),
        ('no_role', 'admin'),
        ('no_role', 'manager'),
        ('no_role', 'inaccessible'),
    )

    def setUp(self):
        self.users = {
            'owner': factories.UserFactory(),
            'admin': factories.UserFactory(),
            'manager': factories.UserFactory(),
            'no_role': factories.UserFactory(),
            'multirole': factories.UserFactory(),
        }

        self.projects = {
            'owner': factories.ProjectFactory(),
            'admin': factories.ProjectFactory(),
            'manager': factories.ProjectFactory(),
            'inaccessible': factories.ProjectFactory(),
        }

        self.projects['admin'].add_user(self.users['admin'], ProjectRole.ADMINISTRATOR)
        self.projects['manager'].add_user(self.users['manager'], ProjectRole.MANAGER)

        self.projects['admin'].add_user(self.users['multirole'], ProjectRole.ADMINISTRATOR)
        self.projects['manager'].add_user(self.users['multirole'], ProjectRole.MANAGER)
        self.projects['owner'].customer.add_user(self.users['owner'], CustomerRole.OWNER)

    # TODO: Test for customer owners
    # Creation tests
    def test_anonymous_user_cannot_create_project(self):
        for old_project in self.projects.values():
            project = factories.ProjectFactory(customer=old_project.customer)
            response = self.client.post(reverse('project-list'), self._get_valid_payload(project))
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_cannot_create_project_within_customer_he_doesnt_own_but_admins_its_project(self):
        self.client.force_authenticate(user=self.users['admin'])

        customer = self.projects['admin'].customer

        project = factories.ProjectFactory(customer=customer)
        response = self.client.post(reverse('project-list'), self._get_valid_payload(project))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertDictContainsSubset(
            {'detail': 'You do not have permission to perform this action.'}, response.data)

    def test_user_cannot_create_project_within_customer_he_doesnt_own_but_manages_its_project(self):
        self.client.force_authenticate(user=self.users['manager'])

        customer = self.projects['manager'].customer

        project = factories.ProjectFactory(customer=customer)
        response = self.client.post(reverse('project-list'), self._get_valid_payload(project))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertDictContainsSubset(
            {'detail': 'You do not have permission to perform this action.'}, response.data)

    def test_user_cannot_create_project_within_customer_he_is_not_affiliated_with(self):
        self.client.force_authenticate(user=self.users['admin'])

        project = factories.ProjectFactory()
        response = self.client.post(reverse('project-list'), self._get_valid_payload(project))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertDictContainsSubset(
            {'customer': ['Invalid hyperlink - Object does not exist.']}, response.data)

    def test_user_can_create_project_within_customer_he_owns(self):
        self.client.force_authenticate(user=self.users['owner'])

        customer = self.projects['owner'].customer

        project = factories.ProjectFactory(customer=customer)
        response = self.client.post(reverse('project-list'), self._get_valid_payload(project))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_staff_user_can_create_project(self):
        staff = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        customer = self.projects['inaccessible'].customer

        project = factories.ProjectFactory(customer=customer)
        response = self.client.post(reverse('project-list'), self._get_valid_payload(project))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    # List filtration tests
    def test_anonymous_user_cannot_list_projects(self):
        response = self.client.get(reverse('project-list'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_can_list_projects_belonging_to_customer_he_owns(self):
        self._ensure_list_access_allowed('owner')

    def test_user_can_list_projects_he_is_administrator_of(self):
        self._ensure_list_access_allowed('admin')

    def test_user_can_list_projects_he_is_manager_of(self):
        self._ensure_list_access_allowed('manager')

    def test_user_cannot_list_projects_he_has_no_role_in(self):
        for user_role, project in self.forbidden_combinations:
            self._ensure_list_access_forbidden(user_role, project)

    def test_user_can_filter_by_projects_where_he_has_manager_role(self):
        self.client.force_authenticate(user=self.users['multirole'])
        response = self.client.get(reverse('project-list') + '?can_manage')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        managed_project_url = self._get_project_url(self.projects['manager'])
        administrated_project_url = self._get_project_url(self.projects['admin'])

        self.assertIn(managed_project_url, [resource['url'] for resource in response.data])
        self.assertNotIn(administrated_project_url, [resource['url'] for resource in response.data])

    # Direct instance access tests
    def test_anonymous_user_cannot_access_project(self):
        project = factories.ProjectFactory()
        response = self.client.get(self._get_project_url(project))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_can_access_project_belonging_to_customer_he_owns(self):
        self._ensure_direct_access_allowed('owner')

    def test_user_can_access_project_he_is_administrator_of(self):
        self._ensure_direct_access_allowed('admin')

    def test_user_can_access_project_he_is_manager_of(self):
        self._ensure_direct_access_allowed('manager')

    def test_user_cannot_access_project_he_has_no_role_in(self):
        for user_role, project in self.forbidden_combinations:
            self._ensure_direct_access_forbidden(user_role, project)

    # Helper methods
    def _get_project_url(self, project):
        return factories.ProjectFactory.get_url(project)

    def _get_valid_payload(self, resource=None):
        resource = resource or factories.ProjectFactory()
        return {
            'name': resource.name,
            'customer': factories.CustomerFactory.get_url(resource.customer),
        }

    def _ensure_list_access_allowed(self, user_role):
        self.client.force_authenticate(user=self.users[user_role])

        response = self.client.get(reverse('project-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        project_url = self._get_project_url(self.projects[user_role])
        self.assertIn(project_url, [instance['url'] for instance in response.data])

    def _ensure_list_access_forbidden(self, user_role, project):
        self.client.force_authenticate(user=self.users[user_role])

        response = self.client.get(reverse('project-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        project_url = self._get_project_url(self.projects[project])
        self.assertNotIn(project_url, [resource['url'] for resource in response.data])

    def _ensure_direct_access_allowed(self, user_role):
        self.client.force_authenticate(user=self.users[user_role])
        response = self.client.get(self._get_project_url(self.projects[user_role]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def _ensure_direct_access_forbidden(self, user_role, project):
        self.client.force_authenticate(user=self.users[user_role])

        response = self.client.get(self._get_project_url(self.projects[project]))
        # 404 is used instead of 403 to hide the fact that the resource exists at all
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class ProjectUsersListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.admin = self.fixture.admin
        self.manager = self.fixture.manager
        self.project = self.fixture.project
        self.url = factories.ProjectFactory.get_url(self.project, action='users')

    @data('staff', 'owner', 'manager', 'admin')
    def test_user_can_list_project_users(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        self.assertSetEqual({user['role'] for user in response.data}, {'admin', 'manager'})
        self.assertSetEqual({user['uuid'] for user in response.data},
                            {self.admin.uuid.hex, self.manager.uuid.hex})

    def test_user_can_not_list_project_users(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ProjectCountersListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.owner = self.fixture.owner
        self.admin = self.fixture.admin
        self.manager = self.fixture.manager
        self.project = self.fixture.project
        self.service = self.fixture.service
        self.resource = self.fixture.resource
        self.url = factories.ProjectFactory.get_url(self.project, action='counters')

    def test_user_can_get_project_counters(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url, {'fields': ['users', 'apps', 'vms']})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {'users': 2, 'apps': 0, 'vms': 1})

    def test_additional_counters_could_be_registered(self):
        views.ProjectCountersView.register_counter('test', lambda project: 100)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url, {'fields': ['test']})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {'test': 100})


@ddt
class ProjectCertificationUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.associated_certification = factories.ServiceCertificationFactory()
        self.project.certifications.add(self.associated_certification)
        self.new_certification = factories.ServiceCertificationFactory()
        self.url = factories.ProjectFactory.get_url(self.project, action='update_certifications')

    @data('staff', 'owner')
    def test_user_can_update_certifications(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_payload(self.new_certification)

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(self.project.certifications.filter(pk=self.new_certification.pk).exists())
        self.assertFalse(self.project.certifications.filter(pk=self.associated_certification.pk).exists())

    @data('global_support', 'manager')
    def test_user_cannot_update_certifications_if_he_has_no_permissions(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = self._get_payload(self.new_certification)

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def _get_payload(self, *certifications):
        urls = [{"url": factories.ServiceCertificationFactory.get_url(c)} for c in certifications]
        return {
            'certifications': urls
        }


class ProjectCertificationGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()

    def test_service_certification_state_is_ok_if_project_certifications_is_a_subset_of_service_certifications(self):
        self.client.force_authenticate(self.fixture.owner)
        link = self.fixture.service_project_link
        project_certifications = [factories.ServiceCertificationFactory()]
        service_certifications = project_certifications + [factories.ServiceCertificationFactory()]
        link.service.settings.certifications.add(*service_certifications)
        link.project.certifications.add(*project_certifications)
        url = factories.ProjectFactory.get_url(link.project)

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone('validation_state', response.data['services'][0])
        self.assertEqual(response.data['services'][0]['validation_state'], "OK")

    def test_certification_state_is_erred_if_project_certifications_is_not_a_subset_of_service_certifications(self):
        self.client.force_authenticate(self.fixture.owner)
        link = self.fixture.service_project_link
        service_certification = factories.ServiceCertificationFactory()
        project_certification = factories.ServiceCertificationFactory()
        link.service.settings.certifications.add(service_certification)
        link.project.certifications.add(project_certification)
        url = factories.ProjectFactory.get_url(link.project)

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone('validation_state', response.data['services'][0])
        self.assertEqual(response.data['services'][0]['validation_state'], "ERRED")

    def test_missing_certification_name_is_in_error_message(self):
        self.client.force_authenticate(self.fixture.owner)
        link = self.fixture.service_project_link
        service_certification = factories.ServiceCertificationFactory()
        project_certification = factories.ServiceCertificationFactory()
        link.service.settings.certifications.add(service_certification)
        link.project.certifications.add(project_certification)
        url = factories.ProjectFactory.get_url(link.project)

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone('validation_state', response.data['services'][0])
        self.assertEqual(response.data['services'][0]['validation_state'], "ERRED")
        self.assertIn(project_certification.name, response.data['services'][0]['validation_message'])


@ddt
class ProjectQuotasTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.quota = self.fixture.project.quotas.get(name=Project.Quotas.nc_app_count)

    def update_quota(self):
        url = quota_factories.QuotaFactory.get_url(self.quota)
        return self.client.put(url, {
            'limit': 100
        })

    @data('staff', 'owner')
    def test_authorized_user_can_edit_project_quota(self, user):
        self.client.force_login(getattr(self.fixture, user))

        response = self.update_quota()
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.quota.refresh_from_db()
        self.assertEqual(self.quota.limit, 100)

    @data('admin', 'manager')
    def test_non_authorized_user_can_not_edit_project_quota(self, user):
        self.client.force_login(getattr(self.fixture, user))

        response = self.update_quota()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.quota.refresh_from_db()
        self.assertNotEqual(self.quota.limit, 100)


class TestExecutor(executors.BaseCleanupExecutor):
    pre_models = (test_models.TestNewInstance,)


@mock.patch('waldur_core.core.WaldurExtension.get_extensions')
class ProjectCleanupTest(test.APITransactionTestCase):

    def test_executors_are_sorted_in_topological_order(self, get_extensions):

        class ParentExecutor(executors.BaseCleanupExecutor):
            pass

        class ParentExtension(object):
            @staticmethod
            def get_cleanup_executor():
                return ParentExecutor

        class ChildExecutor(executors.BaseCleanupExecutor):
            related_executor = ParentExecutor

        class ChildExtension(object):
            @staticmethod
            def get_cleanup_executor():
                return ChildExecutor

        get_extensions.return_value = [ParentExtension, ChildExtension]

        self.assertEqual([ChildExecutor, ParentExecutor],
                         executors.ProjectCleanupExecutor.get_executors())

    def test_project_without_resources_is_deleted(self, get_extensions):
        fixture = fixtures.ServiceFixture()
        project = fixture.project

        get_extensions.return_value = []
        executors.ProjectCleanupExecutor.execute(fixture.project, async=False)

        self.assertFalse(models.Project.objects.filter(id=project.id).exists())

    def test_project_with_resources_without_executors_is_not_deleted(self, get_extensions):
        fixture = fixtures.ServiceFixture()
        project = fixture.project
        resource = fixture.resource

        get_extensions.return_value = []
        executors.ProjectCleanupExecutor.execute(fixture.project, async=False)

        self.assertTrue(models.Project.objects.filter(id=project.id).exists())
        self.assertTrue(test_models.TestNewInstance.objects.filter(id=resource.id).exists())

    def test_project_with_resources_and_executors_is_deleted(self, get_extensions):
        fixture = fixtures.ServiceFixture()
        project = fixture.project
        resource = fixture.resource

        class TestExtension(object):
            @staticmethod
            def get_cleanup_executor():
                return TestExecutor

        get_extensions.return_value = [TestExtension]
        executors.ProjectCleanupExecutor.execute(fixture.project, async=False)

        self.assertFalse(models.Project.objects.filter(id=project.id).exists())
        self.assertFalse(test_models.TestNewInstance.objects.filter(id=resource.id).exists())
