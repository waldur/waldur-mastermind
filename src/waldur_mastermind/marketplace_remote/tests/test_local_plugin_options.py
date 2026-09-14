from django.test import TestCase

from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace_remote.utils import (
    keep_local_plugin_options,
    pull_fields,
)

OPTION = "enable_resource_limit_change_requests"


class KeepLocalPluginOptionsTest(TestCase):
    def pull(self, local_options, remote_options):
        offering = factories.OfferingFactory(plugin_options=local_options)
        remote = {"plugin_options": remote_options}
        pull_fields(
            ("plugin_options",), offering, keep_local_plugin_options(offering, remote)
        )
        offering.refresh_from_db()
        return offering.plugin_options

    def test_local_switch_survives_pull(self):
        self.assertEqual(
            self.pull({OPTION: True, "stale": 1}, {"remote": 2}),
            {"remote": 2, OPTION: True},
        )

    def test_local_false_wins_over_remote_true(self):
        self.assertIs(self.pull({OPTION: False}, {OPTION: True})[OPTION], False)

    def test_unset_local_switch_follows_remote(self):
        self.assertEqual(self.pull({}, {OPTION: True}), {OPTION: True})

    def test_remote_without_plugin_options_is_left_alone(self):
        offering = factories.OfferingFactory(plugin_options={OPTION: True})
        remote = {"name": "x"}
        self.assertIs(keep_local_plugin_options(offering, remote), remote)
