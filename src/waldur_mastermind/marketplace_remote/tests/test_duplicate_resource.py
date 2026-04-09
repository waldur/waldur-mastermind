import uuid
from unittest import mock

import respx
from django.test import TestCase
from rest_framework import status, test
from rest_framework.exceptions import ValidationError

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    REMOTE_OFFERING,
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests.factories import (
    OfferingFactory,
    OrderFactory,
    PlanFactory,
    ResourceFactory,
)
from waldur_mastermind.marketplace_remote.processors import (
    RemoteCreateResourceProcessor,
    RemoteDeleteResourceProcessor,
)
from waldur_mastermind.marketplace_remote.tests.dns_utils import (
    create_selective_dns_mock,
)


class ValidateOrderDuplicateTest(TestCase):
    def setUp(self):
        fixture = ProjectFixture()
        self.project = fixture.project
        self.offering = OfferingFactory(
            type=REMOTE_OFFERING,
            secret_options={
                "api_url": "https://remote-waldur.com",
                "token": "valid_token",
            },
        )
        self.order = OrderFactory(
            project=self.project,
            offering=self.offering,
            state=OrderStates.PENDING_CONSUMER,
            type=OrderTypes.CREATE,
            attributes={"name": "my-resource"},
        )
        # OrderFactory auto-creates a resource with the same name;
        # terminate it so it does not interfere with duplicate detection tests.
        self.order.resource.state = ResourceStates.TERMINATED
        self.order.resource.save()

    def test_validate_order_raises_when_active_resource_exists(self):
        ResourceFactory(
            project=self.project,
            offering=self.offering,
            name="my-resource",
            state=ResourceStates.OK,
        )
        processor = RemoteCreateResourceProcessor(self.order)
        with self.assertRaises(ValidationError):
            processor.validate_order(request=None)

    def test_validate_order_raises_when_creating_resource_exists(self):
        ResourceFactory(
            project=self.project,
            offering=self.offering,
            name="my-resource",
            state=ResourceStates.CREATING,
        )
        processor = RemoteCreateResourceProcessor(self.order)
        with self.assertRaises(ValidationError):
            processor.validate_order(request=None)

    def test_validate_order_passes_when_resource_is_terminated(self):
        ResourceFactory(
            project=self.project,
            offering=self.offering,
            name="my-resource",
            state=ResourceStates.TERMINATED,
        )
        processor = RemoteCreateResourceProcessor(self.order)
        # Should not raise
        processor.validate_order(request=None)

    def test_validate_order_passes_when_resource_is_erred(self):
        ResourceFactory(
            project=self.project,
            offering=self.offering,
            name="my-resource",
            state=ResourceStates.ERRED,
        )
        processor = RemoteCreateResourceProcessor(self.order)
        # Should not raise
        processor.validate_order(request=None)

    def test_validate_order_passes_when_name_differs(self):
        ResourceFactory(
            project=self.project,
            offering=self.offering,
            name="other-resource",
            state=ResourceStates.OK,
        )
        processor = RemoteCreateResourceProcessor(self.order)
        # Should not raise
        processor.validate_order(request=None)

    def test_validate_order_passes_when_offering_differs(self):
        other_offering = OfferingFactory(type=REMOTE_OFFERING)
        ResourceFactory(
            project=self.project,
            offering=other_offering,
            name="my-resource",
            state=ResourceStates.OK,
        )
        processor = RemoteCreateResourceProcessor(self.order)
        # Should not raise
        processor.validate_order(request=None)

    def test_validate_order_passes_when_no_name_in_attributes(self):
        self.order.attributes = {}
        self.order.save()
        ResourceFactory(
            project=self.project,
            offering=self.offering,
            name="",
            state=ResourceStates.OK,
        )
        processor = RemoteCreateResourceProcessor(self.order)
        # Should not raise — empty name is not checked
        processor.validate_order(request=None)


class ProcessOrderRemoteDuplicateTest(TestCase):
    def setUp(self):
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()
        respx.start()

        fixture = ProjectFixture()
        self.api_url = "https://remote-waldur.com"
        self.offering = OfferingFactory(
            type=REMOTE_OFFERING,
            secret_options={
                "api_url": self.api_url,
                "token": "valid_token",
            },
        )
        self.offering.backend_id = uuid.uuid4().hex
        self.offering.save()

        self.order = OrderFactory(
            project=fixture.project,
            offering=self.offering,
            state=OrderStates.EXECUTING,
            type=OrderTypes.CREATE,
            attributes={"name": "my-resource"},
        )
        self.user = fixture.owner

    def tearDown(self):
        self.dns_patcher.stop()
        respx.stop()
        mock.patch.stopall()

    def _mock_get_or_create_remote_project(self):
        remote_project = mock.MagicMock()
        remote_project.uuid = uuid.uuid4()
        return mock.patch(
            "waldur_mastermind.marketplace_remote.processors.utils.get_or_create_remote_project",
            return_value=(remote_project, True),
        )

    def test_process_order_raises_when_remote_duplicate_exists(self):
        remote_resource_uuid = uuid.uuid4()
        with self._mock_get_or_create_remote_project():
            respx.get(f"{self.api_url}/api/marketplace-resources/").respond(
                200,
                json=[
                    {
                        "uuid": str(remote_resource_uuid),
                        "name": "my-resource",
                        "state": "OK",
                    }
                ],
            )

            processor = RemoteCreateResourceProcessor(self.order)
            with self.assertRaises(Exception) as ctx:
                processor.process_order(user=self.user)

            self.assertIn("already exists in remote project", str(ctx.exception))
            self.assertIn(str(remote_resource_uuid), str(ctx.exception))

    def test_process_order_proceeds_when_no_remote_duplicate(self):
        remote_order_uuid = uuid.uuid4()
        with self._mock_get_or_create_remote_project():
            # Empty list — no duplicates
            respx.get(f"{self.api_url}/api/marketplace-resources/").respond(
                200, json=[]
            )
            respx.post(f"{self.api_url}/api/marketplace-orders/").respond(
                201,
                json={
                    "uuid": str(remote_order_uuid),
                    "state": "executing",
                },
            )

            processor = RemoteCreateResourceProcessor(self.order)
            processor.process_order(user=self.user)

            self.order.refresh_from_db()
            self.assertEqual(self.order.backend_id, remote_order_uuid.hex)


class DeleteProcessorEmptyBackendIdTest(TestCase):
    def setUp(self):
        fixture = ProjectFixture()
        self.offering = OfferingFactory(
            type=REMOTE_OFFERING,
            secret_options={
                "api_url": "https://remote-waldur.com",
                "token": "valid_token",
            },
        )
        self.resource = ResourceFactory(
            project=fixture.project,
            offering=self.offering,
            backend_id="",
            state=ResourceStates.OK,
        )
        self.order = OrderFactory(
            project=fixture.project,
            offering=self.offering,
            resource=self.resource,
            state=OrderStates.EXECUTING,
            type=OrderTypes.TERMINATE,
        )

    def test_warning_logged_when_backend_id_is_empty(self):
        processor = RemoteDeleteResourceProcessor(self.order)
        with self.assertLogs(
            "waldur_mastermind.marketplace_remote.processors", level="WARNING"
        ) as cm:
            result = processor.send_request(user=None, resource=self.resource)

        self.assertTrue(result)
        self.assertTrue(
            any("backend_id is empty" in msg for msg in cm.output),
        )


class OrderCreateValidationTest(test.APITransactionTestCase):
    """
    Test API order creation order validation for duplicate resources.

    """

    def setUp(self):
        self.fixture = ProjectFixture()
        self.project = self.fixture.project
        self.user = self.fixture.owner
        self.offering = OfferingFactory(
            type=REMOTE_OFFERING,
            state=OfferingStates.ACTIVE,
            secret_options={
                "api_url": "https://remote-waldur.com",
                "token": "valid_token",
            },
        )
        self.plan = PlanFactory(offering=self.offering)

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.MEMBER.add_permission(PermissionEnum.CREATE_ORDER)

    def test_order_creation_succeeds_with_fix(self):
        """
        Test that order creation succeeds when a resource is created first.
        """
        self.client.force_authenticate(self.user)
        url = OrderFactory.get_list_url()

        resource_name = "test4all-1-ahti-tes-1-lumi-eta-1-44"
        payload = {
            "project": f"http://testserver/api/projects/{self.project.uuid.hex}/",
            "offering": f"http://testserver/api/marketplace-public-offerings/{self.offering.uuid.hex}/",
            "plan": f"http://testserver/api/marketplace-public-offerings/{self.offering.uuid.hex}/plans/{self.plan.uuid.hex}/",
            "attributes": {"name": resource_name},
            "limits": {},
            "accepting_terms_of_service": True,
        }

        response = self.client.post(url, payload)

        self.assertEqual(
            response.status_code,
            status.HTTP_201_CREATED,
            f"Expected 201 but got {response.status_code}. Response: {response.data}",
        )
        order = marketplace_models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.resource.name, resource_name)
