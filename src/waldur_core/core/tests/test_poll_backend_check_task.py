import pickle  # noqa: S403
from unittest.mock import Mock

from celery.exceptions import Retry
from django.test import SimpleTestCase

from waldur_core.core.tasks import PollBackendCheckTask
from waldur_core.structure.exceptions import (
    ServiceBackendError,
    ServiceBackendRateLimited,
)


class PollBackendCheckTaskRateLimitTest(SimpleTestCase):
    def setUp(self):
        self.task = PollBackendCheckTask()
        self.task.retry = Mock(side_effect=Retry())
        self.backend = Mock()
        self.instance = Mock(pk=1)
        self.instance.get_backend.return_value = self.backend

    def _execute_with_error(self, error):
        self.backend.is_deleted.side_effect = error
        with self.assertRaises(Retry):
            self.task.execute(self.instance, "is_deleted")
        return self.task.retry.call_args.kwargs

    def test_rate_limit_retries_after_requested_delay(self):
        error = ServiceBackendRateLimited("Rate limit", retry_after=30)

        kwargs = self._execute_with_error(error)

        self.assertEqual(kwargs["countdown"], 30)
        self.assertIs(kwargs["exc"], error)

    def test_rate_limit_without_delay_uses_poll_interval(self):
        kwargs = self._execute_with_error(ServiceBackendRateLimited("Rate limit"))

        self.assertEqual(kwargs["countdown"], PollBackendCheckTask.default_retry_delay)

    def test_rate_limit_delay_is_capped(self):
        kwargs = self._execute_with_error(
            ServiceBackendRateLimited("Rate limit", retry_after=86400)
        )

        self.assertEqual(kwargs["countdown"], PollBackendCheckTask.max_rate_limit_delay)

    def test_other_backend_errors_are_not_retried(self):
        self.backend.is_deleted.side_effect = ServiceBackendError("boom")

        with self.assertRaises(ServiceBackendError):
            self.task.execute(self.instance, "is_deleted")

        self.task.retry.assert_not_called()

    def test_rate_limit_error_survives_pickling(self):
        error = pickle.loads(  # noqa: S301
            pickle.dumps(ServiceBackendRateLimited("Rate limit", retry_after=7))
        )

        self.assertEqual(error.retry_after, 7)
        self.assertEqual(str(error.args[0]), "Rate limit")
