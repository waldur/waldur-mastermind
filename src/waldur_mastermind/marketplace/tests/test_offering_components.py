from unittest.mock import patch

from rest_framework import status

from waldur_core.logging import models as event_models
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
)
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import (
    VMWARE_VM_OFFERING,
    BillingTypes,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.test_offerings import BaseOfferingUpdateTest
from waldur_mastermind.marketplace.views import ProviderOfferingViewSet


class OfferingComponentRemoveTest(BaseOfferingUpdateTest):
    def setUp(self):
        super().setUp()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)

    def remove_offering_component(self, component, role):
        url = factories.OfferingFactory.get_url(
            self.offering, "remove_offering_component"
        )
        self.client.force_authenticate(getattr(self.fixture, role))
        return self.client.post(url, {"uuid": component.uuid.hex})

    def test_it_should_not_be_possible_to_remove_builtin_components(self):
        # Arrange
        self.offering.type = VMWARE_VM_OFFERING
        self.offering.save()

        cpu_component = factories.OfferingComponentFactory(
            offering=self.offering, type="cpu"
        )

        # Act
        response = self.remove_offering_component(cpu_component, "owner")

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        cpu_component.refresh_from_db()

    def test_it_should_not_be_possible_to_remove_components_if_they_are_used(self):
        # Arrange
        component = factories.OfferingComponentFactory(offering=self.offering)
        factories.ResourceFactory(offering=self.offering)

        # Act
        response = self.remove_offering_component(component, "owner")

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_it_should_be_possible_to_remove_components_if_they_are_not_used(self):
        # Arrange
        component = factories.OfferingComponentFactory(offering=self.offering)

        # Act
        response = self.remove_offering_component(component, "owner")

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertEqual(0, self.offering.components.count())


class OfferingComponentCreateTest(BaseOfferingUpdateTest):
    def setUp(self):
        super().setUp()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)

    def create_offering_component(self, role, payload):
        url = factories.OfferingFactory.get_url(
            self.offering, "create_offering_component"
        )
        self.client.force_authenticate(getattr(self.fixture, role))
        return self.client.post(url, payload)

    def test_validation_of_offering_and_type(self):
        # Act
        response = self.create_offering_component(
            "owner",
            {
                "type": "cores",
                "name": "Cores",
                "measured_unit": "hours",
                "billing_type": "fixed",
            },
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Act
        response = self.create_offering_component(
            "owner",
            {
                "type": "cores",
                "name": "Cores",
                "measured_unit": "hours",
                "billing_type": "fixed",
            },
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_it_should_be_possible_to_create_new_components(self):
        # Act
        response = self.create_offering_component(
            "owner",
            {
                "type": "cores",
                "name": "Cores",
                "measured_unit": "hours",
                "billing_type": "fixed",
            },
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        component = self.offering.components.get()
        self.assertEqual("cores", component.type)
        self.assertEqual("hours", component.measured_unit)
        self.assertEqual(BillingTypes.FIXED, component.billing_type)


class OfferingComponentUpdateTest(BaseOfferingUpdateTest):
    def setUp(self):
        super().setUp()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)

    def update_offering_component(self, payload, role):
        url = factories.OfferingFactory.get_url(
            self.offering, "update_offering_component"
        )
        self.client.force_authenticate(getattr(self.fixture, role))
        return self.client.post(url, payload)

    def test_it_should_be_possible_to_update_existing_components(self):
        component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="cores",
            name="CPU",
            measured_unit="H",
        )
        # Act
        response = self.update_offering_component(
            {
                "type": "cores",
                "name": "Cores",
                "measured_unit": "hours",
                "billing_type": "fixed",
                "uuid": component.uuid.hex,
            },
            "owner",
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        component = self.offering.components.get()
        self.assertEqual("Cores", component.name)
        self.assertEqual("hours", component.measured_unit)
        self.assertEqual(BillingTypes.FIXED, component.billing_type)

    def test_update_event_includes_changes(self):
        """
        Test that the update event for offering component includes the changes in message.
        """
        # Arrange
        component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="cores",
            name="CPU",
        )

        # Act
        self.update_offering_component(
            {
                "type": "cores",
                "name": "NewName",
                "measured_unit": "hours",
                "billing_type": "fixed",
                "uuid": component.uuid.hex,
            },
            "owner",
        )

        # Assert
        event = event_models.Event.objects.filter(
            event_type="marketplace_offering_component_updated"
        ).first()
        self.assertIn("name: CPU -> NewName", event.message)
        self.assertIn("measured_unit:  -> hours", event.message)


class OfferingComponentPrepaidValidationTest(BaseOfferingUpdateTest):
    def setUp(self):
        # Call the parent setUp to initialize self.fixture, self.customer, self.offering etc.
        super().setUp()

        # Add the specific permission needed for these component tests
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)

        # Authenticate the client as the owner from the fixture
        self.client.force_authenticate(self.fixture.owner)

        # Define URLs for creating and updating components for this offering
        self.create_url = factories.OfferingFactory.get_url(
            self.offering, "create_offering_component"
        )
        self.update_url = factories.OfferingFactory.get_url(
            self.offering, "update_offering_component"
        )

    def test_create_valid_prepaid_with_overage_component(self):
        # Arrange: Create a valid overage component first
        overage_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=models.BillingTypes.USAGE,
            is_prepaid=False,
            type="storage-overage",
        )

        payload = {
            "type": "storage-prepaid",
            "name": "Prepaid Storage",
            "measured_unit": "GB",
            "billing_type": models.BillingTypes.ONE_TIME,
            "is_prepaid": True,
            "overage_component": overage_component.uuid.hex,
        }

        # Act
        response = self.client.post(self.create_url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        prepaid_component = models.OfferingComponent.objects.get(type="storage-prepaid")
        self.assertEqual(prepaid_component.overage_component, overage_component)

    def test_cannot_link_overage_to_non_prepaid_component(self):
        # Arrange: Create a component to be used for overage
        overage_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=models.BillingTypes.USAGE,
            type="storage-overage",
        )

        payload = {
            "type": "storage-standard",
            "name": "Standard Storage",
            "measured_unit": "GB",
            "billing_type": models.BillingTypes.USAGE,
            "is_prepaid": False,  # This is the key part of the test
            "overage_component": overage_component.uuid.hex,
        }

        # Act
        response = self.client.post(self.create_url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("overage_component", response.data)
        self.assertIn(
            "can only be specified for prepaid components",
            response.data["overage_component"][0],
        )

    def test_cannot_link_prepaid_to_another_prepaid_component(self):
        # Arrange: Create a would-be overage component that is also prepaid (invalid)
        invalid_overage_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=models.BillingTypes.ONE_TIME,
            is_prepaid=True,
            type="storage-overage-prepaid",
        )

        payload = {
            "type": "storage-main-prepaid",
            "name": "Main Prepaid Storage",
            "measured_unit": "GB",
            "billing_type": models.BillingTypes.ONE_TIME,
            "is_prepaid": True,
            "overage_component": invalid_overage_component.uuid.hex,
        }

        # Act
        response = self.client.post(self.create_url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("overage_component", response.data)
        self.assertIn(
            "cannot be a prepaid component itself",
            response.data["overage_component"][0],
        )

    def test_cannot_link_to_overage_component_with_invalid_billing_type(self):
        # Arrange: Create an overage component with a FIXED billing type (invalid)
        invalid_overage_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=models.BillingTypes.FIXED,  # Invalid type for overage
            is_prepaid=False,
            type="storage-overage-fixed",
        )

        payload = {
            "type": "storage-main-prepaid",
            "name": "Main Prepaid Storage",
            "measured_unit": "GB",
            "billing_type": models.BillingTypes.ONE_TIME,
            "is_prepaid": True,
            "overage_component": invalid_overage_component.uuid.hex,
        }

        # Act
        response = self.client.post(self.create_url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("overage_component", response.data)
        self.assertIn(
            "must have a billing type of 'usage'", response.data["overage_component"][0]
        )

    def test_update_non_prepaid_to_link_overage_without_setting_is_prepaid_fails(self):
        # Arrange
        component_to_update = factories.OfferingComponentFactory(
            offering=self.offering, is_prepaid=False, type="to-update"
        )
        overage_component = factories.OfferingComponentFactory(
            offering=self.offering,
            is_prepaid=False,
            billing_type=models.BillingTypes.USAGE,
            type="overage",
        )

        payload = {
            "uuid": component_to_update.uuid.hex,
            "overage_component": overage_component.uuid.hex,
            # 'is_prepaid' is omitted, so it should default to its current value (False)
        }

        # Act
        response = self.client.post(self.update_url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("overage_component", response.data)

    def test_update_non_prepaid_to_prepaid_with_overage_succeeds(self):
        # Arrange
        component_to_update = factories.OfferingComponentFactory(
            offering=self.offering, is_prepaid=False, type="to-update"
        )
        overage_component = factories.OfferingComponentFactory(
            offering=self.offering,
            is_prepaid=False,
            billing_type=models.BillingTypes.USAGE,
            type="overage",
        )

        payload = {
            "uuid": component_to_update.uuid.hex,
            "overage_component": overage_component.uuid.hex,
            "is_prepaid": True,  # Explicitly setting it to prepaid
        }

        # Act
        response = self.client.post(self.update_url, payload)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        component_to_update.refresh_from_db()
        self.assertTrue(component_to_update.is_prepaid)
        self.assertEqual(component_to_update.overage_component, overage_component)

    def test_update_non_prepaid_component_with_null_prepaid_fields_succeeds(self):
        # Regression for WAL-9908: frontend sends null for prepaid/renewal duration
        # fields on non-prepaid components; null means "no constraint" and must be
        # accepted instead of rejected as a "set" field.
        component_to_update = factories.OfferingComponentFactory(
            offering=self.offering, is_prepaid=False, type="to-update", name="old"
        )

        payload = {
            "uuid": component_to_update.uuid.hex,
            "name": "updated",
            "is_prepaid": False,
            "min_prepaid_duration": None,
            "max_prepaid_duration": None,
            "prepaid_duration_step": None,
            "min_renewal_duration": None,
            "max_renewal_duration": None,
            "renewal_duration_step": None,
        }

        response = self.client.post(self.update_url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        component_to_update.refresh_from_db()
        self.assertEqual(component_to_update.name, "updated")

    def test_update_non_prepaid_component_with_set_prepaid_field_fails(self):
        component_to_update = factories.OfferingComponentFactory(
            offering=self.offering, is_prepaid=False, type="to-update"
        )

        payload = {
            "uuid": component_to_update.uuid.hex,
            "is_prepaid": False,
            "min_prepaid_duration": 3,
        }

        response = self.client.post(self.update_url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("min_prepaid_duration", response.data)


class OfferingComponentMigrationTest(BaseOfferingUpdateTest):
    """Test automatic migration of connected objects when component type changes."""

    def setUp(self):
        super().setUp()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)

        # Create a component with old type
        self.component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="GPU-hour",
            name="GPU Resources",
            billing_type=BillingTypes.LIMIT,
        )

        # Create resources with old component limits
        self.resource1 = factories.ResourceFactory(
            offering=self.offering, limits={"GPU-hour": 1000}
        )
        self.resource2 = factories.ResourceFactory(
            offering=self.offering, limits={"GPU-hour": 2000, "cpu": 4}
        )

        # Create invoice items with old component type
        self.invoice_item1 = self._create_invoice_item(self.resource1, "GPU-hour", 500)
        self.invoice_item2 = self._create_invoice_item(self.resource2, "GPU-hour", 750)

    def _create_invoice_item(self, resource, component_type, quantity):
        """Helper to create invoice item with component type in details."""
        from waldur_mastermind.invoices.tests import factories as invoice_factories

        return invoice_factories.InvoiceItemFactory(
            resource=resource,
            quantity=quantity,
            details={
                "offering_component_type": component_type,
                "offering_component_name": "GPU Resources",
            },
        )

    def update_offering_component(self, payload, role="owner"):
        """Helper method to update offering component."""
        url = factories.OfferingFactory.get_url(
            self.offering, "update_offering_component"
        )
        self.client.force_authenticate(getattr(self.fixture, role))
        return self.client.post(url, payload)

    def test_component_type_change_migrates_resource_limits(self):
        """Test that changing component type updates resource limits."""
        # Act - Change component type from "GPU-hour" to "gres/gpu"
        response = self.update_offering_component(
            {
                "uuid": self.component.uuid.hex,
                "type": "gres/gpu",
                "name": "GPU Resources",
                "billing_type": BillingTypes.LIMIT,
            }
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check component was updated
        self.component.refresh_from_db()
        self.assertEqual(self.component.type, "gres/gpu")

        # Check resource limits were migrated
        self.resource1.refresh_from_db()
        self.resource2.refresh_from_db()

        self.assertNotIn("GPU-hour", self.resource1.limits)
        self.assertIn("gres/gpu", self.resource1.limits)
        self.assertEqual(self.resource1.limits["gres/gpu"], 1000)

        self.assertNotIn("GPU-hour", self.resource2.limits)
        self.assertIn("gres/gpu", self.resource2.limits)
        self.assertEqual(self.resource2.limits["gres/gpu"], 2000)
        # Other limits should remain unchanged
        self.assertEqual(self.resource2.limits["cpu"], 4)

    def test_component_type_change_migrates_invoice_items(self):
        """Test that changing component type updates invoice item details."""
        # Act - Change component type
        response = self.update_offering_component(
            {
                "uuid": self.component.uuid.hex,
                "type": "gres/gpu",
                "billing_type": BillingTypes.LIMIT,
            }
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check invoice items were updated
        self.invoice_item1.refresh_from_db()
        self.invoice_item2.refresh_from_db()

        self.assertEqual(
            self.invoice_item1.details["offering_component_type"], "gres/gpu"
        )
        self.assertEqual(
            self.invoice_item2.details["offering_component_type"], "gres/gpu"
        )

        # Other details should remain unchanged
        self.assertEqual(
            self.invoice_item1.details["offering_component_name"], "GPU Resources"
        )

    def test_no_migration_when_type_unchanged(self):
        """Test that no migration occurs when component type is not changed."""
        # Store original values
        original_limits_1 = self.resource1.limits.copy()
        original_limits_2 = self.resource2.limits.copy()
        original_details_1 = self.invoice_item1.details.copy()

        # Act - Update component without changing type
        response = self.update_offering_component(
            {
                "uuid": self.component.uuid.hex,
                "type": "GPU-hour",  # Same type
                "name": "Updated GPU Name",
                "billing_type": BillingTypes.LIMIT,
            }
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check component name was updated but type unchanged
        self.component.refresh_from_db()
        self.assertEqual(self.component.type, "GPU-hour")
        self.assertEqual(self.component.name, "Updated GPU Name")

        # Check no migration occurred - limits and invoice details unchanged
        self.resource1.refresh_from_db()
        self.resource2.refresh_from_db()
        self.invoice_item1.refresh_from_db()

        self.assertEqual(self.resource1.limits, original_limits_1)
        self.assertEqual(self.resource2.limits, original_limits_2)
        self.assertEqual(self.invoice_item1.details, original_details_1)

    def test_migration_scoped_to_offering(self):
        """Test that migration only affects resources of the specific offering."""
        # Create another offering with same component type
        other_offering = factories.OfferingFactory()
        factories.OfferingComponentFactory(offering=other_offering, type="GPU-hour")
        other_resource = factories.ResourceFactory(
            offering=other_offering, limits={"GPU-hour": 3000}
        )

        # Act - Update component in original offering
        response = self.update_offering_component(
            {
                "uuid": self.component.uuid.hex,
                "type": "gres/gpu",
                "billing_type": BillingTypes.LIMIT,
            }
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check other offering's resources are not affected
        other_resource.refresh_from_db()
        self.assertIn("GPU-hour", other_resource.limits)
        self.assertNotIn("gres/gpu", other_resource.limits)
        self.assertEqual(other_resource.limits["GPU-hour"], 3000)

    def test_migration_with_missing_component_in_limits(self):
        """Test migration when some resources don't have the old component in limits."""
        # Create resource without the old component
        resource_without_component = factories.ResourceFactory(
            offering=self.offering, limits={"cpu": 2, "memory": 8}
        )

        # Act
        response = self.update_offering_component(
            {
                "uuid": self.component.uuid.hex,
                "type": "gres/gpu",
                "billing_type": BillingTypes.LIMIT,
            }
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Resource without old component should be unchanged
        resource_without_component.refresh_from_db()
        self.assertEqual(resource_without_component.limits, {"cpu": 2, "memory": 8})

        # Resources with old component should be migrated
        self.resource1.refresh_from_db()
        self.assertIn("gres/gpu", self.resource1.limits)
        self.assertNotIn("GPU-hour", self.resource1.limits)

    @patch("waldur_mastermind.marketplace.views.logger")
    def test_migration_logging(self, mock_logger):
        """Test that migration operations are properly logged."""
        # Act
        response = self.update_offering_component(
            {
                "uuid": self.component.uuid.hex,
                "type": "gres/gpu",
                "billing_type": BillingTypes.LIMIT,
            }
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check logging calls
        mock_logger.info.assert_any_call(
            "Component type change detected: GPU-hour -> gres/gpu"
        )
        mock_logger.info.assert_any_call(
            "Successfully migrated component GPU-hour -> gres/gpu"
        )

        # Check resource update logging
        self.assertTrue(
            any(
                "Updated Resource" in str(call)
                and "GPU-hour=1000 -> gres/gpu=1000" in str(call)
                for call in mock_logger.info.call_args_list
            )
        )

        # Check invoice item update logging
        self.assertTrue(
            any(
                "Updated InvoiceItem" in str(call)
                and "GPU-hour -> gres/gpu" in str(call)
                for call in mock_logger.info.call_args_list
            )
        )

        # Check migration summary
        self.assertTrue(
            any(
                "Migration summary: Updated 2 resource limits, 2 invoice items"
                in str(call)
                for call in mock_logger.info.call_args_list
            )
        )

    def test_transaction_rollback_on_error(self):
        """Test that migration is rolled back on error."""
        with patch.object(
            ProviderOfferingViewSet, "_migrate_component_connected_objects"
        ) as mock_migrate:
            # Simulate error during migration
            mock_migrate.side_effect = Exception("Database error")

            # Act
            response = self.update_offering_component(
                {
                    "uuid": self.component.uuid.hex,
                    "type": "gres/gpu",
                    "billing_type": BillingTypes.LIMIT,
                }
            )

            # Assert - Should return error
            self.assertEqual(
                response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR
            )

            # Check rollback occurred - component type should be unchanged
            self.component.refresh_from_db()
            self.assertEqual(self.component.type, "GPU-hour")

            # Resource limits should be unchanged
            self.resource1.refresh_from_db()
            self.assertEqual(self.resource1.limits, {"GPU-hour": 1000})

    def test_component_type_uniqueness_validation(self):
        """Test that component type must be unique within offering."""
        # Create another component with a different type
        factories.OfferingComponentFactory(
            offering=self.offering, type="cpu", name="CPU Resources"
        )

        # Try to update our component to use the same type as the other component
        response = self.update_offering_component(
            {
                "uuid": self.component.uuid.hex,
                "type": "cpu",  # This type already exists
                "billing_type": BillingTypes.LIMIT,
            }
        )

        # Assert - Should fail validation
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("type", response.data)
        self.assertIn("already exists in this offering", str(response.data["type"]))

        # Component should remain unchanged
        self.component.refresh_from_db()
        self.assertEqual(self.component.type, "GPU-hour")

    def test_component_type_update_to_same_type_allowed(self):
        """Test that updating component with same type is allowed."""
        # Update component with same type (should be allowed)
        response = self.update_offering_component(
            {
                "uuid": self.component.uuid.hex,
                "type": "GPU-hour",  # Same type
                "name": "Updated GPU Name",
                "billing_type": BillingTypes.LIMIT,
            }
        )

        # Assert - Should succeed
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check component was updated
        self.component.refresh_from_db()
        self.assertEqual(self.component.type, "GPU-hour")
        self.assertEqual(self.component.name, "Updated GPU Name")

    def test_component_type_update_to_new_unique_type_allowed(self):
        """Test that updating to a new unique type is allowed."""
        # Update component to a new type that doesn't exist
        response = self.update_offering_component(
            {
                "uuid": self.component.uuid.hex,
                "type": "gres/gpu",  # New unique type
                "billing_type": BillingTypes.LIMIT,
            }
        )

        # Assert - Should succeed
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check component was updated
        self.component.refresh_from_db()
        self.assertEqual(self.component.type, "gres/gpu")
