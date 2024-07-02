from unittest import mock

from rest_framework import test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_script.tasks import (
    pull_resource,
    resource_options_have_been_changed,
)

from . import fixtures


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


class ResourceOptionsHandlerTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ScriptFixture()
        self.offering = self.fixture.offering
        self.resource = self.fixture.resource

    @mock.patch(
        "waldur_mastermind.marketplace_script.handlers.tasks.resource_options_have_been_changed"
    )
    def test_task_has_been_running_if_resource_options_has_been_changed(
        self, mock_task
    ):
        self.resource.options = {"key": "value"}
        self.resource.save()
        mock_task.delay.assert_called_once_with(self.resource.id, None)

    @mock.patch("waldur_mastermind.marketplace_script.utils.execute_script")
    def test_task(self, execute_script):
        self.resource.options = {"key": "value"}
        self.resource.save()
        resource_options_have_been_changed(self.resource.id, None)
        execute_script.assert_called_once()
