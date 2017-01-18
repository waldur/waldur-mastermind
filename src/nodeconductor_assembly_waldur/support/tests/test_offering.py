from __future__ import unicode_literals

from django.core.urlresolvers import reverse
from django.test import override_settings
from rest_framework import status, test

from . import fixtures
from .. import models


class BaseOfferingTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SupportFixture()
        self.client.force_authenticate(self.fixture.staff)


class OfferingGetTest(BaseOfferingTest):
    settings = {
        'ISSUE': {
            'types': ['Informational', 'Service Request', 'Change Request', 'Incident'],
        },
        'OFFERING': {
            'transformation': {
                'summary': {},
                'description': {},
                'type': {
                    'default': 'Service Request',
                    'help_text': 'SOS',
                },
                'status': {
                    'type': 'integer',
                }
            },
            'devops': {
                'summary': {
                    'default': 'Configuration',
                },
            },
        }
    }

    @override_settings(WALDUR_SUPPORT=settings)
    def test_offering_view_returns_configured_offerings(self):
        url = reverse('offering-list')

        response = self.client.get(url)
        available_offerings = response.data
        self.assertIsNotNone(available_offerings['transformation'])
        self.assertIsNotNone(available_offerings['transformation']['summary'])
        self.assertIsNotNone(available_offerings['transformation']['description'])
        self.assertIsNotNone(available_offerings['transformation']['type'])
        self.assertIsNotNone(available_offerings['transformation']['type']['help_text'])
        self.assertIsNotNone(available_offerings['transformation']['type']['default'])
        self.assertIsNotNone(available_offerings['transformation']['status'])
        self.assertIsNotNone(available_offerings['transformation']['status']['type'])
        self.assertIsNotNone(available_offerings['devops'])
        self.assertIsNotNone(available_offerings['devops']['summary'])
        self.assertIsNotNone(available_offerings['devops']['summary']['default'])


class OfferingCreateTest(BaseOfferingTest):
    settings = {
        'ISSUE': {
            'types': ['Informational', 'Service Request', 'Change Request', 'Incident'],
        },
        'OFFERING': {
            'transformation': {
                'summary': {},
                'description': {},
                'type': {
                    'default': 'Service Request',
                    'help_text': 'SOS',
                },
                'status': {
                    'type': 'integer',
                }
            },
        }
    }

    def setUp(self):
        super(OfferingCreateTest, self).setUp()
        self.url = reverse('offering-detail', kwargs={'name': 'transformation'})

    @override_settings(WALDUR_SUPPORT=settings)
    def test_offering_create_creates_issue_with_valid_request(self):
        request_data = self._get_valid_request()

        response = self.client.post(self.url, data=request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.Issue.objects.count(), 1)

    def _get_valid_request(self):
        return {
            'summary': 'Do not reboot it, just patch',
            'description': 'We got Linux, and there\'s no doubt. Gonna fix',
            #'type': 'Service Desk Request',
            'status': 4
        }
