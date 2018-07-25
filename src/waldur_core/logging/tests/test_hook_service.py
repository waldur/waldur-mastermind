import logging
import time

from django.conf import settings
from django.core import mail
from rest_framework import test
from six.moves import mock

from waldur_core.logging import models as logging_models
from waldur_core.logging.log import HookHandler
from waldur_core.logging.tasks import process_event
from waldur_core.structure import models as structure_models
from waldur_core.structure.log import event_logger
from waldur_core.structure.tests import factories as structure_factories


class TestHookService(test.APITransactionTestCase):
    def setUp(self):
        self.owner = structure_factories.UserFactory()
        self.customer = structure_factories.CustomerFactory()
        self.customer.add_user(self.owner, structure_models.CustomerRole.OWNER)
        self.other_user = structure_factories.UserFactory()

        self.event_type = 'customer_update_succeeded'
        self.other_event = 'customer_deletion_succeeded'
        self.message = 'Customer {customer_name} has been updated.'
        self.event = {
            'message': self.message,
            'type': self.event_type,
            'context': event_logger.customer.compile_context(customer=self.customer),
            'timestamp': time.time()
        }

        # Create email hook for another user
        self.other_hook = logging_models.EmailHook.objects.create(user=self.other_user,
                                                                  email=self.owner.email,
                                                                  event_types=[self.event_type])

    @mock.patch('celery.app.base.Celery.send_task')
    def test_logger_handler_sends_task_if_handler_attached(self, mocked_task):
        # Prepare logger
        logger = logging.getLogger('waldur_core')
        logger.setLevel(logging.DEBUG)

        # Inject handler
        handler = HookHandler()
        logger.addHandler(handler)

        event_logger.customer.warning(self.message,
                                      event_type=self.event_type,
                                      event_context={'customer': self.customer})

        mocked_task.assert_called_once_with('waldur_core.logging.process_event', mock.ANY, {}, countdown=2)
        mocked_task.reset_mock()

        # Remove hook handler so that other tests won't depend on it
        logger.removeHandler(handler)

        # Trigger an event
        event_logger.customer.warning(self.message,
                                      event_type=self.event_type,
                                      event_context={'customer': self.customer})

        # If hook handler is not attached hook is not processed
        self.assertFalse(mocked_task.called)

    def test_email_hook_filters_events_by_user_and_event_type(self):
        # Create email hook for customer owner
        email_hook = logging_models.EmailHook.objects.create(user=self.owner,
                                                             email=self.owner.email,
                                                             event_types=[self.event_type])

        # Trigger processing
        process_event(self.event)

        # Test that one message has been sent for email hook of customer owner
        self.assertEqual(len(mail.outbox), 1)

        # Verify that destination address of message is correct
        self.assertEqual(mail.outbox[0].to, [email_hook.email])

    @mock.patch('requests.post')
    def test_webhook_makes_post_request_against_destination_url(self, requests_post):

        # Create web hook for customer owner
        self.web_hook = logging_models.WebHook.objects.create(user=self.owner,
                                                              destination_url='http://example.com/',
                                                              event_types=[self.event_type])

        # Trigger processing
        process_event(self.event)

        # Event is captured and POST request is triggered because event_type and user_uuid match
        requests_post.assert_called_once_with(
            self.web_hook.destination_url, json=mock.ANY, verify=settings.VERIFY_WEBHOOK_REQUESTS)

    def test_email_hook_processor_can_be_called_twice(self):
        # Create email hook for customer owner
        email_hook = logging_models.EmailHook.objects.create(user=self.owner,
                                                             email=self.owner.email,
                                                             event_types=[self.event_type])

        # If event is not mutated, exception is not raised, see also SENTRY-1396
        email_hook.process(self.event)
        email_hook.process(self.event)
