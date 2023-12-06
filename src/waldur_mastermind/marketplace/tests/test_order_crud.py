import datetime
from unittest import mock

from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.factories import OFFERING_OPTIONS
from waldur_mastermind.marketplace_support import PLUGIN_NAME


@ddt
class OrderCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project

    @data('staff', 'owner', 'admin', 'manager')
    def test_user_can_create_order_in_valid_project(self, user):
        user = getattr(self.fixture, user)
        response = self.create_order(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Order.objects.filter(created_by=user).exists())

    @data('user')
    def test_user_can_not_create_order_in_invalid_project(self, user):
        user = getattr(self.fixture, user)
        response = self.create_order(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_not_create_order_if_offering_is_not_available(self):
        offering = factories.OfferingFactory(state=models.Offering.States.ARCHIVED)
        response = self.create_order(self.fixture.staff, offering)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_order_with_plan(self):
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        plan = factories.PlanFactory(offering=offering)
        add_payload = {
            'offering': factories.OfferingFactory.get_public_url(offering),
            'plan': factories.PlanFactory.get_public_url(plan),
            'attributes': {},
        }
        response = self.create_order(
            self.fixture.staff, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @mock.patch(
        'waldur_mastermind.marketplace.tasks.notify_consumer_about_pending_order.delay'
    )
    def test_notification_is_sent_when_order_is_created(self, mock_task):
        offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE, shared=True, billable=True
        )
        plan = factories.PlanFactory(offering=offering)
        add_payload = {
            'offering': factories.OfferingFactory.get_public_url(offering),
            'plan': factories.PlanFactory.get_public_url(plan),
            'attributes': {},
        }
        response = self.create_order(
            self.fixture.manager, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mock_task.assert_called_once()

    def test_can_not_create_order_if_offering_is_not_available_to_customer(self):
        offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE, shared=False
        )
        offering.customer.add_user(self.fixture.owner, CustomerRole.OWNER)
        plan = factories.PlanFactory(offering=offering)
        add_payload = {
            'offering': factories.OfferingFactory.get_public_url(offering),
            'plan': factories.PlanFactory.get_public_url(plan),
            'attributes': {},
        }
        response = self.create_order(
            self.fixture.owner, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_can_not_create_order_with_plan_related_to_another_offering(self):
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        plan = factories.PlanFactory(offering=offering)
        add_payload = {
            'offering': factories.OfferingFactory.get_public_url(),
            'plan': factories.PlanFactory.get_public_url(plan),
            'attributes': {},
        }
        response = self.create_order(
            self.fixture.staff, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_can_not_create_order_if_plan_max_amount_has_been_reached(self):
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        plan = factories.PlanFactory(offering=offering, max_amount=3)
        factories.ResourceFactory.create_batch(3, plan=plan, offering=offering)
        add_payload = {
            'offering': factories.OfferingFactory.get_public_url(offering),
            'plan': factories.PlanFactory.get_public_url(plan),
            'attributes': {},
        }
        response = self.create_order(
            self.fixture.staff, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_create_order_with_valid_attributes_specified_by_options(self):
        attributes = {
            'storage': 1000,
            'ram': 30,
            'cpu_count': 5,
        }
        offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE, options=OFFERING_OPTIONS
        )
        plan = factories.PlanFactory(offering=offering)
        add_payload = {
            'offering': factories.OfferingFactory.get_public_url(offering),
            'plan': factories.PlanFactory.get_public_url(plan),
            'attributes': attributes,
        }
        response = self.create_order(
            self.fixture.staff, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['attributes'], attributes)

    def test_user_can_not_create_order_with_invalid_attributes(self):
        attributes = {
            'storage': 'invalid value',
        }
        offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE, options=OFFERING_OPTIONS
        )
        plan = factories.PlanFactory(offering=offering)
        add_payload = {
            'offering': factories.OfferingFactory.get_public_url(offering),
            'plan': factories.PlanFactory.get_public_url(plan),
            'attributes': attributes,
        }
        response = self.create_order(
            self.fixture.staff, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_create_order_with_valid_limits(self):
        limits = {
            'storage': 1000,
            'ram': 30,
            'cpu_count': 5,
        }

        offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE, type=PLUGIN_NAME
        )
        plan = factories.PlanFactory(offering=offering)

        for key in limits.keys():
            models.OfferingComponent.objects.create(
                offering=offering,
                type=key,
                billing_type=models.OfferingComponent.BillingTypes.LIMIT,
            )

        add_payload = {
            'offering': factories.OfferingFactory.get_public_url(offering),
            'plan': factories.PlanFactory.get_public_url(plan),
            'limits': limits,
            'attributes': {},
        }

        response = self.create_order(
            self.fixture.staff, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        order = models.Order.objects.last()
        self.assertEqual(order.limits['cpu_count'], 5)

    def test_user_can_not_create_order_with_invalid_limits(self):
        limits = {
            'storage': 1000,
            'ram': 30,
            'cpu_count': 5,
        }

        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        plan = factories.PlanFactory(offering=offering)

        for key in limits.keys():
            models.OfferingComponent.objects.create(
                offering=offering,
                type=key,
                billing_type=models.OfferingComponent.BillingTypes.FIXED,
            )

        add_payload = {
            'offering': factories.OfferingFactory.get_public_url(offering),
            'plan': factories.PlanFactory.get_public_url(plan),
            'limits': limits,
            'attributes': {},
        }

        response = self.create_order(
            self.fixture.staff, offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_order_creating_is_not_available_for_blocked_organization(self):
        user = self.fixture.owner
        self.fixture.customer.blocked = True
        self.fixture.customer.save()
        response = self.create_order(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_create_order_if_terms_of_service_have_been_accepted(self):
        user = self.fixture.admin
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        offering.terms_of_service = 'Terms of service'
        offering.save()
        add_payload = {
            'offering': factories.OfferingFactory.get_public_url(offering),
            'attributes': {},
            'accepting_terms_of_service': True,
        }
        response = self.create_order(user, offering=offering, add_payload=add_payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Order.objects.filter(created_by=user).exists())

    def test_user_can_create_order_if_terms_of_service_are_not_filled(self):
        user = self.fixture.admin
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        add_payload = {
            'offering': factories.OfferingFactory.get_public_url(offering),
            'attributes': {},
        }
        response = self.create_order(user, offering=offering, add_payload=add_payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Order.objects.filter(created_by=user).exists())

    def test_user_can_create_order_if_offering_is_not_shared(self):
        user = self.fixture.admin
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        offering.shared = False
        offering.customer = self.project.customer
        offering.save()
        add_payload = {
            'offering': factories.OfferingFactory.get_public_url(offering),
            'attributes': {},
        }
        response = self.create_order(user, offering=offering, add_payload=add_payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Order.objects.filter(created_by=user).exists())

    def test_user_cannot_create_order_if_terms_of_service_have_been_not_accepted(self):
        user = self.fixture.admin
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        offering.terms_of_service = 'Terms of service'
        offering.save()
        add_payload = {
            'offering': factories.OfferingFactory.get_public_url(offering),
            'attributes': {},
        }
        response = self.create_order(user, offering=offering, add_payload=add_payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            str(response.content, 'utf-8'),
            '{"non_field_errors":["Terms of service for offering \'%s\' have not been accepted."]}'
            % offering,
        )
        self.assertFalse(models.Order.objects.filter(created_by=user).exists())

    def test_user_cannot_create_order_in_project_is_expired(self):
        user = getattr(self.fixture, 'staff')
        self.project.end_date = datetime.datetime(day=1, month=1, year=2020)
        self.project.save()

        with freeze_time('2020-01-01'):
            response = self.create_order(user)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_if_divisions_do_not_match_order_validation_fails(self):
        user = self.fixture.staff
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        division = structure_factories.DivisionFactory()
        offering.divisions.add(division)

        response = self.create_order(user, offering)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_if_divisions_match_order_validation_passes(self):
        user = self.fixture.staff
        offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        division = structure_factories.DivisionFactory()
        offering.divisions.add(division)
        self.fixture.customer.division = division
        self.fixture.customer.save()

        response = self.create_order(user, offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Order.objects.filter(created_by=user).exists())

    def create_order(self, user, offering=None, add_payload=None):
        if offering is None:
            offering = factories.OfferingFactory(state=models.Offering.States.ACTIVE)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_list_url()
        payload = {
            'project': structure_factories.ProjectFactory.get_url(self.project),
            'offering': factories.OfferingFactory.get_public_url(offering),
            'attributes': {},
        }

        if add_payload:
            payload.update(add_payload)

        return self.client.post(url, payload)


@ddt
class OrderDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(
            project=self.project, created_by=self.manager
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.DESTROY_ORDER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.DESTROY_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.DESTROY_ORDER)

    @data('staff', 'owner', 'admin', 'manager')
    def test_authorized_user_can_delete_order(self, user):
        response = self.delete_order(user)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(models.Order.objects.filter(project=self.project).exists())

    @data('user')
    def test_other_user_can_not_delete_order(self, user):
        response = self.delete_order(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(models.Order.objects.filter(created_by=self.manager).exists())

    def test_unauthorized_user_can_not_delete_order(self):
        url = factories.OrderFactory.get_url(self.order)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_order_deleting_is_not_available_for_blocked_organization(self):
        self.fixture.customer.blocked = True
        self.fixture.customer.save()
        response = self.delete_order('owner')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def delete_order(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(self.order)
        response = self.client.delete(url)
        return response


@ddt
class OrderFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.manager = self.fixture.manager
        self.order = factories.OrderFactory(
            project=self.project, created_by=self.manager
        )
        self.url = factories.OrderFactory.get_list_url()

    @data('staff', 'owner', 'admin', 'manager')
    def test_orders_should_be_visible_to_colleagues_and_staff(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    @data('user')
    def test_orders_should_be_invisible_to_other_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    def test_orders_should_be_invisible_to_unauthenticated_users(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filter_orders_for_service_manager(self):
        # Arrange
        offering = factories.OfferingFactory(customer=self.fixture.customer)
        offering.add_user(self.fixture.user, OfferingRole.MANAGER)
        order = factories.OrderFactory(
            offering=offering, project=self.project, created_by=self.manager
        )

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            self.url, {'service_manager_uuid': self.fixture.user.uuid.hex}
        )

        # Assert
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['uuid'], order.uuid.hex)

    def test_service_provider_can_see_order(self):
        # Arrange
        user = structure_factories.UserFactory()
        self.order.offering.customer.add_user(user, CustomerRole.OWNER)

        # Act
        self.client.force_authenticate(user)
        response = self.client.get(self.url)

        # Assert
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['uuid'], self.order.uuid.hex)
