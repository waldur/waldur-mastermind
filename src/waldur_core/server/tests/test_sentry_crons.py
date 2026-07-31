from django.test import SimpleTestCase

from waldur_core.server.sentry_crons import (
    MONITOR_SLUG_MAX_LENGTH,
    apply_monitor_config_defaults,
    build_monitor_config_defaults,
    scope_beat_schedule,
    scope_monitor_name,
)


class ScopeMonitorNameTest(SimpleTestCase):
    def test_appends_environment(self):
        self.assertEqual(
            scope_monitor_name("sync-slurm-periodic-settings", "waldur-rtu-prd"),
            "sync-slurm-periodic-settings-waldur-rtu-prd",
        )

    def test_environments_get_distinct_names(self):
        self.assertNotEqual(
            scope_monitor_name("sync-slurm-periodic-settings", "waldur-rtu-prd"),
            scope_monitor_name("sync-slurm-periodic-settings", "uninett-sigma-no"),
        )

    def test_missing_scope_leaves_name_alone(self):
        self.assertEqual(scope_monitor_name("some-task", None), "some-task")
        self.assertEqual(scope_monitor_name("some-task", ""), "some-task")

    def test_normalizes_unsafe_characters(self):
        self.assertEqual(scope_monitor_name("task", "Prod Env!"), "task-prod-env")

    def test_long_names_stay_within_the_sentry_limit(self):
        name = "waldur-create-offering-users-for-site-agent-offerings"
        scoped = scope_monitor_name(name, "ncc-netherlands-hpcservicehub-eu")
        self.assertLessEqual(len(scoped), MONITOR_SLUG_MAX_LENGTH)

    def test_truncation_keeps_the_deployment_identifiable(self):
        """A truncated monitor must still say which deployment it belongs to."""
        scoped = scope_monitor_name(
            "process_maintenance_announcement_transitions", "waldur-rtu-prd"
        )
        self.assertTrue(scoped.endswith("-waldur-rtu-prd"), scoped)
        self.assertLessEqual(len(scoped), MONITOR_SLUG_MAX_LENGTH)

    def test_very_long_scope_falls_back_to_hashing(self):
        """When the scope crowds out the name, uniqueness still wins."""
        scoped = scope_monitor_name(
            "terminate_resources_in_state_erred_without_backend_id",
            "an-extremely-long-deployment-environment-name-here",
        )
        self.assertLessEqual(len(scoped), MONITOR_SLUG_MAX_LENGTH)

    def test_truncated_names_do_not_collide(self):
        """Long entry names sharing a prefix must not merge after truncation."""
        env = "ncc-netherlands-hpcservicehub-eu"
        first = scope_monitor_name("waldur-remote-pull-offering-users-and-more", env)
        second = scope_monitor_name("waldur-remote-pull-offering-users-and-else", env)
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(first), MONITOR_SLUG_MAX_LENGTH)

    def test_same_long_name_is_stable_across_calls(self):
        name = "waldur-create-offering-users-for-site-agent-offerings"
        self.assertEqual(
            scope_monitor_name(name, "some-very-long-environment-name"),
            scope_monitor_name(name, "some-very-long-environment-name"),
        )


class ScopeBeatScheduleTest(SimpleTestCase):
    schedule = {
        "sync-slurm-periodic-settings": {"task": "a", "schedule": 600},
        "process_maintenance_announcement_transitions": {"task": "b", "schedule": 300},
    }

    def test_every_entry_is_preserved(self):
        scoped = scope_beat_schedule(self.schedule, "waldur-rtu-prd")

        self.assertEqual(len(scoped), len(self.schedule))
        self.assertEqual(sorted(entry["task"] for entry in scoped.values()), ["a", "b"])

    def test_names_are_scoped(self):
        scoped = scope_beat_schedule(self.schedule, "waldur-rtu-prd")
        self.assertIn("sync-slurm-periodic-settings-waldur-rtu-prd", scoped)

    def test_original_is_not_mutated(self):
        before = dict(self.schedule)
        scope_beat_schedule(self.schedule, "waldur-rtu-prd")
        self.assertEqual(self.schedule, before)

    def test_missing_scope_returns_schedule_unchanged(self):
        self.assertEqual(scope_beat_schedule(self.schedule, None), self.schedule)


class MonitorConfigDefaultsTest(SimpleTestCase):
    def test_blank_values_are_dropped(self):
        self.assertEqual(
            build_monitor_config_defaults(
                checkin_margin=5, failure_issue_threshold=None
            ),
            {"checkin_margin": 5},
        )

    def test_defaults_are_merged_into_config(self):
        config = {"schedule": {"type": "crontab", "value": "*/10 * * * *"}}
        merged = apply_monitor_config_defaults(config, {"checkin_margin": 5})

        self.assertEqual(merged["checkin_margin"], 5)
        self.assertEqual(merged["schedule"]["value"], "*/10 * * * *")

    def test_existing_values_win(self):
        merged = apply_monitor_config_defaults(
            {"schedule": {}, "checkin_margin": 1}, {"checkin_margin": 5}
        )
        self.assertEqual(merged["checkin_margin"], 1)

    def test_empty_config_is_left_alone(self):
        """An empty config means sentry-sdk is skipping the monitor entirely."""
        self.assertEqual(apply_monitor_config_defaults({}, {"checkin_margin": 5}), {})


class PatchMonitorConfigTest(SimpleTestCase):
    def test_patch_wraps_the_sentry_builder(self):
        from sentry_sdk.integrations.celery import beat as sentry_beat

        from waldur_core.server.sentry_crons import patch_monitor_config_defaults

        original = sentry_beat._get_monitor_config
        try:
            applied = patch_monitor_config_defaults({"checkin_margin": 5})
            self.assertTrue(applied)

            from celery.schedules import crontab

            from waldur_core.server import celeryconf

            config = sentry_beat._get_monitor_config(
                crontab(minute="*/10"), celeryconf.app, "sync-slurm-periodic-settings"
            )
            self.assertEqual(config["checkin_margin"], 5)
            self.assertEqual(config["schedule"]["value"], "*/10 * * * *")
        finally:
            sentry_beat._get_monitor_config = original

    def test_patch_is_idempotent(self):
        from sentry_sdk.integrations.celery import beat as sentry_beat

        from waldur_core.server.sentry_crons import patch_monitor_config_defaults

        original = sentry_beat._get_monitor_config
        try:
            patch_monitor_config_defaults({"checkin_margin": 5})
            once = sentry_beat._get_monitor_config
            patch_monitor_config_defaults({"checkin_margin": 5})
            self.assertIs(sentry_beat._get_monitor_config, once)
        finally:
            sentry_beat._get_monitor_config = original

    def test_no_defaults_means_no_patch(self):
        from sentry_sdk.integrations.celery import beat as sentry_beat

        from waldur_core.server.sentry_crons import patch_monitor_config_defaults

        original = sentry_beat._get_monitor_config
        self.assertFalse(patch_monitor_config_defaults({}))
        self.assertIs(sentry_beat._get_monitor_config, original)
