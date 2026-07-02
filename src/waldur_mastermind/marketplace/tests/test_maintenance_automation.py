from constance.test.unittest import override_config
from django.test import TestCase
from django.utils import timezone

from waldur_mastermind.marketplace import tasks
from waldur_mastermind.marketplace.enums import MaintenanceState
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.notifications.models import AdminAnnouncement


class MaintenanceAnnouncementAutomationTest(TestCase):
    def setUp(self):
        self.service_provider = factories.ServiceProviderFactory()

    def _create(self, state, start_offset, end_offset):
        now = timezone.now()
        return factories.MaintenanceAnnouncementFactory(
            service_provider=self.service_provider,
            state=state,
            scheduled_start=now + timezone.timedelta(hours=start_offset),
            scheduled_end=now + timezone.timedelta(hours=end_offset),
        )

    def test_scheduled_maintenance_is_auto_started_when_start_time_passed(self):
        maintenance = self._create(
            MaintenanceState.SCHEDULED, start_offset=-1, end_offset=1
        )

        tasks.process_maintenance_announcement_transitions()

        maintenance.refresh_from_db()
        self.assertEqual(maintenance.state, MaintenanceState.IN_PROGRESS)
        self.assertEqual(maintenance.actual_start, maintenance.scheduled_start)

    def test_scheduled_maintenance_is_not_started_before_start_time(self):
        maintenance = self._create(
            MaintenanceState.SCHEDULED, start_offset=1, end_offset=2
        )

        tasks.process_maintenance_announcement_transitions()

        maintenance.refresh_from_db()
        self.assertEqual(maintenance.state, MaintenanceState.SCHEDULED)
        self.assertIsNone(maintenance.actual_start)

    def test_in_progress_maintenance_is_auto_completed_when_end_time_passed(self):
        maintenance = self._create(
            MaintenanceState.IN_PROGRESS, start_offset=-2, end_offset=-1
        )

        tasks.process_maintenance_announcement_transitions()

        maintenance.refresh_from_db()
        self.assertEqual(maintenance.state, MaintenanceState.COMPLETED)
        self.assertEqual(maintenance.actual_end, maintenance.scheduled_end)

    def test_in_progress_maintenance_is_not_completed_before_end_time(self):
        maintenance = self._create(
            MaintenanceState.IN_PROGRESS, start_offset=-1, end_offset=1
        )

        tasks.process_maintenance_announcement_transitions()

        maintenance.refresh_from_db()
        self.assertEqual(maintenance.state, MaintenanceState.IN_PROGRESS)
        self.assertIsNone(maintenance.actual_end)

    def test_draft_maintenance_is_never_auto_started(self):
        maintenance = self._create(
            MaintenanceState.DRAFT, start_offset=-1, end_offset=1
        )

        tasks.process_maintenance_announcement_transitions()

        maintenance.refresh_from_db()
        self.assertEqual(maintenance.state, MaintenanceState.DRAFT)
        self.assertIsNone(maintenance.actual_start)

    def test_fully_elapsed_window_is_started_and_completed_in_one_run(self):
        # A maintenance window whose start and end are both already in the past
        # (e.g. the task was down during the whole window) is resolved in a
        # single run: the auto-complete pass sees the record the auto-start pass
        # just moved to IN_PROGRESS, so it doesn't linger until the next tick.
        maintenance = self._create(
            MaintenanceState.SCHEDULED, start_offset=-2, end_offset=-1
        )

        tasks.process_maintenance_announcement_transitions()

        maintenance.refresh_from_db()
        self.assertEqual(maintenance.state, MaintenanceState.COMPLETED)
        # Timestamps reflect the scheduled window, not the catch-up run time, so
        # the record isn't a misleading near-zero-duration blip.
        self.assertEqual(maintenance.actual_start, maintenance.scheduled_start)
        self.assertEqual(maintenance.actual_end, maintenance.scheduled_end)

    def test_task_is_idempotent(self):
        maintenance = self._create(
            MaintenanceState.SCHEDULED, start_offset=-1, end_offset=1
        )

        tasks.process_maintenance_announcement_transitions()
        maintenance.refresh_from_db()
        self.assertEqual(maintenance.state, MaintenanceState.IN_PROGRESS)
        actual_start = maintenance.actual_start

        # Running again must not re-transition or move actual_start.
        tasks.process_maintenance_announcement_transitions()
        maintenance.refresh_from_db()
        self.assertEqual(maintenance.state, MaintenanceState.IN_PROGRESS)
        self.assertEqual(maintenance.actual_start, actual_start)

    @override_config(
        MAINTENANCE_ANNOUNCEMENT_NOTIFY_BEFORE_MINUTES=60,
        MAINTENANCE_ANNOUNCEMENT_TRAILING_BUFFER_MINUTES=60,
    )
    def test_banner_window_is_refreshed_when_maintenance_is_auto_started(self):
        # Go through the real schedule() path so an AdminAnnouncement banner
        # exists, then verify the auto-start refreshes its window via the
        # post_save handler. This also guards that save(update_fields=...) does
        # not stop the state-change handler from firing.
        now = timezone.now()
        maintenance = factories.MaintenanceAnnouncementFactory(
            service_provider=self.service_provider,
            state=MaintenanceState.DRAFT,
            scheduled_start=now - timezone.timedelta(hours=1),
            scheduled_end=now + timezone.timedelta(hours=1),
        )
        maintenance.schedule()
        maintenance.save()
        maintenance.refresh_from_db()
        self.assertIsNotNone(maintenance.admin_announcement)

        tasks.process_maintenance_announcement_transitions()

        maintenance.refresh_from_db()
        self.assertEqual(maintenance.state, MaintenanceState.IN_PROGRESS)
        # Banner is preserved (not recreated) and its window matches the handler
        # computation: active_from = actual_start - notify_before, and
        # active_to = scheduled_end + trailing_buffer (actual_end still unset).
        self.assertEqual(AdminAnnouncement.objects.count(), 1)
        banner = maintenance.admin_announcement
        self.assertIsNotNone(banner)
        self.assertEqual(
            banner.active_from,
            maintenance.actual_start - timezone.timedelta(minutes=60),
        )
        self.assertEqual(
            banner.active_to,
            maintenance.scheduled_end + timezone.timedelta(minutes=60),
        )
