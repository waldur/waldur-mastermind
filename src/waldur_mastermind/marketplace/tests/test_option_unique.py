import copy

from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import enums, models, utils
from waldur_mastermind.marketplace.enums import (
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace.tests.test_order_crud import BaseOrderCreateTest

UNIQUE_OPTIONS = {
    "order": ["custom_bucket", "bucket", "port", "notes"],
    "options": {
        "custom_bucket": {"type": "boolean", "label": "Custom bucket"},
        "bucket": {"type": "string", "label": "Bucket", "unique": True},
        "port": {"type": "integer", "label": "Port", "unique": True},
        "notes": {"type": "string", "label": "Notes"},
    },
}

TAKEN = "This value is already used by another resource of this offering."


class OfferingUniqueSaveTest(test.APITestCase):
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

    def test_flag_is_saved_and_returned(self):
        response = self.create_offering(UNIQUE_OPTIONS)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertTrue(offering.options["options"]["bucket"]["unique"])
        self.assertTrue(response.data["options"]["options"]["port"]["unique"])

    def test_flag_is_saved_for_resource_options(self):
        response = self.create_offering(UNIQUE_OPTIONS, field="resource_options")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_flag_on_unsupported_type_is_rejected(self):
        options = copy.deepcopy(UNIQUE_OPTIONS)
        options["options"]["custom_bucket"]["unique"] = True
        response = self.create_offering(options)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("options", response.data)


class OrderUniqueTest(BaseOrderCreateTest):
    def setUp(self):
        super().setUp()
        self.offering = self.create_offering()

    def create_offering(self, options=UNIQUE_OPTIONS, resource_options=None):
        return factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            options=copy.deepcopy(options),
            resource_options=copy.deepcopy(
                resource_options or {"options": {}, "order": []}
            ),
        )

    def order(self, attributes, offering=None):
        return self.create_order(
            self.fixture.staff,
            offering or self.offering,
            add_payload={"attributes": attributes},
        )

    def assert_taken(self, response, name):
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(name, response.data)
        self.assertIn(TAKEN, str(response.data[name]))

    def test_value_held_by_a_live_resource_is_rejected(self):
        response = self.order({"bucket": "backups"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        self.assert_taken(self.order({"bucket": "backups"}), "bucket")

    def test_different_value_is_accepted(self):
        self.order({"bucket": "backups"})
        response = self.order({"bucket": "archive"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_value_of_terminated_resource_is_free_again(self):
        response = self.order({"bucket": "backups"})
        resource = models.Order.objects.get(uuid=response.data["uuid"]).resource
        resource.state = ResourceStates.TERMINATED
        resource.save()

        response = self.order({"bucket": "backups"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_value_of_erred_resource_is_taken(self):
        # An erred resource may still exist in the backend, so it keeps its
        # values until it is terminated.
        factories.ResourceFactory(
            offering=self.offering,
            state=ResourceStates.ERRED,
            attributes={"bucket": "backups"},
        )
        self.assert_taken(self.order({"bucket": "backups"}), "bucket")

    def assert_value_freed_when_create_order_ends(self, from_state, to_state):
        # The order-state handler terminates the resource of a create order
        # that is rejected, canceled, or errs before the backend created
        # anything, which frees the value.
        resource = factories.ResourceFactory(
            offering=self.offering,
            state=ResourceStates.CREATING,
            backend_id="",
            attributes={"bucket": "backups"},
        )
        order = factories.OrderFactory(
            resource=resource,
            offering=self.offering,
            type=OrderTypes.CREATE,
            state=from_state,
            attributes={"bucket": "backups"},
        )
        self.assert_taken(self.order({"bucket": "backups"}), "bucket")

        order.state = to_state
        order.save()
        resource.refresh_from_db()
        self.assertEqual(resource.state, ResourceStates.TERMINATED)

        response = self.order({"bucket": "backups"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_value_of_create_order_erred_before_provisioning_is_free(self):
        self.assert_value_freed_when_create_order_ends(
            OrderStates.EXECUTING, OrderStates.ERRED
        )

    def test_value_of_rejected_create_order_is_free(self):
        self.assert_value_freed_when_create_order_ends(
            OrderStates.PENDING_PROVIDER, OrderStates.REJECTED
        )

    def test_value_of_canceled_create_order_is_free(self):
        self.assert_value_freed_when_create_order_ends(
            OrderStates.PENDING_CONSUMER, OrderStates.CANCELED
        )

    def test_integer_matches_whether_stored_as_number_or_string(self):
        factories.ResourceFactory(offering=self.offering, attributes={"port": "8080"})
        self.assert_taken(self.order({"port": 8080}), "port")

    def test_comparison_is_case_sensitive(self):
        self.order({"bucket": "backups"})
        response = self.order({"bucket": "Backups"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_same_value_in_another_offering_is_accepted(self):
        self.order({"bucket": "backups"})
        response = self.order({"bucket": "backups"}, offering=self.create_offering())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_options_without_the_flag_may_repeat(self):
        self.order({"notes": "same"})
        response = self.order({"notes": "same"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_omitted_values_are_not_checked(self):
        self.order({"notes": "first"})
        response = self.order({"notes": "second"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_hidden_option_is_not_checked(self):
        options = copy.deepcopy(UNIQUE_OPTIONS)
        options["options"]["bucket"]["visible_if"] = {
            "field": "custom_bucket",
            "values": [True],
        }
        offering = self.create_offering(options)
        self.order({"custom_bucket": True, "bucket": "backups"}, offering=offering)
        response = self.order(
            {"custom_bucket": False, "bucket": "backups"}, offering=offering
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_value_changed_on_a_pending_order_is_taken(self):
        # Editing a pending order changes only the order's attributes.
        order = factories.OrderFactory(
            offering=self.offering,
            state=OrderStates.PENDING_CONSUMER,
            attributes={"bucket": "old"},
        )
        order.attributes = {"bucket": "backups"}
        order.save()
        self.assert_taken(self.order({"bucket": "backups"}), "bucket")

    def test_unique_resource_option_is_checked(self):
        offering = self.create_offering(
            options={"options": {}, "order": []},
            resource_options={
                "order": ["namespace"],
                "options": {
                    "namespace": {
                        "type": "string",
                        "label": "Namespace",
                        "unique": True,
                    }
                },
            },
        )
        response = self.order({"namespace": "team-a"}, offering=offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assert_taken(
            self.order({"namespace": "team-a"}, offering=offering), "namespace"
        )


class OrderUpdateUniqueTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            customer=self.fixture.customer,
            options=copy.deepcopy(UNIQUE_OPTIONS),
        )
        factories.OrderFactory(
            offering=self.offering,
            attributes={"bucket": "backups"},
        )
        self.order = factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.manager,
            offering=self.offering,
            plan=factories.PlanFactory(offering=self.offering),
            state=OrderStates.PENDING_CONSUMER,
            attributes={"bucket": "archive"},
        )
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.admin)

    def update(self, attributes):
        return self.client.patch(
            factories.OrderFactory.get_url(self.order),
            {"attributes": attributes},
            format="json",
        )

    def test_changing_to_a_taken_value_is_rejected(self):
        response = self.update({"bucket": "backups"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.order.refresh_from_db()
        self.assertEqual(self.order.attributes, {"bucket": "archive"})

    def test_keeping_its_own_value_is_accepted(self):
        response = self.update({"bucket": "archive", "notes": "changed"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_value_copied_to_a_unique_resource_option_is_checked(self):
        # Editing a create order also sets its resource's options.
        self.offering.resource_options = copy.deepcopy(RESOURCE_OPTIONS)
        self.offering.save()
        factories.ResourceFactory(
            offering=self.offering,
            state=ResourceStates.OK,
            options={"namespace": "team-b"},
        )
        response = self.update({"bucket": "archive", "namespace": "team-b"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("namespace", response.data)

    def test_changing_to_a_free_value_is_accepted(self):
        response = self.update({"bucket": "logs"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.attributes, {"bucket": "logs"})

    def test_edit_frees_the_old_value(self):
        # The resource follows its edited order, so it stops holding the value
        # the order was placed with.
        response = self.update({"bucket": "logs"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.resource.refresh_from_db()
        self.assertEqual(self.order.resource.attributes, {"bucket": "logs"})
        utils.validate_unique_order_values(self.offering, {"bucket": "archive"})


RESOURCE_OPTIONS = {
    "order": ["namespace", "label"],
    "options": {
        "namespace": {"type": "string", "label": "Namespace", "unique": True},
        "label": {"type": "string", "label": "Label"},
    },
}


class ResourceOptionsUniqueTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.offering.resource_options = copy.deepcopy(RESOURCE_OPTIONS)
        self.fixture.offering.save()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.options = {"namespace": "team-a"}
        self.resource.save()
        self.other = factories.ResourceFactory(
            offering=self.fixture.offering,
            state=ResourceStates.OK,
            options={"namespace": "team-b"},
        )
        self.url = factories.ResourceFactory.get_url(self.resource, "update_options")
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_OPTIONS)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        self.client.force_authenticate(self.fixture.owner)

    def update(self, options):
        return self.client.post(self.url, {"options": options}, format="json")

    def test_value_of_another_resource_is_rejected(self):
        response = self.update({"namespace": "team-b"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(TAKEN, str(response.data))
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options, {"namespace": "team-a"})

    def test_own_value_is_accepted(self):
        response = self.update({"namespace": "team-a"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_free_value_is_stored(self):
        response = self.update({"namespace": "team-c"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options["namespace"], "team-c")

    def test_other_options_can_change_while_an_old_duplicate_exists(self):
        # Two resources may already share a value from before the flag was set.
        self.other.options = {"namespace": "team-a"}
        self.other.save()
        response = self.update({"label": "renamed"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_value_of_terminated_resource_is_free(self):
        self.other.state = ResourceStates.TERMINATED
        self.other.save()
        response = self.update({"namespace": "team-b"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_full_payload_with_an_unchanged_shared_value_is_accepted(self):
        # Clients may send the whole option set; only changed values count.
        self.other.options = {"namespace": "team-a"}
        self.other.save()
        response = self.update({"namespace": "team-a", "label": "renamed"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def pending_update_order(self, new_options, state=OrderStates.PENDING_PROVIDER):
        return factories.OrderFactory(
            resource=self.other,
            offering=self.fixture.offering,
            project=self.other.project,
            type=OrderTypes.UPDATE,
            state=state,
            attributes={
                "old_options": dict(self.other.options),
                "new_options": new_options,
            },
        )

    def test_value_of_a_pending_update_order_is_taken(self):
        # Its new_options are written only when it completes; until then the
        # value is reserved.
        self.pending_update_order({"namespace": "team-c"})
        response = self.update({"namespace": "team-c"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(TAKEN, str(response.data))

    def test_value_of_an_erred_update_order_is_free(self):
        self.pending_update_order({"namespace": "team-c"}, state=OrderStates.ERRED)
        response = self.update({"namespace": "team-c"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_update_order_is_not_created_for_a_value_another_one_reserves(self):
        self.fixture.offering.plugin_options = {
            "create_orders_on_resource_option_change": True
        }
        self.fixture.offering.save()
        self.pending_update_order({"namespace": "team-c"})

        response = self.update({"namespace": "team-c"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            models.Order.objects.filter(
                resource=self.resource, type=OrderTypes.UPDATE
            ).exists()
        )

        response = self.update({"namespace": "team-d"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order = models.Order.objects.get(resource=self.resource, type=OrderTypes.UPDATE)
        self.assertEqual(order.attributes["new_options"]["namespace"], "team-d")


class OrderApproveByProviderUniqueTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.offering.resource_options = copy.deepcopy(RESOURCE_OPTIONS)
        self.fixture.offering.save()
        self.resource = self.fixture.resource
        self.resource.options = {"namespace": "team-a"}
        self.resource.save()
        factories.ResourceFactory(
            offering=self.fixture.offering,
            state=ResourceStates.OK,
            options={"namespace": "team-b"},
        )
        self.order = factories.OrderFactory(
            resource=self.resource,
            offering=self.fixture.offering,
            project=self.resource.project,
            type=OrderTypes.UPDATE,
            state=OrderStates.PENDING_PROVIDER,
            attributes={
                "old_options": dict(self.resource.options),
                "new_options": {"namespace": "team-c"},
            },
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.offering_owner)
        self.url = factories.OrderFactory.get_url(self.order, "approve_by_provider")

    def approve(self, new_options):
        return self.client.post(
            self.url, {"attributes": {"new_options": new_options}}, format="json"
        )

    def test_taken_value_is_rejected(self):
        response = self.approve({"namespace": "team-b"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(TAKEN, str(response.data))

    def test_free_value_is_accepted(self):
        response = self.approve({"namespace": "team-c"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_unchanged_value_shared_from_before_does_not_block(self):
        # new_options carries the full option set; only changed keys count.
        factories.ResourceFactory(
            offering=self.fixture.offering,
            state=ResourceStates.OK,
            options={"namespace": "team-a"},
        )
        response = self.approve({"namespace": "team-a", "label": "renamed"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class OrderApproveCreateUniqueTest(test.APITestCase):
    # The provider may change a create order's values when approving it.
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.options = copy.deepcopy(UNIQUE_OPTIONS)
        self.offering.save()
        factories.ResourceFactory(
            offering=self.offering,
            state=ResourceStates.OK,
            attributes={"bucket": "backups"},
        )
        self.order = factories.OrderFactory(
            offering=self.offering,
            project=self.fixture.project,
            type=OrderTypes.CREATE,
            state=OrderStates.PENDING_PROVIDER,
            attributes={"bucket": "archive"},
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.client.force_authenticate(self.fixture.offering_owner)
        self.url = factories.OrderFactory.get_url(self.order, "approve_by_provider")

    def approve(self, attributes):
        return self.client.post(self.url, {"attributes": attributes}, format="json")

    def test_changing_to_a_taken_value_is_rejected(self):
        response = self.approve({"bucket": "backups"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(TAKEN, str(response.data))
        self.order.refresh_from_db()
        self.assertEqual(self.order.attributes, {"bucket": "archive"})

    def test_changing_to_a_free_value_is_accepted(self):
        response = self.approve({"bucket": "logs"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_approval_change_frees_the_old_value(self):
        response = self.approve({"bucket": "logs"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.order.resource.refresh_from_db()
        self.assertEqual(self.order.resource.attributes, {"bucket": "logs"})
        utils.validate_unique_order_values(self.offering, {"bucket": "archive"})

    def test_unchanged_value_is_not_checked(self):
        # Another resource sharing the ordered value from before the option was
        # made unique must not block approving an unrelated change.
        factories.ResourceFactory(
            offering=self.offering,
            state=ResourceStates.OK,
            attributes={"bucket": "archive"},
        )
        response = self.approve({"bucket": "archive", "notes": "checked"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class ResourceRestoreUniqueTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ServiceFixture()
        self.offering = factories.OfferingFactory(
            options=copy.deepcopy(UNIQUE_OPTIONS),
            resource_options=copy.deepcopy(RESOURCE_OPTIONS),
            plugin_options={"can_restore_resource": True},
        )
        self.resource = factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            state=ResourceStates.TERMINATED,
            attributes={"bucket": "backups"},
            options={"namespace": "team-a"},
        )
        self.url = factories.ResourceFactory.get_url(self.resource, "restore")
        self.client.force_authenticate(self.fixture.staff)

    def assert_not_restored(self, response, name):
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(name, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.TERMINATED)
        self.assertFalse(
            models.Order.objects.filter(
                resource=self.resource, type=OrderTypes.RESTORE
            ).exists()
        )

    def test_value_taken_since_termination_blocks_restore(self):
        factories.ResourceFactory(
            offering=self.offering,
            state=ResourceStates.OK,
            attributes={"bucket": "backups"},
        )
        self.assert_not_restored(self.client.post(self.url), "bucket")

    def test_resource_option_taken_since_termination_blocks_restore(self):
        factories.ResourceFactory(
            offering=self.offering,
            state=ResourceStates.OK,
            options={"namespace": "team-a"},
        )
        self.assert_not_restored(self.client.post(self.url), "namespace")

    def test_free_values_allow_restore(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.CREATING)
