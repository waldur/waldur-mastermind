from unittest import mock

from constance.test.unittest import override_config
from rest_framework import test

from waldur_core.changelog.models import ChangelogImpactAnalysis
from waldur_core.changelog.tasks import compute_changelog_impact
from waldur_core.core.models import User


def _release(version, entry_id):
    return {
        "version": version,
        "date": "2026-01-01",
        "type": "stable",
        "summary": "Test release",
        "entries": [
            {
                "id": entry_id,
                "type": "breaking",
                "category": "auth",
                "title": f"Breaking change in {version}",
                "description": "Test description",
                "scope": "plugin",
                "component": ["backend"],
                "impact": {"risk": "high"},
                "relevant_when": {
                    "plugins": ["waldur_openstack"],
                    "feature_flags": [],
                    "settings": [],
                },
            },
        ],
    }


class ComputeChangelogImpactTest(test.APITestCase):
    @mock.patch("waldur_core.changelog.tasks.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.tasks.get_pending_versions")
    def test_only_analyzes_entries_up_to_target_version(
        self, mock_pending, mock_release
    ):
        # current -> 8.0.8 (target) -> 8.0.9 (beyond target, must be excluded)
        mock_pending.return_value = [
            {"version": "8.0.8", "type": "stable"},
            {"version": "8.0.9", "type": "stable"},
        ]
        mock_release.side_effect = lambda version: {
            "8.0.8": _release("8.0.8", "8.0.8-1"),
            "8.0.9": _release("8.0.9", "8.0.9-1"),
        }[version]

        compute_changelog_impact("8.0.7", "8.0.8")

        analysis = ChangelogImpactAnalysis.objects.get(
            current_version="8.0.7", target_version="8.0.8"
        )
        self.assertEqual(analysis.status, ChangelogImpactAnalysis.Status.COMPLETED)
        self.assertIn("8.0.8-1", analysis.results)
        self.assertNotIn("8.0.9-1", analysis.results)
        mock_release.assert_called_once_with("8.0.8")


def _release_with_impact(impact):
    return {
        "version": "8.0.8",
        "date": "2026-01-01",
        "type": "stable",
        "summary": "Test release",
        "entries": [
            {
                "id": "8.0.8-1",
                "type": "breaking",
                "category": "marketplace",
                "title": "Test entry",
                "description": "Test description",
                "scope": "core",
                "component": ["backend"],
                "impact": impact,
                "relevant_when": {"plugins": [], "feature_flags": [], "settings": []},
            },
        ],
    }


class ComputeChangelogImpactAllowlistTest(test.APITestCase):
    """Impact analysis builds ORM filter() calls from remote (docs.waldur.com)
    JSON. Only allowlisted (model, field) combinations may be queried - see
    ALLOWED_IMPACT_MODELS / ALLOWED_USER_IMPACT_FIELDS in tasks.py."""

    @mock.patch("waldur_core.changelog.tasks.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.tasks.get_pending_versions")
    def test_counts_resources_when_model_and_field_are_allowlisted(
        self, mock_pending, mock_release
    ):
        mock_pending.return_value = [{"version": "8.0.8", "type": "stable"}]
        mock_release.return_value = _release_with_impact(
            {
                "risk": "high",
                "affected_resources": {
                    "model": "marketplace.Resource",
                    "filter": {"state": 3},
                },
            }
        )

        compute_changelog_impact("8.0.7", "8.0.8")

        analysis = ChangelogImpactAnalysis.objects.get(
            current_version="8.0.7", target_version="8.0.8"
        )
        self.assertIn("affected_resources_count", analysis.results["8.0.8-1"])

    @mock.patch("waldur_core.changelog.tasks.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.tasks.get_pending_versions")
    def test_skips_resources_for_non_allowlisted_model(
        self, mock_pending, mock_release
    ):
        mock_pending.return_value = [{"version": "8.0.8", "type": "stable"}]
        mock_release.return_value = _release_with_impact(
            {
                "risk": "high",
                "affected_resources": {
                    "model": "core.User",
                    "filter": {"password__startswith": "a"},
                },
            }
        )

        compute_changelog_impact("8.0.7", "8.0.8")

        analysis = ChangelogImpactAnalysis.objects.get(
            current_version="8.0.7", target_version="8.0.8"
        )
        self.assertEqual(analysis.status, ChangelogImpactAnalysis.Status.COMPLETED)
        self.assertNotIn("8.0.8-1", analysis.results)

    @mock.patch("waldur_core.changelog.tasks.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.tasks.get_pending_versions")
    def test_skips_resources_for_non_allowlisted_lookup(
        self, mock_pending, mock_release
    ):
        mock_pending.return_value = [{"version": "8.0.8", "type": "stable"}]
        mock_release.return_value = _release_with_impact(
            {
                "risk": "high",
                "affected_resources": {
                    "model": "marketplace.Resource",
                    "filter": {"state__gte": 1},
                },
            }
        )

        compute_changelog_impact("8.0.7", "8.0.8")

        analysis = ChangelogImpactAnalysis.objects.get(
            current_version="8.0.7", target_version="8.0.8"
        )
        self.assertNotIn("8.0.8-1", analysis.results)

    @mock.patch("waldur_core.changelog.tasks.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.tasks.get_pending_versions")
    def test_counts_users_when_field_is_allowlisted(self, mock_pending, mock_release):
        mock_pending.return_value = [{"version": "8.0.8", "type": "stable"}]
        mock_release.return_value = _release_with_impact(
            {
                "risk": "high",
                "affected_users": {"filter": {"is_staff": True}},
            }
        )

        compute_changelog_impact("8.0.7", "8.0.8")

        analysis = ChangelogImpactAnalysis.objects.get(
            current_version="8.0.7", target_version="8.0.8"
        )
        self.assertIn("affected_users_count", analysis.results["8.0.8-1"])

    @mock.patch("waldur_core.changelog.tasks.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.tasks.get_pending_versions")
    def test_skips_users_for_non_allowlisted_field(self, mock_pending, mock_release):
        mock_pending.return_value = [{"version": "8.0.8", "type": "stable"}]
        mock_release.return_value = _release_with_impact(
            {
                "risk": "high",
                "affected_users": {"filter": {"password__icontains": "x"}},
            }
        )

        compute_changelog_impact("8.0.7", "8.0.8")

        analysis = ChangelogImpactAnalysis.objects.get(
            current_version="8.0.7", target_version="8.0.8"
        )
        self.assertNotIn("8.0.8-1", analysis.results)


class MalformedRemoteEntryTest(test.APITestCase):
    """One malformed remote entry must not fail the whole analysis."""

    @mock.patch("waldur_core.changelog.tasks.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.tasks.get_pending_versions")
    def test_null_impact_does_not_fail_whole_analysis(self, mock_pending, mock_release):
        mock_pending.return_value = [{"version": "8.0.8", "type": "stable"}]
        good_entry = {
            "id": "8.0.8-1",
            "type": "feature",
            "category": "marketplace",
            "title": "Good entry",
            "description": "Test description",
            "scope": "plugin",
            "component": ["backend"],
            "impact": {"risk": "low"},
            "relevant_when": {
                "plugins": ["waldur_openstack"],
                "feature_flags": [],
                "settings": [],
            },
        }
        bad_entry = dict(good_entry, id="8.0.8-2", impact=None)
        mock_release.return_value = {
            "version": "8.0.8",
            "date": "2026-01-01",
            "type": "stable",
            "summary": "Test release",
            "entries": [bad_entry, good_entry],
        }

        compute_changelog_impact("8.0.7", "8.0.8")

        analysis = ChangelogImpactAnalysis.objects.get(
            current_version="8.0.7", target_version="8.0.8"
        )
        self.assertEqual(analysis.status, ChangelogImpactAnalysis.Status.COMPLETED)
        self.assertIn("8.0.8-1", analysis.results)
        self.assertNotIn("8.0.8-2", analysis.results)


class SecretSettingExposureTest(test.APITestCase):
    """relevant_when.settings names a constance key from remote,
    unauthenticated JSON, with no allowlist. Only is_customized may ever
    be stored or served for it - never the actual value, or a secret_field
    setting (API keys, passwords, ...) leaks in plaintext to any staff or
    support user reading /api/changelog/pending/."""

    SECRET = "hunter2-plaintext"

    @override_config(SMAX_PASSWORD=SECRET)
    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    @mock.patch("waldur_core.changelog.tasks.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.tasks.get_pending_versions")
    def test_secret_value_not_stored_or_served(
        self, task_pending, task_release, view_pending, view_release
    ):
        pending = [{"version": "8.0.8", "type": "stable"}]
        release = {
            "version": "8.0.8",
            "date": "2026-01-01",
            "type": "stable",
            "summary": "",
            "entries": [
                {
                    "id": "8.0.8-1",
                    "type": "fix",
                    "category": "support",
                    "title": "Entry",
                    "description": "",
                    "scope": "plugin",
                    "component": ["backend"],
                    "impact": {"risk": "low"},
                    "relevant_when": {"settings": ["SMAX_PASSWORD"]},
                }
            ],
        }
        task_pending.return_value = pending
        task_release.return_value = release
        view_pending.return_value = pending
        view_release.return_value = release

        compute_changelog_impact("8.0.7", "8.0.8")

        analysis = ChangelogImpactAnalysis.objects.get()
        self.assertNotIn(self.SECRET, str(analysis.results))
        self.assertEqual(
            analysis.results["8.0.8-1"]["settings_analysis"],
            {"SMAX_PASSWORD": {"is_customized": True}},
        )

        staff = User.objects.create_user(
            username="support", password="support", is_support=True
        )
        self.client.force_authenticate(staff)
        response = self.client.get("/api/changelog/pending/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.SECRET, response.content.decode())
