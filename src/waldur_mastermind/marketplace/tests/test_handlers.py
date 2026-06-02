import datetime
from unittest import mock

import httpx
import respx
from django.db import transaction
from rest_framework.test import APITestCase

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.logging.models import Event
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.models import Customer, Project
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import models as structure_tests_models
from waldur_mastermind.marketplace import callbacks, tasks, utils
from waldur_mastermind.marketplace import handlers as marketplace_handlers
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    BillingTypes,
    OfferingUserStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories, fixtures


def add_user_to_project(user, project, role=None):
    if role is None:
        role = ProjectRole.MANAGER
    project.add_user(user, role)
    tasks.create_or_restore_offering_users_for_user(user.uuid.hex, project.uuid.hex)


class OfferingOptionsHandlerTest(APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

    def test_event_emitted_when_offering_options_are_updated(self):
        Event.objects.all().delete()
        self.fixture.offering.options = {"options": {}, "order": []}
        self.fixture.offering.save()

        self.fixture.offering.options = {
            "order": ["storageRequest"],
            "options": {
                "storageRequest": {
                    "type": "integer",
                    "label": "Storage request (GB)",
                    "required": True,
                    "min": 0,
                    "max": 102400,
                }
            },
        }
        self.fixture.offering.save()

        event = Event.objects.filter(
            event_type="marketplace_offering_options_updated"
        ).first()
        self.assertIsNotNone(event)
        self.assertIn(self.fixture.offering.name, event.message)
        self.assertIn("storageRequest", event.message)
        self.assertIn("Details:", event.message)

    def test_event_emitted_when_offering_resource_options_are_updated(self):
        Event.objects.all().delete()
        self.fixture.offering.resource_options = {"options": {}, "order": []}
        self.fixture.offering.save()

        self.fixture.offering.resource_options = {
            "order": ["researchFields"],
            "options": {
                "researchFields": {
                    "type": "string",
                    "label": "Research fields",
                    "required": False,
                }
            },
        }
        self.fixture.offering.save()

        event = Event.objects.filter(
            event_type="marketplace_offering_resource_options_updated"
        ).first()
        self.assertIsNotNone(event)
        self.assertIn(self.fixture.offering.name, event.message)
        self.assertIn("researchFields", event.message)
        self.assertIn("Details:", event.message)


class ResourceHandlerTest(APITestCase):
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

    def test_resource_update_log_skipped_for_date_string_equivalence(self):
        """
        This test ensures that the resource update log is not created when a date field
        changes from datetime to string but string representations are equivalent.
        This addresses the noisy logging issue where end_date: 2025-12-11 -> 2025-12-11.
        """
        Event.objects.all().delete()
        resource = self.fixture.resource

        from datetime import date

        # Simulate the scenario: datetime object vs string with same value
        old_date = date(2025, 12, 11)
        new_date_str = "2025-12-11"

        resource.end_date = new_date_str
        # Mock the tracker to simulate what happens when django-model-utils detects change
        resource.tracker.changed = lambda: {"end_date": old_date}

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
            f"Expected 0 events for equivalent date string change, got {event_count}",
        )

    def test_resource_update_log_created_for_real_date_change(self):
        """
        This test ensures that the resource update log is still created when a date field
        actually changes to a different value.
        """
        Event.objects.all().delete()
        resource = self.fixture.resource

        from datetime import date

        # Simulate actual date change
        old_date = date(2025, 12, 11)
        new_date = date(2025, 12, 12)  # Different date

        resource.end_date = new_date
        resource.tracker.changed = lambda: {"end_date": old_date}

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
            f"Expected 1 event for real date change, got {event_count}",
        )


class UpdateOfferingUserUsernameAfterUserChangeTest(APITestCase):
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


class UpdateOfferingUserUsernameAfterOfferingSettingsChangeTest(APITestCase):
    def setUp(self):
        self.old_username = "old_username"
        self.site_username = "site-user"
        self.preserved_username = "preserved_username"
        self.offering = factories.OfferingFactory(
            type=BASIC_OFFERING,
            plugin_options={
                "username_generation_policy": utils.UsernameGenerationPolicy.WALDUR_USERNAME.value
            },
        )

        self.offering_user = factories.OfferingUserFactory(
            offering=self.offering,
            username=self.old_username,
        )

    def test_update_offering_user_username_when_username_generation_policy_changes(
        self,
    ):
        self.offering.plugin_options["username_generation_policy"] = (
            utils.UsernameGenerationPolicy.IDENTITY_CLAIM.value
        )
        self.offering_user.user.details = {"site_username": self.site_username}
        self.offering_user.user.save()
        self.offering.save()

        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.username, self.site_username)

    def test_do_not_update_offering_user_username_when_unrelated_plugin_option_changes(
        self,
    ):
        self.offering.plugin_options["offering_user_auto_deletion"] = True
        self.offering.save()

        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.username, self.old_username)

    def test_do_not_clear_offering_user_username_for_service_provider_policy_on_unrelated_change(
        self,
    ):
        self.offering.plugin_options = {
            "username_generation_policy": utils.UsernameGenerationPolicy.SERVICE_PROVIDER.value
        }
        self.offering.save()

        self.offering_user.username = self.preserved_username
        self.offering_user.save()

        self.offering.plugin_options["offering_user_auto_deletion"] = True
        self.offering.save()

        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.username, self.preserved_username)


class SetOrderCompletionTimestampTest(APITestCase):
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
class ServiceAccountHandlersTest(APITestCase):
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


class OfferingUserCreationWithUsernameTest(APITestCase):
    """
    Test that OfferingUser instances are created with correct state when username is known.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        # Create a second offering for testing multiple offerings
        self.offering2 = factories.OfferingFactory(customer=self.fixture.customer)

    @mock.patch(
        "waldur_mastermind.marketplace.tasks.create_or_restore_offering_users_for_project.delay"
    )
    def test_create_offering_user_for_new_resource_dispatches_batch_task(
        self, mock_delay
    ):
        """
        Test that handlers.create_offering_user_for_new_resource dispatches
        a single batch Celery task for the project.
        """
        # Configure offering to generate usernames and allow user creation
        self.fixture.offering.plugin_options = {
            "username_generation_policy": "full_name",
            "service_provider_can_create_offering_user": True,
        }
        self.fixture.offering.save()

        # Create a resource that triggers the handler
        resource = factories.ResourceFactory(
            offering=self.fixture.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )

        # Call the handler function inside captureOnCommitCallbacks to execute deferred callbacks
        with self.captureOnCommitCallbacks(execute=True):
            marketplace_handlers.create_offering_user_for_new_resource(
                None, resource, created=True
            )

        # Verify a single batch Celery task was dispatched for the project
        mock_delay.assert_called_once_with(self.fixture.project.uuid.hex)


class OfferingUserDirectCreationTest(APITestCase):
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


class OfferingUserDeletionOnProjectAccessLossTest(APITestCase):
    """Test that offering users are marked for deletion when user loses project access."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        # Configure offering to support offering users with auto deletion enabled
        self.fixture.offering.plugin_options = {
            "service_provider_can_create_offering_user": True,
            "username_generation_policy": "waldur_username",
            "offering_user_auto_deletion": True,
        }
        self.fixture.offering.save()

        self.resource = factories.ResourceFactory(
            offering=self.fixture.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )

        self.test_user = structure_factories.UserFactory(username="test_user")

    def test_offering_user_deletion_requested_when_user_removed_from_project(self):
        """Test that offering user deletion is requested when user is removed from project."""
        add_user_to_project(self.test_user, self.fixture.project)

        offering_user = marketplace_models.OfferingUser.objects.get(
            user=self.test_user, offering=self.fixture.offering
        )
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

        self.fixture.project.remove_user(self.test_user, ProjectRole.MANAGER)

        tasks.request_offering_user_deletion_for_user(self.test_user.uuid.hex)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETION_REQUESTED)

    def test_offering_user_not_deleted_when_user_has_access_via_other_project(self):
        """Test that offering user is not deleted if user still has access via another project."""
        second_project = structure_factories.ProjectFactory(
            customer=self.fixture.customer
        )
        factories.ResourceFactory(
            offering=self.fixture.offering,
            project=second_project,
            state=ResourceStates.OK,
        )

        add_user_to_project(self.test_user, self.fixture.project)

        offering_user = marketplace_models.OfferingUser.objects.get(
            user=self.test_user, offering=self.fixture.offering
        )
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

        add_user_to_project(self.test_user, second_project)

        self.fixture.project.remove_user(self.test_user, ProjectRole.MANAGER)

        tasks.request_offering_user_deletion_for_user(self.test_user.uuid.hex)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

        second_project.remove_user(self.test_user, ProjectRole.MANAGER)

        tasks.request_offering_user_deletion_for_user(self.test_user.uuid.hex)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETION_REQUESTED)

    def test_only_ok_state_offering_users_are_deleted(self):
        """Test that only offering users in OK state are marked for deletion, unprovisioned ones are marked DELETED."""
        second_offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            plugin_options={
                "service_provider_can_create_offering_user": True,
                "username_generation_policy": "waldur_username",
                "offering_user_auto_deletion": True,
            },
        )
        factories.ResourceFactory(
            offering=second_offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )

        offering_user_creating = marketplace_models.OfferingUser.objects.create(
            user=self.test_user,
            offering=self.fixture.offering,
            state=OfferingUserStates.CREATION_REQUESTED,
        )

        offering_user_ok = marketplace_models.OfferingUser.objects.create(
            user=self.test_user,
            offering=second_offering,
            state=OfferingUserStates.OK,
            username="test_user",
        )

        self.fixture.project.add_user(self.test_user, ProjectRole.MANAGER)
        self.fixture.project.remove_user(self.test_user, ProjectRole.MANAGER)

        tasks.request_offering_user_deletion_for_user(self.test_user.uuid.hex)

        offering_user_creating.refresh_from_db()
        offering_user_ok.refresh_from_db()

        # CREATION_REQUESTED should go directly to DELETED (never provisioned)
        self.assertEqual(offering_user_creating.state, OfferingUserStates.DELETED)
        # OK state should go to DELETION_REQUESTED (provisioned, needs cleanup)
        self.assertEqual(offering_user_ok.state, OfferingUserStates.DELETION_REQUESTED)

    def test_handler_does_not_fail_when_user_has_no_offering_users(self):
        """Test that handler doesn't fail when user has no offering users."""
        empty_project = structure_factories.ProjectFactory(
            customer=self.fixture.customer
        )

        empty_project.add_user(self.test_user, ProjectRole.MANAGER)
        empty_project.remove_user(self.test_user, ProjectRole.MANAGER)

        self.assertFalse(
            marketplace_models.OfferingUser.objects.filter(user=self.test_user).exists()
        )


class OfferingUserRestorationOnProjectAccessGainedTest(APITestCase):
    """Test that offering users are restored when user regains project access."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.offering.plugin_options = {
            "service_provider_can_create_offering_user": True,
            "username_generation_policy": "waldur_username",
        }
        self.fixture.offering.save()

        self.resource = factories.ResourceFactory(
            offering=self.fixture.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )

        self.test_user = structure_factories.UserFactory(username="test_user")

    def test_offering_user_restored_from_deletion_requested_when_user_regains_access(
        self,
    ):
        """Test that offering user in DELETION_REQUESTED is restored to OK when user regains access."""
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.test_user,
            offering=self.fixture.offering,
            state=OfferingUserStates.OK,
            username="test_user",
        )
        offering_user.request_deletion()
        offering_user.save()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETION_REQUESTED)

        add_user_to_project(self.test_user, self.fixture.project)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

    def test_offering_user_restored_from_deleting_when_user_regains_access(self):
        """Test that offering user in DELETING is restored to OK when user regains access."""
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.test_user,
            offering=self.fixture.offering,
            state=OfferingUserStates.OK,
            username="test_user",
        )
        offering_user.request_deletion()
        offering_user.set_deleting()
        offering_user.save()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETING)

        add_user_to_project(self.test_user, self.fixture.project)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

    def test_offering_user_requests_new_account_when_deleted_and_user_regains_access(
        self,
    ):
        """Test that offering user in DELETED requests new account creation when user regains access."""
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.test_user,
            offering=self.fixture.offering,
            state=OfferingUserStates.OK,
            username="test_user",
        )
        offering_user.request_deletion()
        offering_user.set_deleting()
        offering_user.set_deleted()
        offering_user.save()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETED)

        add_user_to_project(self.test_user, self.fixture.project)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.CREATION_REQUESTED)

    def test_offering_user_without_username_restored_to_creation_requested(self):
        """Test that offering user without username is restored to CREATION_REQUESTED."""
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.test_user,
            offering=self.fixture.offering,
            state=OfferingUserStates.CREATION_REQUESTED,
        )
        offering_user.set_deleted()
        offering_user.save()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETED)
        self.assertFalse(offering_user.username)

        add_user_to_project(self.test_user, self.fixture.project)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.CREATION_REQUESTED)

    def test_offering_user_not_restored_if_already_in_ok_state(self):
        """Test that offering user already in OK state is not changed when user gains access."""
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.test_user,
            offering=self.fixture.offering,
            state=OfferingUserStates.OK,
            username="test_user",
        )

        add_user_to_project(self.test_user, self.fixture.project)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

    def test_event_emitted_when_restoring_from_deletion_requested_to_ok(self):
        """Test that event is emitted when offering user is restored from DELETION_REQUESTED to OK."""
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.test_user,
            offering=self.fixture.offering,
            state=OfferingUserStates.OK,
            username="test_user",
        )
        offering_user.request_deletion()
        offering_user.save()

        add_user_to_project(self.test_user, self.fixture.project)

        event = Event.objects.filter(
            event_type="marketplace_offering_user_updated"
        ).first()
        self.assertIsNotNone(event)
        self.assertIn("has been restored from", event.message)
        self.assertIn("to OK because user regained project access", event.message)
        self.assertIn(self.test_user.username, event.message)
        self.assertIn(self.fixture.offering.name, event.message)

    def test_event_emitted_when_restoring_from_deletion_requested_to_creation_requested(
        self,
    ):
        """Test that event is emitted when offering user without username is restored to CREATION_REQUESTED."""
        # Create in CREATING state (no username) so we can transition to DELETION_REQUESTED
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.test_user,
            offering=self.fixture.offering,
            state=OfferingUserStates.CREATING,
        )
        offering_user.request_deletion()
        offering_user.save()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETION_REQUESTED)
        self.assertFalse(offering_user.username)

        add_user_to_project(self.test_user, self.fixture.project)

        event = Event.objects.filter(
            event_type="marketplace_offering_user_updated"
        ).first()
        self.assertIsNotNone(event)
        self.assertIn("Account creation", event.message)
        self.assertIn("has been requested", event.message)
        self.assertIn("because user regained project access", event.message)
        self.assertIn(self.test_user.username, event.message)
        self.assertIn(self.fixture.offering.name, event.message)

    def test_event_emitted_when_restoring_from_deleted_to_creation_requested(self):
        """Test that event is emitted when offering user in DELETED state requests new account creation."""
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.test_user,
            offering=self.fixture.offering,
            state=OfferingUserStates.OK,
            username="test_user",
        )
        offering_user.request_deletion()
        offering_user.set_deleting()
        offering_user.set_deleted()
        offering_user.save()

        add_user_to_project(self.test_user, self.fixture.project)

        event = Event.objects.filter(
            event_type="marketplace_offering_user_updated"
        ).first()
        self.assertIsNotNone(event)
        self.assertIn("New account creation", event.message)
        self.assertIn("has been requested", event.message)
        self.assertIn("after offering user was deleted", event.message)
        self.assertIn("because user regained project access", event.message)
        self.assertIn(self.test_user.username, event.message)
        self.assertIn(self.fixture.offering.name, event.message)


class CleanupStaleOfferingUsersTest(APITestCase):
    """Test the periodic cleanup task for stale offering users."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.offering.plugin_options = {
            "service_provider_can_create_offering_user": True,
            "username_generation_policy": "waldur_username",
        }
        self.fixture.offering.save()

        self.user1 = structure_factories.UserFactory(username="user1")
        self.user2 = structure_factories.UserFactory(username="user2")

    def _create_offering_user(self, user, offering, state, username=None):
        return marketplace_models.OfferingUser.objects.create(
            user=user,
            offering=offering,
            state=state,
            username=username,
        )

    def _transition_to_deletion_requested(self, offering_user):
        offering_user.request_deletion()
        offering_user.save()
        return offering_user

    @mock.patch(
        "waldur_mastermind.marketplace.tasks.request_offering_user_deletion_for_user.delay"
    )
    def test_cleanup_task_schedules_deletion_for_users_with_active_offering_users(
        self, mock_delay
    ):
        """Test that cleanup task schedules deletion task for each user with active offering users."""
        self._create_offering_user(
            self.user1, self.fixture.offering, OfferingUserStates.OK, "user1"
        )
        self._create_offering_user(
            self.user2, self.fixture.offering, OfferingUserStates.CREATING
        )

        tasks.cleanup_stale_offering_users()

        self.assertEqual(mock_delay.call_count, 2)
        called_uuids = {call[0][0] for call in mock_delay.call_args_list}
        self.assertEqual(called_uuids, {self.user1.uuid.hex, self.user2.uuid.hex})

    @mock.patch(
        "waldur_mastermind.marketplace.tasks.request_offering_user_deletion_for_user.delay"
    )
    def test_cleanup_task_excludes_users_already_in_deletion_flow(self, mock_delay):
        """Test that cleanup task doesn't schedule tasks for users already being deleted."""
        offering_user1 = self._create_offering_user(
            self.user1, self.fixture.offering, OfferingUserStates.OK, "user1"
        )
        self._transition_to_deletion_requested(offering_user1)

        offering_user2 = self._create_offering_user(
            self.user2, self.fixture.offering, OfferingUserStates.OK, "user2"
        )
        self._transition_to_deletion_requested(offering_user2)
        offering_user2.set_deleting()
        offering_user2.save()

        tasks.cleanup_stale_offering_users()

        mock_delay.assert_not_called()

    @mock.patch(
        "waldur_mastermind.marketplace.tasks.request_offering_user_deletion_for_user.delay"
    )
    def test_cleanup_task_handles_users_with_mixed_offering_user_states(
        self, mock_delay
    ):
        """Test that cleanup task schedules task if user has at least one non-deletion state."""
        self._create_offering_user(
            self.user1, self.fixture.offering, OfferingUserStates.OK, "user1"
        )

        second_offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        offering_user_deleted = self._create_offering_user(
            self.user1, second_offering, OfferingUserStates.CREATION_REQUESTED
        )
        offering_user_deleted.set_deleted()
        offering_user_deleted.save()

        tasks.cleanup_stale_offering_users()

        mock_delay.assert_called_once_with(self.user1.uuid.hex)


class OfferingUserAutoDeleteDisabledTest(APITestCase):
    """Test that offering users are NOT auto-deleted when offering_user_auto_deletion is False or missing."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        # Configure offering WITHOUT offering_user_auto_deletion (default is False)
        self.fixture.offering.plugin_options = {
            "service_provider_can_create_offering_user": True,
            "username_generation_policy": "waldur_username",
        }
        self.fixture.offering.save()

        self.resource = factories.ResourceFactory(
            offering=self.fixture.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )

        self.test_user = structure_factories.UserFactory(username="test_user")

    def test_offering_user_not_deleted_when_auto_deletion_disabled(self):
        """Offering user in OK state is NOT marked for deletion when auto_deletion is off."""
        add_user_to_project(self.test_user, self.fixture.project)

        offering_user = marketplace_models.OfferingUser.objects.get(
            user=self.test_user, offering=self.fixture.offering
        )
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

        self.fixture.project.remove_user(self.test_user, ProjectRole.MANAGER)
        tasks.request_offering_user_deletion_for_user(self.test_user.uuid.hex)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

    def test_offering_user_deleted_when_auto_deletion_enabled(self):
        """Offering user in OK state IS marked for deletion when auto_deletion is on."""
        self.fixture.offering.plugin_options["offering_user_auto_deletion"] = True
        self.fixture.offering.save()

        add_user_to_project(self.test_user, self.fixture.project)

        offering_user = marketplace_models.OfferingUser.objects.get(
            user=self.test_user, offering=self.fixture.offering
        )
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

        self.fixture.project.remove_user(self.test_user, ProjectRole.MANAGER)
        tasks.request_offering_user_deletion_for_user(self.test_user.uuid.hex)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETION_REQUESTED)

    def test_unprovisioned_offering_user_not_deleted_when_auto_deletion_disabled(self):
        """Offering user in CREATION_REQUESTED state is NOT deleted when auto_deletion is off."""
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.test_user,
            offering=self.fixture.offering,
            state=OfferingUserStates.CREATION_REQUESTED,
        )

        self.fixture.project.add_user(self.test_user, ProjectRole.MANAGER)
        self.fixture.project.remove_user(self.test_user, ProjectRole.MANAGER)

        tasks.request_offering_user_deletion_for_user(self.test_user.uuid.hex)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.CREATION_REQUESTED)

    def test_offering_user_not_deleted_when_auto_deletion_explicitly_false(self):
        """Offering user is NOT deleted when auto_deletion is explicitly set to False."""
        self.fixture.offering.plugin_options["offering_user_auto_deletion"] = False
        self.fixture.offering.save()

        add_user_to_project(self.test_user, self.fixture.project)

        offering_user = marketplace_models.OfferingUser.objects.get(
            user=self.test_user, offering=self.fixture.offering
        )
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

        self.fixture.project.remove_user(self.test_user, ProjectRole.MANAGER)
        tasks.request_offering_user_deletion_for_user(self.test_user.uuid.hex)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.OK)


class UserOfferingsMappingRestorationTest(APITestCase):
    """Test that user_offerings_mapping restores offering users from DELETION_REQUESTED state."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.offering.plugin_options = {
            "service_provider_can_create_offering_user": True,
            "username_generation_policy": "waldur_username",
        }
        self.fixture.offering.save()

        self.resource = factories.ResourceFactory(
            offering=self.fixture.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )

        self.test_user = structure_factories.UserFactory(username="test_user")
        self.fixture.project.add_user(self.test_user, ProjectRole.MANAGER)

    def test_restores_deletion_requested_user_with_username(self):
        """Offering user in DELETION_REQUESTED with username is restored to OK."""
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.test_user,
            offering=self.fixture.offering,
            state=OfferingUserStates.OK,
            username="test_user",
        )
        offering_user.request_deletion()
        offering_user.save()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETION_REQUESTED)

        utils.user_offerings_mapping([self.fixture.offering])

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.OK)

    def test_restores_deletion_requested_user_without_username(self):
        """Offering user in DELETION_REQUESTED without username is set to CREATION_REQUESTED."""
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.test_user,
            offering=self.fixture.offering,
            state=OfferingUserStates.OK,
            username="",
        )
        offering_user.request_deletion()
        offering_user.save()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETION_REQUESTED)

        utils.user_offerings_mapping([self.fixture.offering])

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.CREATION_REQUESTED)

    def test_does_not_restore_deleting_user(self):
        """Offering user in DELETING state is left untouched."""
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.test_user,
            offering=self.fixture.offering,
            state=OfferingUserStates.OK,
            username="test_user",
        )
        offering_user.request_deletion()
        offering_user.set_deleting()
        offering_user.save()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETING)

        utils.user_offerings_mapping([self.fixture.offering])

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETING)

    def test_does_not_restore_deleted_user(self):
        """Offering user in DELETED state is left untouched."""
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.test_user,
            offering=self.fixture.offering,
            state=OfferingUserStates.CREATION_REQUESTED,
        )
        offering_user.set_deleted()
        offering_user.save()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETED)

        utils.user_offerings_mapping([self.fixture.offering])

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.state, OfferingUserStates.DELETED)


class OfferingUserCreationHandlerWhenOrderIsValidTest(APITestCase):
    """
    Tests for create_offering_users_if_order_is_valid handler. Offering users are created when order reaches PENDING_PROVIDER or EXECUTING.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.offering.plugin_options = {
            "service_provider_can_create_offering_user": True,
        }
        self.fixture.offering.save()

        self.project_user = structure_factories.UserFactory()
        self.fixture.project.add_user(self.project_user, ProjectRole.MANAGER)

        self.order = factories.OrderFactory(
            project=self.fixture.project,
            offering=self.fixture.offering,
            plan=self.fixture.plan,
            state=OrderStates.PENDING_CONSUMER,
            type=OrderTypes.CREATE,
        )

    def _call_handler_for_state_change(self, new_state):
        self.order.state = new_state
        marketplace_handlers.create_offering_users_if_order_is_valid(
            sender=None, instance=self.order, created=False
        )
        self.order.save()
        tasks.create_or_restore_offering_users_for_project(
            self.fixture.project.uuid.hex
        )

    def _offering_user_exists(self):
        return marketplace_models.OfferingUser.objects.filter(
            user=self.project_user,
            offering=self.fixture.offering,
        ).exists()

    def test_offering_users_created_for_existing_members_when_order_reaches_pending_provider(
        self,
    ):
        self._call_handler_for_state_change(OrderStates.PENDING_PROVIDER)
        self.assertTrue(self._offering_user_exists())

    def test_offering_users_created_when_order_transitions_to_executing(self):
        self._call_handler_for_state_change(OrderStates.EXECUTING)
        self.assertTrue(self._offering_user_exists())

    def test_no_offering_user_when_order_state_has_not_changed(self):
        marketplace_handlers.create_offering_users_if_order_is_valid(
            sender=None, instance=self.order, created=False
        )
        self.assertFalse(self._offering_user_exists())

    def test_no_offering_user_when_order_transitions_to_unrelated_state(self):
        self.order.state = OrderStates.REJECTED
        marketplace_handlers.create_offering_users_if_order_is_valid(
            sender=None, instance=self.order, created=False
        )
        self.assertFalse(self._offering_user_exists())

    def test_no_offering_user_for_non_create_order_type(self):
        self.order.type = OrderTypes.UPDATE
        self.order.state = OrderStates.PENDING_PROVIDER
        marketplace_handlers.create_offering_users_if_order_is_valid(
            sender=None, instance=self.order, created=False
        )
        self.assertFalse(self._offering_user_exists())

    def test_no_offering_user_when_plugin_option_disabled(self):
        self.fixture.offering.plugin_options = {
            "service_provider_can_create_offering_user": False,
        }
        self.fixture.offering.save()
        self.order.state = OrderStates.PENDING_PROVIDER
        marketplace_handlers.create_offering_users_if_order_is_valid(
            sender=None, instance=self.order, created=False
        )
        self.assertFalse(self._offering_user_exists())

    def test_no_offering_user_for_disallowed_offering_type(self):
        self.fixture.offering.type = "Marketplace.OpenStack"
        self.fixture.offering.save()
        self.order.state = OrderStates.PENDING_PROVIDER
        marketplace_handlers.create_offering_users_if_order_is_valid(
            sender=None, instance=self.order, created=False
        )
        self.assertFalse(self._offering_user_exists())

    def test_offering_user_not_duplicated_if_already_exists(self):
        marketplace_models.OfferingUser.objects.create(
            offering=self.fixture.offering,
            user=self.project_user,
            username="existing",
        )
        self._call_handler_for_state_change(OrderStates.PENDING_PROVIDER)
        count = marketplace_models.OfferingUser.objects.filter(
            user=self.project_user,
            offering=self.fixture.offering,
        ).count()
        self.assertEqual(count, 1)


class CreateOrRestoreOfferingUsersTaskOrderStateFilterTest(APITestCase):
    """
    Tests for the create_or_restore_offering_users_for_user task state filter.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.offering.plugin_options = {
            "service_provider_can_create_offering_user": True,
        }
        self.fixture.offering.save()

        self.project_user = structure_factories.UserFactory()
        self.fixture.project.add_user(self.project_user, ProjectRole.MANAGER)

    def _make_resource_with_order(
        self, resource_state, order_state, order_type=OrderTypes.CREATE
    ):
        resource = factories.ResourceFactory(
            offering=self.fixture.offering,
            project=self.fixture.project,
            state=resource_state,
        )
        factories.OrderFactory(
            project=self.fixture.project,
            offering=self.fixture.offering,
            plan=self.fixture.plan,
            resource=resource,
            state=order_state,
            type=order_type,
        )
        return resource

    def _run_task(self):
        tasks.create_or_restore_offering_users_for_user(
            self.project_user.uuid.hex,
            self.fixture.project.uuid.hex,
        )

    def _offering_user_exists(self):
        return marketplace_models.OfferingUser.objects.filter(
            user=self.project_user,
            offering=self.fixture.offering,
        ).exists()

    def test_no_offering_user_while_order_pending_consumer(self):
        """Customer hasn't approved yet — SP must not see users at this stage."""
        self._make_resource_with_order(
            ResourceStates.CREATING, OrderStates.PENDING_CONSUMER
        )
        self._run_task()
        self.assertFalse(self._offering_user_exists())

    def test_fix_offering_user_created_once_order_reaches_pending_provider(self):
        """fix: resource still CREATING but order approved → offering user created."""
        self._make_resource_with_order(
            ResourceStates.CREATING, OrderStates.PENDING_PROVIDER
        )
        self._run_task()
        self.assertTrue(self._offering_user_exists())

    def test_offering_user_created_when_resource_creating_and_order_executing(self):
        self._make_resource_with_order(ResourceStates.CREATING, OrderStates.EXECUTING)
        self._run_task()
        self.assertTrue(self._offering_user_exists())

    def test_offering_user_created_when_resource_ok(self):
        self._make_resource_with_order(ResourceStates.OK, OrderStates.DONE)
        self._run_task()
        self.assertTrue(self._offering_user_exists())

    def test_no_offering_user_when_order_pending_project(self):
        self._make_resource_with_order(
            ResourceStates.CREATING, OrderStates.PENDING_PROJECT
        )
        self._run_task()
        self.assertFalse(self._offering_user_exists())

    def test_no_offering_user_for_erred_resource(self):
        self._make_resource_with_order(ResourceStates.ERRED, OrderStates.ERRED)
        self._run_task()
        self.assertFalse(self._offering_user_exists())
