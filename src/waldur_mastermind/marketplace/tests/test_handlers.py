import datetime
from unittest import mock

import httpx
import respx
from django.db import transaction
from rest_framework.test import APITransactionTestCase

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.logging.models import Event
from waldur_core.structure.models import Customer, Project
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import models as structure_tests_models
from waldur_mastermind.marketplace import callbacks, utils
from waldur_mastermind.marketplace import handlers as marketplace_handlers
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    BillingTypes,
    OfferingUserStates,
    OrderStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories, fixtures


class ResourceHandlerTest(APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

    def test_marketplace_resource_name_should_be_updated_if_resource_name_in_plugin_is_updated(
        self,
    ):
        marketplace_handlers.connect_resource_metadata_handlers(
            structure_tests_models.TestNewInstance
        )
        instance = structure_factories.TestNewInstanceFactory()
        resource = factories.ResourceFactory(scope=instance)
        instance.name = "New name"
        instance.save()
        resource.refresh_from_db()
        self.assertEqual(resource.name, "New name")

    def test_resource_update_logging_happens_only_once_in_callback(self):
        """
        This test ensures that the resource update log is only created once in the resource_update_succeeded callback.
        """

        # Get the existing component and make it LIMIT type
        offering_component = self.fixture.offering_component
        offering_component.billing_type = BillingTypes.LIMIT
        offering_component.save()

        # Set initial resource state
        self.fixture.resource.limits = {offering_component.type: 20}
        self.fixture.resource.state = ResourceStates.OK
        self.fixture.resource.plan = self.fixture.plan
        self.fixture.resource.save()  # Save initial state

        # Create update order
        order = self.fixture.update_order
        order.limits = {offering_component.type: 16}
        order.save()

        # Clear existing events
        Event.objects.filter(
            event_type="marketplace_resource_update_succeeded",
            context__resource_uuid=str(self.fixture.resource.uuid),
        ).delete()

        # Execute callback
        with transaction.atomic():
            callbacks.resource_update_succeeded(self.fixture.resource)

        event_count = Event.objects.filter(
            event_type="marketplace_resource_update_succeeded",
            context__resource_uuid=str(self.fixture.resource.uuid),
        ).count()

        self.assertEqual(
            event_count,
            1,
            f"Expected 1 event 'marketplace_resource_update_succeeded', got {event_count}",
        )

    def test_resource_update_log_skipped_for_blacklisted_fields(self):
        """
        This test ensures that the resource update log is not created when only blacklisted fields are updated.
        """
        Event.objects.all().delete()
        # Update a blacklisted field ( backend_metadata )
        self.fixture.resource.backend_metadata = {"some": "metadata"}
        self.fixture.resource.save()

        event_count = Event.objects.filter(
            event_type="marketplace_resource_update_succeeded",
            context__resource_uuid=str(self.fixture.resource.uuid),
        ).count()

        self.assertEqual(
            event_count,
            0,
            f"Expected 0 events 'marketplace_resource_update_succeeded', got {event_count}",
        )

    def test_resource_update_log_happens_once_for_multiple_fields(self):
        """
        This test ensures that when multiple non-blacklisted fields are updated,
        only one event is created.
        """
        # Clear existing events
        Event.objects.all().delete()

        # Update multiple non-blacklisted fields
        self.fixture.resource.name = "New name"
        self.fixture.resource.description = "New description"
        self.fixture.resource.save()

        event_count = Event.objects.filter(
            event_type="marketplace_resource_update_succeeded",
            context__resource_uuid=str(self.fixture.resource.uuid),
        ).count()

        self.assertEqual(
            event_count,
            1,
            f"Expected 1 event for multiple field updates, got {event_count}",
        )

    def test_service_settings_should_be_disabled_if_resource_is_terminated(
        self,
    ):
        marketplace_handlers.connect_resource_metadata_handlers(
            structure_tests_models.TestNewInstance
        )
        instance = structure_factories.TestNewInstanceFactory()
        resource: marketplace_models.Resource = factories.ResourceFactory(
            scope=instance
        )

        offering: marketplace_models.Offering = resource.offering
        service_settings = structure_factories.ServiceSettingsFactory()
        offering.scope = service_settings
        offering.archive()
        offering.save()

        service_settings.refresh_from_db()
        self.assertTrue(service_settings.is_active)

        resource.set_state_terminated()
        resource.save()

        service_settings.refresh_from_db()

        self.assertFalse(service_settings.is_active)

    def test_service_settings_should_be_disabled_if_offering_is_archived(
        self,
    ):
        marketplace_handlers.connect_resource_metadata_handlers(
            structure_tests_models.TestNewInstance
        )
        instance = structure_factories.TestNewInstanceFactory()
        resource: marketplace_models.Resource = factories.ResourceFactory(
            scope=instance
        )

        offering: marketplace_models.Offering = resource.offering
        service_settings = structure_factories.ServiceSettingsFactory()
        offering.scope = service_settings
        offering.save()

        resource.set_state_terminated()
        resource.save()

        service_settings.refresh_from_db()
        self.assertTrue(service_settings.is_active)

        offering.archive()
        offering.save()

        service_settings.refresh_from_db()

        self.assertFalse(service_settings.is_active)

    def test_service_settings_should_be_enabled_if_resource_is_not_terminated(
        self,
    ):
        marketplace_handlers.connect_resource_metadata_handlers(
            structure_tests_models.TestNewInstance
        )
        instance = structure_factories.TestNewInstanceFactory()
        resource: marketplace_models.Resource = factories.ResourceFactory(
            scope=instance
        )

        offering: marketplace_models.Offering = resource.offering
        service_settings = structure_factories.ServiceSettingsFactory()
        service_settings.is_active = False
        service_settings.save()
        offering.scope = service_settings
        offering.save()

        resource.set_state_ok()
        resource.save()

        service_settings.refresh_from_db()
        self.assertTrue(service_settings.is_active)

    def test_service_settings_should_be_enabled_if_offering_is_not_archived(
        self,
    ):
        marketplace_handlers.connect_resource_metadata_handlers(
            structure_tests_models.TestNewInstance
        )
        instance = structure_factories.TestNewInstanceFactory()
        resource: marketplace_models.Resource = factories.ResourceFactory(
            scope=instance
        )

        service_settings = structure_factories.ServiceSettingsFactory()
        service_settings.is_active = False
        service_settings.save()

        offering: marketplace_models.Offering = resource.offering
        offering.scope = service_settings
        offering.save()

        service_settings.refresh_from_db()
        self.assertFalse(service_settings.is_active)

    def test_resource_update_log_skipped_for_noop_change(self):
        """
        This test ensures that the resource update log is not created when a field is set to the same value.
        """
        Event.objects.all().delete()
        # Simulate a no-op change
        resource = self.fixture.resource
        resource.name = resource.name  # set to same value
        resource.tracker.changed = lambda: {"name": resource.name}  # fake tracker
        marketplace_handlers.resource_has_been_changed(
            sender=type(resource), instance=resource, created=False
        )
        event_count = Event.objects.filter(
            event_type="marketplace_resource_update_succeeded",
            context__resource_uuid=str(resource.uuid),
        ).count()
        self.assertEqual(
            event_count,
            0,
            f"Expected 0 events for no-op change, got {event_count}",
        )

    def test_resource_update_log_created_for_real_change(self):
        """
        This test ensures that the resource update log is created when a real change happens.
        """

        Event.objects.all().delete()
        resource = self.fixture.resource
        old_name = resource.name
        resource.name = "A new name!"
        resource.tracker.changed = lambda: {"name": old_name}  # fake tracker
        marketplace_handlers.resource_has_been_changed(
            sender=type(resource), instance=resource, created=False
        )
        event_count = Event.objects.filter(
            event_type="marketplace_resource_update_succeeded",
            context__resource_uuid=str(resource.uuid),
        ).count()
        self.assertEqual(
            event_count,
            1,
            f"Expected 1 event for real change, got {event_count}",
        )

    def test_resource_update_log_skipped_for_state_noop_display(self):
        """
        This test ensures that the resource update log is not created when the state field changes but the display value does not change.
        """
        fixture = fixtures.MarketplaceFixture()
        Event.objects.all().delete()
        resource = fixture.resource
        # Simulate a state change where display value is the same
        resource.state = resource.state  # set to same value
        resource.tracker.changed = lambda: {"state": resource.state}  # fake tracker
        marketplace_handlers.resource_has_been_changed(
            sender=type(resource), instance=resource, created=False
        )
        event_count = Event.objects.filter(
            event_type="marketplace_resource_update_succeeded",
            context__resource_uuid=str(resource.uuid),
        ).count()
        self.assertEqual(
            event_count,
            0,
            f"Expected 0 events for state display no-op, got {event_count}",
        )


class UpdateOfferingUserUsernameAfterUserChangeTest(APITransactionTestCase):
    def setUp(self):
        self.offering = factories.OfferingFactory(
            type=BASIC_OFFERING,
            plugin_options={
                "username_generation_policy": utils.UsernameGenerationPolicy.IDENTITY_CLAIM.value
            },
        )
        self.offering_user = factories.OfferingUserFactory(
            offering=self.offering, username="old_username"
        )
        self.user = self.offering_user.user

    def test_update_offering_user_username_after_user_change(self):
        new_username = "new_site_username"
        self.user.details = {"site_username": new_username}
        self.user.save()

        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.username, new_username)

    def test_do_not_update_offering_user_username_if_site_username_is_not_changed(self):
        self.user.first_name = "new_first_name"
        self.user.save()

        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.username, "old_username")

    def test_do_not_update_offering_user_username_if_username_generation_policy_is_not_identity_claim(
        self,
    ):
        offering = factories.OfferingFactory(
            type=BASIC_OFFERING,
            plugin_options={
                "username_generation_policy": utils.UsernameGenerationPolicy.SERVICE_PROVIDER.value
            },
        )
        offering_user = factories.OfferingUserFactory(
            offering=offering, username="old_username"
        )
        user = offering_user.user

        user.details = {"site_username": "new_site_username"}
        user.save()

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, "old_username")


class SetOrderCompletionTimestampTest(APITransactionTestCase):
    def setUp(self):
        self.fixed_time = datetime.datetime(2025, 5, 23, 12, 0, 0)
        self.order = factories.OrderFactory(state=OrderStates.PENDING_PROVIDER)
        self.order.save()

    def set_order_executing(self):
        self.order.state = OrderStates.EXECUTING
        self.order.save()

    def test_set_order_completion_timestamp_created(self):
        self.set_order_executing()
        self.assertIsNone(self.order.completed_at)

    @mock.patch("django.utils.timezone.now")
    def test_set_order_completion_timestamp_completed(self, mock_now):
        mock_now.return_value = self.fixed_time
        self.set_order_executing()
        self.order.complete()
        self.order.save()
        self.assertIsNotNone(self.order.completed_at)
        self.assertEqual(self.fixed_time, self.order.completed_at)

    @mock.patch("django.utils.timezone.now")
    def test_set_order_completion_timestamp_failed(self, mock_now):
        mock_now.return_value = self.fixed_time
        self.set_order_executing()
        self.order.fail()
        self.order.save()
        self.assertIsNotNone(self.order.completed_at)
        self.assertEqual(self.fixed_time, self.order.completed_at)

    @mock.patch("django.utils.timezone.now")
    def test_set_order_completion_timestamp_cancelled(self, mock_now):
        mock_now.return_value = self.fixed_time
        self.set_order_executing()
        self.order.cancel()
        self.order.save()
        self.assertIsNotNone(self.order.completed_at)
        self.assertEqual(self.fixed_time, self.order.completed_at)

    @mock.patch("django.utils.timezone.now")
    def test_set_order_completion_timestamp_rejected(self, mock_now):
        mock_now.return_value = self.fixed_time
        self.order.reject()
        self.order.save()
        self.assertIsNotNone(self.order.completed_at)
        self.assertEqual(self.fixed_time, self.order.completed_at)


SERVICE_ACCOUNT_URL = "http://example.com/api/service-accounts"
TOKEN_URL = "http://example.com/api/token"
TOKEN_CLIENT_ID = "test-client"
TOKEN_SECRET = "test-secret"


@override_waldur_core_settings(
    SERVICE_ACCOUNT_USE_API=True,
    SERVICE_ACCOUNT_TOKEN_URL=TOKEN_URL,
    SERVICE_ACCOUNT_URL=SERVICE_ACCOUNT_URL,
    SERVICE_ACCOUNT_TOKEN_CLIENT_ID=TOKEN_CLIENT_ID,
    SERVICE_ACCOUNT_TOKEN_SECRET=TOKEN_SECRET,
)
class ServiceAccountHandlersTest(APITransactionTestCase):
    def setUp(self):
        respx.start()
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.customer = self.fixture.customer
        self.token = "test-token"
        self.account_username = "test-account"

        service_account_response = {
            "serviceAccount": {
                "status": "active",
                "username": self.account_username,
                "email": "test@example.com",
                "description": "test description",
                "unixUid": 1000,
                "unixGid": 1000,
                "scopeType": "scope",
                "scopeName": "Test scope",
                "scopeSlug": "test-scope",
                "owner": {
                    "username": "test-owner",
                    "email": "owner@example.com",
                },
            },
            "apiKey": {
                "apiKey": self.token,
                "createdAt": "2025-04-28T12:00:00Z",
                "expiresAt": "2025-05-28T12:00:00Z",
                "ttl": 2592000,
            },
        }

        respx.post(
            TOKEN_URL,
            content=f"grant_type=client_credentials&client_id={TOKEN_CLIENT_ID}&client_secret={TOKEN_SECRET}",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ).mock(return_value=httpx.Response(200, json={"access_token": self.token}))

        respx.get(
            f"{SERVICE_ACCOUNT_URL}/{self.account_username}/close",
            headers={"Authorization": f"Bearer {self.token}"},
        ).mock(return_value=httpx.Response(200, json={}))

        respx.get(
            f"{SERVICE_ACCOUNT_URL}/{self.account_username}",
            headers={"Authorization": f"Bearer {self.token}"},
        ).mock(return_value=httpx.Response(200, json=service_account_response))

    def tearDown(self):
        respx.stop()
        super().tearDown()

    def test_project_service_account_deletion_on_project_deletion(self):
        """
        This test ensures that a project service account is deleted and requested to be deleted when a project is deleted.
        """
        # Create a project service account
        service_account = marketplace_models.ProjectServiceAccount.objects.create(
            project=self.project,
            username=self.account_username,
        )

        response = respx.put(
            f"{SERVICE_ACCOUNT_URL}/{service_account.username}/close",
            headers={"Authorization": f"Bearer {self.token}"},
        ).mock(return_value=httpx.Response(200, json={}))

        self.project.delete()

        # Verify the call to close the service account was made
        self.assertTrue(response.called)

        self.assertFalse(
            marketplace_models.ProjectServiceAccount.objects.filter(
                uuid=service_account.uuid
            ).exists()
        )

    def test_customer_service_account_deletion_on_customer_deletion(self):
        """
        This test ensures that a customer service account is deleted and requested to be deleted when a customer is deleted.
        """
        # Create a customer service account
        service_account = marketplace_models.CustomerServiceAccount.objects.create(
            customer=self.customer,
            username=self.account_username,
        )

        response = respx.put(
            f"{SERVICE_ACCOUNT_URL}/{service_account.username}/close",
            headers={"Authorization": f"Bearer {self.token}"},
        ).mock(return_value=httpx.Response(200, json={}))

        self.customer.delete()

        # Verify the call to close the service account was made
        self.assertTrue(response.called)

        # Verify service account was deleted
        self.assertFalse(
            marketplace_models.CustomerServiceAccount.objects.filter(
                id=service_account.id
            ).exists()
        )

    def test_project_service_account_deletion_failure_does_not_block_project_deletion(
        self,
    ):
        """
        This test ensures that a project can be deleted even if the service account deletion fails.
        """
        # Create a project service account
        service_account = marketplace_models.ProjectServiceAccount.objects.create(
            project=self.project,
            username=self.account_username,
        )

        # Mock failed service account deletion
        response = respx.put(
            f"{SERVICE_ACCOUNT_URL}/{service_account.username}/close",
            headers={"Authorization": f"Bearer {self.token}"},
        ).mock(
            return_value=httpx.Response(500, json={"error": "Internal Server Error"})
        )

        # Projects can be deleted with soft delete or hard delete, default is soft delete
        self.project.delete()

        self.assertTrue(response.called)

        self.assertFalse(
            Project.available_objects.filter(uuid=self.project.uuid).exists()
        )
        self.assertTrue(Project.objects.filter(uuid=self.project.uuid).exists())
        self.assertTrue(Project.objects.get(uuid=self.project.uuid).is_removed)
        # Verify that the service account was not deleted due to the failure
        self.assertTrue(
            marketplace_models.ProjectServiceAccount.objects.filter(
                uuid=service_account.uuid
            ).exists()
        )

    def test_customer_service_account_deletion_failure_does_not_block_customer_deletion(
        self,
    ):
        """
        This test ensures that a customer can be deleted even if the service account deletion fails.
        """
        service_account = marketplace_models.CustomerServiceAccount.objects.create(
            customer=self.customer,
            username=self.account_username,
        )
        service_account.save()

        response = respx.put(
            f"{SERVICE_ACCOUNT_URL}/{service_account.username}/close",
            headers={"Authorization": f"Bearer {self.token}"},
        ).mock(
            return_value=httpx.Response(500, json={"error": "Internal Server Error"})
        )

        self.customer.delete()

        # Verify customer was deleted
        self.assertFalse(Customer.objects.filter(uuid=self.customer.uuid).exists())

        self.assertTrue(response.called)
        # Verify that the service account was still deleted regardless of the failure
        self.assertFalse(
            marketplace_models.CustomerServiceAccount.objects.filter(
                uuid=service_account.uuid
            ).exists()
        )


class OfferingUserCreationWithUsernameTest(APITransactionTestCase):
    """
    Test that OfferingUser instances are created with correct state when username is known.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        # Create a second offering for testing multiple offerings
        self.offering2 = factories.OfferingFactory(customer=self.fixture.customer)

    def test_create_offering_user_for_new_resource_with_ok_state_when_username_provided(
        self,
    ):
        """
        Test that handlers.create_offering_user_for_new_resource creates OfferingUser with OK state.
        """
        # Configure offering to generate usernames
        self.fixture.offering.plugin_options = {
            "username_generation_policy": "full_name",
        }
        self.fixture.offering.save()

        # Create a resource that triggers the handler
        resource = factories.ResourceFactory(
            offering=self.fixture.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )

        # Call the handler function
        marketplace_handlers.create_offering_user_for_new_resource(
            None, resource, created=True
        )

        # Verify OfferingUser was created with OK state
        project_users = self.fixture.project.get_users()
        for user in project_users:
            offering_user = marketplace_models.OfferingUser.objects.get(
                user=user, offering=self.fixture.offering
            )
            self.assertEqual(offering_user.state, OfferingUserStates.OK)
            self.assertIsNotNone(offering_user.username)
            # Username should be based on full name for this policy
            self.assertIn(user.first_name.lower(), offering_user.username)

    def test_create_offering_user_for_new_resource_with_creation_requested_when_no_username(
        self,
    ):
        """
        Test that handlers.create_offering_user_for_new_resource creates OfferingUser with CREATION_REQUESTED state.
        """
        # Configure offering to NOT generate usernames
        self.fixture.offering.plugin_options = {
            "username_generation_policy": "service_provider",
        }
        self.fixture.offering.save()

        # Create a resource that triggers the handler
        resource = factories.ResourceFactory(
            offering=self.fixture.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )

        # Call the handler function
        marketplace_handlers.create_offering_user_for_new_resource(
            None, resource, created=True
        )

        # Verify OfferingUser was created with CREATION_REQUESTED state
        project_users = self.fixture.project.get_users()
        for user in project_users:
            offering_user = marketplace_models.OfferingUser.objects.get(
                user=user, offering=self.fixture.offering
            )
            self.assertEqual(offering_user.state, OfferingUserStates.CREATION_REQUESTED)
            self.assertEqual(offering_user.username, "")


class OfferingUserDirectCreationTest(APITransactionTestCase):
    """
    Test direct creation of OfferingUser objects with different username scenarios.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

    def test_direct_offering_user_creation_with_username_sets_ok_state(self):
        """
        Test that directly creating OfferingUser with username sets state to OK.
        """
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.fixture.user,
            offering=self.fixture.offering,
            username="direct_test_user",
        )

        self.assertEqual(offering_user.state, OfferingUserStates.OK)
        self.assertEqual(offering_user.username, "direct_test_user")

    def test_direct_offering_user_creation_without_username_keeps_default_state(self):
        """
        Test that directly creating OfferingUser without username keeps default state.
        """
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.fixture.user,
            offering=self.fixture.offering,
        )

        self.assertEqual(offering_user.state, OfferingUserStates.CREATION_REQUESTED)
        self.assertIsNone(offering_user.username)

    def test_direct_offering_user_creation_with_empty_username_keeps_default_state(
        self,
    ):
        """
        Test that directly creating OfferingUser with empty username keeps default state.
        """
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.fixture.user,
            offering=self.fixture.offering,
            username="",
        )

        self.assertEqual(offering_user.state, OfferingUserStates.CREATION_REQUESTED)
        self.assertEqual(offering_user.username, "")
