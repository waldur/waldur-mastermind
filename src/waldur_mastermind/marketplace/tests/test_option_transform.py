import copy

from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import enums, models
from waldur_mastermind.marketplace.enums import (
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace.tests.test_order_crud import BaseOrderCreateTest

TRANSFORM = {"prefix": "acme-", "suffix": "-prod", "case": "lower"}

TRANSFORM_OPTIONS = {
    "order": ["bucket", "notes"],
    "options": {
        "bucket": {
            "type": "string",
            "label": "Bucket",
            "required": True,
            "pattern": "[A-Za-z]+",
            "transform": TRANSFORM,
        },
        "notes": {"type": "text", "label": "Notes"},
    },
}

TAKEN = "This value is already used by another resource of this offering."


class OfferingTransformSaveTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        factories.ServiceProviderFactory(customer=self.customer)
        self.client.force_authenticate(self.fixture.staff)

    def create_offering(self, options, field="options"):
        payload = {
            "name": "offering",
            "category": factories.CategoryFactory.get_url(),
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "type": enums.SUPPORT_OFFERING,
            field: options,
        }
        return self.client.post(
            factories.OfferingFactory.get_list_url(), payload, format="json"
        )

    def options_with(self, **bucket_fields):
        options = copy.deepcopy(TRANSFORM_OPTIONS)
        options["options"]["bucket"].update(bucket_fields)
        return options

    def saved_bucket(self, response, field="options"):
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        return getattr(offering, field)["options"]["bucket"]

    def test_transform_is_saved_and_returned(self):
        response = self.create_offering(TRANSFORM_OPTIONS)
        self.assertEqual(self.saved_bucket(response)["transform"], TRANSFORM)
        self.assertEqual(
            response.data["options"]["options"]["bucket"]["transform"], TRANSFORM
        )

    def test_transform_is_saved_for_resource_options(self):
        response = self.create_offering(TRANSFORM_OPTIONS, field="resource_options")
        bucket = self.saved_bucket(response, field="resource_options")
        self.assertEqual(bucket["transform"], TRANSFORM)

    def test_transform_on_non_string_option_is_rejected(self):
        options = self.options_with(type="integer")
        del options["options"]["bucket"]["pattern"]
        response = self.create_offering(options)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("transform", str(response.data))

    def test_unknown_case_is_rejected(self):
        response = self.create_offering(self.options_with(transform={"case": "title"}))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_too_long_prefix_is_rejected(self):
        response = self.create_offering(
            self.options_with(transform={"prefix": "a" * 101})
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_prefix_whitespace_is_kept(self):
        response = self.create_offering(self.options_with(transform={"prefix": "a "}))
        self.assertEqual(self.saved_bucket(response)["transform"], {"prefix": "a "})

    def test_blank_keys_are_dropped(self):
        response = self.create_offering(
            self.options_with(transform={"prefix": "acme-", "suffix": "", "case": ""})
        )
        self.assertEqual(self.saved_bucket(response)["transform"], {"prefix": "acme-"})

    def test_default_is_checked_against_the_pattern_without_affixes(self):
        response = self.create_offering(self.options_with(default="acme-logs-prod"))
        self.assertEqual(self.saved_bucket(response)["default"], "acme-logs-prod")

        response = self.create_offering(self.options_with(default="acme-lo-gs-prod"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("default", str(response.data))

    def test_cleared_transform_means_none(self):
        for cleared in (None, {}, {"prefix": "", "case": ""}):
            with self.subTest(cleared=cleared):
                response = self.create_offering(self.options_with(transform=cleared))
                self.assertNotIn("transform", self.saved_bucket(response))


class OrderTransformTest(BaseOrderCreateTest):
    def setUp(self):
        super().setUp()
        self.offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            options=copy.deepcopy(TRANSFORM_OPTIONS),
            resource_options={"options": {}, "order": []},
        )

    def order(self, attributes):
        return self.create_order(
            self.fixture.staff, self.offering, add_payload={"attributes": attributes}
        )

    def test_transformed_value_is_stored_on_order_and_resource(self):
        response = self.order({"bucket": "Backups", "notes": "Nightly"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        expected = {"bucket": "acme-backups-prod", "notes": "Nightly"}
        self.assertEqual(order.attributes, expected)
        self.assertEqual(order.resource.attributes, expected)

    def test_pattern_is_checked_before_the_transform(self):
        response = self.order({"bucket": "back-ups"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("bucket", response.data)

    def test_uniqueness_compares_transformed_values(self):
        self.offering.options["options"]["bucket"]["unique"] = True
        self.offering.save()
        response = self.order({"bucket": "Backups"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        for duplicate in ("backups", "acme-backups-prod"):
            with self.subTest(duplicate=duplicate):
                response = self.order({"bucket": duplicate})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(TAKEN, str(response.data["bucket"]))


class OrderUpdateTransformTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            customer=self.fixture.customer,
            options=copy.deepcopy(TRANSFORM_OPTIONS),
        )
        self.order = factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.manager,
            offering=self.offering,
            plan=factories.PlanFactory(offering=self.offering),
            state=OrderStates.PENDING_CONSUMER,
            attributes={"bucket": "acme-archive-prod"},
        )
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.admin)

    def update(self, attributes):
        return self.client.patch(
            factories.OrderFactory.get_url(self.order),
            {"attributes": attributes},
            format="json",
        )

    def test_resubmitted_stored_value_is_not_transformed_twice(self):
        # The order form is filled from the stored, already transformed value.
        response = self.update({"bucket": "acme-archive-prod", "notes": "changed"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(
            self.order.attributes, {"bucket": "acme-archive-prod", "notes": "changed"}
        )

    def test_new_value_is_transformed(self):
        response = self.update({"bucket": "Logs"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.attributes, {"bucket": "acme-logs-prod"})

    def test_edited_value_is_checked_against_the_pattern(self):
        response = self.update({"bucket": "back-ups"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.order.refresh_from_db()
        self.assertEqual(self.order.attributes, {"bucket": "acme-archive-prod"})


class ResourceOptionsTransformTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        resource_options = copy.deepcopy(TRANSFORM_OPTIONS)
        resource_options["options"]["bucket"]["unique"] = True
        self.fixture.offering.resource_options = resource_options
        self.fixture.offering.save()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.options = {"bucket": "acme-archive-prod"}
        self.resource.save()
        self.url = factories.ResourceFactory.get_url(self.resource, "update_options")
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_OPTIONS)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        self.client.force_authenticate(self.fixture.owner)

    def update(self, options):
        return self.client.post(self.url, {"options": options}, format="json")

    def test_new_value_is_stored_transformed(self):
        response = self.update({"bucket": "Logs"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options["bucket"], "acme-logs-prod")

    def test_resubmitted_stored_value_is_not_transformed_twice(self):
        response = self.update({"bucket": "acme-archive-prod", "notes": "changed"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.options, {"bucket": "acme-archive-prod", "notes": "changed"}
        )

    def test_uniqueness_compares_transformed_values(self):
        factories.ResourceFactory(
            offering=self.fixture.offering,
            state=ResourceStates.OK,
            options={"bucket": "acme-logs-prod"},
        )
        response = self.update({"bucket": "Logs"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(TAKEN, str(response.data))

    def test_update_order_carries_the_transformed_value(self):
        self.fixture.offering.plugin_options = {
            "create_orders_on_resource_option_change": True
        }
        self.fixture.offering.save()
        response = self.update({"bucket": "Logs"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.attributes["new_options"]["bucket"], "acme-logs-prod")


class OrderApproveByProviderTransformTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.offering.resource_options = copy.deepcopy(TRANSFORM_OPTIONS)
        self.fixture.offering.save()
        self.resource = self.fixture.resource
        self.resource.options = {"bucket": "acme-archive-prod"}
        self.resource.save()
        self.order = factories.OrderFactory(
            resource=self.resource,
            offering=self.fixture.offering,
            project=self.resource.project,
            type=OrderTypes.UPDATE,
            state=OrderStates.PENDING_PROVIDER,
            attributes={
                "old_options": dict(self.resource.options),
                "new_options": {"bucket": "acme-logs-prod"},
            },
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.offering_owner)
        self.url = factories.OrderFactory.get_url(self.order, "approve_by_provider")

    def approve(self, new_options):
        return self.client.post(
            self.url, {"attributes": {"new_options": new_options}}, format="json"
        )

    def test_provider_change_is_transformed(self):
        response = self.approve({"bucket": "Metrics"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(
            self.order.attributes["new_options"], {"bucket": "acme-metrics-prod"}
        )

    def test_stored_value_is_not_transformed_twice(self):
        response = self.approve({"bucket": "acme-logs-prod"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(
            self.order.attributes["new_options"], {"bucket": "acme-logs-prod"}
        )


class CreateOrderApproveByProviderTransformTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        options = copy.deepcopy(TRANSFORM_OPTIONS)
        options["options"]["bucket"]["unique"] = True
        self.fixture.offering.options = options
        self.fixture.offering.save()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.CREATING
        self.resource.attributes = {"bucket": "acme-archive-prod"}
        self.resource.save()
        self.order = factories.OrderFactory(
            resource=self.resource,
            offering=self.fixture.offering,
            project=self.resource.project,
            type=OrderTypes.CREATE,
            state=OrderStates.PENDING_PROVIDER,
            attributes={"bucket": "acme-archive-prod"},
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.offering_owner)
        self.url = factories.OrderFactory.get_url(self.order, "approve_by_provider")

    def approve(self, attributes):
        return self.client.post(self.url, {"attributes": attributes}, format="json")

    def test_provider_change_is_transformed(self):
        response = self.approve({"bucket": "Metrics"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.attributes["bucket"], "acme-metrics-prod")
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.attributes["bucket"], "acme-metrics-prod")

    def test_stored_value_is_not_transformed_twice(self):
        response = self.approve({"bucket": "acme-archive-prod", "notes": "Nightly"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(
            self.order.attributes, {"bucket": "acme-archive-prod", "notes": "Nightly"}
        )

    def test_provider_change_is_checked_against_the_pattern(self):
        response = self.approve({"bucket": "back-ups"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.order.refresh_from_db()
        self.assertEqual(self.order.attributes, {"bucket": "acme-archive-prod"})

    def test_uniqueness_compares_transformed_values(self):
        factories.ResourceFactory(
            offering=self.fixture.offering,
            state=ResourceStates.OK,
            attributes={"bucket": "acme-logs-prod"},
        )
        response = self.approve({"bucket": "Logs"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(TAKEN, str(response.data))
