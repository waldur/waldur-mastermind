import datetime
import re
import uuid
from decimal import Decimal
from unittest import mock

from constance.test.unittest import override_config
from dateutil.relativedelta import relativedelta
from ddt import data, ddt, unpack
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core.utils import month_start
from waldur_core.logging import models as logging_models
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests.factories import ProjectFactory, UserFactory
from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace import callbacks, models, plugins
from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    SUPPORT_OFFERING,
    BillingTypes,
    LimitPeriods,
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import utils as test_utils
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture
from waldur_openstack.tests import factories as openstack_factories


class ResourceGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan = factories.PlanFactory()
        self.offering = self.plan.offering
        self.resource = models.Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
        )

    def get_resource(self, user=None):
        if not user:
            user = self.fixture.owner
        self.client.force_authenticate(user)
        url = factories.ResourceFactory.get_url(self.resource)
        return self.client.get(url)

    def test_suggest_name(self):
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_list_url("suggest_name")
        response = self.client.post(
            url, {"project": self.project.uuid.hex, "offering": self.offering.uuid.hex}
        )
        self.assertEqual(
            response.data["name"],
            f"{self.project.customer.slug}-{self.project.slug}-{self.offering.slug}-2",
        )

    def test_suggest_name_with_pattern_core_variables(self):
        self.offering.plugin_options = {
            "resource_name_pattern": "{project_slug}-{offering_slug}-{counter}"
        }
        self.offering.save()
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_list_url("suggest_name")
        response = self.client.post(
            url, {"project": self.project.uuid.hex, "offering": self.offering.uuid.hex}
        )
        # There is 1 existing resource, so counter = 2
        self.assertEqual(
            response.data["name"],
            f"{self.project.slug}-{self.offering.slug}-2",
        )

    def test_suggest_name_with_pattern_counter_omitted_for_first(self):
        self.resource.delete()
        self.offering.plugin_options = {
            "resource_name_pattern": "{project_slug}-{offering_slug}-{counter}"
        }
        self.offering.save()
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_list_url("suggest_name")
        response = self.client.post(
            url, {"project": self.project.uuid.hex, "offering": self.offering.uuid.hex}
        )
        # No existing resources, so counter renders as empty and trailing hyphen is stripped
        self.assertEqual(
            response.data["name"],
            f"{self.project.slug}-{self.offering.slug}",
        )

    def test_suggest_name_with_pattern_and_attributes(self):
        self.offering.plugin_options = {
            "resource_name_pattern": "{project_slug}-{attributes[environment]}-{counter}"
        }
        self.offering.save()
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_list_url("suggest_name")
        response = self.client.post(
            url,
            {
                "project": self.project.uuid.hex,
                "offering": self.offering.uuid.hex,
                "attributes": {"environment": "prod"},
            },
            format="json",
        )
        self.assertEqual(
            response.data["name"],
            f"{self.project.slug}-prod-2",
        )

    def test_suggest_name_with_pattern_and_plan_name(self):
        self.offering.plugin_options = {
            "resource_name_pattern": "{project_slug}-{plan_name}-{counter}"
        }
        self.offering.save()
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_list_url("suggest_name")
        response = self.client.post(
            url,
            {
                "project": self.project.uuid.hex,
                "offering": self.offering.uuid.hex,
                "plan": self.plan.uuid.hex,
            },
            format="json",
        )
        plan_name_sanitized = re.sub(r"[^A-Za-z0-9.-]", "-", self.plan.name)
        expected = f"{self.project.slug}-{plan_name_sanitized}-2"
        self.assertEqual(response.data["name"], expected)

    def test_suggest_name_with_missing_attribute_renders_empty(self):
        self.offering.plugin_options = {
            "resource_name_pattern": "{project_slug}-{attributes[missing_key]}-{offering_slug}"
        }
        self.offering.save()
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_list_url("suggest_name")
        response = self.client.post(
            url, {"project": self.project.uuid.hex, "offering": self.offering.uuid.hex}
        )
        # Missing attribute renders as empty, duplicate hyphens collapsed and stripped
        self.assertEqual(
            response.data["name"],
            f"{self.project.slug}-{self.offering.slug}",
        )

    def test_suggest_name_with_invalid_pattern_falls_back_to_default(self):
        self.offering.plugin_options = {
            "resource_name_pattern": "{project_slug}-{!invalid}"
        }
        self.offering.save()
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_list_url("suggest_name")
        response = self.client.post(
            url, {"project": self.project.uuid.hex, "offering": self.offering.uuid.hex}
        )
        # Fallback to default behavior
        self.assertEqual(
            response.data["name"],
            f"{self.project.customer.slug}-{self.project.slug}-{self.offering.slug}-2",
        )

    def test_suggest_name_without_pattern_preserves_default(self):
        self.offering.plugin_options = {}
        self.offering.save()
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_list_url("suggest_name")
        response = self.client.post(
            url, {"project": self.project.uuid.hex, "offering": self.offering.uuid.hex}
        )
        self.assertEqual(
            response.data["name"],
            f"{self.project.customer.slug}-{self.project.slug}-{self.offering.slug}-2",
        )

    def test_suggest_name_replaces_underscores_with_hyphens(self):
        self.project.customer.slug = "my_customer"
        self.project.customer.save()
        self.project.slug = "my_project"
        self.project.save()
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_list_url("suggest_name")
        response = self.client.post(
            url, {"project": self.project.uuid.hex, "offering": self.offering.uuid.hex}
        )
        self.assertNotIn("_", response.data["name"])
        self.assertTrue(response.data["name"].startswith("my-customer-my-project-"))

    def test_suggest_name_with_pattern_replaces_underscores(self):
        self.offering.plugin_options = {
            "resource_name_pattern": "{customer_name}-{project_name}"
        }
        self.offering.save()
        self.project.customer.name = "My_Customer"
        self.project.customer.save()
        self.project.name = "My_Project"
        self.project.save()
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_list_url("suggest_name")
        response = self.client.post(
            url, {"project": self.project.uuid.hex, "offering": self.offering.uuid.hex}
        )
        self.assertNotIn("_", response.data["name"])

    def test_suggest_name_is_lowercased(self):
        self.offering.plugin_options = {
            "resource_name_pattern": "{customer_name}-{attributes[environment]}"
        }
        self.offering.save()
        self.project.customer.name = "MyCustomer"
        self.project.customer.save()
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_list_url("suggest_name")
        response = self.client.post(
            url,
            {
                "project": self.project.uuid.hex,
                "offering": self.offering.uuid.hex,
                "attributes": {"environment": "Production"},
            },
            format="json",
        )
        self.assertEqual(response.data["name"], "mycustomer-production")

    def test_resource_is_usage_based(self):
        factories.OfferingComponentFactory(
            offering=self.offering, billing_type=BillingTypes.USAGE
        )
        self.assertTrue(self.get_resource().data["is_usage_based"])

    def test_resource_is_not_usage_based(self):
        self.assertFalse(self.get_resource().data["is_usage_based"])

    def test_project_manager_can_get_resource_data(self):
        response = self.get_resource(self.fixture.manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_customer_owner_can_get_resource_data(self):
        response = self.get_resource(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_service_provider_can_get_resource_data(self):
        owner = UserFactory()
        self.offering.customer.add_user(owner, CustomerRole.OWNER)

        response = self.get_resource()
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_other_user_can_not_get_resource_data(self):
        response = self.get_resource(UserFactory())
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_filter_resources_for_service_manager(self):
        # Arrange
        offering = factories.OfferingFactory(customer=self.fixture.customer)
        offering.add_user(self.fixture.user, OfferingRole.MANAGER)
        resource = factories.ResourceFactory(project=self.project, offering=offering)

        # Act
        self.client.force_authenticate(self.fixture.owner)
        url = factories.ResourceFactory.get_list_url()
        response = self.client.get(
            url, {"service_manager_uuid": self.fixture.user.uuid.hex}
        )

        # Assert
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], resource.uuid.hex)

    def test_resource_contains_project_and_customer_data_after_project_deletion(self):
        expected_data = {
            "project_name": self.project.name,
            "project_uuid": self.project.uuid.hex,
            "project_description": self.project.description,
            "customer_name": self.project.customer.name,
            "customer_uuid": self.project.customer.uuid.hex,
        }

        self.project.delete()
        response_data = self.get_resource().data
        for key, value in expected_data.items():
            self.assertEqual(value, response_data[key])

    def test_username_is_fetched_for_current_user_and_offering(self):
        models.OfferingUser.objects.create(
            offering=self.offering, user=self.fixture.manager, username="alice"
        )
        response = self.get_resource(self.fixture.manager)
        self.assertEqual(response.data["username"], "alice")

    def test_resource_data_includes_order_in_progress(self):
        response = self.get_resource(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("order_in_progress", response.data)
        self.assertIsNone(response.data["order_in_progress"])

    def test_resource_data_includes_order_info_for_existing_one(self):
        models.Order.objects.create(
            project=self.project,
            resource=self.resource,
            state=OrderStates.EXECUTING,
            created_by=self.fixture.owner,
            offering=self.offering,
        )
        response = self.get_resource(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("order_in_progress", response.data)
        self.assertIsNotNone(response.data["order_in_progress"])

    def test_order_in_progress_includes_url(self):
        order = models.Order.objects.create(
            project=self.project,
            resource=self.resource,
            state=OrderStates.EXECUTING,
            created_by=self.fixture.owner,
            offering=self.offering,
        )
        response = self.get_resource(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        order_in_progress = response.data["order_in_progress"]
        self.assertIsNotNone(order_in_progress)
        self.assertIn("url", order_in_progress)
        self.assertTrue(
            order_in_progress["url"].endswith(f"marketplace-orders/{order.uuid.hex}/")
        )

    def test_creation_order_includes_url(self):
        order = models.Order.objects.create(
            project=self.project,
            resource=self.resource,
            state=OrderStates.ERRED,
            created_by=self.fixture.owner,
            offering=self.offering,
        )
        self.resource.set_state_erred()
        self.resource.save()
        response = self.get_resource(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        creation_order = response.data["creation_order"]
        self.assertIsNotNone(creation_order)
        self.assertIn("url", creation_order)
        self.assertTrue(
            creation_order["url"].endswith(f"marketplace-orders/{order.uuid.hex}/")
        )

    def test_resource_erred_creation_order_is_exposed(self):
        models.Order.objects.create(
            project=self.project,
            resource=self.resource,
            state=OrderStates.ERRED,
            created_by=self.fixture.owner,
            offering=self.offering,
        )

        self.resource.set_state_erred()
        self.resource.save()

        response = self.get_resource(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("creation_order", response.data)
        self.assertIsNotNone(response.data["creation_order"])

    def test_offering_backend_id_is_exposed(self):
        self.offering.backend_id = "external-offering-123"
        self.offering.save()
        response = self.get_resource()
        self.assertEqual(response.data["offering_backend_id"], "external-offering-123")

    def test_offering_backend_id_is_empty_by_default(self):
        response = self.get_resource()
        self.assertEqual(response.data["offering_backend_id"], "")


class ResourceSwitchPlanTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan1 = factories.PlanFactory()
        self.offering = self.plan1.offering
        self.plan2 = factories.PlanFactory(offering=self.offering)
        self.resource1 = models.Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan1,
            state=ResourceStates.OK,
        )
        self.resource2 = models.Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan2,
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.SWITCH_RESOURCE_PLAN)
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.SWITCH_RESOURCE_PLAN)
        ProjectRole.MANAGER.add_permission(PermissionEnum.SWITCH_RESOURCE_PLAN)
        # Switching a plan submits an order, so it needs order creation rights.
        for role in (CustomerRole.OWNER, ProjectRole.ADMIN, ProjectRole.MANAGER):
            role.add_permission(PermissionEnum.CREATE_ORDER)

    def switch_plan(self, user, resource, plan):
        self.client.force_authenticate(user)
        url = factories.ResourceFactory.get_url(resource, "switch_plan")
        payload = {"plan": factories.PlanFactory.get_public_url(plan)}
        return self.client.post(url, payload)

    def test_plan_switch_is_available_if_plan_limit_is_not_reached(self):
        # Arrange
        self.plan2.max_amount = 10
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_plan_switch_is_available_if_resource_is_terminated(self):
        # Arrange
        self.resource2.state = ResourceStates.TERMINATED
        self.resource2.save()

        self.plan2.max_amount = 1
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_plan_switch_is_not_available_if_plan_limit_has_been_reached(self):
        # Arrange
        self.plan2.max_amount = 1
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_plan_switch_is_not_available_if_plan_is_related_to_another_offering(self):
        # Act
        response = self.switch_plan(
            self.fixture.owner, self.resource1, factories.PlanFactory()
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_plan_switch_is_not_available_if_resource_is_not_OK(self):
        # Arrange
        self.resource1.state = ResourceStates.UPDATING
        self.resource1.save()

        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_plan_switch_is_not_available_if_plan_unit_is_different(self):
        # Arrange
        self.plan2.unit = common_mixins.UnitPriceMixin.Units.PER_DAY
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Billing period of new plan must match the old one.",
            response.data["plan"][0],
        )

    def test_order_is_created(self):
        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(
            models.Order.objects.filter(
                type=OrderTypes.UPDATE,
                plan=self.plan2,
                resource=self.resource1,
            ).exists()
        )

    def test_order_is_approved_implicitly_for_authorized_user(self):
        # Act
        response = self.switch_plan(self.fixture.staff, self.resource1, self.plan2)

        # Assert
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.state, OrderStates.EXECUTING)
        self.assertEqual(order.created_by, self.fixture.staff)

    def test_plan_switch_is_not_allowed_if_pending_order_for_resource_already_exists(
        self,
    ):
        # Arrange
        factories.OrderFactory(
            resource=self.resource1, state=OrderStates.PENDING_CONSUMER
        )

        # Act
        response = self.switch_plan(self.fixture.staff, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_plan_switching_is_not_available_for_blocked_organization(self):
        self.fixture.customer.blocked = True
        self.fixture.customer.save()
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("waldur_mastermind.marketplace.tasks.process_order")
    def test_order_has_been_approved_if_user_has_got_permissions(self, mock_task):
        # Arrange
        self.plan2.max_amount = 10
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.owner, self.resource1, self.plan2)

        # Assert
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_task.delay.assert_called_once_with(
            "marketplace.order:%s" % order.id, "core.user:%s" % self.fixture.owner.id
        )

    @mock.patch("waldur_mastermind.marketplace.views.tasks")
    def test_order_has_not_been_approved_if_user_has_not_got_permissions(
        self, mock_tasks
    ):
        # Arrange
        self.plan2.max_amount = 10
        self.plan2.save()

        # Act
        response = self.switch_plan(self.fixture.admin, self.resource1, self.plan2)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_tasks.process_order.delay.assert_not_called()


class ResourceRenewTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project

        # Create a prepaid offering and plan
        self.offering = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        self.component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="storage",
            is_prepaid=True,
            billing_type=BillingTypes.ONE_TIME,
        )
        self.plan = factories.PlanFactory(offering=self.offering)
        factories.PlanComponentFactory(
            plan=self.plan,
            component=self.component,
            price=Decimal("10.0"),  # Price is $10/GB/month for renewal calculations
        )

        # Create a resource to be renewed
        self.resource = factories.ResourceFactory(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            state=ResourceStates.OK,
            limits={"storage": 100},  # 100 GB
            end_date=timezone.now().date() + relativedelta(months=1),
        )

        # Create a non-prepaid resource for failure tests
        self.non_prepaid_resource = factories.ResourceFactory(
            project=self.project, state=models.Resource.States.OK
        )

        # Set permissions
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        ProjectRole.ADMIN.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        # Renewal submits an order, so it needs order creation rights too.
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.CREATE_ORDER)

    def renew_resource(self, user, resource, payload):
        self.client.force_authenticate(user)
        url = factories.ResourceFactory.get_url(resource, "renew")
        # Check if payload contains files, then use multipart format
        has_files = any(hasattr(value, "read") for value in payload.values())
        if has_files:
            # For multipart, we need to flatten nested data like limits
            flattened_payload = {}
            for key, value in payload.items():
                if key == "limits" and isinstance(value, dict):
                    # Convert limits dict to JSON string for multipart
                    import json

                    flattened_payload[key] = json.dumps(value)
                else:
                    flattened_payload[key] = value
            return self.client.post(url, flattened_payload, format="multipart")
        return self.client.post(url, payload)

    def test_user_can_renew_prepaid_resource(self):
        # Arrange
        payload = {"extension_months": 12}

        # Act
        response = self.renew_resource(self.fixture.owner, self.resource, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(
            models.Order.objects.filter(
                type=OrderTypes.UPDATE,
                resource=self.resource,
                attributes__action="renew",
            ).exists()
        )

    def test_renewal_fails_for_non_prepaid_resource(self):
        # Arrange
        payload = {"extension_months": 12}

        # Act
        response = self.renew_resource(
            self.fixture.owner, self.non_prepaid_resource, payload
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "This action is only available for prepaid resources.", str(response.data)
        )

    def test_renewal_fails_if_resource_is_not_in_stable_state(self):
        # Arrange
        self.resource.state = models.Resource.States.UPDATING
        self.resource.save()
        payload = {"extension_months": 12}

        # Act
        response = self.renew_resource(self.fixture.owner, self.resource, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @freeze_time("2024-06-01")
    def test_order_is_created_with_correct_data_and_cost(self):
        # Arrange
        self.resource.end_date = timezone.datetime.fromisoformat("2024-07-01").date()
        self.resource.save()

        payload = {
            "extension_months": 12,
            "limits": {"storage": 200},  # Upgrade from 100 to 200 GB
        }

        # Expected cost = price * limit * months = 10 * 200 * 12 = 24000
        expected_cost = Decimal("24000.0")
        expected_new_end_date = self.resource.end_date + relativedelta(months=12)

        # Act
        response = self.renew_resource(self.fixture.admin, self.resource, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.type, OrderTypes.UPDATE)
        self.assertEqual(order.resource, self.resource)
        self.assertEqual(order.plan, self.plan)

        # Verify cost
        self.assertEqual(order.cost, expected_cost)

        # Verify attributes
        self.assertEqual(order.attributes["action"], "renew")
        self.assertEqual(order.attributes["extension_months"], 12)
        self.assertEqual(
            order.attributes["new_end_date"], expected_new_end_date.isoformat()
        )
        self.assertEqual(order.limits["storage"], 200)

    def test_user_can_renew_with_request_comment(self):
        # Arrange
        payload = {
            "extension_months": 60,
            "request_comment": "Need extension for project completion",
        }

        # Act
        response = self.renew_resource(self.fixture.owner, self.resource, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.request_comment, "Need extension for project completion")

    def test_user_can_renew_with_attachment(self):
        # Arrange
        pdf_content = b"%PDF-1.4\n1 0 obj\n<<\n/Type /Catalog\n>>\nendobj\nxref\n0 1\n0000000000 65535 f \ntrailer\n<<\n/Size 1\n/Root 1 0 R\n>>\nstartxref\n9\n%%EOF"
        attachment = SimpleUploadedFile(
            "renewal_request.pdf", pdf_content, content_type="application/pdf"
        )

        payload = {"extension_months": 60, "attachment": attachment}

        # Act
        response = self.renew_resource(self.fixture.owner, self.resource, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertTrue(order.attachment)
        self.assertIn("renewal_request.pdf", order.attachment.name)

    def test_user_can_renew_with_both_comment_and_attachment(self):
        # Arrange
        pdf_content = b"%PDF-1.4\n1 0 obj\n<<\n/Type /Catalog\n>>\nendobj\nxref\n0 1\n0000000000 65535 f \ntrailer\n<<\n/Size 1\n/Root 1 0 R\n>>\nstartxref\n9\n%%EOF"
        attachment = SimpleUploadedFile(
            "renewal_documentation.pdf", pdf_content, content_type="application/pdf"
        )

        payload = {
            "extension_months": 12,
            "request_comment": "Annual renewal with increased storage",
            "attachment": attachment,
        }

        # Act
        response = self.renew_resource(self.fixture.owner, self.resource, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.request_comment, "Annual renewal with increased storage")
        self.assertTrue(order.attachment)
        self.assertIn("renewal_documentation.pdf", order.attachment.name)

    def test_renewal_validates_comment_length(self):
        # Arrange - comment longer than 255 characters
        long_comment = "x" * 256
        payload = {"extension_months": 60, "request_comment": long_comment}

        # Act
        response = self.renew_resource(self.fixture.owner, self.resource, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_renewal_allows_empty_comment(self):
        # Arrange
        payload = {"extension_months": 60, "request_comment": ""}

        # Act
        response = self.renew_resource(self.fixture.owner, self.resource, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.request_comment, "")


class ResourceRenewCostWithFactorTest(test.APITestCase):
    """Test that renewal cost calculation correctly accounts for component factor."""

    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project

        # Create offering with OpenStack.Tenant type so component_factors returns real values
        self.offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            type="OpenStack.Tenant",
        )
        self.storage_component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="storage",
            is_prepaid=True,
            billing_type=BillingTypes.ONE_TIME,
        )
        self.ram_component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="ram",
            is_prepaid=True,
            billing_type=BillingTypes.ONE_TIME,
        )
        self.cores_component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="cores",
            is_prepaid=True,
            billing_type=BillingTypes.ONE_TIME,
        )
        self.plan = factories.PlanFactory(offering=self.offering)
        factories.PlanComponentFactory(
            plan=self.plan,
            component=self.storage_component,
            price=Decimal("3.0"),  # €3 per GB per month
        )
        factories.PlanComponentFactory(
            plan=self.plan,
            component=self.ram_component,
            price=Decimal("2.0"),  # €2 per GB per month
        )
        factories.PlanComponentFactory(
            plan=self.plan,
            component=self.cores_component,
            price=Decimal("1.0"),  # €1 per core per month
        )

        # Limits stored in internal units: RAM=3072 MB (3 GB), Storage=125952 MB (123 GB)
        self.resource = factories.ResourceFactory(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            state=ResourceStates.OK,
            limits={"cores": 23, "ram": 3072, "storage": 125952},
            end_date=timezone.now().date() + relativedelta(months=1),
        )

    def test_get_renewal_cost_divides_by_factor(self):
        """Renewal cost should use display units (GB), not internal units (MB)."""
        cost = self.resource.get_renewal_cost(extension_months=1)
        # cores: 1 * 23 * 1 = 23
        # ram: 2 * (3072/1024) * 1 = 2 * 3 = 6
        # storage: 3 * (125952/1024) * 1 = 3 * 123 = 369
        # total = 398
        self.assertEqual(cost, Decimal("398.0"))

    def test_get_renewal_cost_with_new_limits(self):
        """Renewal cost with upgraded limits should also use display units."""
        cost = self.resource.get_renewal_cost(
            extension_months=6,
            new_limits={"cores": 46, "ram": 6144, "storage": 251904},
        )
        # cores: 1 * 46 * 6 = 276
        # ram: 2 * (6144/1024) * 6 = 2 * 6 * 6 = 72
        # storage: 3 * (251904/1024) * 6 = 3 * 246 * 6 = 4428
        # total = 4776
        self.assertEqual(cost, Decimal("4776.0"))

    def test_get_renewal_estimate_uses_display_units(self):
        """Estimate should show display units in component details."""
        estimate = self.resource.get_renewal_estimate(extension_months=1)
        components = {c["component_type"]: c for c in estimate["components"]}

        # Storage: current_limit and new_limit should be in GB, not MB
        self.assertEqual(components["storage"]["new_limit"], Decimal("123"))
        self.assertEqual(components["storage"]["current_limit"], Decimal("123"))
        self.assertEqual(components["ram"]["new_limit"], Decimal("3"))
        self.assertEqual(components["ram"]["current_limit"], Decimal("3"))
        # Cores have factor=1, so no conversion
        self.assertEqual(components["cores"]["new_limit"], Decimal("23"))


@ddt
class ResourceTerminateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan = factories.PlanFactory()
        self.offering = self.plan.offering
        self.resource = models.Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            state=ResourceStates.OK,
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.TERMINATE_RESOURCE)
        ProjectRole.ADMIN.add_permission(PermissionEnum.TERMINATE_RESOURCE)

    def terminate(self, user, attributes=None):
        attributes = attributes or {}
        self.client.force_authenticate(user)
        url = factories.ResourceFactory.get_url(self.resource, "terminate")
        if attributes:
            return self.client.post(url, {"attributes": attributes})
        else:
            return self.client.post(url)

    def terminate_by_provider(self, user):
        self.client.force_authenticate(user)
        url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, "terminate"
        )
        return self.client.post(url)

    @mock.patch(
        "waldur_mastermind.marketplace.tasks.notify_consumer_about_pending_order.delay"
    )
    def test_service_provider_can_terminate_resource(self, mocked_approve):
        # Arrange
        owner = UserFactory()
        self.offering.customer.add_user(owner, CustomerRole.OWNER)

        # Act
        response = self.terminate_by_provider(owner)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mocked_approve.assert_not_called()

    def test_order_is_created_when_user_submits_termination_request(self):
        # Act
        response = self.terminate(self.fixture.owner)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.project, self.project)

    @data(
        ResourceStates.CREATING,
        ResourceStates.UPDATING,
    )
    def test_termination_request_is_not_accepted_if_resource_is_not_ok_or_erred(
        self, state
    ):
        # Arrange
        self.resource.state = state
        self.resource.save()

        # Act
        response = self.terminate(self.fixture.owner)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_termination_request_is_not_accepted_if_resource_is_terminating_without_pending_order(
        self,
    ):
        self.resource.state = ResourceStates.TERMINATING
        self.resource.save()

        response = self.terminate(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @data(ResourceStates.OK, ResourceStates.ERRED)
    def test_termination_request_is_accepted_if_resource_is_ok_or_erred(self, state):
        # Arrange
        self.resource.state = state
        self.resource.save()

        # Act
        response = self.terminate(self.fixture.owner)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_order_is_approved_implicitly_for_authorized_user(self):
        # Act
        response = self.terminate(self.fixture.staff)

        # Assert
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.state, OrderStates.EXECUTING)
        self.assertEqual(order.created_by, self.fixture.staff)

    def test_order_is_approved_implicitly_when_purchase_order_is_required(self):
        # Regression: require_purchase_order_upload used to hold every terminate
        # order in PENDING_CONSUMER, and the terminate endpoint accepts no
        # attachment, so the order could only be unblocked by uploading a file
        # to it out of band. Termination is exempt from the requirement.
        self.offering.plugin_options = {"require_purchase_order_upload": True}
        self.offering.save()

        # Act
        response = self.terminate(self.fixture.staff)

        # Assert
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.state, OrderStates.EXECUTING)

    def test_plan_switch_is_not_allowed_if_pending_order_for_resource_already_exists(
        self,
    ):
        # Arrange
        factories.OrderFactory(
            resource=self.resource, state=OrderStates.PENDING_CONSUMER
        )

        # Act
        response = self.terminate(self.fixture.staff)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("waldur_mastermind.marketplace.tasks.process_order.delay")
    def test_owner_confirms_pending_terminate_order_on_terminate(
        self, mocked_process_order
    ):
        """Default offering (Support) skips provider review and project is active."""
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)

        order = factories.OrderFactory(
            resource=self.resource,
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            type=OrderTypes.TERMINATE,
            state=OrderStates.PENDING_CONSUMER,
            created_by=self.fixture.admin,
        )

        owner_response = self.terminate(self.fixture.owner)
        self.assertEqual(owner_response.status_code, status.HTTP_200_OK)
        self.assertEqual(owner_response.data["order_uuid"], order.uuid.hex)

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.EXECUTING)
        self.assertEqual(order.consumer_reviewed_by, self.fixture.owner)
        self.assertEqual(models.Order.objects.filter(resource=self.resource).count(), 1)

    @mock.patch("waldur_mastermind.marketplace.tasks.process_order.delay")
    def test_owner_confirms_pending_terminate_order_when_purchase_order_is_required(
        self, mocked_process_order
    ):
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.offering.plugin_options = {"require_purchase_order_upload": True}
        self.offering.save()

        order = factories.OrderFactory(
            resource=self.resource,
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            type=OrderTypes.TERMINATE,
            state=OrderStates.PENDING_CONSUMER,
            created_by=self.fixture.admin,
        )

        owner_response = self.terminate(self.fixture.owner)
        self.assertEqual(owner_response.status_code, status.HTTP_200_OK)

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.EXECUTING)
        self.assertEqual(order.consumer_reviewed_by, self.fixture.owner)

    @mock.patch(
        "waldur_mastermind.marketplace.tasks.notify_provider_about_pending_order.delay"
    )
    def test_owner_confirms_pending_terminate_order_moves_to_pending_provider(
        self, mocked_notify
    ):
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        offering = factories.OfferingFactory(
            customer=self.fixture.customer, type=BASIC_OFFERING
        )
        plan = factories.PlanFactory(offering=offering)
        self.resource.offering = offering
        self.resource.plan = plan
        self.resource.save()

        order = factories.OrderFactory(
            resource=self.resource,
            project=self.project,
            offering=offering,
            plan=plan,
            type=OrderTypes.TERMINATE,
            state=OrderStates.PENDING_CONSUMER,
            created_by=self.fixture.admin,
        )

        response = self.terminate(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["order_uuid"], order.uuid.hex)

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.PENDING_PROVIDER)
        self.assertEqual(order.consumer_reviewed_by, self.fixture.owner)
        mocked_notify.assert_called_once()

    def test_owner_confirms_pending_terminate_order_moves_to_pending_project(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.project.start_date = datetime.date(2030, 1, 1)
        self.project.save()

        order = factories.OrderFactory(
            resource=self.resource,
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            type=OrderTypes.TERMINATE,
            state=OrderStates.PENDING_CONSUMER,
            created_by=self.fixture.admin,
        )

        response = self.terminate(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["order_uuid"], order.uuid.hex)

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.PENDING_PROJECT)
        self.assertEqual(order.consumer_reviewed_by, self.fixture.owner)

    def test_user_without_approve_permission_cannot_terminate_with_pending_order(self):
        member = UserFactory()
        self.project.add_user(member, ProjectRole.MEMBER)
        ProjectRole.MEMBER.add_permission(PermissionEnum.TERMINATE_RESOURCE)
        ProjectRole.MEMBER.add_permission(PermissionEnum.LIST_RESOURCES)

        factories.OrderFactory(
            resource=self.resource,
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            type=OrderTypes.TERMINATE,
            state=OrderStates.PENDING_CONSUMER,
            created_by=self.fixture.admin,
        )

        response = self.terminate(member)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_resource_terminating_is_not_available_for_blocked_organization(self):
        self.fixture.customer.blocked = True
        self.fixture.customer.save()
        response = self.terminate(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_delete_options_can_be_passed_if_resource_is_terminated(self):
        # Act
        response = self.terminate(self.fixture.staff, {"param": True})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.project, self.project)
        self.assertTrue(order.attributes.get("param"))

    def test_user_can_terminate_resource_if_project_has_been_soft_deleted(self):
        self.project.is_removed = True
        self.project.save()
        response = self.terminate(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class PlanUsageTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan1 = factories.PlanFactory()
        self.offering = self.plan1.offering
        self.plan2 = factories.PlanFactory(offering=self.offering)

        factories.ResourceFactory.create_batch(
            3,
            project=self.project,
            offering=self.offering,
            plan=self.plan1,
            state=ResourceStates.OK,
        )

        factories.ResourceFactory.create_batch(
            2,
            project=self.project,
            offering=self.offering,
            plan=self.plan2,
            state=ResourceStates.OK,
        )

        factories.ResourceFactory.create_batch(
            2,
            project=self.project,
            offering=self.offering,
            plan=self.plan2,
            state=ResourceStates.TERMINATED,
        )

    def get_stats(self, data=None):
        self.client.force_authenticate(self.fixture.owner)
        url = factories.PlanFactory.get_provider_list_url("usage_stats")
        response = self.client.get(url, data)
        return response

    def test_count_plans_for_ok_resources(self):
        response = self.get_stats()
        self.assertEqual(response.data[0]["offering_uuid"], self.offering.uuid.hex)
        self.assertEqual(
            response.data[0]["customer_provider_uuid"], self.offering.customer.uuid.hex
        )
        self.assertEqual(response.data[0]["plan_uuid"], self.plan1.uuid.hex)
        self.assertEqual(response.data[0]["usage"], 3)

    def test_count_plans_for_terminated_resources(self):
        response = self.get_stats()
        self.assertEqual(response.data[1]["usage"], 2)

    def test_order_by_remaining_ascending(self):
        self.plan1.max_amount = 100
        self.plan1.save()

        self.plan2.max_amount = 10
        self.plan2.save()

        response = self.get_stats({"o": "remaining"})
        data = response.data

        self.assertEqual(data[0]["remaining"], 10 - 2)
        self.assertEqual(data[1]["remaining"], 100 - 3)

    def test_order_by_remaining_descending(self):
        self.plan1.max_amount = 100
        self.plan1.save()

        self.plan2.max_amount = 10
        self.plan2.save()

        response = self.get_stats({"o": "-remaining"})
        data = response.data

        self.assertEqual(data[0]["remaining"], 100 - 3)
        self.assertEqual(data[1]["remaining"], 10 - 2)

    def test_filter_plans_by_offering_uuid(self):
        plan = factories.PlanFactory()

        factories.ResourceFactory.create_batch(
            4,
            project=self.project,
            offering=plan.offering,
            plan=plan,
            state=ResourceStates.OK,
        )

        response = self.get_stats({"offering_uuid": plan.offering.uuid.hex})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["usage"], 4)
        self.assertEqual(response.data[0]["offering_uuid"], plan.offering.uuid.hex)

    def test_filter_plans_by_customer_provider_uuid(self):
        plan = factories.PlanFactory()

        factories.ResourceFactory.create_batch(
            4,
            project=self.project,
            offering=plan.offering,
            plan=plan,
            state=ResourceStates.OK,
        )

        response = self.get_stats(
            {"customer_provider_uuid": plan.offering.customer.uuid.hex}
        )
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["usage"], 4)
        self.assertEqual(
            response.data[0]["customer_provider_uuid"], plan.offering.customer.uuid.hex
        )


class ResourceCostEstimateTest(test.APITestCase):
    @override_config(
        WALDUR_SUPPORT_ENABLED=True,
        WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="basic",
    )
    def test_when_order_is_processed_cost_estimate_is_initialized(self):
        # Arrange
        fixture = fixtures.ProjectFixture()
        offering = factories.OfferingFactory(type=SUPPORT_OFFERING)
        plan = factories.PlanFactory(unit_price=10)

        order = factories.OrderFactory(
            offering=offering,
            plan=plan,
            attributes={"name": "item_name", "description": "Description"},
            state=OrderStates.EXECUTING,
        )

        # Act
        marketplace_utils.process_order(order, fixture.staff)

        # Assert
        order.refresh_from_db()
        self.assertEqual(order.resource.cost, plan.unit_price)

    def test_initialization_cost_is_added_to_cost_estimate_for_creation_request(self):
        # Arrange
        offering = factories.OfferingFactory(type=SUPPORT_OFFERING)
        one_time_offering_component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=BillingTypes.ONE_TIME,
            type="signup",
        )
        usage_offering_component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=BillingTypes.USAGE,
            type="cpu",
        )

        plan = factories.PlanFactory()
        factories.PlanComponentFactory(
            plan=plan, component=one_time_offering_component, price=100
        )
        factories.PlanComponentFactory(
            plan=plan, component=usage_offering_component, price=10
        )

        order = factories.OrderFactory(
            offering=offering,
            plan=plan,
        )
        order.init_cost()
        self.assertEqual(order.cost, 100)

    def test_when_plan_is_switched_cost_estimate_is_updated(self):
        # Arrange
        old_plan = factories.PlanFactory(unit_price=10)
        new_plan = factories.PlanFactory(unit_price=100)
        resource = factories.ResourceFactory(plan=old_plan)

        factories.OrderFactory(
            state=OrderStates.EXECUTING,
            type=OrderTypes.UPDATE,
            resource=resource,
            plan=new_plan,
        )

        # Act
        callbacks.resource_update_succeeded(resource)
        resource.refresh_from_db()

        # Assert
        self.assertEqual(resource.cost, new_plan.unit_price)

    def test_plan_switch_cost_is_added_to_cost_estimate_for_order(self):
        # Arrange
        offering = factories.OfferingFactory(type=SUPPORT_OFFERING)
        switch_offering_component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=BillingTypes.ON_PLAN_SWITCH,
            type="plan_switch",
        )
        usage_offering_component = factories.OfferingComponentFactory(
            offering=offering,
            billing_type=BillingTypes.USAGE,
            type="cpu",
        )

        plan = factories.PlanFactory()
        factories.PlanComponentFactory(
            plan=plan, component=switch_offering_component, price=50
        )
        factories.PlanComponentFactory(
            plan=plan, component=usage_offering_component, price=10
        )

        order = factories.OrderFactory(
            offering=offering,
            plan=plan,
            type=OrderTypes.UPDATE,
        )
        order.init_cost()
        self.assertEqual(order.cost, 50)


class ResourceUpdateLimitsTest(test.APITransactionTestCase):
    def setUp(self):
        plugins.manager.register(
            offering_type="TEST_TYPE",
            create_resource_processor=test_utils.TestCreateProcessor,
            update_resource_processor=test_utils.TestUpdateScopedProcessor,
            can_update_limits=True,
        )

        self.fixture = fixtures.ServiceFixture()
        self.resource = factories.ResourceFactory()
        self.resource.state = ResourceStates.OK
        self.resource.project.customer = self.fixture.customer
        self.resource.project.save()
        self.resource.limits = {"vcpu": 1}
        self.resource.save()
        self.resource.offering.type = "TEST_TYPE"
        self.resource.offering.save()
        factories.OfferingComponentFactory(
            offering=self.resource.offering,
            type="vcpu",
            billing_type=BillingTypes.LIMIT,
        )

        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        # A limit update submits an order, so it needs order creation rights too.
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)

    def update_limits(self, user, resource, limits=None):
        limits = limits or {"vcpu": 10}
        self.client.force_authenticate(user)
        url = factories.ResourceFactory.get_url(resource, "update_limits")
        payload = {"limits": limits}
        return self.client.post(url, payload)

    def test_create_update_limits_order(self):
        response = self.update_limits(self.fixture.owner, self.resource)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_update_limits_is_not_available_if_resource_is_not_OK(self):
        # Arrange
        self.resource.state = ResourceStates.UPDATING
        self.resource.save()

        # Act
        response = self.update_limits(self.fixture.owner, self.resource)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_order_is_created(self):
        # Act
        response = self.update_limits(self.fixture.owner, self.resource)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(
            models.Order.objects.filter(
                type=OrderTypes.UPDATE,
                resource=self.resource,
            ).exists()
        )

    def test_order_is_approved_implicitly_for_authorized_user(self):
        # Act
        response = self.update_limits(self.fixture.staff, self.resource)

        # Assert
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.state, OrderStates.EXECUTING)
        self.assertEqual(order.created_by, self.fixture.staff)

    def test_update_limits_is_not_allowed_if_pending_order_for_resource_already_exists(
        self,
    ):
        # Arrange
        factories.OrderFactory(
            resource=self.resource, state=OrderStates.PENDING_CONSUMER
        )

        # Act
        response = self.update_limits(self.fixture.owner, self.resource)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_limits_is_not_available_for_blocked_organization(self):
        customer = self.resource.project.customer
        customer.blocked = True
        customer.save()
        response = self.update_limits(self.fixture.owner, self.resource)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("waldur_mastermind.marketplace.tasks.process_order")
    def test_order_has_been_approved_if_user_has_got_permissions(self, mock_task):
        # Act
        response = self.update_limits(self.fixture.staff, self.resource)

        # Assert
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_task.delay.assert_called_once_with(
            "marketplace.order:%s" % order.id, "core.user:%s" % self.fixture.staff.id
        )

    @mock.patch("waldur_mastermind.marketplace.views.tasks")
    def test_order_has_not_been_approved_if_user_has_not_got_permissions(
        self, mock_tasks
    ):
        # Act
        response = self.update_limits(self.fixture.owner, self.resource)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_tasks.process_order.delay.assert_not_called()

    def test_update_limit_process(self):
        response = self.update_limits(self.fixture.staff, self.resource)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order = models.Order.objects.get(
            type=OrderTypes.UPDATE,
            state=OrderStates.EXECUTING,
            resource=self.resource,
        )
        marketplace_utils.process_order(order, self.fixture.staff)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits["vcpu"], 10)

    def test_impossible_set_the_same_limits(self):
        response = self.update_limits(self.fixture.owner, self.resource, {"vcpu": 1})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_limit_if_offering_is_paused(self):
        self.resource.offering.state = OfferingStates.PAUSED
        self.resource.offering.save()
        response = self.update_limits(self.fixture.owner, self.resource)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_update_limit_fails_if_offering_is_unavailable(self):
        self.resource.offering.state = OfferingStates.UNAVAILABLE
        self.resource.offering.save()
        response = self.update_limits(self.fixture.owner, self.resource)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_limits_validates_max_value(self):
        component = self.resource.offering.components.get(type="vcpu")
        component.max_value = 10
        component.save()
        response = self.update_limits(self.fixture.owner, self.resource, {"vcpu": 15})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_limits_validates_min_value(self):
        component = self.resource.offering.components.get(type="vcpu")
        component.min_value = 2
        component.save()
        response = self.update_limits(self.fixture.owner, self.resource, {"vcpu": 0})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_limits_succeeds_within_bounds(self):
        component = self.resource.offering.components.get(type="vcpu")
        component.min_value = 1
        component.max_value = 20
        component.save()
        response = self.update_limits(self.fixture.owner, self.resource, {"vcpu": 10})
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ResourceReallocateLimitsTest(test.APITestCase):
    def setUp(self):
        plugins.manager.register(
            offering_type="TEST_TYPE",
            create_resource_processor=test_utils.TestCreateProcessor,
            update_resource_processor=test_utils.TestUpdateScopedProcessor,
            can_update_limits=True,
        )

        self.fixture = fixtures.ServiceFixture()
        self.source_resource = factories.ResourceFactory()
        self.source_resource.state = ResourceStates.OK
        self.source_resource.project.customer = self.fixture.customer
        self.source_resource.project.save()
        self.source_resource.limits = {"vcpu": 10, "ram": 20}
        self.source_resource.save()
        self.source_resource.offering.type = "TEST_TYPE"
        self.source_resource.offering.save()

        factories.OfferingComponentFactory(
            offering=self.source_resource.offering,
            type="vcpu",
            billing_type=BillingTypes.LIMIT,
        )
        factories.OfferingComponentFactory(
            offering=self.source_resource.offering,
            type="ram",
            billing_type=BillingTypes.LIMIT,
        )
        factories.OfferingComponentFactory(
            offering=self.source_resource.offering,
            type="storage",
            billing_type=BillingTypes.LIMIT,
        )

        self.target_resource_1 = factories.ResourceFactory(
            offering=self.source_resource.offering,
            project=self.fixture.project,
        )
        self.target_resource_1.state = ResourceStates.OK
        self.target_resource_1.limits = {"vcpu": 2, "ram": 4}
        self.target_resource_1.save()

        self.target_resource_2 = factories.ResourceFactory(
            offering=self.source_resource.offering,
            project=self.fixture.project,
        )
        self.target_resource_2.state = ResourceStates.OK
        self.target_resource_2.limits = {"vcpu": 1, "ram": 2}
        self.target_resource_2.save()

        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)
        # Reallocation submits one order per affected resource.
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_ORDER)

    def reallocate_limits(self, user, source_resource, limits, targets):
        self.client.force_authenticate(user)
        url = factories.ResourceFactory.get_url(source_resource, "reallocate_limits")
        payload = {"limits": limits, "targets": targets}
        return self.client.post(url, payload)

    def test_create_reallocate_limits_orders(self):
        targets = [
            {
                "resource_uuid": self.target_resource_1.uuid.hex,
                "allocated_limits": {"vcpu": 3, "ram": 6},
            },
            {
                "resource_uuid": self.target_resource_2.uuid.hex,
                "allocated_limits": {"vcpu": 2, "ram": 4},
            },
        ]
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {"vcpu": 5, "ram": 10},
            targets,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("source_order_uuid", response.data)
        self.assertIn("target_order_uuids", response.data)
        self.assertEqual(len(response.data["target_order_uuids"]), 2)

    def test_reallocate_limits_creates_source_and_target_orders(self):
        targets = [
            {
                "resource_uuid": self.target_resource_1.uuid.hex,
                "allocated_limits": {"vcpu": 3, "ram": 6},
            }
        ]
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {"vcpu": 3, "ram": 6},
            targets,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check source order was created
        source_order = models.Order.objects.get(uuid=response.data["source_order_uuid"])
        self.assertEqual(source_order.type, OrderTypes.UPDATE)
        self.assertEqual(source_order.resource, self.source_resource)
        # Limits should be subtracted from source resource, 10 - 3 = 7 and 20 - 6 = 14
        self.assertEqual(source_order.limits["vcpu"], 7)
        self.assertEqual(source_order.limits["ram"], 14)

        target_order = models.Order.objects.get(
            uuid=response.data["target_order_uuids"][0]
        )
        self.assertEqual(target_order.type, OrderTypes.UPDATE)
        self.assertEqual(target_order.resource, self.target_resource_1)
        # Limits should be added to target resource, 2 + 3 = 5 and 4 + 6 = 10
        self.assertEqual(target_order.limits["vcpu"], 5)
        self.assertEqual(target_order.limits["ram"], 10)

    def test_reallocate_limits_is_not_available_if_source_resource_is_not_OK(self):
        self.source_resource.state = ResourceStates.UPDATING
        self.source_resource.save()

        targets = [
            {
                "resource_uuid": self.target_resource_1.uuid.hex,
                "allocated_limits": {"vcpu": 3, "ram": 6},
            }
        ]
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {"vcpu": 3, "ram": 6},
            targets,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reallocate_limits_is_not_available_if_target_resource_is_not_OK(self):
        self.target_resource_1.state = ResourceStates.UPDATING
        self.target_resource_1.save()

        targets = [
            {
                "resource_uuid": self.target_resource_1.uuid.hex,
                "allocated_limits": {"vcpu": 3, "ram": 6},
            }
        ]
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {"vcpu": 3, "ram": 6},
            targets,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reallocate_limits_is_not_allowed_if_pending_order_for_source_exists(self):
        factories.OrderFactory(
            resource=self.source_resource, state=OrderStates.PENDING_CONSUMER
        )

        targets = [
            {
                "resource_uuid": self.target_resource_1.uuid.hex,
                "allocated_limits": {"vcpu": 3, "ram": 6},
            }
        ]
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {"vcpu": 3, "ram": 6},
            targets,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reallocate_limits_is_not_allowed_if_pending_order_for_target_exists(self):
        factories.OrderFactory(
            resource=self.target_resource_1, state=OrderStates.PENDING_CONSUMER
        )

        targets = [
            {
                "resource_uuid": self.target_resource_1.uuid.hex,
                "allocated_limits": {"vcpu": 3, "ram": 6},
            }
        ]
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {"vcpu": 3, "ram": 6},
            targets,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reallocate_limits_validates_source_cannot_be_target(self):
        targets = [
            {
                "resource_uuid": self.source_resource.uuid.hex,
                "allocated_limits": {"vcpu": 3, "ram": 6},
            }
        ]
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {"vcpu": 3, "ram": 6},
            targets,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reallocate_limits_validates_component_exists_in_source(self):
        targets = [
            {
                "resource_uuid": self.target_resource_1.uuid.hex,
                "allocated_limits": {"vcpu": 3, "ram": 6},
            }
        ]
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {"invalid_component": 5},
            targets,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reallocate_limits_validates_cannot_exceed_source_limits(self):
        # Source resource has 10 vcpu and 20 ram but we are trying to reallocate 15 vcpu
        targets = [
            {
                "resource_uuid": self.target_resource_1.uuid.hex,
                "allocated_limits": {"vcpu": 3, "ram": 6},
            }
        ]
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {"vcpu": 15},
            targets,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Cannot reallocate 15 of vcpu. Source resource only has 10 available.",
            response.data,
        )

    def test_reallocate_limits_validates_total_allocated_does_not_exceed_reallocated(
        self,
    ):
        targets = [
            {
                "resource_uuid": self.target_resource_1.uuid.hex,
                "allocated_limits": {"vcpu": 3, "ram": 6},
            },
            {
                "resource_uuid": self.target_resource_2.uuid.hex,
                "allocated_limits": {"vcpu": 5, "ram": 10},
            },
        ]
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {"vcpu": 5, "ram": 10},
            targets,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "exceeds reallocated amount",
            str(response.data),
        )

    def test_reallocate_limits_validates_total_allocated_is_not_less_than_reallocated(
        self,
    ):
        # Reallocating 5 vcpu and 10 ram, but only allocating 3 vcpu and 6 ram total, should not be allowed
        targets = [
            {
                "resource_uuid": self.target_resource_1.uuid.hex,
                "allocated_limits": {"vcpu": 2, "ram": 4},
            },
            {
                "resource_uuid": self.target_resource_2.uuid.hex,
                "allocated_limits": {"vcpu": 1, "ram": 2},
            },
        ]
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {"vcpu": 5, "ram": 10},
            targets,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "is less than reallocated amount",
            str(response.data),
        )
        self.assertIn(
            "All allocated limits must sum to the reallocated amount",
            str(response.data),
        )

    def test_reallocate_limits_validates_target_has_same_components(self):
        # Create target with different components (storage instead of vcpu and ram)
        target_different = factories.ResourceFactory(
            offering=self.source_resource.offering,
            project=self.fixture.project,
        )
        target_different.state = ResourceStates.OK
        target_different.limits = {"storage": 100}
        target_different.save()

        targets = [
            {
                "resource_uuid": target_different.uuid.hex,
                "allocated_limits": {"vcpu": 3, "ram": 6},
            }
        ]
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {"vcpu": 3, "ram": 6},
            targets,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("must have the same components as the source", str(response.data))

    def test_reallocate_limits_validates_target_resource_exists(self):
        fake_uuid = uuid.uuid4()
        targets = [
            {
                "resource_uuid": fake_uuid,
                "allocated_limits": {"vcpu": 3, "ram": 6},
            }
        ]
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {"vcpu": 3, "ram": 6},
            targets,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Target resources with UUIDs %s do not exist." % fake_uuid,
            str(response.data),
        )

    def test_reallocate_limits_validates_target_same_offering(self):
        different_offering = factories.OfferingFactory()
        factories.OfferingComponentFactory(
            offering=different_offering,
            type="vcpu",
            billing_type=BillingTypes.LIMIT,
        )
        factories.OfferingComponentFactory(
            offering=different_offering,
            type="ram",
            billing_type=BillingTypes.LIMIT,
        )
        target_different_offering = factories.ResourceFactory(
            offering=different_offering,
            project=self.fixture.project,
        )
        target_different_offering.state = ResourceStates.OK
        target_different_offering.limits = {"vcpu": 2, "ram": 4}
        target_different_offering.save()

        targets = [
            {
                "resource_uuid": target_different_offering.uuid.hex,
                "allocated_limits": {"vcpu": 3, "ram": 6},
            }
        ]
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {"vcpu": 3, "ram": 6},
            targets,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "must be from the same offering",
            str(response.data),
        )

    def test_reallocate_limits_validates_empty_limits(self):
        targets = [
            {
                "resource_uuid": self.target_resource_1.uuid.hex,
                "allocated_limits": {"vcpu": 3, "ram": 6},
            }
        ]
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {},
            targets,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Limits to reallocate and targets cannot be empty.", str(response.data)
        )

    def test_reallocate_limits_validates_empty_targets(self):
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {"vcpu": 3, "ram": 6},
            [],
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Limits to reallocate and targets cannot be empty.", str(response.data)
        )

    def test_reallocate_limits_validates_positive_values(self):
        targets = [
            {
                "resource_uuid": self.target_resource_1.uuid.hex,
                "allocated_limits": {"vcpu": -1, "ram": 6},
            }
        ]
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {"vcpu": 3, "ram": 6},
            targets,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Ensure this value is greater than or equal to 1.", str(response.data)
        )

    def test_reallocate_limits_requires_permission_for_target_resource(self):
        other_project = ProjectFactory()
        target_other = factories.ResourceFactory(
            offering=self.source_resource.offering,
            project=other_project,
        )
        target_other.state = ResourceStates.OK
        target_other.limits = {"vcpu": 1, "ram": 2}
        target_other.save()

        targets = [
            {
                "resource_uuid": target_other.uuid.hex,
                "allocated_limits": {"vcpu": 3, "ram": 6},
            }
        ]
        response = self.reallocate_limits(
            self.fixture.owner,
            self.source_resource,
            {"vcpu": 3, "ram": 6},
            targets,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "User does not have permission to update target resource",
            str(response.data),
        )

    @mock.patch("waldur_mastermind.marketplace.tasks.process_order")
    def test_reallocate_limits_orders_are_created_in_transaction(self, mock_task):
        targets = [
            {
                "resource_uuid": self.target_resource_1.uuid.hex,
                "allocated_limits": {"vcpu": 3, "ram": 6},
            }
        ]
        response = self.reallocate_limits(
            self.fixture.staff,
            self.source_resource,
            {"vcpu": 3, "ram": 6},
            targets,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            models.Order.objects.filter(
                resource__in=[self.source_resource, self.target_resource_1],
                type=OrderTypes.UPDATE,
            ).count(),
            2,
        )


class ResourceMoveTest(test.APITestCase):
    def setUp(self):
        self.tenant = openstack_factories.TenantFactory()
        self.fixture = fixtures.ProjectFixture()
        self.new_project = ProjectFactory()
        self.project = self.fixture.project

        self.resource = factories.ResourceFactory(project=self.project)
        self.resource.scope = self.tenant
        self.resource.save()

        self.url = factories.ResourceFactory.get_url(
            self.resource, action="move_resource"
        )

    def get_response(self, role):
        self.client.force_authenticate(role)
        payload = {"project": {"url": ProjectFactory.get_url(self.new_project)}}
        return self.client.post(self.url, payload)

    def test_move_resource_rest(self):
        response = self.get_response(self.fixture.staff)

        self.resource.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.resource.project, self.new_project)

    def test_move_resource_is_not_possible_for_project_owner(self):
        response = self.get_response(self.fixture.owner)

        self.resource.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.resource.project, self.project)

    def test_move_resource_is_not_possible_when_new_customer_is_blocked(self):
        new_customer = self.new_project.customer
        new_customer.blocked = True
        new_customer.save()

        response = self.get_response(self.fixture.staff)

        self.resource.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.resource.project, self.project)

    def test_move_resource_exception_handling(self):
        start_invoice = invoices_factories.InvoiceFactory(
            customer=self.project.customer,
            year=2020,
            month=1,
            state=invoices_models.Invoice.States.PENDING,
        )
        invoices_factories.InvoiceItemFactory(
            invoice=start_invoice,
            project=self.project,
            resource=self.resource,
        )

        invoices_factories.InvoiceFactory(
            customer=self.new_project.customer,
            year=2020,
            month=1,
            state=invoices_models.Invoice.States.CREATED,
        )

        response = self.get_response(self.fixture.staff)

        self.resource.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            response.json(),
            {
                "error_message": "Resource moving is not possible, because invoice items moving is not possible."
            },
        )
        self.assertEqual(self.resource.project, self.project)


@ddt
class ResourceBackendIDTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, action="set_backend_id"
        )

        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_BACKEND_ID)

    def make_request(self, role):
        self.client.force_authenticate(role)
        payload = {"backend_id": "new_backend_id"}
        return self.client.post(self.url, payload)

    @data("staff", "offering_owner", "service_owner")
    def test_user_can_set_backend_id_of_resource(self, user):
        response = self.make_request(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.backend_id, "new_backend_id")

    @data("owner", "admin", "manager")
    def test_user_can_not_set_backend_id_of_resource(self, user):
        response = self.make_request(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class ResourceEffectiveIDTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, action="set_effective_id"
        )

        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_BACKEND_ID)

    def make_request(self, role):
        self.client.force_authenticate(role)
        payload = {"effective_id": "new_effective_id"}
        return self.client.post(self.url, payload)

    @data("staff", "offering_owner", "service_owner")
    def test_user_can_set_effective_id_of_resource(self, user):
        response = self.make_request(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.effective_id, "new_effective_id")

    @data("owner", "admin", "manager")
    def test_user_can_not_set_effective_id_of_resource(self, user):
        response = self.make_request(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class ResourceBackendMetadataTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, action="set_backend_metadata"
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_BACKEND_METADATA)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.SET_RESOURCE_BACKEND_METADATA
        )

    def make_request(self, role):
        self.client.force_authenticate(role)
        payload = {"backend_metadata": {"new_backend_field": "new_value"}}
        return self.client.post(self.url, payload)

    @data("staff", "offering_owner", "service_owner", "service_manager")
    def test_user_can_set_backend_metadata_of_resource(self, user):
        response = self.make_request(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.backend_metadata["new_backend_field"], "new_value"
        )

    @data("owner", "admin", "manager")
    def test_user_can_not_set_backend_id_of_resource(self, user):
        response = self.make_request(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class ResourceSetEndpointsTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, action="set_endpoints"
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_BACKEND_METADATA)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.SET_RESOURCE_BACKEND_METADATA
        )

    def make_request(self, role, endpoints=None):
        self.client.force_authenticate(role)
        payload = {
            "endpoints": endpoints
            if endpoints is not None
            else [
                {"name": "vLLM API", "url": "http://192.168.0.150:8000/v1"},
                {"name": "Chat playground", "url": "http://192.168.0.150:8000"},
            ]
        }
        return self.client.post(self.url, payload, format="json")

    @data("staff", "offering_owner", "service_owner", "service_manager")
    def test_user_can_set_endpoints_of_resource(self, user):
        response = self.make_request(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        endpoints = {e.name: e.url for e in self.resource.endpoints.all()}
        self.assertEqual(
            endpoints,
            {
                "vLLM API": "http://192.168.0.150:8000/v1",
                "Chat playground": "http://192.168.0.150:8000",
            },
        )

    def test_set_endpoints_replaces_previous_set(self):
        models.ResourceAccessEndpoint.objects.create(
            resource=self.resource, name="stale", url="http://old:1/"
        )
        response = self.make_request(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = set(self.resource.endpoints.values_list("name", flat=True))
        self.assertEqual(names, {"vLLM API", "Chat playground"})

    @data("owner", "admin", "manager")
    def test_user_can_not_set_endpoints_of_resource(self, user):
        response = self.make_request(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class ResourceSetStateErredTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, action="set_as_erred"
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_STATE)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.SET_RESOURCE_STATE)

    def make_request(self, role, payload=None):
        self.client.force_authenticate(role)
        if payload is not None:
            return self.client.post(self.url, payload)
        else:
            return self.client.post(self.url)

    @data("staff", "offering_owner", "service_owner", "service_manager")
    def test_user_can_set_resource_state_erred_without_body(self, user):
        response = self.make_request(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(ResourceStates.ERRED, self.resource.state)
        self.assertEqual("", self.resource.error_message)
        self.assertEqual("", self.resource.error_traceback)

    @data("staff", "offering_owner", "service_owner", "service_manager")
    def test_user_can_set_resource_state_erred_with_body(self, user):
        payload = {
            "error_message": "Error occurred",
            "error_traceback": "Error traceback",
        }
        response = self.make_request(getattr(self.fixture, user), payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(ResourceStates.ERRED, self.resource.state)
        self.assertEqual(payload["error_message"], self.resource.error_message)
        self.assertEqual(payload["error_traceback"], self.resource.error_traceback)

    @data("owner", "admin", "manager")
    def test_user_can_not_set_resource_state_erred(self, user):
        response = self.make_request(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class ResourceReportTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.resource = factories.ResourceFactory(project=self.project)
        self.url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, action="submit_report"
        )
        self.valid_report = [{"header": "Section header", "body": "Section body"}]

        service_manager = UserFactory()
        self.resource.offering.customer.add_user(
            service_manager, role=ServiceProviderRole.MANAGER
        )
        setattr(self.fixture, "service_manager", service_manager)

        service_owner = UserFactory()
        self.resource.offering.customer.add_user(service_owner, role=CustomerRole.OWNER)
        setattr(self.fixture, "service_owner", service_manager)
        CustomerRole.OWNER.add_permission(PermissionEnum.SUBMIT_RESOURCE_REPORT)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.SUBMIT_RESOURCE_REPORT
        )

    def make_request(self, role, payload):
        self.client.force_authenticate(role)
        return self.client.post(self.url, {"report": payload})

    @data("staff", "service_owner", "service_manager")
    def test_user_can_submit_report(self, user):
        response = self.make_request(getattr(self.fixture, user), self.valid_report)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.report, self.valid_report)

    def test_admin_can_not_submit_report(self):
        response = self.make_request(self.fixture.admin, self.valid_report)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_report_should_contain_at_least_one_section(self):
        response = self.make_request(self.fixture.staff, [])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_report_section_should_contain_header_and_body(self):
        response = self.make_request(self.fixture.staff, [1, 2])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ResourceDetailsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.offering.add_user(self.fixture.user, OfferingRole.MANAGER)
        service_provider = factories.ServiceProviderFactory(
            customer=self.fixture.customer
        )
        service_provider.add_user(self.fixture.user, ServiceProviderRole.MANAGER)
        self.resource = factories.ResourceFactory(
            project=self.project, offering=self.offering
        )

    def make_request(self):
        url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, action="details"
        )
        self.client.force_authenticate(self.fixture.user)
        return self.client.get(url)

    def test_resource_without_scope_returns_error_404(self):
        response = self.make_request()
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_resource_with_scope_returns_valid_resource_details(self):
        self.resource.scope = openstack_factories.TenantFactory(project=self.project)
        self.resource.save()
        response = self.make_request()
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ResourceTeamMembersMultiRoleTest(test.APITestCase):
    """team_members deduplicates by user, so a user holding several
    resource-scope roles must expose all of them via roles[]; the scalar
    role_name reflects only the first grant and is kept for
    backward compatibility."""

    def setUp(self) -> None:
        from django.contrib.contenttypes.models import ContentType

        from waldur_core.permissions.models import Role

        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.resource = factories.ResourceFactory(
            project=self.fixture.project, offering=self.offering
        )
        self.user = UserFactory()
        resource_ct = ContentType.objects.get_for_model(models.Resource)
        self.role_a = Role.objects.create(
            name="custom_role_a", content_type=resource_ct, is_system_role=False
        )
        self.role_b = Role.objects.create(
            name="custom_role_b", content_type=resource_ct, is_system_role=False
        )
        self.resource.add_user(self.user, self.role_a)
        self.resource.add_user(self.user, self.role_b)
        self.url = factories.ResourceFactory.get_url(
            self.resource, action="team_members"
        )

    def test_all_resource_scope_roles_are_returned(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"field": ["full_name", "role_name", "roles"]}
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        rows = [row for row in response.data if row["full_name"] == self.user.full_name]
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(
            {"custom_role_a", "custom_role_b"},
            {grant["role_name"] for grant in row["roles"]},
        )
        # The legacy scalar still carries one of the grants.
        self.assertIn(row["role_name"], {"custom_role_a", "custom_role_b"})


class ResourceGetTeamTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.service_owner = self.fixture.owner
        self.admin = self.fixture.admin

        self.resource = factories.ResourceFactory(
            project=self.project, offering=self.offering
        )

        self.provider_url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, action="team"
        )
        self.customer_url = factories.ResourceFactory.get_url(
            self.resource, action="team"
        )

    def test_service_owner_can_get_resource_team(self):
        self.client.force_authenticate(self.service_owner)

        response = self.client.get(self.provider_url)
        users = response.data
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(users))
        user = users[0]
        self.assertEqual(self.admin.full_name, user["full_name"])

    def test_user_can_get_resource_team_username(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get(self.customer_url)
        users = response.data
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(users))
        user = users[0]
        self.assertEqual(self.admin.username, user["username"])

    def test_has_consent_filter_excludes_users_without_consent(self):
        self.client.force_authenticate(self.service_owner)

        response = self.client.get(self.provider_url, {"has_consent": "true"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(0, len(response.data))

    def test_has_consent_filter_includes_users_with_active_consent(self):
        models.UserOfferingConsent.objects.create(
            user=self.admin,
            offering=self.offering,
            version="1.0",
        )
        self.client.force_authenticate(self.service_owner)

        response = self.client.get(self.provider_url, {"has_consent": "true"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(response.data))
        self.assertEqual(self.admin.full_name, response.data[0]["full_name"])

    def test_has_consent_filter_excludes_users_with_revoked_consent(self):
        models.UserOfferingConsent.objects.create(
            user=self.admin,
            offering=self.offering,
            version="1.0",
            revocation_date=timezone.now(),
        )
        self.client.force_authenticate(self.service_owner)

        response = self.client.get(self.provider_url, {"has_consent": "true"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(0, len(response.data))

    def test_without_has_consent_filter_returns_all_team_members(self):
        self.client.force_authenticate(self.service_owner)

        response = self.client.get(self.provider_url)
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(response.data))

    def test_consumer_team_returns_all_members_without_consent_filter(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get(self.customer_url)
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(response.data))


@override_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
class ResourceProviderTeamConsentEnforcementTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.service_owner = self.fixture.owner
        self.admin = self.fixture.admin
        self.staff = self.fixture.staff

        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Test ToS",
            version="1.0",
            is_active=True,
        )

        self.resource = factories.ResourceFactory(
            project=self.project, offering=self.offering
        )
        self.provider_url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, action="team"
        )
        self.customer_url = factories.ResourceFactory.get_url(
            self.resource, action="team"
        )

    def test_provider_team_hides_users_without_consent(self):
        self.client.force_authenticate(self.service_owner)

        response = self.client.get(self.provider_url)
        self.assertEqual(200, response.status_code)
        self.assertEqual(0, len(response.data))

    def test_provider_team_includes_users_with_active_consent(self):
        models.UserOfferingConsent.objects.create(
            user=self.admin,
            offering=self.offering,
            version="1.0",
        )
        self.client.force_authenticate(self.service_owner)

        response = self.client.get(self.provider_url)
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(response.data))
        self.assertEqual(self.admin.full_name, response.data[0]["full_name"])

    def test_provider_team_staff_sees_all_members_without_consent(self):
        self.client.force_authenticate(self.staff)

        response = self.client.get(self.provider_url)
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(response.data))

    def test_consumer_team_returns_all_members_when_enforcement_enabled(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get(self.customer_url)
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(response.data))

    def test_consumer_team_has_consent_filter_still_works(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get(self.customer_url, {"has_consent": "true"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(0, len(response.data))


class ResourceUsageLimitsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.user = self.fixture.staff

        self.resource = factories.ResourceFactory()
        self.resource.state = ResourceStates.OK
        self.resource.limits = {"cpu": 100}
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.resource.offering,
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.TOTAL,
        )
        self.resource.plan = factories.PlanFactory(offering=self.resource.offering)
        factories.PlanComponentFactory(
            component=self.offering_component,
            plan=self.resource.plan,
        )
        self.resource.save()

        self.url = factories.ResourceFactory.get_url(self.resource)

        factories.ComponentUsageFactory(
            resource=self.resource, component=self.offering_component, usage=10
        )
        new_date = datetime.datetime(
            year=datetime.date.today().year - 1, month=1, day=1
        )
        factories.ComponentUsageFactory(
            resource=self.resource,
            component=self.offering_component,
            usage=5,
            date=new_date,
            billing_period=month_start(new_date),
        )

    def test_if_limit_period_is_total(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["limit_usage"], {"cpu": 15})

    def test_if_limit_period_is_annual(self):
        self.offering_component.limit_period = LimitPeriods.ANNUAL
        self.offering_component.save()

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["limit_usage"], {"cpu": 10})

    def test_if_limit_period_is_month(self):
        """When limit_period is 'month' (default), limit_usage aggregates current month's
        ComponentUsage records (source of truth), not current_usages snapshot."""
        self.offering_component.limit_period = LimitPeriods.MONTH
        self.offering_component.save()

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Current month has usage=10, previous year has usage=5
        # With limit_period='month' → current month only → 10
        self.assertEqual(response.data["limit_usage"], {"cpu": 10})


@ddt
class ResourceForceTerminateTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.save()

        CustomerRole.OWNER.add_permission(PermissionEnum.TERMINATE_RESOURCE)
        ProjectRole.ADMIN.add_permission(PermissionEnum.TERMINATE_RESOURCE)

        self.url = factories.ResourceFactory.get_url(self.resource, "terminate")
        mock_patch = mock.patch(
            "waldur_mastermind.marketplace.utils.get_order_processor"
        )
        get_order_processor = mock_patch.start()

        class MockProcessor:
            def __init__(self, *args, **kwargs):
                return

            def process_order(self, *args, **kwargs):
                raise Exception("MockProcessor exception.")

            def validate_order(self, *args, **kwargs):
                return

        get_order_processor.return_value = MockProcessor

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    @data("staff")
    def test_user_can_force_terminate_resource(self, user):
        order_state, resource_state = self._terminate_order(user)
        self.assertEqual(order_state, OrderStates.ERRED)
        self.assertEqual(resource_state, ResourceStates.TERMINATED)

    @data(
        "owner",
        "admin",
        "service_owner",
    )
    def test_user_can_not_force_terminate_resource(self, user):
        order_state, resource_state = self._terminate_order(user)
        if user == "service_owner":
            # user connected to the resource with offering customer cannot get data from marketplace resource endpoint
            self.assertIsNone(order_state)
            self.assertEqual(resource_state, ResourceStates.OK)
        else:
            self.assertEqual(order_state, OrderStates.ERRED)
            self.assertEqual(resource_state, ResourceStates.ERRED)

    def _terminate_order(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(
            self.url, {"attributes": {"action": "force_destroy"}}
        )
        if response.status_code == 404:
            return None, self.resource.state
        order_uuid = response.data["order_uuid"]
        order = models.Order.objects.get(uuid=order_uuid)
        marketplace_utils.process_order(order, user)
        order.refresh_from_db()
        self.resource.refresh_from_db()
        return order.state, self.resource.state


@ddt
class ProviderResourcesTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.url = factories.ResourceFactory.get_provider_resource_url(self.resource)

    @data("staff", "provider_owner", "provider_manager")
    def test_provider_users_can_get_provider_resources(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("admin", "owner")
    def test_non_provider_users_can_not_get_provider_resources(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class ProviderResourceLimitsSetTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.component = self.fixture.offering_component
        self.component.billing_type = BillingTypes.LIMIT
        self.component.save()
        self.resource.limits = {"cpu": 10}
        self.resource.save()
        self.url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, "set_limits"
        )
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.SET_RESOURCE_STATE)

    def make_request(self, user, limits=None):
        self.client.force_authenticate(user)
        payload = {"limits": limits or {"cpu": 20}}
        return self.client.post(self.url, payload)

    @data(
        ("staff", status.HTTP_200_OK, 20),
        ("service_manager", status.HTTP_200_OK, 20),
        ("admin", status.HTTP_404_NOT_FOUND, 10),
    )
    @unpack
    def test_set_limits_permission(self, user_type, expected_status, expected_limit):
        user = getattr(self.fixture, user_type)
        response = self.make_request(user)
        self.assertEqual(response.status_code, expected_status)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits["cpu"], expected_limit)

    def test_set_limits_logs_changes(self):
        self.make_request(self.fixture.staff)
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="marketplace_resource_update_succeeded",
                message__contains="cpu",
            ).exists()
        )

    def test_keys_without_matching_component_are_rejected(self):
        # An agent may push a backend-native key that does not correspond to any
        # offering component. This is configuration skew that must surface, not
        # be persisted (an orphan key would later crash limit formatting on
        # resource update), so the whole request is rejected and limits stay put.
        response = self.make_request(
            self.fixture.staff, limits={"cpu": 20, "max_tokens": 1000000000}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("max_tokens", str(response.data))
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits, {"cpu": 10})


class ProviderResourceSetLimitsWithPeriodicPolicyTest(test.APITestCase):
    """When a SlurmPeriodicUsagePolicy is active on the offering, the
    set_limits action must not let backend echoes overwrite LIMIT-typed
    components — that's the trigger of the geometric inflation loop with
    the periodic policy task (see policy/models.py:calculate_slurm_settings)."""

    def setUp(self):
        from waldur_mastermind.invoices.models import PeriodMixin
        from waldur_mastermind.policy import models as policy_models

        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.component = self.fixture.offering_component
        self.component.type = "node"
        self.component.billing_type = BillingTypes.LIMIT
        self.component.save()
        self.resource.limits = {"node": 18800}
        self.resource.save()
        self.url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, "set_limits"
        )
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.SET_RESOURCE_STATE)

        self.policy = policy_models.SlurmPeriodicUsagePolicy.objects.create(
            scope=self.resource.offering,
            actions="notify_organization_owners",
            apply_to_all=True,
            grace_ratio=0.3,
            carryover_enabled=False,
            limit_type="GrpTRESMins",
            tres_billing_enabled=False,
            raw_usage_reset=True,
            period=PeriodMixin.Periods.MONTH_3,
        )

    def _post(self, limits):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(self.url, {"limits": limits})

    def test_inflated_echo_is_dropped_silently(self):
        """The site agent posts the grace-inflated value (18800 * 1.3 = 24440).
        The endpoint must accept the request (no 4xx — the agent has no
        retry/error handling for set_limits) but leave resource.limits
        untouched for LIMIT-typed components."""
        response = self._post({"node": int(18800 * 1.3)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits["node"], 18800)

    def test_noop_write_is_accepted(self):
        """Idempotent retries with the unchanged value should pass through
        cleanly — otherwise the agent's polling would generate noise."""
        response = self._post({"node": 18800})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits["node"], 18800)

    def test_change_allowed_when_no_policy(self):
        """Sanity check: removing the policy restores the original
        admin-override behavior of set_limits."""
        self.policy.delete()
        response = self._post({"node": 24440})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits["node"], 24440)

    def test_usage_component_changes_pass_through(self):
        """The gate only protects LIMIT-typed components. A USAGE-typed
        component on the same offering must still be writable."""
        from waldur_mastermind.marketplace.models import OfferingComponent

        OfferingComponent.objects.create(
            offering=self.resource.offering,
            type="cpu",
            name="CPU",
            billing_type=BillingTypes.USAGE,
        )
        self.resource.limits = {"node": 18800, "cpu": 100}
        self.resource.save()

        response = self._post({"node": int(18800 * 1.3), "cpu": 200})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        # node held (gated), cpu updated (not LIMIT-typed).
        self.assertEqual(self.resource.limits["node"], 18800)
        self.assertEqual(self.resource.limits["cpu"], 200)


@ddt
class ProviderUpdateOptionsDirectTest(test.APITestCase):
    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_OPTIONS)
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.CREATING
        self.resource.options = {"storage": 10}
        self.resource.save()

        # Set up offering with resource options metadata
        self.resource.offering.resource_options = {
            "options": {
                "storage": {"type": "integer", "label": "Storage"},
                "ram": {"type": "integer", "label": "RAM"},
            }
        }
        self.resource.offering.save()

        self.url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, action="update_options_direct"
        )

    @data("offering_owner")
    def test_service_provider_can_update_options_directly(self, user_type):
        user = getattr(self.fixture, user_type)
        self.client.force_authenticate(user)

        new_options = {"storage": 20, "ram": 4}
        response = self.client.post(self.url, {"options": new_options})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["status"], "Resource options have been updated directly."
        )

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options, new_options)

    def test_update_options_direct_works_during_transitional_states(self):
        # Test that update works even during CREATING state
        self.client.force_authenticate(self.fixture.offering_owner)

        new_options = {"storage": 15, "ram": 8}
        response = self.client.post(self.url, {"options": new_options})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options, new_options)

    def test_consumer_users_cannot_access_provider_endpoint(self):
        # Project admin should not be able to access provider endpoint
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.post(self.url, {"options": {"storage": 20}})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_invalid_options_are_rejected(self):
        self.client.force_authenticate(self.fixture.offering_owner)

        # Missing required metadata should cause validation error
        self.resource.offering.resource_options = {}
        self.resource.offering.save()

        response = self.client.post(self.url, {"options": {"storage": 20}})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ResourceSlugTemplateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.project = self.fixture.project
        self.plan = factories.PlanFactory()
        self.offering = self.plan.offering

    def _set_template(self, template):
        self.offering.plugin_options = {"resource_slug_template": template}
        self.offering.save()

    def _create_resource(self, name="My Resource"):
        return models.Resource.objects.create(
            project=self.project,
            offering=self.offering,
            plan=self.plan,
            name=name,
        )

    def test_default_slug_is_truncated_to_ten_chars(self):
        # Without a template the core SlugMixin behaviour applies: slugify(name)[:10].
        resource = self._create_resource(name="A very long resource name")
        self.assertEqual(resource.slug, "a-very-lon")

    def test_template_slug_is_not_truncated_to_ten(self):
        self._set_template("{customer_slug}-{project_slug}-{counter_padded}")
        resource = self._create_resource()
        expected = f"{self.project.customer.slug}-{self.project.slug}-001"
        self.assertEqual(resource.slug, expected)
        self.assertGreater(len(resource.slug), 10)

    def test_template_slug_uniqueness(self):
        self._set_template("{offering_slug}")
        first = self._create_resource()
        second = self._create_resource()
        third = self._create_resource()
        self.assertEqual(first.slug, self.offering.slug)
        self.assertEqual(second.slug, f"{self.offering.slug}-2")
        self.assertEqual(third.slug, f"{self.offering.slug}-3")

    def test_counter_template_sequential_is_clean(self):
        # A {counter} template yields a single, clean, incrementing counter.
        self._set_template("{project_slug}-{counter}")
        first = self._create_resource()
        second = self._create_resource()
        third = self._create_resource()
        self.assertEqual(first.slug, f"{self.project.slug}-1")
        self.assertEqual(second.slug, f"{self.project.slug}-2")
        self.assertEqual(third.slug, f"{self.project.slug}-3")

    def test_counter_template_avoids_double_counter_after_churn(self):
        # Regression for WAL-9925: deleting a lower-numbered resource drops the
        # count-based counter onto a slug that still exists. The counter must be
        # advanced (proj-3), NOT a second counter appended (proj-2-2).
        self._set_template("{project_slug}-{counter}")
        first = self._create_resource()
        second = self._create_resource()
        self.assertEqual(first.slug, f"{self.project.slug}-1")
        self.assertEqual(second.slug, f"{self.project.slug}-2")
        first.delete()  # count drops to 1, so the next counter re-renders proj-2
        third = self._create_resource()
        self.assertEqual(third.slug, f"{self.project.slug}-3")
        self.assertNotIn(f"{self.project.slug}-2-", third.slug)

    def test_invalid_template_falls_back_to_default(self):
        # An unknown placeholder must not crash slug generation at save time.
        self._set_template("{nonexistent}-suffix")
        resource = self._create_resource(name="Fallback Name")
        self.assertEqual(resource.slug, "fallback-n")

    def test_existing_resource_slug_not_regenerated_on_save(self):
        # Backward compatibility: an already-persisted slug is never rewritten,
        # even after a template is added to the offering.
        resource = self._create_resource(name="A very long resource name")
        original_slug = resource.slug
        self._set_template("{customer_slug}-{project_slug}-{counter_padded}")
        resource.name = "A completely different name"
        resource.save()
        resource.refresh_from_db()
        self.assertEqual(resource.slug, original_slug)

    def test_max_length_extends_default_name_slug(self):
        # The numeric knob lengthens the name-based slug beyond the 10-char default.
        self.offering.plugin_options = {"resource_slug_max_length": 20}
        self.offering.save()
        resource = self._create_resource(name="A very long resource name")
        self.assertEqual(resource.slug, "a-very-long-resource")
        self.assertGreater(len(resource.slug), 10)

    def test_max_length_clamped_to_ceiling(self):
        # Values above the ceiling are clamped to RESOURCE_SLUG_MAX_LENGTH.
        self.offering.plugin_options = {"resource_slug_max_length": 100}
        self.offering.save()
        resource = self._create_resource(
            name="This is an extremely long resource name exceeding forty chars"
        )
        self.assertLessEqual(len(resource.slug), models.RESOURCE_SLUG_MAX_LENGTH)
        self.assertGreater(len(resource.slug), 10)

    def test_template_takes_precedence_over_max_length(self):
        self.offering.plugin_options = {
            "resource_slug_template": "{offering_slug}",
            "resource_slug_max_length": 40,
        }
        self.offering.save()
        resource = self._create_resource(name="A very long resource name")
        self.assertEqual(resource.slug, self.offering.slug)


class ResourceSlugTemplateValidatorTest(test.APITestCase):
    def _validate(self, template):
        serializer = marketplace_serializers.LifecyclePluginOptionsSerializer(
            data={"resource_slug_template": template}
        )
        return serializer

    def test_valid_template_is_accepted(self):
        serializer = self._validate("{customer_slug}-{project_slug}-{counter_padded}")
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_unknown_placeholder_is_rejected(self):
        serializer = self._validate("{bogus}-{counter}")
        self.assertFalse(serializer.is_valid())
        self.assertIn("resource_slug_template", serializer.errors)

    def test_too_long_template_is_rejected(self):
        serializer = self._validate(
            "{customer_slug}-{project_slug}-{offering_slug}-{year}-{month}-extra-padding"
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("resource_slug_template", serializer.errors)

    def test_blank_template_is_accepted(self):
        serializer = self._validate("")
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def _validate_max_length(self, value):
        return marketplace_serializers.LifecyclePluginOptionsSerializer(
            data={"resource_slug_max_length": value}
        )

    def test_max_length_valid_value_is_accepted(self):
        serializer = self._validate_max_length(40)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_max_length_above_ceiling_is_rejected(self):
        serializer = self._validate_max_length(100)
        self.assertFalse(serializer.is_valid())
        self.assertIn("resource_slug_max_length", serializer.errors)

    def test_max_length_zero_is_rejected(self):
        serializer = self._validate_max_length(0)
        self.assertFalse(serializer.is_valid())
        self.assertIn("resource_slug_max_length", serializer.errors)
