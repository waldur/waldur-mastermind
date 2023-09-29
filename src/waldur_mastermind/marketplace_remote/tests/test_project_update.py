from datetime import date
from unittest import mock

from django.test import override_settings
from django.urls import reverse
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core import middleware
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests.factories import ProjectFactory
from waldur_mastermind.marketplace.models import Resource
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture
from waldur_mastermind.marketplace_remote.models import ProjectUpdateRequest

from .. import PLUGIN_NAME


@override_settings(
    WALDUR_AUTH_SOCIAL={'ENABLE_EDUTEAMS_SYNC': True},
    task_always_eager=True,
)
class ProjectUpdateRequestCreateTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        fixture = MarketplaceFixture()
        fixture.resource.offering.type = PLUGIN_NAME
        fixture.resource.offering.save()

        fixture.resource.state = Resource.States.OK
        fixture.resource.save()

        self.project = fixture.project
        self.offering = fixture.offering
        self.fixture = fixture

        self.patcher = mock.patch(
            "waldur_mastermind.marketplace_remote.utils.WaldurClient"
        )
        self.client_mock = self.patcher.start()

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def test_when_project_is_updated_request_is_created_for_each_offering(self):
        old_name = self.project.name
        self.client.force_login(self.fixture.owner)
        self.client.patch(
            ProjectFactory.get_url(self.project), {'name': 'New project name'}
        )

        request = ProjectUpdateRequest.objects.filter(
            project=self.project,
            offering=self.offering,
            old_name=old_name,
            new_name='New project name',
            state=ProjectUpdateRequest.States.PENDING,
        ).get()

        request_url = reverse(
            "marketplace-project-update-request-detail",
            kwargs={'uuid': request.uuid.hex},
        )

        response = self.client.get(request_url)
        self.assertTrue(response.status_code, status.HTTP_200_OK)

    def test_when_consecutive_update_is_applied_previous_request_is_cancelled(self):
        self.client.force_login(self.fixture.owner)
        self.client.patch(
            ProjectFactory.get_url(self.project), {'name': 'First project'}
        )

        self.client.force_login(self.fixture.admin)
        self.client.patch(
            ProjectFactory.get_url(self.project), {'name': 'Second project'}
        )

        self.assertTrue(
            ProjectUpdateRequest.objects.filter(
                project=self.project,
                offering=self.offering,
                new_name='First project',
                state=ProjectUpdateRequest.States.CANCELED,
            ).exists()
        )

        self.assertTrue(
            ProjectUpdateRequest.objects.filter(
                project=self.project,
                offering=self.offering,
                old_name='First project',
                new_name='Second project',
                state=ProjectUpdateRequest.States.PENDING,
            ).exists()
        )

    def test_when_request_is_approved_change_is_applied_remotely(self):
        # Arrange
        self.offering.secret_options = {
            'api_url': 'http://example.com',
            'token': 'secret',
        }
        self.offering.save()
        self.client_mock().list_projects.return_value = [{'uuid': 'valid_uuid'}]

        # Act
        self.client.force_login(self.fixture.owner)
        self.client.patch(
            ProjectFactory.get_url(self.project),
            {'name': 'First project', 'is_industry': True},
        )
        request = ProjectUpdateRequest.objects.get(
            project=self.project, offering=self.offering
        )
        self.client.force_login(self.fixture.offering_owner)
        base_url = reverse(
            "marketplace-project-update-request-detail",
            kwargs={'uuid': request.uuid.hex},
        )
        response = self.client.post(f'{base_url}approve/')

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.client_mock().update_project.assert_called_once()
        self.assertEqual(
            self.client_mock().update_project.call_args_list[0].kwargs['is_industry'],
            True,
        )

    def test_when_request_is_rejected_change_is_not_applied_remotely(self):
        # Arrange
        self.client.force_login(self.fixture.owner)
        self.client.patch(
            ProjectFactory.get_url(self.project), {'name': 'First project'}
        )
        request = ProjectUpdateRequest.objects.get(
            project=self.project, offering=self.offering
        )

        # Act
        self.client.force_login(self.fixture.offering_owner)
        base_url = reverse(
            "marketplace-project-update-request-detail",
            kwargs={'uuid': request.uuid.hex},
        )
        response = self.client.post(f'{base_url}reject/')

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(0, self.client_mock().update_project.call_count)

    def test_when_changes_made_by_same_owner_they_applied_immediately(self):
        offering_owner = self.fixture.offering_owner
        self.client.force_login(offering_owner)
        self.fixture.customer.add_user(offering_owner, CustomerRole.OWNER)

        response = self.client.patch(
            ProjectFactory.get_url(self.project), {'name': 'First project'}
        )

        self.assertEqual(200, response.status_code)
        self.project.refresh_from_db()

        self.assertEqual('First project', self.project.name)
        requests = ProjectUpdateRequest.objects.filter(
            project=self.project,
            offering=self.offering,
            state=ProjectUpdateRequest.States.APPROVED,
        )
        self.assertEqual(1, requests.count())

    @freeze_time('2023-01-01')
    def test_when_changes_made_by_staff_they_applied_immediately(self):
        staff = self.fixture.staff
        self.client.force_login(staff)

        response = self.client.patch(
            ProjectFactory.get_url(self.project), {'end_date': '2023-01-26'}
        )

        self.assertEqual(200, response.status_code, response.data)
        self.project.refresh_from_db()

        self.assertEqual(date(2023, 1, 26), self.project.end_date)
        requests = ProjectUpdateRequest.objects.filter(
            project=self.project,
            offering=self.offering,
            state=ProjectUpdateRequest.States.APPROVED,
        )
        self.assertEqual(1, requests.count())

    def test_project_data_pushing(self):
        owner = self.fixture.offering_owner

        self.offering.type = PLUGIN_NAME
        self.offering.secret_options = {'api_url': 'abc', 'token': '123'}
        self.offering.save()

        middleware.set_current_user(None)

        self.project.name = 'Correct project name'
        self.project.oecd_fos_2007_code = '1.1'
        self.project.description = "Correct description"
        self.project.end_date = date(year=2023, month=5, day=16)
        self.project.is_industry = True
        self.project.save()

        self.client.force_login(owner)

        payload = dict(
            name=self.project.customer.name + " / " + self.project.name,
            description=self.project.description,
            end_date=self.project.end_date.isoformat(),
            oecd_fos_2007_code=self.project.oecd_fos_2007_code,
            is_industry=self.project.is_industry,
        )

        url = '/api/remote-waldur-api/push_project_data/{}/'.format(
            self.offering.uuid.hex
        )

        self.client_mock().list_projects.return_value = [
            {
                'uuid': '8192843ee7e848d4b425ea135043053a',
                'name': "Incorrect project name",
                'description': "Incorrect description",
                'end_date': date(year=2023, month=5, day=10).isoformat(),
                'oecd_fos_2007_code': '1.2',
                'is_industry': False,
            }
        ]

        response = self.client.post(url)

        self.assertEqual(200, response.status_code)

        self.client_mock().update_project.assert_called_once_with(
            project_uuid='8192843ee7e848d4b425ea135043053a', **payload
        )
