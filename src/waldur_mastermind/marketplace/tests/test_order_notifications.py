from unittest import mock

from django.core import mail
from rest_framework import status, test

from waldur_core.core.utils import get_system_robot
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import serializers, tasks
from waldur_mastermind.marketplace.enums import OrderStates, OrderTypes
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class NewOrderNotificationTest(test.APITestCase):
    """Recipients configured on the offering are notified about a new order."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.order = self.fixture.order
        self.notification = structure_factories.NotificationFactory(
            key="marketplace.notify_about_new_order"
        )

    def set_options(self, **options):
        self.offering.secret_options.update(options)
        self.offering.save(update_fields=["secret_options"])

    def test_explicit_addresses_are_notified(self):
        self.set_options(
            order_notification_emails=["ops@example.com", "billing@example.com"]
        )

        tasks.notify_about_new_order(self.order.uuid.hex)

        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(
            sorted(message.to[0] for message in mail.outbox),
            ["billing@example.com", "ops@example.com"],
        )

    def test_organization_role_is_resolved_on_provider_customer(self):
        owner = self.fixture.offering_owner
        self.set_options(order_notification_roles=[RoleEnum.CUSTOMER_OWNER])

        tasks.notify_about_new_order(self.order.uuid.hex)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [owner.email])

    def test_offering_role_is_resolved_on_offering(self):
        manager = self.fixture.offering_manager
        self.set_options(order_notification_roles=[RoleEnum.OFFERING_MANAGER])

        tasks.notify_about_new_order(self.order.uuid.hex)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [manager.email])

    def test_user_with_disabled_notifications_is_skipped(self):
        owner = self.fixture.offering_owner
        owner.notifications_enabled = False
        owner.save(update_fields=["notifications_enabled"])
        self.set_options(
            order_notification_emails=["ops@example.com"],
            order_notification_roles=[RoleEnum.CUSTOMER_OWNER],
        )

        tasks.notify_about_new_order(self.order.uuid.hex)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ops@example.com"])

    def test_option_holding_a_bare_string_is_ignored(self):
        """secret_options is hand-editable as raw JSON in the Django admin, where
        a string would otherwise fan out into one recipient per character."""
        self.set_options(order_notification_emails="ops@example.com")

        tasks.notify_about_new_order(self.order.uuid.hex)

        self.assertEqual(len(mail.outbox), 0)

    def test_non_string_members_of_an_option_are_ignored(self):
        self.set_options(order_notification_emails=["ops@example.com", None, 42])

        tasks.notify_about_new_order(self.order.uuid.hex)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ops@example.com"])

    def test_order_type_is_named_in_the_subject(self):
        self.order.type = OrderTypes.TERMINATE
        self.order.save(update_fields=["type"])
        self.set_options(order_notification_emails=["ops@example.com"])

        tasks.notify_about_new_order(self.order.uuid.hex)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("new terminate order", mail.outbox[0].subject)

    def test_order_without_creator_is_rendered_without_a_blank_name(self):
        self.order.created_by = None
        self.order.save(update_fields=["created_by"])
        self.set_options(order_notification_emails=["ops@example.com"])

        tasks.notify_about_new_order(self.order.uuid.hex)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("placed by a user", mail.outbox[0].subject)

    def test_no_mail_is_sent_when_no_recipient_is_configured(self):
        tasks.notify_about_new_order(self.order.uuid.hex)

        self.assertEqual(len(mail.outbox), 0)

    def test_no_mail_is_sent_when_notification_is_disabled(self):
        self.notification.enabled = False
        self.notification.save(update_fields=["enabled"])
        self.set_options(order_notification_emails=["ops@example.com"])

        tasks.notify_about_new_order(self.order.uuid.hex)

        self.assertEqual(len(mail.outbox), 0)

    def test_no_mail_is_sent_when_notification_key_is_absent(self):
        self.notification.delete()
        self.set_options(order_notification_emails=["ops@example.com"])

        tasks.notify_about_new_order(self.order.uuid.hex)

        self.assertEqual(len(mail.outbox), 0)


@mock.patch("waldur_mastermind.marketplace.tasks.notify_about_new_order.delay")
class NewOrderNotificationTriggerTest(test.APITestCase):
    """The notification is scheduled by order creation, not by the approval flow."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering

    def set_options(self, **options):
        self.offering.secret_options.update(options)
        self.offering.save(update_fields=["secret_options"])

    def create_order(self, state, **kwargs):
        kwargs.setdefault("created_by", self.fixture.manager)
        with self.captureOnCommitCallbacks(execute=True):
            return factories.OrderFactory(
                project=self.fixture.project,
                offering=self.offering,
                plan=self.fixture.plan,
                state=state,
                **kwargs,
            )

    def test_auto_approved_order_is_notified(self, mock_delay):
        """The regression this feature exists for: an order which never enters an
        approval state produces no approval mail, but must still notify."""
        self.set_options(order_notification_emails=["ops@example.com"])

        order = self.create_order(OrderStates.EXECUTING)

        mock_delay.assert_called_once_with(order.uuid)

    def test_pending_order_is_notified(self, mock_delay):
        self.set_options(order_notification_roles=[RoleEnum.CUSTOMER_OWNER])

        order = self.create_order(OrderStates.PENDING_PROVIDER)

        mock_delay.assert_called_once_with(order.uuid)

    def test_task_is_not_scheduled_when_no_option_is_set(self, mock_delay):
        self.create_order(OrderStates.EXECUTING)

        mock_delay.assert_not_called()

    def test_task_is_not_scheduled_for_an_order_created_in_a_terminal_state(
        self, mock_delay
    ):
        """Reconciliation sweeps and import commands write orders directly in a
        terminal state as audit records. Nobody placed those."""
        self.set_options(order_notification_emails=["ops@example.com"])

        self.create_order(OrderStates.DONE)

        mock_delay.assert_not_called()

    def test_task_is_not_scheduled_for_a_robot_order(self, mock_delay):
        """The cost-policy sweep terminates over-budget resources one order per
        resource, in a non-terminal state, on behalf of the system robot."""
        self.set_options(order_notification_emails=["ops@example.com"])

        self.create_order(OrderStates.EXECUTING, created_by=get_system_robot())

        mock_delay.assert_not_called()

    def test_task_is_not_scheduled_for_an_order_without_a_creator(self, mock_delay):
        self.set_options(order_notification_emails=["ops@example.com"])

        self.create_order(OrderStates.EXECUTING, created_by=None)

        mock_delay.assert_not_called()

    def test_task_is_not_scheduled_on_order_update(self, mock_delay):
        self.set_options(order_notification_emails=["ops@example.com"])
        order = self.create_order(OrderStates.EXECUTING)
        mock_delay.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            order.state = OrderStates.DONE
            order.save(update_fields=["state"])

        mock_delay.assert_not_called()


class OrderNotificationOptionsUpdateTest(test.APITestCase):
    """The options are editable by anyone who can already edit the offering."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)

    def test_offering_owner_can_set_both_options(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        url = factories.OfferingFactory.get_url(
            self.fixture.offering, "update_integration"
        )

        response = self.client.post(
            url,
            {
                "secret_options": {
                    "order_notification_emails": ["ops@example.com"],
                    "order_notification_roles": [RoleEnum.CUSTOMER_OWNER],
                }
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.fixture.offering.refresh_from_db()
        self.assertEqual(
            self.fixture.offering.secret_options["order_notification_emails"],
            ["ops@example.com"],
        )
        self.assertEqual(
            self.fixture.offering.secret_options["order_notification_roles"],
            [RoleEnum.CUSTOMER_OWNER],
        )

    def test_options_are_not_exposed_to_marketplace_consumers(self):
        """The addresses are provider-internal contact details, and the public
        offering endpoint is readable by every authenticated user."""
        offering = self.fixture.offering
        offering.state = offering.States.ACTIVE
        offering.shared = True
        offering.secret_options["order_notification_emails"] = ["ops@example.com"]
        offering.save()
        self.client.force_authenticate(structure_factories.UserFactory())

        response = self.client.get(
            factories.OfferingFactory.get_public_url(offering),
            {"field": ["secret_options", "plugin_options"]},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("secret_options", response.data)
        self.assertNotIn(
            "order_notification_emails", response.data.get("plugin_options", {})
        )


class OrderNotificationOptionsValidationTest(test.APITestCase):
    def test_valid_organization_role_is_accepted(self):
        serializer = serializers.MergedSecretOptionsSerializer(
            data={"order_notification_roles": [CustomerRole.OWNER.name]}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_valid_offering_role_is_accepted(self):
        serializer = serializers.MergedSecretOptionsSerializer(
            data={"order_notification_roles": [OfferingRole.MANAGER.name]}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_project_role_is_rejected(self):
        serializer = serializers.MergedSecretOptionsSerializer(
            data={"order_notification_roles": [ProjectRole.MANAGER.name]}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("order_notification_roles", serializer.errors)

    def test_unknown_role_is_rejected(self):
        serializer = serializers.MergedSecretOptionsSerializer(
            data={"order_notification_roles": ["NONEXISTENT.ROLE"]}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("order_notification_roles", serializer.errors)

    def test_more_than_ten_roles_are_rejected(self):
        serializer = serializers.MergedSecretOptionsSerializer(
            data={
                "order_notification_roles": [
                    CustomerRole.OWNER.name
                    for _ in range(serializers.MAX_ORDER_NOTIFICATION_ROLES + 1)
                ]
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("order_notification_roles", serializer.errors)

    def test_valid_addresses_are_accepted(self):
        serializer = serializers.MergedSecretOptionsSerializer(
            data={"order_notification_emails": ["ops@example.com"]}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_malformed_address_is_rejected(self):
        serializer = serializers.MergedSecretOptionsSerializer(
            data={"order_notification_emails": ["not-an-address"]}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("order_notification_emails", serializer.errors)

    def test_more_than_ten_addresses_are_rejected(self):
        serializer = serializers.MergedSecretOptionsSerializer(
            data={
                "order_notification_emails": [
                    f"ops{index}@example.com"
                    for index in range(serializers.MAX_ORDER_NOTIFICATION_EMAILS + 1)
                ]
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("order_notification_emails", serializer.errors)
