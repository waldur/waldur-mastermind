import mock
from rest_framework import test

from waldur_core.core import utils as core_utils
from waldur_mastermind.support.tests import factories as support_factories

from . import factories, fixtures
from .. import tasks


@mock.patch('waldur_core.core.utils.send_mail')
class NewExpertRequestMailTest(test.APITransactionTestCase):
    def setUp(self):
        self.expert_fixture = fixtures.ExpertsFixture()
        self.expert_provider = factories.ExpertProviderFactory(customer=self.expert_fixture.customer)
        self.expert_manager = self.expert_fixture.owner
        self.expert_request = fixtures.ExpertsFixture().expert_request

    def test_when_new_expert_request_is_sent_to_all_expert_managers(self, send_mail_mock):
        tasks.send_new_request(self.expert_request.uuid.hex)
        send_mail_mock.assert_called_once()

    def test_email_is_not_sent_if_there_are_no_provider(self, send_mail_mock):
        self.expert_provider.delete()
        tasks.send_new_request(self.expert_request.uuid.hex)
        self.assertEqual(send_mail_mock.call_count, 0)

    def test_email_is_not_sent_if_there_are_no_active_providers(self, send_mail_mock):
        self.expert_provider.enable_notifications = False
        self.expert_provider.save()
        tasks.send_new_request(self.expert_request.uuid.hex)
        self.assertEqual(send_mail_mock.call_count, 0)

    def test_email_is_not_sent_if_expert_manager_does_not_have_email(self, send_mail_mock):
        self.expert_manager.email = ''
        self.expert_manager.save()

        tasks.send_new_request(self.expert_request.uuid.hex)

        self.assertEqual(send_mail_mock.call_count, 0)

    def test_planned_budget_is_rendered_in_text_message(self, send_mail_mock):
        self.expert_request.extra = {'price': 100}
        self.expert_request.save()

        tasks.send_new_request(self.expert_request.uuid.hex)

        message = send_mail_mock.call_args[0][1]
        self.assertTrue('100.0 EUR' in message)

    def test_site_name_is_rendered_in_html_message(self, send_mail_mock):
        tasks.send_new_request(self.expert_request.uuid.hex)

        message = send_mail_mock.call_args[1]['html_message']
        self.assertTrue('Waldur MasterMind' in message)


@mock.patch('waldur_core.core.utils.send_mail')
class NewExpertBidMailTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ExpertsFixture()
        self.bid = self.fixture.bid

    def test_when_new_expert_request_is_sent_to_customer_owners(self, send_mail_mock):
        self.fixture.owner
        tasks.send_new_bid(self.bid.uuid.hex)
        send_mail_mock.assert_called_once()

    def test_email_is_not_sent_if_there_are_owners(self, send_mail_mock):
        tasks.send_new_bid(self.bid.uuid.hex)
        self.assertEqual(send_mail_mock.call_count, 0)

    def test_expert_organization_name_is_rendered_in_text_message(self, send_mail_mock):
        self.fixture.owner
        tasks.send_new_bid(self.bid.uuid.hex)
        message = send_mail_mock.call_args[0][1]
        self.assertTrue(self.bid.team.customer.name in message)


@mock.patch('waldur_mastermind.experts.tasks._send_issue_notification')
class NewCommentMailTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ExpertsFixture()

    def test_send_notification_for_expert_if_user_added_comment(self, send_mock):
        self.comment = support_factories.CommentFactory(issue=self.fixture.issue)
        self.fixture.contract
        self.fixture.expert_request
        self.fixture.admin
        serialized_comment = core_utils.serialize_instance(self.comment)
        tasks.send_expert_comment_added_notification(serialized_comment)
        self.assertEqual(send_mock.call_count, 1)

    def test_dont_send_notification_for_expert_if_expert_added_comment(self, send_mock):
        expert_support_user = support_factories.SupportUserFactory(user=self.fixture.admin)
        self.comment = support_factories.CommentFactory(issue=self.fixture.issue,
                                                        author=expert_support_user)
        self.fixture.contract
        self.fixture.expert_request
        self.fixture.admin
        serialized_comment = core_utils.serialize_instance(self.comment)
        tasks.send_expert_comment_added_notification(serialized_comment)
        self.assertEqual(send_mock.call_count, 0)
