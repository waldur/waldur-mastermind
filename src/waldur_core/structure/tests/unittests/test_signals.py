from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time

from waldur_core.structure.tests import factories


class StartTimeTest(TestCase):
    def test_if_resource_becomes_online_start_time_is_initialized(self):
        now = timezone.now()
        with freeze_time(now):
            vm = factories.TestNewInstanceFactory(
                runtime_state="in-progress", start_time=None
            )
            vm.runtime_state = "online"
            vm.save()
            vm.refresh_from_db()
            self.assertEqual(vm.start_time, now)

    def test_if_resource_becomes_offline_start_time_is_resetted(self):
        vm = factories.TestNewInstanceFactory(
            runtime_state="paused", start_time=timezone.now()
        )
        vm.runtime_state = "offline"
        vm.save()
        vm.refresh_from_db()
        self.assertEqual(vm.start_time, None)

    def test_if_runtime_state_changed_to_other_state_start_time_is_not_modified(self):
        vm = factories.TestNewInstanceFactory(runtime_state="online", start_time=None)
        vm.runtime_state = "extending"
        vm.save()
        vm.refresh_from_db()
        self.assertEqual(vm.start_time, None)
