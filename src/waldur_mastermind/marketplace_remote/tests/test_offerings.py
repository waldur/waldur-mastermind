import responses
from django.test import override_settings
from rest_framework import status, test

from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import fixtures
from waldur_mastermind.marketplace_remote.tasks import OfferingPullTask


class RemoteCustomersTest(test.APITransactionTestCase):
    @responses.activate
    def test_remote_customers_are_listed_for_given_token_and_api_url(self):
        responses.add(responses.GET, 'https://remote-waldur.com/customers/', json=[])
        self.client.force_login(UserFactory())
        response = self.client.post(
            '/api/remote-waldur-api/remote_customers/',
            {'api_url': 'https://remote-waldur.com/', 'token': 'valid_token',},
        )
        self.assertEqual(
            responses.calls[0].request.headers['Authorization'], 'token valid_token'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])


class OfferingComponentPullTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        fixture = fixtures.MarketplaceFixture()
        self.offering = fixture.offering
        self.component = fixture.offering_component
        self.offering.backend_id = 'offering-backend-id'
        self.offering.secret_options = {
            'api_url': 'https://remote-waldur.com/',
            'token': '123',
            'customer_uuid': '456',
        }
        self.task = OfferingPullTask()

        self.remote_offering = {
            'name': self.offering.name,
            'description': self.offering.description,
            'full_description': self.offering.full_description,
            'terms_of_service': self.offering.terms_of_service,
            'options': self.offering.options,
            'components': [
                {
                    'name': self.component.name,
                    'type': self.component.type,
                    'description': self.component.description,
                    'article_code': self.component.article_code,
                    'measured_unit': self.component.measured_unit,
                    'billing_type': self.component.billing_type,
                    'min_value': self.component.min_value,
                    'max_value': self.component.max_value,
                    'is_boolean': self.component.is_boolean,
                    'default_limit': self.component.default_limit,
                    'limit_period': self.component.limit_period,
                    'limit_amount': self.component.limit_amount,
                }
            ],
        }

    @responses.activate
    @override_settings(task_always_eager=True)
    def test_update_component(self):
        new_billing_type = 'usage'
        self.remote_offering['components'][0]['billing_type'] = new_billing_type
        responses.add(
            responses.GET,
            f'https://remote-waldur.com/marketplace-offerings/{self.offering.backend_id}/',
            json=self.remote_offering,
        )
        self.task.pull(self.offering)
        self.component.refresh_from_db()
        self.assertEqual(new_billing_type, self.component.billing_type)
        self.assertEqual(1, self.offering.components.count())

    @responses.activate
    @override_settings(task_always_eager=True)
    def test_stale_and_new_components(self):
        new_type = 'gpu'
        self.remote_offering['components'][0]['type'] = new_type
        responses.add(
            responses.GET,
            f'https://remote-waldur.com/marketplace-offerings/{self.offering.backend_id}/',
            json=self.remote_offering,
        )
        self.task.pull(self.offering)
        self.assertEqual(1, self.offering.components.count())
        self.assertEqual(
            1, models.OfferingComponent.objects.filter(type=new_type).count()
        )
        self.assertEqual(
            0, models.OfferingComponent.objects.filter(type=self.component.type).count()
        )
