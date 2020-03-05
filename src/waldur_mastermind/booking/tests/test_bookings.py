from dateutil.parser import parse as parse_datetime
from rest_framework import status, test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from .. import PLUGIN_NAME


class BookingsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.offering = marketplace_factories.OfferingFactory(
            customer=self.fixture.customer, type=PLUGIN_NAME
        )
        self.schedules = [
            {'start': '2020-02-12', 'end': '2020-02-15',},
            {'start': '2020-03-01', 'end': '2020-03-05',},
        ]
        for schedule in self.schedules:
            marketplace_factories.ResourceFactory(
                offering=self.offering,
                state=marketplace_models.Resource.States.OK,
                attributes={'schedules': [schedule],},
            )

    def test_offering_bookings(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            f'/api/marketplace-bookings/{self.offering.uuid.hex}/'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertCountEqual(
            response.data,
            [
                {
                    'start': parse_datetime('2020-02-12'),
                    'end': parse_datetime('2020-02-15'),
                },
                {
                    'start': parse_datetime('2020-03-01'),
                    'end': parse_datetime('2020-03-05'),
                },
            ],
        )
