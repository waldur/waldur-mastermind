from unittest import mock

from ddt import ddt
from rest_framework import test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_script.tasks import pull_resource

from . import fixtures


@ddt
@mock.patch("waldur_mastermind.marketplace_script.utils.execute_script")
class PullResourceTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ScriptFixture()
        self.offering = self.fixture.offering
        self.offering.options.update({"pull": "raise Exception()"})
        self.resource = self.fixture.resource

    def test_set_state_erred(self, execute_script):
        execute_script.side_effect = Exception("Container exception")
        pull_resource(self.fixture.resource.id)
        self.fixture.resource.refresh_from_db()
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.ERRED)
        self.assertEqual(self.resource.error_message, "Container exception")

    def test_set_state_ok(self, execute_script):
        self.resource.state = marketplace_models.Resource.States.ERRED
        self.resource.save()
        execute_script.return_value = ""
        pull_resource(self.fixture.resource.id)
        self.fixture.resource.refresh_from_db()
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.OK)
        self.assertEqual(self.resource.error_message, "")
