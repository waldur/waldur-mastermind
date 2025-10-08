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
