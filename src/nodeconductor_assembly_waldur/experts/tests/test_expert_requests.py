import mock
from rest_framework import test, status

from nodeconductor.structure import models as structure_models
from nodeconductor.structure.tests import factories as structure_factories
from nodeconductor.structure.tests import fixtures as structure_fixtures
from nodeconductor_assembly_waldur.support.tests.base import override_offerings

from .. import models
from . import factories


@override_offerings()
class ExpertRequestCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.project_fixture = structure_fixtures.ProjectFixture()
        self.project_manager = self.project_fixture.manager
        self.customer_owner = self.project_fixture.owner
        self.project = self.project_fixture.project

        self.expert_fixture = structure_fixtures.ProjectFixture()
        self.expert_provider = factories.ExpertProviderFactory(customer=self.expert_fixture.customer)
        self.expert_manager = self.expert_fixture.owner

        self.backend_patcher = mock.patch('nodeconductor_assembly_waldur.support.backend.get_active_backend')
        self.backend_patcher.start()

    def tearDown(self):
        mock.patch.stopall()

    def test_expert_requests_are_visible_to_any_expert_manager(self):
        self.client.force_authenticate(self.expert_manager)
        self.expert_request = factories.ExpertRequestFactory(project=self.project)
        url = factories.ExpertRequestFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(1, len(response.data))

    def test_expert_requests_are_not_visible_to_project_manager(self):
        self.client.force_authenticate(self.project_manager)
        self.expert_request = factories.ExpertRequestFactory(project=self.project)
        url = factories.ExpertRequestFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(0, len(response.data))

    def test_expert_request_could_be_created_by_customer_owner(self):
        self.client.force_authenticate(self.customer_owner)
        response = self.create_expert_request()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(models.ExpertRequest.objects.filter(project=self.project).exists())

    def test_expert_request_could_not_be_created_by_project_manager(self):
        self.client.force_authenticate(self.project_manager)
        response = self.create_expert_request()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.ExpertRequest.objects.filter(project=self.project).exists())

    def test_expert_request_could_be_created_for_project_without_active_request(self):
        self.expert_request = factories.ExpertRequestFactory(project=self.project)
        self.client.force_authenticate(self.customer_owner)
        response = self.create_expert_request()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.ExpertRequest.objects.filter(project=self.project).exists())

    def test_expert_request_could_not_be_created_for_project_with_active_request(self):
        self.expert_request = factories.ExpertRequestFactory(
            project=self.project, state=models.ExpertRequest.States.ACTIVE)
        self.client.force_authenticate(self.customer_owner)
        response = self.create_expert_request()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(models.ExpertRequest.objects.filter(project=self.project).exists())

    def test_when_expert_request_is_created_event_is_emitted(self):
        with mock.patch('logging.LoggerAdapter.info') as mocked_info:
            expert_request = factories.ExpertRequestFactory()
            template = 'User {user_username} with full name {user_full_name} has created ' \
                       'request for experts under {customer_name} / {project_name}.'
            context = {
                'user_username': expert_request.user.username,
                'user_full_name': expert_request.user.full_name,
                'customer_name': expert_request.project.customer.name,
                'project_name': expert_request.project.name,
            }
            expected_message = template.format(**context)
            actual_message = mocked_info.call_args_list[-1][0][0]
            self.assertEqual(expected_message, actual_message)

    def create_expert_request(self):
        url = factories.ExpertRequestFactory.get_list_url()
        return self.client.post(url, {
            'project': structure_factories.ProjectFactory.get_url(self.project),
            'type': 'custom_vpc',
            'name': 'Expert request for custom VPC',
            'ram': 1024,
            'storage': 10240,
        })


class ExpertRequestActionsTest(test.APITransactionTestCase):
    def setUp(self):
        self.expert_request = factories.ExpertRequestFactory()
        self.expert_request.state = models.ExpertRequest.States.ACTIVE
        self.expert_request.save()

        self.expert_team = structure_factories.ProjectFactory()
        self.expert_contract = models.ExpertContract.objects.create(
            request=self.expert_request,
            team=self.expert_team,
        )

        self.staff = structure_factories.UserFactory(is_staff=True)


class ExpertRequestCancelTest(ExpertRequestActionsTest):

    def test_expert_request_can_be_cancelled_by_customer_owner(self):
        owner = structure_factories.UserFactory()
        customer = self.expert_request.project.customer
        customer.add_user(owner, structure_models.CustomerRole.OWNER)

        self.client.force_authenticate(owner)
        response = self.cancel_expert_request()

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_expert_request_can_not_be_cancelled_by_expert_manager(self):
        expert_manager = structure_factories.UserFactory()
        customer = structure_factories.CustomerFactory()
        customer.add_user(expert_manager, structure_models.CustomerRole.OWNER)
        factories.ExpertProviderFactory(customer=customer)

        self.client.force_authenticate(expert_manager)
        response = self.cancel_expert_request()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_when_expert_request_is_cancelled_its_state_is_changed(self):
        self.client.force_authenticate(self.staff)

        self.cancel_expert_request()
        self.expert_request.refresh_from_db()
        self.assertEqual(self.expert_request.state, models.ExpertRequest.States.CANCELLED)

    def test_when_expert_request_is_cancelled_event_is_emitted(self):
        self.client.force_authenticate(self.staff)
        with mock.patch('logging.LoggerAdapter.info') as mocked_info:
            self.cancel_expert_request()
            template = 'Expert request {expert_request_name} has been cancelled.'
            context = {
                'expert_request_name': self.expert_request.name,
            }
            expected_message = template.format(**context)
            actual_message = mocked_info.call_args_list[-1][0][0]
            self.assertEqual(expected_message, actual_message)

    def test_when_expert_request_is_cancelled_team_roles_are_revoked(self):
        # Arrange
        expert = structure_factories.UserFactory()
        self.expert_team.add_user(expert, structure_models.ProjectRole.ADMINISTRATOR)
        self.expert_request.project.add_user(expert, structure_models.ProjectRole.ADMINISTRATOR)

        # Act
        self.client.force_authenticate(self.staff)
        response = self.cancel_expert_request()

        # Assert
        self.assertFalse(self.expert_request.project.has_user(expert))

    def cancel_expert_request(self):
        url = factories.ExpertRequestFactory.get_url(self.expert_request, 'cancel')
        return self.client.post(url)


class ExpertRequestCompleteTest(ExpertRequestActionsTest):

    def test_expert_request_can_be_completed_by_staff(self):
        self.client.force_authenticate(self.staff)
        response = self.complete_expert_request()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.expert_request.refresh_from_db()
        self.assertEqual(self.expert_request.state, models.ExpertRequest.States.COMPLETED)

    def test_when_expert_request_is_completed_event_is_emitted(self):
        self.client.force_authenticate(self.staff)
        with mock.patch('logging.LoggerAdapter.info') as mocked_info:
            self.complete_expert_request()
            template = 'Expert request {expert_request_name} has been completed.'
            context = {
                'expert_request_name': self.expert_request.name,
            }
            expected_message = template.format(**context)
            actual_message = mocked_info.call_args_list[-1][0][0]
            self.assertEqual(expected_message, actual_message)

    def complete_expert_request(self):
        url = factories.ExpertRequestFactory.get_url(self.expert_request, 'complete')
        return self.client.post(url)
