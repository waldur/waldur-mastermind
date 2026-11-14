"""Editing a project template round-trips its offerings.

HomePort's editor fills the offerings from offerings_data and sends back each
one's url. offerings_data had no url, so saving any edit to a template that
already had offerings failed with "offerings: Incorrect type. Expected URL
string, received NoneType."
"""

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openportal import models


class ProjectTemplateOfferingsRoundTripTest(test.APITestCase):
    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory()
        self.template = models.ProjectTemplate.objects.create(
            name="tmpl",
            portal="test-portal",
            provider=structure_factories.CustomerFactory(),
            customer=structure_factories.CustomerFactory(),
        )
        self.template.offerings.add(self.offering)
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        self.url = f"/api/openportal-project-template/{self.template.uuid.hex}/"

    def test_offerings_data_carries_each_offerings_url(self):
        data = self.client.get(self.url).data

        self.assertEqual(len(data["offerings_data"]), 1)
        self.assertEqual(data["offerings_data"][0]["url"], data["offerings"][0])

    def test_saving_the_offerings_as_loaded_succeeds(self):
        loaded = self.client.get(self.url).data

        response = self.client.patch(
            self.url,
            {"offerings": [o["url"] for o in loaded["offerings_data"]]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(list(self.template.offerings.all()), [self.offering])
