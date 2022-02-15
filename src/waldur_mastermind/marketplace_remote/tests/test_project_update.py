import mock
from django.test import override_settings
from django.urls import reverse
from rest_framework import status, test

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
        super(ProjectUpdateRequestCreateTest, self).tearDown()
        mock.patch.stopall()

    def test_when_project_is_updated_request_is_created_for_each_offering(self):
        old_name = self.project.name
        self.client.force_authenticate(self.fixture.owner)
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
        self.client.force_authenticate(self.fixture.owner)
        self.client.patch(
            ProjectFactory.get_url(self.project), {'name': 'First project'}
        )

        self.client.force_authenticate(self.fixture.admin)
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
        self.client.force_authenticate(self.fixture.owner)
        self.client.patch(
            ProjectFactory.get_url(self.project), {'name': 'First project'}
        )
        request = ProjectUpdateRequest.objects.get(
            project=self.project, offering=self.offering
        )
        self.client.force_authenticate(self.fixture.offering_owner)
        base_url = reverse(
            "marketplace-project-update-request-detail",
            kwargs={'uuid': request.uuid.hex},
        )
        response = self.client.post(f'{base_url}approve/')

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.client_mock().update_project.assert_called_once()

    def test_when_request_is_rejected_change_is_not_applied_remotely(self):
        # Arrange
        self.client.force_authenticate(self.fixture.owner)
        self.client.patch(
            ProjectFactory.get_url(self.project), {'name': 'First project'}
        )
        request = ProjectUpdateRequest.objects.get(
            project=self.project, offering=self.offering
        )

        # Act
        self.client.force_authenticate(self.fixture.offering_owner)
        base_url = reverse(
            "marketplace-project-update-request-detail",
            kwargs={'uuid': request.uuid.hex},
        )
        response = self.client.post(f'{base_url}reject/')

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(0, self.client_mock().update_project.call_count)
