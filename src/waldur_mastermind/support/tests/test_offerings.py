from __future__ import unicode_literals

from ddt import ddt, data
from decimal import Decimal
from django.conf import settings
import mock

from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.support.backend import SupportBackendError
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.support.tests.base import override_support_settings, override_offerings

from . import base, factories
from .. import models


@override_offerings()
class BaseOfferingTest(base.BaseTest):
    def _get_valid_request(self, project=None):
        if project is None:
            project = self.fixture.project

        return {
            'type': 'custom_vpc',
            'name': 'Do not reboot it, just patch',
            'description': 'We got Linux, and there\'s no doubt. Gonna fix',
            'storage': 20,
            'ram': 4,
            'cpu_count': 2,
            'project': structure_factories.ProjectFactory.get_url(project)
        }


@ddt
class OfferingRetrieveTest(BaseOfferingTest):

    def setUp(self, **kwargs):
        super(OfferingRetrieveTest, self).setUp(**kwargs)
        self.url = factories.OfferingFactory.get_list_url()

    @data('staff', 'global_support', 'owner', 'admin', 'manager')
    def test_user_can_see_list_of_offerings_if_he_has_project_level_permissions(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        offering = factories.OfferingFactory(issue__project__customer=self.fixture.customer,
                                             project=self.fixture.project)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(offering.uuid.hex, response.data[0]['uuid'])

    def test_user_cannot_see_list_of_offerings_if_he_has_no_project_level_permissions(self):
        self.fixture.offering
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


@ddt
class OfferingCreateTest(BaseOfferingTest):
    def setUp(self):
        super(OfferingCreateTest, self).setUp()
        self.url = factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(self.fixture.staff)

    def test_error_is_raised_if_type_is_not_provided(self):
        request_data = self._get_valid_request()
        del request_data['type']

        response = self.client.post(self.url, data=request_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('type', response.data)

    def test_field_required_error_is_raised_if_type_is_empty(self):
        request_data = self._get_valid_request()
        request_data['type'] = None

        response = self.client.post(self.url, data=request_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('type', response.data)
        self.assertEqual('This field is required.', response.data['type'])

    def test_error_is_raised_if_type_is_invalid(self):
        request_data = self._get_valid_request()
        request_data['type'] = 'invalid'

        response = self.client.post(self.url, data=request_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('type', response.data)

    def test_error_is_raised_if_data_is_not_provided(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('type', response.data)

    def test_issue_is_created(self):
        request_data = self._get_valid_request()

        response = self.client.post(self.url, data=request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.Issue.objects.count(), 1)

    @override_support_settings(ENABLED=False)
    def test_user_can_not_create_issue_if_support_extension_is_disabled(self):
        request_data = self._get_valid_request()
        response = self.client.post(self.url, data=request_data)
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)

    def test_issue_is_created_with_custom_description(self):
        expected_description = 'This is a description'
        request_data = self._get_valid_request()
        request_data['description'] = expected_description

        response = self.client.post(self.url, data=request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.Issue.objects.count(), 1)
        self.assertIn(expected_description, models.Issue.objects.first().description)

    def test_offering_type_is_filled(self):
        expected_type = 'custom_vpc'
        request_data = self._get_valid_request()

        response = self.client.post(self.url, data=request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.Offering.objects.count(), 1)
        offering = models.Offering.objects.first()
        self.assertIn(offering.type, 'custom_vpc')
        self.assertIn(offering.type_label, settings.WALDUR_SUPPORT['OFFERINGS'][expected_type]['label'])

    def test_user_cannot_create_offering_if_he_has_no_permissions_to_the_project(self):
        request_data = self._get_valid_request()
        request_data['project'] = structure_factories.ProjectFactory.get_url()
        self.client.force_authenticate(self.fixture.user)
        response = self.client.post(self.url, data=request_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_issue_project_is_associated_with_an_offering(self):
        request_data = self._get_valid_request()

        response = self.client.post(self.url, data=request_data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.Issue.objects.count(), 1)
        issue = models.Issue.objects.first()
        self.assertIsNotNone(issue.project)
        self.assertEqual(models.Offering.objects.count(), 1)
        offering = models.Offering.objects.first()
        self.assertEqual(issue.project.uuid, offering.project.uuid)

    @mock.patch('waldur_mastermind.support.backend.get_active_backend')
    def test_offering_is_not_created_if_backend_raises_error(self, get_active_backend_mock):
        get_active_backend_mock.side_effect = SupportBackendError()

        request_data = self._get_valid_request()
        self.assertEqual(models.Offering.objects.count(), 0)
        self.assertEqual(models.Issue.objects.count(), 0)

        with self.assertRaises(SupportBackendError):
            self.client.post(self.url, data=request_data)

        self.assertEqual(models.Offering.objects.count(), 0)
        self.assertEqual(models.Issue.objects.count(), 0)

    @data('user')
    def test_user_cannot_associate_new_offering_with_project_if_he_has_no_project_level_permissions(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        request_data = self._get_valid_request()

        response = self.client.post(self.url, request_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data('admin', 'manager', 'staff', 'global_support', 'owner')
    def test_user_can_create_offering_if_he_has_project_level_permissions(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        request_data = self._get_valid_request(self.fixture.project)

        response = self.client.post(self.url, request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNotNone(response.data)
        self.assertEqual(response.data['project_uuid'].hex, self.fixture.project.uuid.hex)


@override_support_settings(OFFERINGS={
    'security_package': {
        'label': 'Custom security package',
        'article_code': 'WALDUR-SECURITY',
        'product_code': 'PACK-001',
        'price': 100,
        'unit': UnitPriceMixin.Units.PER_DAY,
        'order': ['vm_count'],
        'options': {
            'vm_count': {
                'type': 'integer',
                'label': 'Virtual machines count',
            },
        },
    },
})
class OfferingCreateProductTest(BaseOfferingTest):
    def setUp(self):
        super(OfferingCreateProductTest, self).setUp()
        self.url = factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(self.fixture.staff)

    def _get_valid_request(self, project=None):
        return {
            'type': 'security_package',
            'name': 'Security package request',
            'vm_count': 1000,
            'project': structure_factories.ProjectFactory.get_url(project or self.fixture.project)
        }

    def test_product_code_is_copied_from_configuration_to_offering(self):
        valid_request = self._get_valid_request()
        response = self.client.post(self.url, valid_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['article_code'], 'WALDUR-SECURITY')
        self.assertEqual(response.data['product_code'], 'PACK-001')

    def test_price_is_copied_from_configuration_to_offering(self):
        valid_request = self._get_valid_request()
        response = self.client.post(self.url, valid_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(response.data['unit_price']), Decimal(100))
        self.assertEqual(response.data['unit'], 'day')


class OfferingUpdateTest(BaseOfferingTest):
    def setUp(self):
        super(OfferingUpdateTest, self).setUp()
        self.offering = self.fixture.offering
        self.url = factories.OfferingFactory.get_url(self.offering)

    def test_staff_can_update_offering(self):
        self.client.force_authenticate(self.fixture.staff)

        new_name = 'New name'
        new_report = {'Name': 'Value'}

        response = self.client.put(self.url, {'name': new_name, 'report': new_report})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.name, new_name)
        self.assertEqual(self.offering.report, new_report)

    def test_owner_can_not_update_offering(self):
        self.client.force_authenticate(self.fixture.owner)
        request = {'name': 'New name', 'report': {'Name': 'Value'}}
        response = self.client.put(self.url, request)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class OfferingCompleteTest(BaseOfferingTest):

    def test_offering_is_in_ok_state_when_complete_is_called(self):
        offering = factories.OfferingFactory()
        self.assertEqual(offering.state, models.Offering.States.REQUESTED)
        expected_price = 10

        url = factories.OfferingFactory.get_url(offering=offering, action='complete')
        request_data = {'unit_price': expected_price}
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(url, request_data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering.refresh_from_db()
        self.assertEqual(offering.state, models.Offering.States.OK)
        self.assertEqual(offering.unit_price, expected_price)

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
        request_data = {'unit_price': 10}
        self.client.force_authenticate(self.fixture.user)
        response = self.client.post(url, request_data)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        offering.refresh_from_db()
        self.assertEqual(offering.state, models.Offering.States.REQUESTED)

    def test_staff_can_complete_offering(self):
        offering = self.fixture.offering
        url = factories.OfferingFactory.get_url(offering=offering, action='complete')
        request_data = {'unit_price': 10}
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(url, request_data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering.refresh_from_db()
        self.assertEqual(offering.state, models.Offering.States.OK)


@ddt
class OfferingTerminateTest(BaseOfferingTest):

    def setUp(self, **kwargs):
        super(OfferingTerminateTest, self).setUp(**kwargs)

    def test_staff_can_terminate_offering(self):
        self.client.force_authenticate(self.fixture.staff)
        self.assertEqual(self.fixture.offering.state, models.Offering.States.REQUESTED)
        self.url = factories.OfferingFactory.get_url(offering=self.fixture.offering, action='terminate')

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.offering.refresh_from_db()
        self.assertEqual(self.fixture.offering.state, models.Offering.States.TERMINATED)

    @data('user', 'global_support', 'owner', 'admin', 'manager')
    def test_user_cannot_terminate_offering(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        self.assertEqual(self.fixture.offering.state, models.Offering.States.REQUESTED)
        self.url = factories.OfferingFactory.get_url(offering=self.fixture.offering, action='terminate')

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.fixture.offering.refresh_from_db()
        self.assertEqual(self.fixture.offering.state, models.Offering.States.REQUESTED)


class OfferingGetConfiguredTest(BaseOfferingTest):

    def test_offering_view_returns_configured_offerings(self):
        self.client.force_authenticate(self.fixture.user)
        url = factories.OfferingFactory.get_list_action_url(action='configured')
        response = self.client.get(url)
        available_offerings = response.data
        self.assertDictEqual(available_offerings, settings.WALDUR_SUPPORT['OFFERINGS'])


@ddt
class CountersTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

    @data((True, 1), (False, 0))
    def test_project_counter_has_experts(self, (has_request, expected_value)):
        if has_request:
            factories.OfferingFactory(project=self.fixture.project)

        url = structure_factories.ProjectFactory.get_url(self.fixture.project, action='counters')
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.get(url, {'fields': ['offerings']})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {'offerings': expected_value})

    @data((True, 1), (False, 0))
    def test_customer_counter_has_experts(self, (has_request, expected_value)):
        if has_request:
            factories.OfferingFactory(project=self.fixture.project)

        url = structure_factories.CustomerFactory.get_url(self.fixture.customer, action='counters')
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.get(url, {'fields': ['offerings']})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {'offerings': expected_value})
