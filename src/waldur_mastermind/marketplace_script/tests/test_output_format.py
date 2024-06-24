import base64
from unittest import mock

from rest_framework import test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_script.tasks import pull_resource

from . import fixtures


@mock.patch("waldur_mastermind.marketplace_script.utils.docker")
class CreateOutputFormatTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ScriptFixture()
        self.fixture.offering.secret_options = {
            "language": "python",
            "create": 'print("test creation")',
        }
        self.fixture.offering.save()
        self.order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.fixture.offering,
            attributes={
                "name": "name",
            },
            limits={"cpu": 10},
            state=marketplace_models.Order.States.EXECUTING,
        )

    def test_output_is_blank(self, mock_docker):
        mock_docker.DockerClient().containers.run.return_value = b""
        marketplace_utils.process_order(self.order, self.fixture.staff)
        self.assertEqual(
            self.order.resource.state, marketplace_models.Resource.States.OK
        )
        self.assertEqual(self.order.resource.backend_id, "")
        self.assertEqual(self.order.resource.backend_metadata, {})
        self.assertFalse(
            marketplace_models.ResourceAccessEndpoint.objects.filter(
                resource=self.order.resource
            ).exists()
        )

    def test_output_includes_only_backend_id(self, mock_docker):
        mock_docker.DockerClient().containers.run.return_value = (
            b"Some lines\n" + b"backend_id"
        )
        marketplace_utils.process_order(self.order, self.fixture.staff)
        self.assertEqual(
            self.order.resource.state, marketplace_models.Resource.States.OK
        )
        self.assertEqual(self.order.resource.backend_id, "backend_id")
        self.assertEqual(self.order.resource.backend_metadata, {})
        self.assertFalse(
            marketplace_models.ResourceAccessEndpoint.objects.filter(
                resource=self.order.resource
            ).exists()
        )

    def test_output_includes_backend_id_and_metadata(self, mock_docker):
        mock_docker.DockerClient().containers.run.return_value = (
            b"Some lines\n"
            + b"backend_id"
            + b" "
            + base64.b64encode(b'{"backend_metadata": {"cpu": 1}}')
        )
        marketplace_utils.process_order(self.order, self.fixture.staff)
        self.assertEqual(
            self.order.resource.state, marketplace_models.Resource.States.OK
        )
        self.assertEqual(self.order.resource.backend_id, "backend_id")
        self.assertEqual(self.order.resource.backend_metadata, {"cpu": 1})
        self.assertFalse(
            marketplace_models.ResourceAccessEndpoint.objects.filter(
                resource=self.order.resource
            ).exists()
        )

    def test_output_includes_backend_id_metadata_and_endpoints(self, mock_docker):
        mock_docker.DockerClient().containers.run.return_value = (
            b"Some lines\n"
            + b"backend_id"
            + b" "
            + base64.b64encode(
                b"""{
                    "backend_metadata": {"cpu": 1},
                    "endpoints": [
                        {
                            "name": "start",
                            "url": "/"
                        }
                    ]
                }"""
            )
        )
        marketplace_utils.process_order(self.order, self.fixture.staff)
        self.assertEqual(
            self.order.resource.state, marketplace_models.Resource.States.OK
        )
        self.assertEqual(self.order.resource.backend_id, "backend_id")
        self.assertEqual(self.order.resource.backend_metadata, {"cpu": 1})
        self.assertTrue(
            marketplace_models.ResourceAccessEndpoint.objects.filter(
                resource=self.order.resource
            ).exists()
        )


@mock.patch("waldur_mastermind.marketplace_script.utils.docker")
class PullOutputFormatTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ScriptFixture()
        self.offering = self.fixture.offering
        self.resource = self.fixture.resource
        self.resource.state = marketplace_models.Resource.States.OK
        self.resource.save()
        self.component = self.offering.components.first()
        self.component.billing_type = (
            marketplace_models.OfferingComponent.BillingTypes.USAGE
        )
        self.component.save()

    def test_output_is_blank(self, mock_docker):
        mock_docker.DockerClient().containers.run.return_value = b""
        pull_resource(self.fixture.resource.id)
        self.fixture.resource.refresh_from_db()
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.OK)
        self.assertEqual(self.resource.error_message, "")

    def test_output_includes_usages(self, mock_docker):
        component_type = self.component.type.encode("utf-8")
        self.assertFalse(
            marketplace_models.ComponentUsage.objects.filter(
                resource=self.resource,
                component=self.resource.plan.components.first().component,
            ).exists()
        )
        mock_docker.DockerClient().containers.run.return_value = (
            b"Some lines\n"
            + base64.b64encode(
                b"""{
                    "usages": [
                        {"type": "%s", "amount": 1}
                    ]
                }"""
                % component_type
            )
        )
        pull_resource(self.fixture.resource.id)
        self.fixture.resource.refresh_from_db()
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.OK)
        self.assertEqual(self.resource.error_message, "")
        self.assertTrue(
            marketplace_models.ComponentUsage.objects.filter(
                resource=self.resource,
                component=self.component,
            ).exists()
        )

    def test_output_includes_report(self, mock_docker):
        mock_docker.DockerClient().containers.run.return_value = (
            b"Some lines\n"
            + base64.b64encode(
                b"""{
                    "report": [
                        {"header": "header", "body": "body"}
                    ]
                }"""
            )
        )
        pull_resource(self.fixture.resource.id)
        self.fixture.resource.refresh_from_db()
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.OK)
        self.assertEqual(self.resource.report, [{"header": "header", "body": "body"}])
