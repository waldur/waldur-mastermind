from unittest import mock

from rest_framework import test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from . import fixtures


class OrderProcessedTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ScriptFixture()

    @mock.patch("waldur_mastermind.marketplace_script.utils.docker")
    def test_process_order(self, mock_docker):
        self.fixture.offering.secret_options = {
            "language": "python",
            "create": 'print("test creation")',
        }
        self.fixture.offering.save()
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.fixture.offering,
            attributes={
                "name": "name",
            },
            limits={"cpu": 10},
            state=marketplace_models.Order.States.EXECUTING,
        )
        marketplace_utils.process_order(order, self.fixture.staff)
        mock_docker.DockerClient().containers.run.assert_called_once()
        self.assertEqual(
            mock_docker.DockerClient().containers.run.call_args.kwargs["environment"][
                "ATTRIBUTES"
            ],
            '{"name": "name"}',
        )
        self.assertEqual(
            mock_docker.DockerClient().containers.run.call_args.kwargs["environment"][
                "LIMITS"
            ],
            '{"cpu": 10}',
        )
