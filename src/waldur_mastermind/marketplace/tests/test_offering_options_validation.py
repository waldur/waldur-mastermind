from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import enums
from waldur_mastermind.marketplace.tests import factories


class OfferingOptionsValidationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.provider = factories.ServiceProviderFactory(customer=self.customer)
        self.user = self.fixture.staff
        self.client.force_authenticate(self.user)
        self.url = factories.OfferingFactory.get_list_url()

    def create_offering(self, options):
        payload = {
            "name": "offering",
            "category": factories.CategoryFactory.get_url(),
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "type": enums.SUPPORT_OFFERING,
            "options": options,
        }
        return self.client.post(self.url, payload)

    def test_valid_validators(self):
        options = {
            "order": ["ram", "cpu"],
            "options": {
                "ram": {
                    "type": "integer",
                    "label": "RAM",
                    "validators": [{"type": "gte", "target_field": "cpu"}],
                },
                "cpu": {
                    "type": "integer",
                    "label": "CPU",
                },
            },
        }
        response = self.create_offering(options)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def _test_invalid_validator_type(self):
        options = {
            "order": ["ram", "cpu"],
            "options": {
                "ram": {
                    "type": "integer",
                    "label": "RAM",
                    "validators": [{"type": "invalid_type", "target_field": "cpu"}],
                },
                "cpu": {
                    "type": "integer",
                    "label": "CPU",
                },
            },
        }
        response = self.create_offering(options)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("options", response.data)

    def test_missing_target_field_in_configuration(self):
        options = {
            "order": ["ram"],
            "options": {
                "ram": {
                    "type": "integer",
                    "label": "RAM",
                    "validators": [{"type": "gte", "target_field": "missing_field"}],
                }
            },
        }
        response = self.create_offering(options)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("options", response.data)
