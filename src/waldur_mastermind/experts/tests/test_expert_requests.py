import copy

import mock
from django.conf import settings
from django.test import override_settings
from rest_framework import test, status

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_core.users import models as user_models
from waldur_core.users.tests import factories as user_factories
from waldur_mastermind.support.tests import factories as support_factories

from . import factories, fixtures
from .. import models


def override_experts_contract(contract=None):
    default_contract = {
        'offerings': {
            'custom_vpc_experts': {
                'label': 'Custom VPC',
                'order': ['storage', 'ram', 'cpu_count'],
                'category': 'Experts',
                'description': 'Custom VPC example.',
                'summary': '<div>super long long long long long long <b>summary</b></div>',
                'price': 100,
                'recurring_billing': False,  # False if billing is project based, True if monthly occuring.
                'options': {
                    'storage': {
                        'type': 'integer',
                        'label': 'Max storage, GB',
                        'help_text': 'VPC storage limit in GB.',
                    },
                    'ram': {
                        'type': 'integer',
                        'label': 'Max RAM, GB',
                        'help_text': 'VPC RAM limit in GB.',
                    },
                    'cpu_count': {
                        'type': 'integer',
                        'label': 'Max vCPU',
                        'help_text': 'VPC CPU count limit.',
                    },
                },
            },
        },
        'order': ['objectives'],
        'options': {
            'objectives': {
                'order': ['objectives', 'price'],
                'label': 'Objectives',
                'description': 'Contract objectives.',
                'options': {
                    'objectives': {
                        'type': 'text',
                        'label': 'Objectives',
                        'default': 'This is an objective.',
                    },
                }
            },
        }
    }

    if contract is None:
        contract = default_contract

    if 'offerings' not in contract:
        contract['offerings'] = default_contract['offerings']

    experts_settings = copy.deepcopy(settings.WALDUR_EXPERTS)
    experts_settings.update(CONTRACT=contract)
    return override_settings(WALDUR_EXPERTS=experts_settings)


@override_experts_contract()
class ExpertRequestCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.project_fixture = structure_fixtures.ProjectFixture()
        self.project_manager = self.project_fixture.manager
        self.customer_owner = self.project_fixture.owner
        self.project = self.project_fixture.project

        self.expert_fixture = structure_fixtures.ProjectFixture()
        self.expert_provider = factories.ExpertProviderFactory(customer=self.expert_fixture.customer)
        self.expert_manager = self.expert_fixture.owner

        self.backend_patcher = mock.patch('waldur_mastermind.support.backend.get_active_backend')
        self.backend_patcher.start()

    def tearDown(self):
        mock.patch.stopall()

    def test_expert_requests_are_visible_to_any_expert_manager(self):
        self.client.force_authenticate(self.expert_manager)
        self.expert_request = factories.ExpertRequestFactory(type='custom_vpc_experts', project=self.project)
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

    @override_experts_contract(
        {
            'order': ['objectives', 'milestones', 'terms-and-conditions'],
            'options': {
                'objectives': {
                    'order': ['objectives'],
                    'label': 'Objectives',
                    'description': 'Contract objectives.',
                    'options': {
                        'objectives': {
                            'type': 'string',
                            'label': 'Objectives',
                            'required': True,
                            'default': 'This is an objective.',
                        }
                    }
                }
            }
        })
    def test_expert_request_cannot_be_created_if_it_has_a_missing_required_contract_field(self):
        self.client.force_authenticate(self.customer_owner)
        response = self.create_expert_request()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)

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
            'type': 'custom_vpc_experts',
            'name': 'Expert request for custom VPC',
            'ram': 1024,
            'storage': 10240,
        })


class ExpertRequestActionsTest(test.APITransactionTestCase):
    def setUp(self):
        self.expert_request = factories.ExpertRequestFactory()
        self.expert_request.state = models.ExpertRequest.States.ACTIVE
        self.expert_request.type = 'custom_vpc_experts'
        self.expert_request.save()

        self.expert_team = structure_factories.ProjectFactory()
        self.expert_contract = models.ExpertContract.objects.create(
            request=self.expert_request,
            team=self.expert_team,
        )

        self.staff = structure_factories.UserFactory(is_staff=True)

        self.expert_fixture = structure_fixtures.ProjectFixture()
        self.expert_provider = factories.ExpertProviderFactory(customer=self.expert_fixture.customer)
        self.expert_manager = self.expert_fixture.owner


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

    def test_pending_expert_request_can_be_cancelled(self):
        self.expert_request.state = models.ExpertRequest.States.PENDING
        self.expert_request.save()
        self.expert_contract.delete()

        self.client.force_authenticate(self.staff)
        response = self.cancel_expert_request()
        self.assertEqual(response.status_code, status.HTTP_200_OK)

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
        self.cancel_expert_request()

        # Assert
        self.assertFalse(self.expert_request.project.has_user(expert))

    def test_when_expert_request_is_cancelled_pending_invitations_are_cancelled(self):
        # Arrange
        expert = structure_factories.UserFactory()
        self.expert_team.add_user(expert, structure_models.ProjectRole.ADMINISTRATOR)
        user_factories.ProjectInvitationFactory(
            email=expert.email,
            project=self.expert_request.project,
            project_role=structure_models.ProjectRole.ADMINISTRATOR,
        )

        # Act
        self.client.force_authenticate(self.staff)
        self.cancel_expert_request()

        # Assert
        pending_invitations = user_models.Invitation.objects.filter(state=user_models.Invitation.State.PENDING)
        self.assertFalse(pending_invitations.exists())

        cancelled_invitations = user_models.Invitation.objects.filter(state=user_models.Invitation.State.CANCELED)
        self.assertEqual(cancelled_invitations.count(), 1)

    def cancel_expert_request(self):
        url = factories.ExpertRequestFactory.get_url(self.expert_request, 'cancel')
        return self.client.post(url)


@override_experts_contract()
class ExpertRequestCompleteTest(ExpertRequestActionsTest):
    def _test_count_expert_requests(self, user, count):
        self.client.force_authenticate(user)
        url = factories.ExpertRequestFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), count)

    def test_if_user_is_not_of_related_organization_and_expert_request_is_active(self):
        self._test_count_expert_requests(self.expert_manager, 1)
        self._test_count_expert_requests(self.staff, 1)

    def test_if_user_is_not_of_related_organization_and_expert_request_is_completed(self):
        self.expert_request.state = models.ExpertRequest.States.COMPLETED
        self.expert_request.save()
        self._test_count_expert_requests(self.expert_manager, 0)
        self._test_count_expert_requests(self.staff, 1)

    def test_if_user_is_of_related_organization_and_expert_request_is_completed(self):
        self.expert_request.state = models.ExpertRequest.States.COMPLETED
        self.expert_request.save()
        self.expert_team.permissions.create(user=self.expert_manager, is_active=True)
        self._test_count_expert_requests(self.expert_manager, 1)
        self._test_count_expert_requests(self.staff, 1)

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


class ExpertRequestProjectCacheTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.expert_request = factories.ExpertRequestFactory(project=self.fixture.project)

    def test_cache_is_populated_when_request_is_created(self):
        self.assertEqual(self.expert_request.project_name, self.expert_request.project.name)
        self.assertEqual(self.expert_request.project_uuid, self.expert_request.project.uuid.hex)
        self.assertEqual(self.expert_request.customer, self.expert_request.project.customer)

    def test_cache_is_updated_when_project_is_renamed(self):
        self.expert_request.project.name = 'NEW PROJECT NAME'
        self.expert_request.project.save(update_fields=['name'])

        self.expert_request.refresh_from_db()
        self.assertEqual(self.expert_request.project_name, self.expert_request.project.name)

    def test_request_is_not_removed_when_project_is_removed(self):
        self.expert_request.project.delete()
        self.assertTrue(models.ExpertRequest.objects.filter(id=self.expert_request.id))


class ExpertRequestUsersTest(test.APITransactionTestCase):
    def setUp(self):
        self.expert_fixture = fixtures.ExpertsFixture()
        self.expert_request = self.expert_fixture.expert_request

    def test_empty(self):
        url = factories.ExpertRequestFactory.get_url(self.expert_request, 'users')
        self.client.force_login(self.expert_fixture.staff)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {})

    def test_staff(self):
        self.assert_has_role(self.expert_fixture.staff, 'staff')

    def test_support(self):
        self.assert_has_role(self.expert_fixture.global_support, 'support')

    def test_owner(self):
        self.assert_has_role(self.expert_fixture.owner, 'owner')

    def test_expert(self):
        fixture = fixtures.ExpertsFixture()
        factories.ExpertBidFactory(request=self.expert_request, team=fixture.project)

        expert = fixture.manager
        support_factories.CommentFactory(issue=self.expert_request.issue, author__user=expert)

        self.assert_has_role(expert, 'expert')

    def assert_has_role(self, user, role):
        support_factories.CommentFactory(issue=self.expert_request.issue,
                                         author__user=user)

        url = factories.ExpertRequestFactory.get_url(self.expert_request, 'users')
        self.client.force_login(self.expert_fixture.staff)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(role in response.data[user.uuid.hex])
