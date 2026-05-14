from unittest import mock

from django.conf import settings
from django.core import mail
from rest_framework import test

from waldur_core.logging import models as logging_models
from waldur_core.logging.tasks import process_event
from waldur_core.logging.tests.factories import EventFactory
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories


class TestHookService(test.APITestCase):
    def setUp(self):
        self.owner = structure_factories.UserFactory()
        self.customer = structure_factories.CustomerFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)
        self.other_user = structure_factories.UserFactory()

        self.event_type = "customer_update_succeeded"
        self.other_event = "customer_deletion_succeeded"
        self.message = "Customer {customer_name} has been updated."
        self.event = EventFactory(event_type=self.event_type)
        self.payload = dict(
            created=self.event.created.isoformat(),
            message=self.event.message,
            context=self.event.context,
            event_type=self.event.event_type,
        )
        logging_models.Feed.objects.create(scope=self.customer, event=self.event)

        # Create email hook for another user
        self.other_hook = logging_models.EmailHook.objects.create(
            user=self.other_user, email=self.owner.email, event_types=[self.event_type]
        )

    def test_email_hook_filters_events_by_user_and_event_type(self):
        # Create email hook for customer owner
        email_hook = logging_models.EmailHook.objects.create(
            user=self.owner, email=self.owner.email, event_types=[self.event_type]
        )

        # Trigger processing
        process_event(self.event.id)

        # Test that one message has been sent for email hook of customer owner
        self.assertEqual(len(mail.outbox), 1)

        # Verify that destination address of message is correct
        self.assertEqual(mail.outbox[0].to, [email_hook.email])

    @mock.patch("requests.post")
    def test_webhook_makes_post_request_against_destination_url(self, requests_post):
        # Create web hook for customer owner
        self.web_hook = logging_models.WebHook.objects.create(
            user=self.owner,
            destination_url="http://example.com/",
            event_types=[self.event_type],
        )

        # Trigger processing
        process_event(self.event.id)

        # Event is captured and POST request is triggered because event_type and user_uuid match
        requests_post.assert_called_once()
        args, kwargs = requests_post.call_args
        self.assertEqual(args, (self.web_hook.destination_url,))
        self.assertEqual(kwargs["json"], self.payload)
        self.assertEqual(kwargs["verify"], settings.VERIFY_WEBHOOK_REQUESTS)
        # SEC-C9: outbound webhooks must disable redirects and set a timeout.
        self.assertEqual(kwargs["allow_redirects"], False)
        self.assertIsNotNone(kwargs.get("timeout"))

    def test_email_hook_processor_can_be_called_twice(self):
        # Create email hook for customer owner
        email_hook = logging_models.EmailHook.objects.create(
            user=self.owner, email=self.owner.email, event_types=[self.event_type]
        )

        # If event is not mutated, exception is not raised, see also SENTRY-1396
        email_hook.process(self.event)
        email_hook.process(self.event)
