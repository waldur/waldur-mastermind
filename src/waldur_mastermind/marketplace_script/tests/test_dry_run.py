import mock
from ddt import data, ddt
from rest_framework import test

from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from . import fixtures


@ddt
class DryRunTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ScriptFixture()
        self.offering = self.fixture.offering
        self.url = self.fixture.get_dry_run_url(self.offering)

    @data('staff', 'offering_owner', 'service_manager')
    @mock.patch('waldur_mastermind.marketplace_script.utils.execute_script')
    def test_dry_run_is_allowed(self, user, execute_script):
        output = self.offering.secret_options['create']
        execute_script.return_value = output
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        data = {
            'plan': marketplace_factories.PlanFactory.get_url(self.fixture.plan),
            'type': 'Create',
        }
        response = self.client.post(self.url, data=data)
        self.assertEqual(200, response.status_code)
        self.assertEqual({'output': output}, response.json())

    @data('owner', 'admin', 'manager', 'member')
    @mock.patch('waldur_mastermind.marketplace_script.utils.execute_script')
    def test_dry_run_is_forbidden(self, user, execute_script):
        output = self.offering.secret_options['create']
        execute_script.return_value = output
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        data = {
            'plan': marketplace_factories.PlanFactory.get_url(self.fixture.plan),
            'type': 'Create',
        }
        response = self.client.post(self.url, data=data)
        self.assertEqual(403, response.status_code)
