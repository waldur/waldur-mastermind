import mock
from ddt import data, ddt
from django.core import mail, signing
from django.test import override_settings
from rest_framework import status

from waldur_core.core import utils as core_utils

from .. import models, tasks
from . import base, factories


@ddt
class FeedbackCreateTest(base.BaseTest):
    @data(
        'staff', 'owner', 'admin', 'manager', 'user', '',
    )
    def test_user_can_create_feedback(self, user):
        url = factories.FeedbackFactory.get_list_url()
        issue = factories.IssueFactory()
        signer = signing.TimestampSigner()
        token = signer.sign(issue.uuid.hex)

        if user:
            self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(
            url,
            data={'evaluation': models.Feedback.Evaluation.POSITIVE, 'token': token},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_user_cannot_create_feedback_if_token_is_wrong(self):
        url = factories.FeedbackFactory.get_list_url()
        token = 'token'

        response = self.client.post(
            url,
            data={'evaluation': models.Feedback.Evaluation.POSITIVE, 'token': token},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_create_feedback_if_it_already_exists(self):
        url = factories.FeedbackFactory.get_list_url()
        feedback = factories.FeedbackFactory()
        issue = feedback.issue
        signer = signing.TimestampSigner()
        token = signer.sign(issue.uuid.hex)

        response = self.client.post(
            url,
            data={'evaluation': models.Feedback.Evaluation.POSITIVE, 'token': token},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class FeedbackNotificationTest(base.BaseTest):
    @mock.patch(
        'waldur_mastermind.support.handlers.tasks.send_issue_feedback_notification'
    )
    @override_settings(ISSUE_FEEDBACK_ENABLE=True)
    def test_feedback_notification(self, mock_tasks):
        factories.IssueStatusFactory(
            name='resolved', type=models.IssueStatus.Types.RESOLVED
        )
        factories.IssueStatusFactory(
            name='canceled', type=models.IssueStatus.Types.CANCELED
        )
        issue = factories.IssueFactory()
        issue.set_resolved()
        serialized_issue = core_utils.serialize_instance(issue)
        mock_tasks.delay.assert_called_once_with(serialized_issue)

    def test_feedback_notification_text(self):
        issue = factories.IssueFactory()
        serialized_issue = core_utils.serialize_instance(issue)
        tasks.send_issue_feedback_notification(serialized_issue)
        self.assertEqual(len(mail.outbox), 1)
