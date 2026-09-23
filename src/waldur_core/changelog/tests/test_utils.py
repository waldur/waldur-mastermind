from unittest import mock

import requests
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from waldur_core.changelog.utils import (
    CHANGELOG_INDEX_CACHE_KEY,
    build_changelog_summary,
    compare_versions,
    count_versions_behind,
    enrich_entries_with_relevance,
    fetch_changelog_index,
    fetch_changelog_release,
    get_latest_version,
    get_pending_versions,
    is_rc_version,
    match_relevance,
    merge_delta_entries,
    parse_version,
    select_new_entries,
)


class ParseVersionTest(TestCase):
    def test_stable_version(self):
        v = parse_version("8.0.7")
        self.assertIsNotNone(v)
        self.assertEqual(str(v), "8.0.7")

    def test_rc_version(self):
        v = parse_version("8.0.7-rc.5")
        self.assertIsNotNone(v)
        # PEP 440 normalizes to 8.0.7rc5
        self.assertEqual(str(v), "8.0.7rc5")

    def test_invalid_version(self):
        self.assertIsNone(parse_version("latest"))
        self.assertIsNone(parse_version("develop"))

    def test_rc_sorts_before_stable(self):
        self.assertLess(parse_version("8.0.7-rc.27"), parse_version("8.0.7"))

    def test_rc_ordering(self):
        self.assertLess(parse_version("8.0.7-rc.1"), parse_version("8.0.7-rc.2"))
        self.assertLess(parse_version("8.0.7-rc.9"), parse_version("8.0.7-rc.10"))

    def test_cross_minor_ordering(self):
        self.assertLess(parse_version("8.0.6"), parse_version("8.0.7-rc.1"))
        self.assertLess(parse_version("8.0.7"), parse_version("8.0.8-rc.1"))


class CompareVersionsTest(TestCase):
    def test_equal(self):
        self.assertEqual(compare_versions("8.0.7", "8.0.7"), 0)

    def test_less(self):
        self.assertLess(compare_versions("8.0.6", "8.0.7"), 0)

    def test_greater(self):
        self.assertGreater(compare_versions("8.0.8", "8.0.7"), 0)

    def test_rc_less_than_stable(self):
        self.assertLess(compare_versions("8.0.7-rc.5", "8.0.7"), 0)


class GetPendingVersionsTest(TestCase):
    def test_returns_newer_versions(self):
        index = {
            "releases": [
                {"version": "8.0.5", "type": "stable"},
                {"version": "8.0.6", "type": "stable"},
                {"version": "8.0.7", "type": "stable"},
            ]
        }
        pending = get_pending_versions("8.0.5", index)
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0]["version"], "8.0.6")
        self.assertEqual(pending[1]["version"], "8.0.7")

    def test_includes_rcs(self):
        index = {
            "releases": [
                {"version": "8.0.7", "type": "stable"},
                {"version": "8.0.8-rc.1", "type": "rc"},
                {"version": "8.0.8-rc.2", "type": "rc"},
            ]
        }
        pending = get_pending_versions("8.0.7", index)
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0]["version"], "8.0.8-rc.1")

    def test_returns_empty_for_latest(self):
        index = {"releases": [{"version": "8.0.7", "type": "stable"}]}
        pending = get_pending_versions("8.0.7", index)
        self.assertEqual(len(pending), 0)

    def test_handles_invalid_current_version(self):
        index = {"releases": [{"version": "8.0.7", "type": "stable"}]}
        pending = get_pending_versions("develop", index)
        self.assertEqual(len(pending), 0)


class MatchRelevanceTest(TestCase):
    def test_core_scope_always_relevant(self):
        entry = {
            "scope": "core",
            "relevant_when": {"plugins": [], "feature_flags": [], "settings": []},
        }
        is_relevant, reasons = match_relevance(entry)
        self.assertTrue(is_relevant)

    def test_dev_scope_not_relevant(self):
        entry = {
            "scope": "dev",
            "relevant_when": {"plugins": [], "feature_flags": [], "settings": []},
        }
        is_relevant, reasons = match_relevance(entry)
        self.assertFalse(is_relevant)

    @override_settings(INSTALLED_APPS=["waldur_core.core", "waldur_openstack"])
    def test_plugin_relevant_when_installed(self):
        entry = {
            "scope": "plugin",
            "relevant_when": {
                "plugins": ["waldur_openstack"],
                "feature_flags": [],
                "settings": [],
            },
        }
        is_relevant, reasons = match_relevance(entry)
        self.assertTrue(is_relevant)
        self.assertIn("Plugin waldur_openstack is active", reasons)

    @override_settings(INSTALLED_APPS=["waldur_core.core"])
    def test_plugin_not_relevant_when_not_installed(self):
        entry = {
            "scope": "plugin",
            "relevant_when": {
                "plugins": ["waldur_openstack"],
                "feature_flags": [],
                "settings": [],
            },
        }
        is_relevant, reasons = match_relevance(entry)
        self.assertFalse(is_relevant)

    def test_empty_relevant_when_is_always_relevant(self):
        entry = {
            "scope": "plugin",
            "relevant_when": {"plugins": [], "feature_flags": [], "settings": []},
        }
        is_relevant, reasons = match_relevance(entry)
        self.assertTrue(is_relevant)


class BuildChangelogSummaryTest(TestCase):
    def test_no_pending_returns_none(self):
        index = {"releases": [{"version": "8.0.7", "type": "stable"}]}
        result = build_changelog_summary(index, "8.0.7")
        self.assertIsNone(result)

    def test_summary_with_pending_versions(self):
        index = {
            "releases": [
                {
                    "version": "8.0.7",
                    "type": "stable",
                    "has_breaking": True,
                    "has_security": False,
                },
                {
                    "version": "8.0.8",
                    "type": "stable",
                    "has_breaking": False,
                    "has_security": True,
                    "max_security_urgency": "high",
                },
            ]
        }
        result = build_changelog_summary(index, "8.0.6")
        self.assertIsNotNone(result)
        self.assertEqual(result["versions_behind"], 2)
        self.assertTrue(result["has_breaking_changes"])
        # Counts releases flagged has_breaking, not high-risk entries - the
        # index doesn't carry per-entry risk.
        self.assertEqual(result["breaking_release_count"], 1)
        self.assertIn("security_alert", result)
        self.assertEqual(result["security_alert"]["max_urgency"], "high")

    def test_breaking_release_count_counts_releases_not_entries(self):
        index = {
            "releases": [
                {"version": "8.0.7", "type": "stable", "has_breaking": True},
                {"version": "8.0.8", "type": "stable", "has_breaking": True},
                {"version": "8.0.9", "type": "stable", "has_breaking": False},
            ]
        }
        result = build_changelog_summary(index, "8.0.6")
        self.assertEqual(result["breaking_release_count"], 2)

    def test_summary_no_security(self):
        index = {
            "releases": [
                {
                    "version": "8.0.7",
                    "type": "stable",
                    "has_breaking": False,
                    "has_security": False,
                },
            ]
        }
        result = build_changelog_summary(index, "8.0.6")
        self.assertNotIn("security_alert", result)


class MergeDeltaEntriesTest(TestCase):
    def test_merges_entries_in_range(self):
        releases = [
            {
                "version": "8.0.7-rc.1",
                "since_previous": [{"id": "delta-1", "title": "Feature A"}],
            },
            {
                "version": "8.0.7-rc.2",
                "since_previous": [{"id": "delta-2", "title": "Fix B"}],
            },
            {
                "version": "8.0.7-rc.3",
                "since_previous": [{"id": "delta-3", "title": "Feature C"}],
            },
        ]
        merged = merge_delta_entries("8.0.7-rc.1", "8.0.7-rc.3", releases)
        self.assertEqual(len(merged), 2)  # rc.2 and rc.3, not rc.1
        ids = [e["id"] for e in merged]
        self.assertIn("delta-2", ids)
        self.assertIn("delta-3", ids)


def _mock_response(json_data):
    response = mock.Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = json_data
    return response


class FetchChangelogIndexTest(TestCase):
    def setUp(self):
        cache.clear()

    @mock.patch("waldur_core.changelog.utils.requests.get")
    def test_caches_successful_fetch(self, mock_get):
        mock_get.return_value = _mock_response({"releases": []})

        first = fetch_changelog_index()
        second = fetch_changelog_index()

        self.assertEqual(first, {"releases": []})
        self.assertEqual(second, {"releases": []})
        mock_get.assert_called_once()

    @mock.patch("waldur_core.changelog.utils.requests.get")
    def test_negatively_caches_network_failure(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("docs.waldur.com unreachable")

        first = fetch_changelog_index()
        second = fetch_changelog_index()

        self.assertIsNone(first)
        self.assertIsNone(second)
        # Only one outbound call: the second request was served from the
        # negative cache instead of retrying docs.waldur.com.
        mock_get.assert_called_once()

    @mock.patch("waldur_core.changelog.utils.requests.get")
    def test_negatively_caches_invalid_json(self, mock_get):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.side_effect = ValueError("not json")
        mock_get.return_value = response

        first = fetch_changelog_index()
        second = fetch_changelog_index()

        self.assertIsNone(first)
        self.assertIsNone(second)
        mock_get.assert_called_once()

    @mock.patch("waldur_core.changelog.utils.requests.get")
    def test_success_after_negative_cache_expires(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("docs.waldur.com unreachable")
        self.assertIsNone(fetch_changelog_index())
        mock_get.assert_called_once()

        # Simulate the negative-cache TTL elapsing.
        cache.delete(CHANGELOG_INDEX_CACHE_KEY)

        mock_get.side_effect = None
        mock_get.return_value = _mock_response({"releases": []})
        self.assertEqual(fetch_changelog_index(), {"releases": []})
        self.assertEqual(mock_get.call_count, 2)


class FetchChangelogReleaseTest(TestCase):
    def setUp(self):
        cache.clear()

    @mock.patch("waldur_core.changelog.utils.requests.get")
    def test_caches_successful_fetch(self, mock_get):
        mock_get.return_value = _mock_response({"version": "8.0.7"})

        first = fetch_changelog_release("8.0.7")
        second = fetch_changelog_release("8.0.7")

        self.assertEqual(first, {"version": "8.0.7"})
        self.assertEqual(second, {"version": "8.0.7"})
        mock_get.assert_called_once()

    @mock.patch("waldur_core.changelog.utils.requests.get")
    def test_negatively_caches_network_failure(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("docs.waldur.com unreachable")

        first = fetch_changelog_release("8.0.7")
        second = fetch_changelog_release("8.0.7")

        self.assertIsNone(first)
        self.assertIsNone(second)
        mock_get.assert_called_once()

    @mock.patch("waldur_core.changelog.utils.requests.get")
    def test_failure_for_one_version_does_not_affect_another(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("docs.waldur.com unreachable")
        self.assertIsNone(fetch_changelog_release("8.0.7"))
        mock_get.assert_called_once()

        mock_get.side_effect = None
        mock_get.return_value = _mock_response({"version": "8.0.8"})
        self.assertEqual(fetch_changelog_release("8.0.8"), {"version": "8.0.8"})
        self.assertEqual(mock_get.call_count, 2)


def _relevance_entry(i):
    return {
        "id": f"8.0.8-{i}",
        "scope": "plugin",
        "relevant_when": {
            "plugins": ["waldur_openstack"],
            "feature_flags": ["marketplace.some_flag"],
            "settings": ["SITE_NAME"],
        },
    }


class RelevanceQueryCountTest(TestCase):
    """match_relevance()'s DB-backed context (feature flags, Constance
    settings) must be built once per batch, not once per entry - see
    enrich_entries_with_relevance()."""

    def test_query_count_does_not_scale_with_entry_count(self):
        counts = {}
        for n in (1, 10):
            entries = [_relevance_entry(i) for i in range(n)]
            with CaptureQueriesContext(connection) as ctx:
                enrich_entries_with_relevance(entries)
            counts[n] = len(ctx.captured_queries)

        # 10 entries must not cost ~10x what 1 entry costs - the context
        # (active plugins, feature flags, customized settings) is shared.
        self.assertLessEqual(counts[10], counts[1] + 2)


class SelectNewEntriesTest(TestCase):
    def _release(self, version, previous, entries, since_previous=None):
        data = {"version": version, "entries": entries}
        if previous:
            data["previous_version"] = previous
        if since_previous is not None:
            data["since_previous"] = since_previous
        return ({"version": version}, data)

    def _ids(self, selected):
        return [[e["id"] for e in entries] for _info, _data, entries in selected]

    def _rc_cycle(self):
        a, b, c = {"id": "rc.1-1"}, {"id": "rc.2-1"}, {"id": "8.1.3-1"}
        stable_a = {"id": "8.1.3-2"}
        stable_b = {"id": "8.1.3-3"}
        return [
            self._release("8.1.3-rc.1", "8.1.2", [a], [a]),
            self._release("8.1.3-rc.2", "8.1.3-rc.1", [a, b], [b]),
            # The stable release re-describes the whole cycle under new ids;
            # only c is new since rc.2.
            self._release("8.1.3", "8.1.3-rc.2", [stable_a, stable_b, c], [c]),
        ]

    def test_stable_deployment_sees_each_change_once_through_rcs(self):
        selected = select_new_entries("8.1.2", self._rc_cycle())
        self.assertEqual(self._ids(selected), [["rc.1-1"], ["rc.2-1"], ["8.1.3-1"]])

    def test_rc_deployment_sees_only_later_deltas(self):
        selected = select_new_entries("8.1.3-rc.1", self._rc_cycle()[1:])
        self.assertEqual(self._ids(selected), [["rc.2-1"], ["8.1.3-1"]])

    def test_broken_chain_falls_back_to_cumulative_entries(self):
        # rc.1's file is missing: rc.2 can't use its delta, so it shows its
        # cumulative entries; the stable release then chains from rc.2 again.
        selected = select_new_entries("8.1.2", self._rc_cycle()[1:])
        self.assertEqual(self._ids(selected), [["rc.1-1", "rc.2-1"], ["8.1.3-1"]])

    def test_development_build_of_the_previous_release_keeps_the_chain(self):
        # A staging or dev deployment reports rc.1 plus a local build suffix.
        selected = select_new_entries(
            "8.1.3-rc.1+5.gb6fab9572.dirty", self._rc_cycle()[1:]
        )
        self.assertEqual(self._ids(selected), [["rc.2-1"], ["8.1.3-1"]])

    def test_release_without_since_previous_uses_entries(self):
        releases = [self._release("8.1.3", "8.1.2", [{"id": "8.1.3-1"}])]
        selected = select_new_entries("8.1.2", releases)
        self.assertEqual(self._ids(selected), [["8.1.3-1"]])

    def test_carried_over_ids_are_not_repeated_after_a_break(self):
        a, b = {"id": "rc.1-1"}, {"id": "rc.3-1"}
        releases = [
            self._release("8.1.3-rc.1", "8.1.2", [a], [a]),
            # rc.2 is missing, so rc.3 falls back to entries, which carry a.
            self._release("8.1.3-rc.3", "8.1.3-rc.2", [a, b], [b]),
        ]
        selected = select_new_entries("8.1.2", releases)
        self.assertEqual(self._ids(selected), [["rc.1-1"], ["rc.3-1"]])


class RcDeploymentVersionTest(TestCase):
    INDEX = {
        "latest_stable": "8.1.2",
        "latest_rc": "8.1.3-rc.16",
        "releases": [
            {"version": "8.1.2", "type": "stable"},
            {"version": "8.1.3-rc.15", "type": "rc"},
            {"version": "8.1.3-rc.16", "type": "rc"},
        ],
    }

    def test_is_rc_version(self):
        self.assertTrue(is_rc_version("8.1.3-rc.15"))
        # A development build of an RC, as setuptools-scm reports it.
        self.assertTrue(is_rc_version("8.1.3-rc.15+49.gb97210b96"))
        self.assertFalse(is_rc_version("8.1.2"))
        self.assertFalse(is_rc_version("not-a-version"))

    def test_stable_deployment_is_offered_the_latest_stable(self):
        # The newest stable is reported even when the deployment already runs
        # it, as before; comparing it with the running version is the
        # caller's job.
        self.assertEqual(get_latest_version(self.INDEX, "8.1.2"), "8.1.2")
        self.assertEqual(get_latest_version(self.INDEX, "8.1.1"), "8.1.2")

    def test_rc_deployment_is_offered_a_newer_rc(self):
        self.assertEqual(get_latest_version(self.INDEX, "8.1.3-rc.15"), "8.1.3-rc.16")

    def test_rc_deployment_is_offered_a_stable_newer_than_the_latest_rc(self):
        index = dict(self.INDEX, latest_stable="8.1.3")
        self.assertEqual(get_latest_version(index, "8.1.3-rc.16"), "8.1.3")

    def test_index_without_stable_releases(self):
        index = {"latest_rc": "8.1.3-rc.16", "releases": []}
        self.assertEqual(get_latest_version(index, "8.1.3-rc.15"), "8.1.3-rc.16")
        self.assertIsNone(get_latest_version(index, "8.1.2"))

    def test_versions_behind_counts_rcs_only_for_rc_deployments(self):
        pending = [
            {"version": "8.1.3-rc.16", "type": "rc"},
            {"version": "8.1.3", "type": "stable"},
        ]
        self.assertEqual(count_versions_behind("8.1.3-rc.15", pending), 2)
        self.assertEqual(count_versions_behind("8.1.2", pending), 1)

    def test_summary_for_rc_deployment(self):
        summary = build_changelog_summary(self.INDEX, "8.1.3-rc.15")
        self.assertEqual(summary["versions_behind"], 1)
