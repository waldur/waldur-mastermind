import datetime
from unittest import mock

from django.db import transaction
from rest_framework.test import APITransactionTestCase

from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import models as structure_tests_models
from waldur_mastermind.marketplace import PLUGIN_NAME, callbacks, utils
from waldur_mastermind.marketplace import handlers as marketplace_handlers
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import OrderStates, ResourceStates
from waldur_mastermind.marketplace.tests import factories, fixtures


class ResourceHandlerTest(APITransactionTestCase):
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
        fixture = fixtures.MarketplaceFixture()

        # Get the existing component and make it LIMIT type
        offering_component = fixture.offering_component
        offering_component.billing_type = (
            marketplace_models.OfferingComponent.BillingTypes.LIMIT
        )
        offering_component.save()

        # Set initial resource state
        fixture.resource.limits = {offering_component.type: 20}
        fixture.resource.state = ResourceStates.OK
        fixture.resource.plan = fixture.plan
        fixture.resource.save()  # Save initial state

        # Create update order
        order = fixture.update_order
        order.limits = {offering_component.type: 16}
        order.save()

        # Clear existing events
        Event.objects.filter(
            event_type="marketplace_resource_update_succeeded",
            context__resource_uuid=str(fixture.resource.uuid),
        ).delete()

        # Execute callback
        with transaction.atomic():
            callbacks.resource_update_succeeded(fixture.resource)

        event_count = Event.objects.filter(
            event_type="marketplace_resource_update_succeeded",
            context__resource_uuid=str(fixture.resource.uuid),
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
        fixture = fixtures.MarketplaceFixture()
        Event.objects.all().delete()
        # Update a blacklisted field ( backend_metadata )
        fixture.resource.backend_metadata = {"some": "metadata"}
        fixture.resource.save()

        event_count = Event.objects.filter(
            event_type="marketplace_resource_update_succeeded",
            context__resource_uuid=str(fixture.resource.uuid),
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
        fixture = fixtures.MarketplaceFixture()

        # Clear existing events
        Event.objects.all().delete()

        # Update multiple non-blacklisted fields
        fixture.resource.name = "New name"
        fixture.resource.description = "New description"
        fixture.resource.save()

        event_count = Event.objects.filter(
            event_type="marketplace_resource_update_succeeded",
            context__resource_uuid=str(fixture.resource.uuid),
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


class UpdateOfferingUserUsernameAfterUserChangeTest(APITransactionTestCase):
    def setUp(self):
        self.offering = factories.OfferingFactory(
            type=PLUGIN_NAME,
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
            type=PLUGIN_NAME,
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
