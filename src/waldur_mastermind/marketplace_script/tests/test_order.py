from unittest import mock

from rest_framework import test

from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import OrderStates, OrderTypes, ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from . import fixtures


class OrderProcessedTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ScriptFixture()

    @mock.patch("waldur_mastermind.marketplace_script.utils.docker")
    @mock.patch("waldur_mastermind.marketplace_script.utils.check_docker_socket_access")
    def test_process_order(self, mock_check_access, mock_docker):
        mock_docker.DockerClient().containers.run.return_value = b"OK"
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
            state=OrderStates.EXECUTING,
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

    @mock.patch("waldur_mastermind.marketplace_script.utils.docker")
    @mock.patch("waldur_mastermind.marketplace_script.utils.check_docker_socket_access")
    def test_resource_switches_back_to_ok_after_plan_switch(
        self, mock_check_access, mock_docker
    ):
        """Regression test for https://github.com/waldur/waldur-mastermind/issues/72

        After a successful switch_plan order, the resource must transition
        from Updating back to OK.
        """
        mock_docker.DockerClient().containers.run.return_value = b"OK"

        # Arrange: resource in OK state with plan1
        plan1 = marketplace_factories.PlanFactory(offering=self.fixture.offering)
        plan2 = marketplace_factories.PlanFactory(offering=self.fixture.offering)
        resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.fixture.offering,
            plan=plan1,
            state=ResourceStates.OK,
        )

        # Create an UPDATE order for plan switch (no old_limits = plan switch, not limit update)
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.fixture.offering,
            resource=resource,
            plan=plan2,
            type=OrderTypes.UPDATE,
            state=OrderStates.EXECUTING,
        )

        # Act
        marketplace_utils.process_order(order, self.fixture.staff)

        # Assert
        resource.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(resource.state, ResourceStates.OK)
        self.assertEqual(resource.plan, plan2)
        self.assertEqual(order.state, OrderStates.DONE)
