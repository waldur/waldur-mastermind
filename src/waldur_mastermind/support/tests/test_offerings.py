from decimal import Decimal
from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.support import models
from waldur_mastermind.support.tests import factories, fixtures
from waldur_mastermind.support.tests.base import BaseTest, override_support_settings


class BaseOfferingTest(BaseTest):
    def setUp(self, **kwargs):
        super(BaseOfferingTest, self).setUp(**kwargs)
        self.fixture = fixtures.SupportFixture()
        self.offering = self.fixture.offering
        self.offering_template = self.fixture.offering.template


@ddt
class OfferingRetrieveTest(BaseOfferingTest):
    def setUp(self, **kwargs):
        super(OfferingRetrieveTest, self).setUp(**kwargs)
        self.url = factories.OfferingFactory.get_list_url()

    @data('staff', 'global_support', 'owner', 'admin', 'manager')
    def test_user_can_see_list_of_offerings_if_he_has_project_level_permissions(
        self, user
    ):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(self.offering.uuid.hex, response.data[0]['uuid'])

    def test_user_cannot_see_list_of_offerings_if_he_has_no_project_level_permissions(
        self,
    ):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


@ddt
class OfferingCreateTest(BaseTest):
    def setUp(self):
        super(OfferingCreateTest, self).setUp()
        self.url = factories.OfferingFactory.get_list_url()
        self.fixture = structure_fixtures.ServiceFixture()
        self.client.force_authenticate(self.fixture.staff)
        self.offering_template = factories.OfferingTemplateFactory()

    def test_error_is_raised_if_template_is_not_provided(self):
        request_data = self._get_valid_request()
        del request_data['template']

        response = self.client.post(self.url, data=request_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('template', response.data)

    def test_field_required_error_is_raised_if_template_is_empty(self):
        request_data = self._get_valid_request()
        request_data['template'] = None

        response = self.client.post(self.url, data=request_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('template', response.data)
        self.assertIn('This field may not be null.', response.data['template'])

    def test_error_is_raised_if_template_is_invalid(self):
        request_data = self._get_valid_request()
        request_data['template'] = 'invalid'

        response = self.client.post(self.url, data=request_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('template', response.data)

    def test_issue_is_created(self):
        request_data = self._get_valid_request()

        response = self.client.post(self.url, data=request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(models.Issue.objects.count(), 1)

    def test_issue_is_created_without_attributes_if_all_custom_attributes_are_optional(
        self,
    ):
        # Arrange
        conf = self.offering_template.config['options']
        conf['storage']['required'] = False
        conf['ram']['required'] = False
        conf['cpu_count']['required'] = False
        self.offering_template.save()

        request_data = self._get_valid_request()
        del request_data['attributes']

        # Act
        response = self.client.post(self.url, data=request_data)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(models.Issue.objects.count(), 1)

    def test_issue_is_created_with_explicit_plan(self):
        payload = self._get_valid_request()
        plan = factories.OfferingPlanFactory(template=self.offering_template)
        plan_url = factories.OfferingPlanFactory.get_url(plan)
        payload.update(dict(plan=plan_url))

        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(plan.unit_price, Decimal(response.data['unit_price']))

    def test_issue_is_created_with_implicit_plan(self):
        payload = self._get_valid_request()
        plan = factories.OfferingPlanFactory(template=self.offering_template)

        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(plan.unit_price, Decimal(response.data['unit_price']))

    def test_issue_is_not_created_with_invalid_plan(self):
        payload = self._get_valid_request()
        plan = factories.OfferingPlanFactory()
        plan_url = factories.OfferingPlanFactory.get_url(plan)
        payload.update(dict(plan=plan_url))

        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

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

    def test_offering_template_is_filled(self):
        request_data = self._get_valid_request()

        response = self.client.post(self.url, data=request_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.Offering.objects.count(), 1)

        offering = models.Offering.objects.first()
        self.assertEqual(offering.template, self.offering_template)
        self.assertEqual(offering.type_label, offering.config['label'])

    def test_article_code_and_product_code_is_copied_from_template(self):
        # Arrange
        self.offering_template.config['product_code'] = 'OS-VM'
        self.offering_template.config['article_code'] = 'AA201901'
        self.offering_template.save()

        # Act
        request_data = self._get_valid_request()
        self.client.post(self.url, data=request_data)

        # Assert
        offering = models.Offering.objects.first()
        self.assertEqual(offering.product_code, 'OS-VM')
        self.assertEqual(offering.article_code, 'AA201901')

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
        self.assertEqual(issue.project.uuid.hex, offering.project.uuid.hex)

    @data('user')
    def test_user_cannot_associate_new_offering_with_project_if_he_has_no_project_level_permissions(
        self, user
    ):
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
        self.assertEqual(
            response.data['project_uuid'].hex, self.fixture.project.uuid.hex
        )

    def _get_valid_request(self, project=None):
        if project is None:
            project = self.fixture.project

        return {
            'template': factories.OfferingTemplateFactory.get_url(
                self.offering_template
            ),
            'name': 'Do not reboot it, just patch',
            'description': 'We got Linux, and there\'s no doubt. Gonna fix',
            'project': structure_factories.ProjectFactory.get_url(project),
            'attributes': {'storage': 20, 'ram': 4, 'cpu_count': 2,},
        }


@mock.patch('waldur_mastermind.support.backend.get_active_backend')
class OfferingCreateProductTest(BaseOfferingTest):
    def setUp(self):
        super(OfferingCreateProductTest, self).setUp()
        self.url = factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(self.fixture.staff)
        self.offering_template = factories.OfferingTemplateFactory(
            name='security_package',
            config={
                'label': 'Custom security package',
                'order': ['vm_count'],
                'options': {
                    'vm_count': {'type': 'integer', 'label': 'Virtual machines count',},
                },
            },
        )
        self.plan = models.OfferingPlan.objects.create(
            template=self.offering_template,
            name='Default',
            unit_price=100,
            unit=models.OfferingPlan.Units.PER_DAY,
            article_code='WALDUR-SECURITY',
            product_code='PACK-001',
        )

    def _get_valid_request(self, project=None):
        return {
            'template': factories.OfferingTemplateFactory.get_url(
                self.offering_template
            ),
            'name': 'Security package request',
            'project': structure_factories.ProjectFactory.get_url(
                project or self.fixture.project
            ),
            'attributes': {'vm_count': 1000,},
        }

    def test_product_code_is_copied_from_configuration_to_offering(
        self, mock_active_backend
    ):
        valid_request = self._get_valid_request()
        response = self.client.post(self.url, valid_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['article_code'], 'WALDUR-SECURITY')
        self.assertEqual(response.data['product_code'], 'PACK-001')

    def test_price_is_copied_from_configuration_to_offering(self, mock_active_backend):
        valid_request = self._get_valid_request()
        response = self.client.post(self.url, valid_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(response.data['unit_price']), Decimal(100))
        self.assertEqual(response.data['unit'], 'day')

    def test_attributes_is_copied_to_offering(self, mock_active_backend):
        valid_request = self._get_valid_request()
        response = self.client.post(self.url, valid_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn(
            "Virtual machines count: '1000'", response.data['issue_description']
        )


class OfferingUpdateTest(BaseOfferingTest):
    def setUp(self):
        super(OfferingUpdateTest, self).setUp()
        self.offering = self.fixture.offering
        self.url = factories.OfferingFactory.get_url(self.offering)
        self.new_report = [{'header': 'Volumes', 'body': 'Volume Name'}]

    def test_staff_can_update_offering(self):
        self.client.force_authenticate(self.fixture.staff)

        new_name = 'New name'
        response = self.client.put(
            self.url, {'name': new_name, 'report': self.new_report}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.name, new_name)
        self.assertEqual(self.offering.report, self.new_report)

    def test_owner_can_not_update_offering(self):
        self.client.force_authenticate(self.fixture.owner)
        request = {'name': 'New name', 'report': self.new_report}
        response = self.client.put(self.url, request)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_report_should_contain_at_least_one_section(self):
        self.client.force_authenticate(self.fixture.staff)
        request = {'name': 'New name', 'report': []}
        response = self.client.put(self.url, request)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_report_section_should_be_a_list(self):
        self.client.force_authenticate(self.fixture.staff)
        request = {'name': 'New name', 'report': [1, 2]}
        response = self.client.put(self.url, request)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_set_backend_id(self):
        self.client.force_authenticate(self.fixture.staff)
        request = {'backend_id': 'offering_backend_id'}
        url = factories.OfferingFactory.get_url(self.offering, 'set_backend_id')
        response = self.client.post(url, request)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.backend_id, 'offering_backend_id')


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
    def setUp(self):
        super(OfferingTerminateTest, self).setUp()
        self.url = factories.OfferingFactory.get_url(
            offering=self.fixture.offering, action='terminate'
        )

    def test_staff_can_terminate_offering(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.fixture.offering.refresh_from_db()
        self.assertEqual(self.fixture.offering.state, models.Offering.States.TERMINATED)

    @data('user', 'global_support', 'owner', 'admin', 'manager')
    def test_user_cannot_terminate_offering(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.fixture.offering.refresh_from_db()
        self.assertEqual(self.fixture.offering.state, models.Offering.States.REQUESTED)


@ddt
class OfferingDeleteTest(BaseOfferingTest):
    def setUp(self):
        super(OfferingDeleteTest, self).setUp()
        self.url = factories.OfferingFactory.get_url(offering=self.fixture.offering)

    def test_staff_can_delete_terminated_offering(self):
        self.fixture.offering.state = models.Offering.States.TERMINATED
        self.fixture.offering.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_staff_can_not_delete_pending_offering(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_other_user_can_not_delete_offering(self):
        self.fixture.offering.state = models.Offering.States.TERMINATED
        self.fixture.offering.save()

        self.client.force_authenticate(self.fixture.admin)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class CountersTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

    @data((True, 1), (False, 0))
    def test_project_counter_has_offerings(self, pair):
        (has_request, expected_value) = pair
        if has_request:
            factories.OfferingFactory(project=self.fixture.project)

        url = structure_factories.ProjectFactory.get_url(
            self.fixture.project, action='counters'
        )
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.get(url, {'fields': ['offerings']})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {'offerings': expected_value})

    @data((True, 1), (False, 0))
    def test_customer_counter_has_offerings(self, pair):
        (has_request, expected_value) = pair
        if has_request:
            factories.OfferingFactory(project=self.fixture.project)

        url = structure_factories.CustomerFactory.get_url(
            self.fixture.customer, action='counters'
        )
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.get(url, {'fields': ['offerings']})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {'offerings': expected_value})
