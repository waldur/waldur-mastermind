from __future__ import unicode_literals

from django.conf import settings
from django.core.urlresolvers import reverse
from django.test import override_settings
from rest_framework import status, test

from nodeconductor.structure.tests import factories as structure_factories

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
                'order': ['summary', 'description', 'type', 'status', 'customer', 'project'],
                'options': {
                    'type': {
                        'default': 'Service Request',
                        'help_text': 'SOS',
                    },
                    'status': {
                        'type': 'integer',
                    },
                },
                'devops': {
                    'order': ['summary'],
                    'options': {
                        'summary': {
                            'default': 'Service Request',
                        },
                    }
                },
            },
        }
    }

    @override_settings(WALDUR_SUPPORT=settings)
    def test_offering_view_returns_configured_offerings(self):
        url = reverse('offering-list')
        response = self.client.get(url)
        available_offerings = response.data
        self.assertDictEqual(available_offerings, settings.WALDUR_SUPPORT['OFFERING'])


class OfferingCreateTest(BaseOfferingTest):
    settings = {
        'ISSUE': {
            'types': ['Informational', 'Service Request', 'Change Request', 'Incident'],
        },
        'OFFERING': {
            'transformation': {
                'order': ['summary', 'description', 'type', 'status'],
                'options': {
                    'type': {
                        'default': 'Service Request',
                        'help_text': 'SOS',
                    },
                    'status': {
                        'type': 'integer',
                    },
                },
            },
        }
    }

    settings_with_hyperlinked = {
        'ISSUE': {
            'types': ['Informational', 'Service Request', 'Change Request', 'Incident'],
        },
        'OFFERING': {
            'transformation': {
                'order': ['summary', 'description', 'type', 'status', 'customer', 'project'],
                'options': {
                    'type': {
                        'default': 'Service Request',
                        'help_text': 'SOS',
                    },
                    'status': {
                        'type': 'integer',
                    },
                    'customer': {
                        'type': 'hyperlinked',
                    },
                    'project': {
                        'type': 'hyperlinked',
                    },
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

    @override_settings(WALDUR_SUPPORT=settings_with_hyperlinked)
    def test_offering_create_associates_hyperlinked_fields_with_issue(self):
        request_data = self._get_valid_request()
        customer_url = structure_factories.CustomerFactory.get_url(self.fixture.issue.customer)
        request_data['customer'] = customer_url
        project_url = structure_factories.ProjectFactory.get_url(self.fixture.issue.project)
        request_data['project'] = project_url

        response = self.client.post(self.url, data=request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        issue = models.Issue.objects.first()
        self.assertIsNotNone(issue.customer)
        self.assertEqual(issue.customer.uuid, self.fixture.issue.customer.uuid)
        self.assertIsNotNone(issue.project)
        self.assertEqual(issue.project.uuid, self.fixture.issue.project.uuid)

    @override_settings(WALDUR_SUPPORT=settings)
    def test_offering_create_sets_default_value_if_it_was_not_provided(self):
        default_value = settings.WALDUR_SUPPORT['OFFERING']['transformation']['options']['type']['default']
        request_data = self._get_valid_request()
        del request_data['type']

        response = self.client.post(self.url, data=request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.Issue.objects.count(), 1)
        self.assertEqual(models.Issue.objects.first().type, default_value)

    def _get_valid_request(self):
        return {
            'summary': 'Do not reboot it, just patch',
            'description': 'We got Linux, and there\'s no doubt. Gonna fix',
            'type': 'Service Desk Request',
            'status': 4
        }
