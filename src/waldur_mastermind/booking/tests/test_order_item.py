from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_mastermind.booking import PLUGIN_NAME
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import tasks as marketplace_tasks
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class OrderItemProcessedTest(test.APITransactionTestCase):
    def test_resource_is_created_when_order_item_is_processed(self):
        fixture = fixtures.ProjectFixture()
        offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME)

        order_item = marketplace_factories.OrderItemFactory(
            offering=offering,
            attributes={
                'name': 'item_name',
                'description': 'Description',
                'schedules': [{'start': None, 'end': None}],
            },
        )

        serialized_order = core_utils.serialize_instance(order_item.order)
        serialized_user = core_utils.serialize_instance(fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        self.assertTrue(
            marketplace_models.Resource.objects.filter(name='item_name').exists()
        )
        resource = marketplace_models.Resource.objects.get(name='item_name')
        self.assertEqual(resource.state, marketplace_models.Resource.States.CREATING)


@freeze_time('2018-12-01')
class OrderCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.user = self.fixture.admin
        self.offering = marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME,
            attributes={
                'schedules': [
                    {
                        'start': '2018-11-01T00:00:00.000000Z',
                        'end': '2018-11-01T23:59:59.000000Z',
                    },
                    {
                        'start': '2019-01-01T00:00:00.000000Z',
                        'end': '2019-01-01T23:59:59.000000Z',
                    },
                    {
                        'start': '2019-01-02T00:00:00.000000Z',
                        'end': '2019-01-02T23:59:59.000000Z',
                    },
                    {
                        'start': '2019-01-03T00:00:00.000000Z',
                        'end': '2019-01-03T23:59:59.000000Z',
                    },
                ]
            },
            state=marketplace_models.Offering.States.ACTIVE,
        )

    def test_create_order_if_schedule_is_valid(self):
        add_payload = {
            'items': [
                {
                    'offering': marketplace_factories.OfferingFactory.get_url(
                        self.offering
                    ),
                    'attributes': {
                        'schedules': [
                            {
                                'start': '2019-01-02T00:00:00.000000Z',
                                'end': '2019-01-02T23:59:59.000000Z',
                            },
                        ]
                    },
                },
            ]
        }
        response = self.create_order(
            self.user, offering=self.offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            marketplace_models.Order.objects.filter(created_by=self.user).exists()
        )
        self.assertEqual(1, len(response.data['items']))

    def test_do_not_create_order_if_schedule_is_not_valid_for_selected_offering(self):
        add_payload = {
            'items': [
                {
                    'offering': marketplace_factories.OfferingFactory.get_url(
                        self.offering
                    ),
                    'attributes': {
                        'schedules': [
                            {
                                'start': '2019-01-05T00:00:00.000000Z',
                                'end': '2019-01-05T23:59:59.000000Z',
                            },
                        ]
                    },
                },
            ]
        }
        response = self.create_order(
            self.user, offering=self.offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            str(response.content, 'utf-8'),
            '["Time period from %s to %s is not available for selected offering."]'
            % ('2019-01-05T00:00:00.000000Z', '2019-01-05T23:59:59.000000Z'),
        )

    def test_do_not_create_order_if_schedule_is_empty(self):
        add_payload = {
            'items': [
                {
                    'offering': marketplace_factories.OfferingFactory.get_url(
                        self.offering
                    ),
                    'attributes': {'schedules': []},
                },
            ]
        }
        response = self.create_order(
            self.user, offering=self.offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(str(response.content, 'utf-8'), '["Schedules are required."]')

    def test_do_not_create_order_if_schedule_item_has_not_got_key_start(self):
        add_payload = {
            'items': [
                {
                    'offering': marketplace_factories.OfferingFactory.get_url(
                        self.offering
                    ),
                    'attributes': {
                        'schedules': [{'end': '2019-01-05T23:59:59.000000Z'},]
                    },
                },
            ]
        }
        response = self.create_order(
            self.user, offering=self.offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            str(response.content, 'utf-8'),
            '["Key \'start\' or \'end\' does not exist in schedules item."]',
        )

    def test_do_not_create_order_if_end_is_none(self):
        add_payload = {
            'items': [
                {
                    'offering': marketplace_factories.OfferingFactory.get_url(
                        self.offering
                    ),
                    'attributes': {
                        'schedules': [
                            {'start': '2019-01-05T23:59:59.000000Z', 'end': None},
                        ]
                    },
                },
            ]
        }
        response = self.create_order(
            self.user, offering=self.offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            str(response.content, 'utf-8'),
            '["Value \'start\' or \'end\' does not exist in schedules item."]',
        )

    def test_do_not_create_order_if_schedule_item_does_not_match_format(self):
        add_payload = {
            'items': [
                {
                    'offering': marketplace_factories.OfferingFactory.get_url(
                        self.offering
                    ),
                    'attributes': {
                        'schedules': [
                            {
                                'start': '2019-01-05T00:00:00',
                                'end': '2019-01-05T23:59:59.000000Z',
                            },
                        ]
                    },
                },
            ]
        }
        response = self.create_order(
            self.user, offering=self.offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            str(response.content, 'utf-8'),
            '["The value 2019-01-05T00:00:00 does not match the format."]',
        )

    def test_do_not_create_order_if_schedules_are_not_valid(self):
        marketplace_factories.ResourceFactory(
            offering=self.offering,
            state=marketplace_models.Resource.States.OK,
            attributes={
                'schedules': [
                    {
                        'start': '2019-01-02T00:00:00.000000Z',
                        'end': '2019-01-02T23:59:59.000000Z',
                    },
                ]
            },
        )
        add_payload = {
            'items': [
                {
                    'offering': marketplace_factories.OfferingFactory.get_url(
                        self.offering
                    ),
                    'attributes': {
                        'schedules': [
                            {
                                'start': '2019-01-02T00:00:00.000000Z',
                                'end': '2019-01-02T23:59:59.000000Z',
                            },
                        ]
                    },
                },
            ]
        }
        response = self.create_order(
            self.user, offering=self.offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            str(response.content, 'utf-8'),
            '["Time period from %s to %s is not available."]'
            % ('2019-01-02T00:00:00.000000Z', '2019-01-02T23:59:59.000000Z'),
        )

    def test_past_slots_are_not_available(self):
        add_payload = {
            'items': [
                {
                    'offering': marketplace_factories.OfferingFactory.get_url(
                        self.offering
                    ),
                    'attributes': {
                        'schedules': [
                            {
                                'start': '2018-11-01T00:00:00.000000Z',
                                'end': '2018-11-01T23:59:59.000000Z',
                            },
                        ]
                    },
                },
            ]
        }
        response = self.create_order(
            self.user, offering=self.offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            str(response.content, 'utf-8'),
            '["Past slots are not available for selection."]',
        )

    def test_do_not_create_order_if_other_booking_request_exists(self):
        add_payload = {
            'items': [
                {
                    'offering': marketplace_factories.OfferingFactory.get_url(
                        self.offering
                    ),
                    'attributes': {
                        'schedules': [
                            {
                                'start': '2019-01-02T00:00:00.000000Z',
                                'end': '2019-01-02T23:59:59.000000Z',
                            },
                        ]
                    },
                },
            ]
        }
        response = self.create_order(
            self.user, offering=self.offering, add_payload=add_payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            marketplace_models.Order.objects.filter(created_by=self.user).exists()
        )
        self.assertEqual(1, len(response.data['items']))

        # We try to create another order.
        response = self.create_order(
            self.user, offering=self.offering, add_payload=add_payload
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertEqual(
            str(response.content, 'utf-8'),
            '["Time period from %s to %s is not available. Other booking request exists."]'
            % ('2019-01-02T00:00:00.000000Z', '2019-01-02T23:59:59.000000Z'),
        )

    def create_order(self, user, offering=None, add_payload=None):
        if offering is None:
            offering = marketplace_factories.OfferingFactory(
                state=marketplace_models.Offering.States.ACTIVE
            )

        self.client.force_authenticate(user)
        url = marketplace_factories.OrderFactory.get_list_url()
        payload = {
            'project': structure_factories.ProjectFactory.get_url(self.project),
            'items': [
                {
                    'offering': marketplace_factories.OfferingFactory.get_url(offering),
                    'attributes': {},
                },
            ],
        }

        if add_payload:
            payload.update(add_payload)

        return self.client.post(url, payload)
