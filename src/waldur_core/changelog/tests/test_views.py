import json
import threading
import time
from pathlib import Path
from unittest import mock

from rest_framework import status, test

from waldur_core.changelog import views
from waldur_core.changelog.models import ChangelogImpactAnalysis
from waldur_core.core.models import User


class ChangelogViewTestBase(test.APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staff",
            password="staff",
            is_staff=True,
        )
        self.regular = User.objects.create_user(
            username="regular",
            password="regular",
        )

    def _entry(self, **overrides):
        entry = {
            "id": "8.0.7-1",
            "type": "feature",
            "category": "auth",
            "title": "Personal Access Tokens",
            "description": "Test description",
            "scope": "core",
            "component": ["backend"],
            "highlight": True,
            "impact": {"risk": "low"},
            "actions": [],
            "relevant_when": {
                "plugins": [],
                "feature_flags": [],
                "settings": [],
            },
        }
        entry.update(overrides)
        return entry

    def _mock_release(self):
        return {
            "version": "8.0.7",
            "date": "2026-04-08",
            "type": "stable",
            "summary": "Test release",
            "entries": [self._entry()],
        }


class ChangelogPendingTest(ChangelogViewTestBase):
    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    def test_staff_can_access_pending(self, mock_pending, mock_release):
        mock_pending.return_value = [
            {"version": "8.0.7", "type": "stable"},
        ]
        mock_release.return_value = self._mock_release()

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog/pending/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("current_version", response.data)
        self.assertIn("releases", response.data)

    def test_anonymous_cannot_access(self):
        response = self.client.get("/api/changelog/pending/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_regular_user_cannot_access(self):
        self.client.force_authenticate(self.regular)
        response = self.client.get("/api/changelog/pending/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    def test_returns_empty_when_no_pending(self, mock_pending):
        mock_pending.return_value = []

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog/pending/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["versions_behind"], 0)
        self.assertEqual(response.data["releases"], [])

    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    def test_releases_stay_ordered_despite_concurrent_fetch(
        self, mock_pending, mock_release
    ):
        mock_pending.return_value = [
            {"version": "8.0.6", "type": "stable"},
            {"version": "8.0.7", "type": "stable"},
            {"version": "8.0.8", "type": "stable"},
        ]

        # Releases are fetched newest-first (8.0.8, 8.0.7, 8.0.6) but made to
        # *finish* fastest-last (8.0.6 finishes first here) - if fetches
        # were returned in completion order instead of input order, the
        # response would come back reversed.
        delays = {"8.0.8": 0.05, "8.0.7": 0.02, "8.0.6": 0.0}

        def fetch(version):
            time.sleep(delays[version])
            return {
                "version": version,
                "date": "2026-04-08",
                "type": "stable",
                "summary": "",
                "entries": [],
            }

        mock_release.side_effect = fetch

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog/pending/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        versions = [r["version"] for r in response.data["releases"]]
        self.assertEqual(versions, ["8.0.8", "8.0.7", "8.0.6"])


class ChangelogDetailTest(ChangelogViewTestBase):
    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    def test_staff_can_get_version_detail(self, mock_release):
        mock_release.return_value = self._mock_release()

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog/8.0.7/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["version"], "8.0.7")
        self.assertEqual(len(response.data["entries"]), 1)

    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    def test_returns_404_for_unknown_version(self, mock_release):
        mock_release.return_value = None

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog/99.99.99/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    def test_invalid_version_returns_404_without_fetching(self, mock_release):
        self.client.force_authenticate(self.staff)
        for path in ("not-a-version", "x%3Fy%3D1"):
            response = self.client.get(f"/api/changelog/{path}/")
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        mock_release.assert_not_called()


class ChangelogDisabledTest(ChangelogViewTestBase):
    @mock.patch("waldur_core.changelog.views.is_changelog_enabled", return_value=False)
    def test_pending_returns_404_when_disabled(self, mock_enabled):
        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog/pending/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @mock.patch("waldur_core.changelog.views.is_changelog_enabled", return_value=False)
    def test_detail_returns_404_when_disabled(self, mock_enabled):
        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog/8.0.7/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ChangelogEntriesListTest(ChangelogViewTestBase):
    def _entries(self):
        return [
            self._entry(
                id="8.0.7-1",
                type="feature",
                title="Personal Access Tokens",
                highlight=True,
                impact={"risk": "low"},
            ),
            self._entry(
                id="8.0.7-2",
                type="security",
                title="Fix XSS in login form",
                highlight=False,
                impact={"risk": "high"},
            ),
        ]

    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    def test_staff_can_list_entries(self, mock_pending, mock_release):
        mock_pending.return_value = [
            {"version": "8.0.7", "type": "stable", "date": "2026-04-08"}
        ]
        mock_release.return_value = {"entries": self._entries()}

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog-entries/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(len(response.data["results"]), 2)
        # Sorted by relevance then risk: the high-risk security entry comes first.
        self.assertEqual(response.data["results"][0]["id"], "8.0.7-2")

    def test_anonymous_cannot_access(self):
        response = self.client.get("/api/changelog-entries/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_regular_user_cannot_access(self):
        self.client.force_authenticate(self.regular)
        response = self.client.get("/api/changelog-entries/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch("waldur_core.changelog.views.is_changelog_enabled", return_value=False)
    def test_returns_404_when_disabled(self, mock_enabled):
        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog-entries/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    def test_filter_by_type(self, mock_pending, mock_release):
        mock_pending.return_value = [{"version": "8.0.7", "type": "stable"}]
        mock_release.return_value = {"entries": self._entries()}

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog-entries/?type=security")
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], "8.0.7-2")

    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    def test_filter_by_risk(self, mock_pending, mock_release):
        mock_pending.return_value = [{"version": "8.0.7", "type": "stable"}]
        mock_release.return_value = {"entries": self._entries()}

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog-entries/?risk=high")
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], "8.0.7-2")

    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    def test_filter_by_highlight(self, mock_pending, mock_release):
        mock_pending.return_value = [{"version": "8.0.7", "type": "stable"}]
        mock_release.return_value = {"entries": self._entries()}

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog-entries/?highlight=true")
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], "8.0.7-1")

    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    def test_search(self, mock_pending, mock_release):
        mock_pending.return_value = [{"version": "8.0.7", "type": "stable"}]
        mock_release.return_value = {"entries": self._entries()}

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog-entries/?search=xss")
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], "8.0.7-2")

    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    def test_pagination(self, mock_pending, mock_release):
        mock_pending.return_value = [{"version": "8.0.7", "type": "stable"}]
        mock_release.return_value = {"entries": self._entries()}

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog-entries/?page=2&page_size=1")
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(len(response.data["results"]), 1)

    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    def test_non_numeric_page_params_do_not_error(self, mock_pending, mock_release):
        mock_pending.return_value = [{"version": "8.0.7", "type": "stable"}]
        mock_release.return_value = {"entries": self._entries()}

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog-entries/?page=abc&page_size=xyz")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)

    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    def test_page_size_is_clamped(self, mock_pending, mock_release):
        mock_pending.return_value = [{"version": "8.0.7", "type": "stable"}]
        mock_release.return_value = {"entries": self._entries()}

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog-entries/?page_size=10000")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 2)

    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    def test_cold_cache_fetches_releases_concurrently(self, mock_pending, mock_release):
        # 6 pending versions, each with a slow fetch. Measures actual
        # overlap (peak concurrent fetches), not wall-clock time - a
        # duration threshold flakes on a loaded/slow CI runner even when
        # fetches genuinely ran concurrently.
        versions = [f"8.0.{i}" for i in range(1, 7)]
        mock_pending.return_value = [{"version": v, "type": "stable"} for v in versions]

        lock = threading.Lock()
        state = {"current": 0, "peak": 0}

        def fetch(version):
            with lock:
                state["current"] += 1
                state["peak"] = max(state["peak"], state["current"])
            time.sleep(0.2)
            with lock:
                state["current"] -= 1
            return {"entries": []}

        mock_release.side_effect = fetch

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog-entries/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # More than one fetch was in flight at once - not one at a time.
        self.assertGreater(state["peak"], 1)


class ChangelogDeltaTest(ChangelogViewTestBase):
    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    def test_staff_can_get_delta(self, mock_release):
        mock_release.return_value = {
            "version": "8.0.7",
            "date": "2026-04-08",
            "type": "stable",
            "summary": "Test release",
            "entries": [self._entry()],
            "since_previous": [self._entry(id="8.0.7-2", title="New in this delta")],
        }

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog/8.0.7/delta/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["entries"]), 1)
        self.assertEqual(response.data["entries"][0]["id"], "8.0.7-2")

    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    def test_returns_404_for_unknown_version(self, mock_release):
        mock_release.return_value = None

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog/99.99.99/delta/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    def test_invalid_version_returns_404_without_fetching(self, mock_release):
        self.client.force_authenticate(self.staff)
        for path in ("not-a-version", "x%3Fy%3D1"):
            response = self.client.get(f"/api/changelog/{path}/delta/")
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        mock_release.assert_not_called()

    def test_anonymous_cannot_access(self):
        response = self.client.get("/api/changelog/8.0.7/delta/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @mock.patch("waldur_core.changelog.views.is_changelog_enabled", return_value=False)
    def test_returns_404_when_disabled(self, mock_enabled):
        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog/8.0.7/delta/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ChangelogCompareTest(ChangelogViewTestBase):
    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    def test_staff_can_compare_versions(self, mock_pending, mock_release):
        mock_pending.return_value = [{"version": "8.0.8", "type": "stable"}]
        mock_release.return_value = {
            "version": "8.0.8",
            "since_previous": [self._entry(id="8.0.8-1", title="Delta entry")],
        }

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog/compare/8.0.7/8.0.8/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["current_version"], "8.0.7")
        self.assertEqual(response.data["latest_version"], "8.0.8")
        self.assertEqual(len(response.data["releases"]), 1)
        self.assertEqual(len(response.data["releases"][0]["entries"]), 1)
        self.assertEqual(response.data["releases"][0]["entries"][0]["id"], "8.0.8-1")

    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    def test_returns_empty_when_no_pending(self, mock_pending):
        mock_pending.return_value = []

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog/compare/8.0.7/8.0.8/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["versions_behind"], 0)
        self.assertEqual(response.data["releases"], [])

    def test_anonymous_cannot_access(self):
        response = self.client.get("/api/changelog/compare/8.0.7/8.0.8/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @mock.patch("waldur_core.changelog.views.is_changelog_enabled", return_value=False)
    def test_returns_404_when_disabled(self, mock_enabled):
        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog/compare/8.0.7/8.0.8/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


CURRENT_VERSION = "8.0.7"
STABLE_VERSION = "8.0.8"
RC_VERSION = "8.1.0-rc.1"


@mock.patch("waldur_core.core.views.__version__", CURRENT_VERSION)
@mock.patch("waldur_core.changelog.views.__version__", CURRENT_VERSION)
class RcTargetMismatchTest(ChangelogViewTestBase):
    """compute_changelog_impact() is triggered for the target
    get_impact_analysis_target() picks; changelog_pending/
    changelog_entries_list must look results up by that same target, not
    pending[-1]["version"] (which is the RC whenever one is newest)."""

    INDEX = {
        "latest_stable": STABLE_VERSION,
        "releases": [
            {"version": STABLE_VERSION, "type": "stable"},
            {"version": RC_VERSION, "type": "rc"},
        ],
    }

    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.utils.fetch_changelog_index")
    @mock.patch("waldur_core.core.views.compute_changelog_impact")
    @mock.patch("waldur_core.core.views.fetch_changelog_index")
    def test_impact_results_reach_pending_view_when_rc_is_newest(
        self, core_index, _task, utils_index, mock_release
    ):
        core_index.return_value = self.INDEX
        utils_index.return_value = self.INDEX
        mock_release.side_effect = lambda version: {
            "version": version,
            "date": "2026-09-01",
            "type": "rc" if "rc" in version else "stable",
            "summary": "",
            "entries": [self._entry(id=f"{version}-1", relevant_when={})],
        }

        self.client.force_authenticate(self.staff)
        self.client.get("/api/version/")

        # The analysis must have been queued for the stable release, not the RC.
        analysis = ChangelogImpactAnalysis.objects.get()
        self.assertEqual(analysis.target_version, STABLE_VERSION)

        analysis.status = ChangelogImpactAnalysis.Status.COMPLETED
        analysis.results = {f"{STABLE_VERSION}-1": {"affected_users_count": 3}}
        analysis.save()

        data = self.client.get("/api/changelog/pending/").data
        entries = [e for r in data["releases"] for e in r["entries"]]
        counts = {e["id"]: e.get("affected_users_count") for e in entries}
        self.assertEqual(counts.get(f"{STABLE_VERSION}-1"), 3)


class ChangelogRcCycleTest(ChangelogViewTestBase):
    """A stable deployment behind an RC cycle and its stable release sees
    every change once, not once per release."""

    def _releases(self):
        a = self._entry(id="8.1.3-rc.1-1", title="A")
        b = self._entry(id="8.1.3-rc.2-1", title="B")
        c = self._entry(id="8.1.3-3", title="C")
        return {
            "8.1.3-rc.1": {
                "version": "8.1.3-rc.1",
                "date": "2026-09-23",
                "summary": "",
                "type": "rc",
                "previous_version": "8.1.2",
                "entries": [a],
                "since_previous": [a],
            },
            "8.1.3-rc.2": {
                "version": "8.1.3-rc.2",
                "date": "2026-09-23",
                "summary": "",
                "type": "rc",
                "previous_version": "8.1.3-rc.1",
                "entries": [a, b],
                "since_previous": [b],
            },
            "8.1.3": {
                "version": "8.1.3",
                "date": "2026-09-23",
                "summary": "",
                "type": "stable",
                "previous_version": "8.1.3-rc.2",
                "entries": [
                    self._entry(id="8.1.3-1", title="A"),
                    self._entry(id="8.1.3-2", title="B"),
                    c,
                ],
                "since_previous": [c],
            },
        }

    def _pending(self):
        return [
            {"version": v, "type": data["type"], "date": "2026-09-23"}
            for v, data in self._releases().items()
        ]

    @mock.patch("waldur_core.changelog.views.__version__", "8.1.2")
    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    def test_entries_list_has_each_change_once(self, mock_pending, mock_release):
        mock_pending.return_value = self._pending()
        mock_release.side_effect = lambda v: self._releases()[v]

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog-entries/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = sorted(e["title"] for e in response.data["results"])
        self.assertEqual(titles, ["A", "B", "C"])

    @mock.patch("waldur_core.changelog.views.__version__", "8.1.2")
    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    def test_pending_lists_rcs_with_their_new_entries(self, mock_pending, mock_release):
        mock_pending.return_value = self._pending()
        mock_release.side_effect = lambda v: self._releases()[v]

        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog/pending/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [
                (r["version"], [e["title"] for e in r["entries"]])
                for r in response.data["releases"]
            ],
            [("8.1.3", ["C"]), ("8.1.3-rc.2", ["B"]), ("8.1.3-rc.1", ["A"])],
        )


class ChangelogUpgradeReportTest(ChangelogRcCycleTest):
    """The report covers every pending entry once, whatever the table shows."""

    url = "/api/changelog-upgrade-report/"

    @mock.patch("waldur_core.changelog.views.__version__", "8.1.2")
    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.views.get_pending_versions")
    def test_report_covers_every_pending_entry(self, mock_pending, mock_release):
        mock_pending.return_value = self._pending()
        mock_release.side_effect = lambda v: self._releases()[v]

        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["latest_version"], "8.1.3")
        self.assertEqual(response.data["entry_count"], 3)
        self.assertIn("## All Changes (3)", response.data["report"])
        self.assertIn("**3 total changes:**", response.data["announcement"])
        self.assertEqual(response.data["announcement_type"], "information")
        self.assertIn("--version 8.1.3 ", response.data["commands"]["helm"])

    @mock.patch("waldur_core.changelog.views.get_pending_versions", return_value=[])
    def test_nothing_pending(self, _pending):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["entry_count"], 0)

    def test_regular_user_cannot_access(self):
        self.client.force_authenticate(self.regular)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch("waldur_core.changelog.views.is_changelog_enabled", return_value=False)
    def test_returns_404_when_disabled(self, _enabled):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ChangelogReleaseHistoryTest(ChangelogRcCycleTest):
    """Browsing what any release introduced, not only pending ones."""

    INDEX = {
        "releases": [
            {"version": "8.1.3-rc.1", "type": "rc", "date": "2026-09-01"},
            {"version": "8.1.3-rc.2", "type": "rc", "date": "2026-09-02"},
            {"version": "8.1.3", "type": "stable", "date": "2026-09-03"},
        ]
    }

    @mock.patch("waldur_core.changelog.views.__version__", "8.1.3-rc.2")
    @mock.patch("waldur_core.changelog.views.fetch_changelog_index")
    def test_releases_are_listed_with_status(self, mock_index):
        mock_index.return_value = self.INDEX
        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog/releases/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["current_version"], "8.1.3-rc.2")
        self.assertEqual(
            [(r["version"], r["status"]) for r in response.data["releases"]],
            [("8.1.3", "pending"), ("8.1.3-rc.2", "running"), ("8.1.3-rc.1", "older")],
        )

    def test_regular_user_cannot_list_releases(self):
        self.client.force_authenticate(self.regular)
        response = self.client.get("/api/changelog/releases/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch("waldur_core.changelog.views.__version__", "8.1.3")
    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    @mock.patch("waldur_core.changelog.views.get_pending_versions", return_value=[])
    def test_entries_of_an_older_release(self, _pending, mock_release):
        mock_release.side_effect = lambda v: self._releases()[v]
        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog-entries/", {"release": "8.1.3-rc.2"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # What rc.2 introduced (since_previous), not its cumulative entries.
        self.assertEqual([e["title"] for e in response.data["results"]], ["B"])
        self.assertEqual(response.data["results"][0]["version"], "8.1.3-rc.2")

    @mock.patch("waldur_core.changelog.views.fetch_changelog_release")
    def test_release_without_since_previous_lists_its_entries(self, mock_release):
        release = dict(self._releases()["8.1.3-rc.2"])
        del release["since_previous"]
        mock_release.return_value = release
        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog-entries/", {"release": "8.1.3-rc.2"})
        self.assertEqual(
            sorted(e["title"] for e in response.data["results"]), ["A", "B"]
        )

    @mock.patch(
        "waldur_core.changelog.views.fetch_changelog_release", return_value=None
    )
    def test_unknown_release_is_404(self, _release):
        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog-entries/", {"release": "9.9.9"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_invalid_release_is_404(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get("/api/changelog-entries/", {"release": "latest"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ChangelogEntriesSortAndCategoryTest(ChangelogViewTestBase):
    def _entries(self):
        return [
            self._entry(
                id="1",
                type="fix",
                title="beta",
                category="ui",
                impact={"risk": "low"},
                version="8.1.3",
            ),
            self._entry(
                id="2",
                type="breaking",
                title="Alpha",
                category="auth",
                impact={"risk": "high"},
                version="8.1.3-rc.2",
            ),
            self._entry(
                id="3",
                type="feature",
                title="gamma",
                category="ui",
                impact={"risk": "medium"},
                version="8.1.3-rc.10",
            ),
        ]

    def _get(self, **params):
        with (
            mock.patch(
                "waldur_core.changelog.views.get_pending_versions",
                return_value=[{"version": "8.1.3", "type": "stable"}],
            ),
            mock.patch(
                "waldur_core.changelog.views.fetch_changelog_release",
                return_value={"entries": self._entries()},
            ),
        ):
            self.client.force_authenticate(self.staff)
            response = self.client.get("/api/changelog-entries/", params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [e["title"] for e in response.data["results"]]

    def test_filter_by_category(self):
        self.assertEqual(sorted(self._get(category="ui")), ["beta", "gamma"])

    def test_unknown_category_is_ignored(self):
        self.assertEqual(len(self._get(category="nope")), 3)

    def test_sort_by_title_ignores_case(self):
        self.assertEqual(self._get(o="title"), ["Alpha", "beta", "gamma"])
        self.assertEqual(self._get(o="-title"), ["gamma", "beta", "Alpha"])

    def test_sort_by_type_and_risk_by_severity(self):
        self.assertEqual(self._get(o="type"), ["Alpha", "gamma", "beta"])
        self.assertEqual(self._get(o="risk"), ["Alpha", "gamma", "beta"])
        self.assertEqual(self._get(o="-risk"), ["beta", "gamma", "Alpha"])

    def test_sort_by_version_in_version_order(self):
        # rc.10 after rc.2, which a string sort would get wrong.
        self.assertEqual(self._get(o="version"), ["Alpha", "gamma", "beta"])

    def test_unknown_ordering_keeps_default_order(self):
        self.assertEqual(self._get(o="nope"), self._get())


class ChangelogEnumsMatchSchemaTest(ChangelogViewTestBase):
    """The filter enums published in the API must match what release files
    may contain."""

    def test_enums_match_changelog_schema(self):
        schema_path = Path(__file__).resolve().parents[4] / "changelog" / "schema.json"
        entry = json.loads(schema_path.read_text())["$defs"]["entry"]["properties"]
        self.assertEqual(views.ENTRY_TYPES, entry["type"]["enum"])
        self.assertEqual(views.CATEGORIES, entry["category"]["enum"])
        risk = json.loads(schema_path.read_text())["$defs"]["impact"]["properties"][
            "risk"
        ]
        self.assertEqual(views.RISKS, risk["enum"])
        self.assertEqual(sorted(views.VALID_SCOPES), sorted(entry["scope"]["enum"]))
