from __future__ import unicode_literals

from django.conf import settings
from django.core.urlresolvers import reverse
from rest_framework import status

from nodeconductor.structure.tests import factories as structure_factories

from . import base
from .. import models


class BaseOfferingTest(base.BaseTest):
    def setUp(self):
        super(BaseOfferingTest, self).setUp()

        self.client.force_authenticate(self.fixture.staff)
        settings.WALDUR_SUPPORT['OFFERING'] = {
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


class OfferingGetTest(BaseOfferingTest):
    def test_offering_view_returns_configured_offerings(self):
        url = reverse('offering-list')
        response = self.client.get(url)
        available_offerings = response.data
        self.assertDictEqual(available_offerings, settings.WALDUR_SUPPORT['OFFERING'])


class OfferingCreateTest(BaseOfferingTest):
    def setUp(self):
        super(OfferingCreateTest, self).setUp()
        self.url = reverse('offering-detail', kwargs={'name': 'custom_vpc'})

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
        default_value = settings.WALDUR_SUPPORT['OFFERING']['custom_vpc']['options']['cpu_count']['default']
        request_data = self._get_valid_request()
        del request_data['cpu_count']

        response = self.client.post(self.url, data=request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.Issue.objects.count(), 1)
        self.assertIn(str(default_value), models.Issue.objects.first().description)

    def _get_valid_request(self):
        return {
            'name': 'Do not reboot it, just patch',
            'description': 'We got Linux, and there\'s no doubt. Gonna fix',
            'storage': 20,
            'ram': 4,
            'cpu_count': 2,
            'project': structure_factories.ProjectFactory.get_url(self.fixture.project)
        }
