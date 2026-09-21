from datetime import timedelta
from unittest import mock

from constance.test.unittest import override_config
from django.utils import timezone
from rest_framework import status, test

from waldur_core.changelog.models import ChangelogImpactAnalysis
from waldur_core.structure.tests.factories import UserFactory


class VersionApiPermissionTest(test.APITestCase):
    def setUp(self):
        self.user = UserFactory()
        self.version_url = "http://testserver/api/version/"

    def test_authenticated_user_can_access_version(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.get(self.version_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("version", response.data)
        self.assertNotIn("latest_version", response.data)

    def test_anonymous_user_can_not_access_version(self):
        response = self.client.get(self.version_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class VersionUpdateCheckTest(test.APITestCase):
    def setUp(self):
        self.staff = UserFactory(is_staff=True)
        self.support = UserFactory(is_support=True)
        self.regular_user = UserFactory()
        self.version_url = "http://testserver/api/version/"

    @override_config(CHECK_FOR_UPDATES=False)
    @mock.patch("waldur_core.core.views.fetch_changelog_index")
    @mock.patch("waldur_core.core.views.requests.get")
    def test_no_github_request_when_update_check_disabled(
        self, mock_get, mock_fetch_changelog_index
    ):
        # Isolate the legacy GitHub-tag fallback this test targets: without
        # this, requests.get is shared with fetch_changelog_index() (same
        # `requests` module), so an unconfigured response leaks into the
        # changelog fetch and crashes on caching a MagicMock.
        mock_fetch_changelog_index.return_value = None
        self.client.force_authenticate(user=self.staff)

        response = self.client.get(self.version_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("latest_version", response.data)
        mock_get.assert_not_called()

    @override_config(CHECK_FOR_UPDATES=True)
    @mock.patch("waldur_core.core.views.fetch_changelog_index")
    @mock.patch("waldur_core.core.views.cache")
    @mock.patch("waldur_core.core.views.requests.get")
    def test_staff_gets_latest_version_when_update_check_enabled(
        self, mock_get, mock_cache, mock_fetch_changelog_index
    ):
        mock_fetch_changelog_index.return_value = None
        mock_cache.get.return_value = None
        mock_get.return_value.json.return_value = [
            {"ref": "refs/tags/1.0.0"},
            {"ref": "refs/tags/2.0.0"},
        ]
        self.client.force_authenticate(user=self.staff)

        response = self.client.get(self.version_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["latest_version"], "2.0.0")
        mock_get.assert_called_once()

    @override_config(CHECK_FOR_UPDATES=True)
    @mock.patch("waldur_core.core.views.fetch_changelog_index")
    @mock.patch("waldur_core.core.views.cache")
    @mock.patch("waldur_core.core.views.requests.get")
    def test_support_gets_latest_version_when_update_check_enabled(
        self, mock_get, mock_cache, mock_fetch_changelog_index
    ):
        mock_fetch_changelog_index.return_value = None
        mock_cache.get.return_value = None
        mock_get.return_value.json.return_value = [
            {"ref": "refs/tags/1.0.0"},
            {"ref": "refs/tags/2.0.0"},
        ]
        self.client.force_authenticate(user=self.support)

        response = self.client.get(self.version_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["latest_version"], "2.0.0")
        mock_get.assert_called_once()

    @override_config(CHECK_FOR_UPDATES=True)
    @mock.patch("waldur_core.core.views.requests.get")
    def test_regular_user_does_not_trigger_github_request(self, mock_get):
        self.client.force_authenticate(user=self.regular_user)

        response = self.client.get(self.version_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("version", response.data)
        self.assertNotIn("latest_version", response.data)
        mock_get.assert_not_called()


CURRENT = "8.0.7"
TARGET = "8.0.8"
INDEX = {
    "latest_stable": TARGET,
    "releases": [{"version": TARGET, "type": "stable", "has_breaking": True}],
}


@mock.patch("waldur_core.core.views.__version__", CURRENT)
@mock.patch("waldur_core.core.views.fetch_changelog_index", return_value=INDEX)
@mock.patch("waldur_core.core.views.compute_changelog_impact")
class TriggerImpactAnalysisTest(test.APITestCase):
    """_trigger_impact_analysis_if_needed() must not re-queue an analysis
    that's already in flight, retry a reliably-failing one on every request,
    or leave one stuck in PENDING/RUNNING forever."""

    def setUp(self):
        self.client.force_authenticate(UserFactory(is_staff=True))
        self.version_url = "http://testserver/api/version/"

    def _hit(self, n):
        for _ in range(n):
            response = self.client.get(self.version_url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_stale_analysis_enqueues_once_not_per_request(self, task, _index):
        ChangelogImpactAnalysis.objects.create(
            current_version=CURRENT,
            target_version=TARGET,
            status=ChangelogImpactAnalysis.Status.COMPLETED,
            computed_at=timezone.now() - timedelta(days=2),
        )
        # Task is queued but no worker has picked it up yet (mocked, so the
        # DB row's status never advances past PENDING).
        self._hit(5)
        self.assertEqual(task.delay.call_count, 1)

    def test_failed_analysis_retries_without_hammering(self, task, _index):
        ChangelogImpactAnalysis.objects.create(
            current_version=CURRENT,
            target_version=TARGET,
            status=ChangelogImpactAnalysis.Status.FAILED,
        )

        # Simulate a task that fails deterministically every time it runs.
        def fail(current, target):
            ChangelogImpactAnalysis.objects.filter(
                current_version=current, target_version=target
            ).update(status=ChangelogImpactAnalysis.Status.FAILED)

        task.delay.side_effect = fail
        self._hit(5)
        self.assertLessEqual(task.delay.call_count, 1)

    def test_stuck_running_analysis_is_eventually_retried(self, task, _index):
        analysis = ChangelogImpactAnalysis.objects.create(
            current_version=CURRENT,
            target_version=TARGET,
            status=ChangelogImpactAnalysis.Status.RUNNING,
        )
        # Worker died a week ago mid-task: status never left RUNNING.
        ChangelogImpactAnalysis.objects.filter(pk=analysis.pk).update(
            modified=timezone.now() - timedelta(days=7)
        )
        self._hit(1)
        self.assertEqual(task.delay.call_count, 1)
