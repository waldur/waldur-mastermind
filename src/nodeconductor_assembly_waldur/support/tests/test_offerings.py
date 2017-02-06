from __future__ import unicode_literals

from django.conf import settings
from rest_framework import status

from nodeconductor.structure.tests import factories as structure_factories

from . import base, factories
from .. import models


class BaseOfferingTest(base.BaseTest):
    def setUp(self, **kwargs):
        super(BaseOfferingTest, self).setUp()
        settings.WALDUR_SUPPORT['OFFERINGS'] = {
            'custom_vpc': {
                'label': 'Custom VPC',
                'order': ['storage', 'ram', 'cpu_count'],
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
                        'default': 93,
                        'type': 'integer',
                        'label': 'Max vCPU',
                        'help_text': 'VPC CPU count limit.',
                    },
                },
            },
        }


class OfferingCompleteTest(BaseOfferingTest):

    def test_offering_is_in_ok_state_when_complete_is_called(self):
        offering = factories.OfferingFactory()
        self.assertEqual(offering.state, models.Offering.States.REQUESTED)
        expected_price = 10

        url = factories.OfferingFactory.get_url(offering=offering, action='complete')
        request_data = {'price': expected_price}
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(url, request_data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering.refresh_from_db()
        self.assertEqual(offering.state, models.Offering.States.OK)
        self.assertEqual(offering.price, expected_price)

    def test_offering_cannot_be_completed_if_it_is_terminated(self):
        offering = factories.OfferingFactory(state=models.Offering.States.TERMINATED)
        url = factories.OfferingFactory.get_url(offering=offering, action='complete')
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_offering_cannot_be_completed_without_price(self):
        offering = factories.OfferingFactory()
        url = factories.OfferingFactory.get_url(offering=offering, action='complete')
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        offering.refresh_from_db()
        self.assertEqual(offering.state, models.Offering.States.REQUESTED)

    def test_user_cannot_complete_offering(self):
        offering = self.fixture.offering
        url = factories.OfferingFactory.get_url(offering=offering, action='complete')
        request_data = {'price': 10}
        self.client.force_authenticate(self.fixture.user)
        response = self.client.post(url, request_data)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        offering.refresh_from_db()
        self.assertEqual(offering.state, models.Offering.States.REQUESTED)

    def test_staff_can_complete_offering(self):
        offering = self.fixture.offering
        url = factories.OfferingFactory.get_url(offering=offering, action='complete')
        request_data = {'price': 10}
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(url, request_data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering.refresh_from_db()
        self.assertEqual(offering.state, models.Offering.States.OK)


class OfferingTerminateTest(BaseOfferingTest):

    def test_offering_is_in_terminated_state_when_terminate_is_called(self):
        offering = factories.OfferingFactory()
        self.assertEqual(offering.state, models.Offering.States.REQUESTED)

        url = factories.OfferingFactory.get_url(offering=offering, action='terminate')
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering.refresh_from_db()
        self.assertEqual(offering.state, models.Offering.States.TERMINATED)


class OfferingGetTest(BaseOfferingTest):

    def test_user_can_see_list_of_offerings(self):
        url = factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class OfferingGetConfiguredTest(BaseOfferingTest):

    def test_offering_view_returns_configured_offerings(self):
        self.client.force_authenticate(self.fixture.user)
        url = factories.OfferingFactory.get_list_action_url(action='configured')
        response = self.client.get(url)
        available_offerings = response.data
        self.assertDictEqual(available_offerings, settings.WALDUR_SUPPORT['OFFERINGS'])


class OfferingCreateTest(BaseOfferingTest):
    def setUp(self):
        super(OfferingCreateTest, self).setUp()
        self.url = factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(self.fixture.staff)

    def test_offering_create_raises_error_if_type_is_not_provided(self):
        request_data = self._get_valid_request()
        del request_data['type']
        response = self.client.post(self.url, data=request_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('type', response.data)

    def test_offering_create_raises_error_if_data_is_not_provided(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('type', response.data)

    def test_offering_create_creates_issue_with_valid_request(self):
        request_data = self._get_valid_request()

        response = self.client.post(self.url, data=request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.Issue.objects.count(), 1)

    def test_offering_create_creates_issue_with_custom_description(self):
        expected_description = 'This is a description'
        request_data = self._get_valid_request()
        request_data['description'] = expected_description

        response = self.client.post(self.url, data=request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.Issue.objects.count(), 1)
        self.assertIn(expected_description, models.Issue.objects.first().description)

    def test_offering_create_fills_type_of_the_offering(self):
        expected_type = 'custom_vpc'
        request_data = self._get_valid_request()

        response = self.client.post(self.url, data=request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.Offering.objects.count(), 1)
        offering = models.Offering.objects.first()
        self.assertIn(offering.type, 'custom_vpc')
        self.assertIn(offering.type_label, settings.WALDUR_SUPPORT['OFFERINGS'][expected_type]['label'])

    def test_offering_create_associates_hyperlinked_fields_with_issue(self):
        request_data = self._get_valid_request()

        response = self.client.post(self.url, data=request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        issue = models.Issue.objects.first()
        self.assertIsNotNone(issue.customer)
        self.assertEqual(issue.customer.uuid, self.fixture.issue.project.customer.uuid)
        self.assertIsNotNone(issue.project)
        self.assertEqual(issue.project.uuid, self.fixture.issue.project.uuid)

    def test_offering_create_sets_default_value_if_it_was_not_provided(self):
        default_value = settings.WALDUR_SUPPORT['OFFERINGS']['custom_vpc']['options']['cpu_count']['default']
        request_data = self._get_valid_request()
        del request_data['cpu_count']

        response = self.client.post(self.url, data=request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.Issue.objects.count(), 1)
        self.assertIn(str(default_value), models.Issue.objects.first().description)

    def _get_valid_request(self):
        return {
            'type': 'custom_vpc',
            'name': 'Do not reboot it, just patch',
            'description': 'We got Linux, and there\'s no doubt. Gonna fix',
            'storage': 20,
            'ram': 4,
            'cpu_count': 2,
            'project': structure_factories.ProjectFactory.get_url(self.fixture.project)
        }


class OfferingUpdateTest(BaseOfferingTest):
    def setUp(self):
        super(OfferingUpdateTest, self).setUp()
        self.client.force_authenticate(self.fixture.staff)

    def test_it_is_possible_to_update_offering_name(self):
        offering = self.fixture.offering
        expected_name = 'New name'
        url = factories.OfferingFactory.get_url(offering)

        response = self.client.put(url, {'name': expected_name})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        offering.refresh_from_db()
        self.assertEqual(offering.name, expected_name)

    def test_offering_description_cannot_be_updated(self):
        offering = self.fixture.offering
        issue = models.Issue.objects.first()
        expected_description = 'Old description'
        issue.description = expected_description
        issue.save()
        url = factories.OfferingFactory.get_url(offering)

        response = self.client.put(url, {'name': 'New name', 'description': expected_description})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        issue.refresh_from_db()
        self.assertEqual(issue.description, expected_description)
