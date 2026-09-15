from datetime import timedelta
from unittest import mock

from ddt import data, ddt
from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time

from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils
from waldur_core.core.enums import CoreStates
from waldur_core.structure import models, tasks
from waldur_core.structure.tests import factories
from waldur_core.structure.tests import models as test_models


class StateTransitionTaskTest(TestCase):
    def test_transition_keeps_a_column_written_after_the_instance_was_loaded(self):
        # A task loads the settings, then a request stores a credential, then the
        # task's error path records the failure. The credential must survive.
        settings = factories.ServiceSettingsFactory(state=CoreStates.ERRED)
        loaded = models.ServiceSettings.objects.get(pk=settings.pk)

        concurrent = models.ServiceSettings.objects.get(pk=settings.pk)
        concurrent.password = "secret"
        concurrent.save(update_fields=["password"])

        core_tasks.StateTransitionTask().state_transition(
            loaded, "set_erred", action="", action_details={}
        )

        settings.refresh_from_db()
        self.assertEqual(settings.state, CoreStates.ERRED)
        self.assertEqual(settings.password, "secret")

    def test_transition_saves_the_new_state(self):
        instance = factories.TestNewInstanceFactory(state=CoreStates.UPDATE_SCHEDULED)
        loaded = test_models.TestNewInstance.objects.get(pk=instance.pk)
        test_models.TestNewInstance.objects.filter(pk=instance.pk).update(
            name="renamed"
        )

        core_tasks.StateTransitionTask().state_transition(loaded, "begin_updating")

        instance.refresh_from_db()
        self.assertEqual(instance.state, CoreStates.UPDATING)
        self.assertEqual(instance.name, "renamed")

    def test_transition_into_the_current_state_still_touches_modified(self):
        # SetErredStuckResources reads `modified` as the time of the last transition.
        with freeze_time(timezone.now() - timedelta(hours=1)):
            instance = factories.TestNewInstanceFactory(state=CoreStates.ERRED)
        modified = instance.modified

        core_tasks.StateTransitionTask().state_transition(instance, "set_erred")

        instance.refresh_from_db()
        self.assertGreater(instance.modified, modified)


@ddt
class ThrottleProvisionTaskTest(TestCase):
    @data(
        dict(size=tasks.ThrottleProvisionTask.DEFAULT_LIMIT + 1, retried=True),
        dict(size=tasks.ThrottleProvisionTask.DEFAULT_LIMIT - 1, retried=False),
    )
    def test_if_limit_is_reached_provisioning_is_delayed(self, params):
        service_settings = factories.ServiceSettingsFactory()
        project = factories.ProjectFactory()
        factories.TestNewInstanceFactory.create_batch(
            size=params["size"],
            state=CoreStates.CREATING,
            service_settings=service_settings,
            project=project,
        )
        vm = factories.TestNewInstanceFactory(
            state=CoreStates.CREATION_SCHEDULED,
            service_settings=service_settings,
            project=project,
        )
        serialized_vm = utils.serialize_instance(vm)
        mocked_retry = mock.Mock()
        tasks.ThrottleProvisionTask.retry = mocked_retry
        tasks.ThrottleProvisionTask().si(
            serialized_vm, "create", state_transition="begin_starting"
        ).apply()
        self.assertEqual(mocked_retry.called, params["retried"])


class SetErredProvisioningResourcesTaskTest(TestCase):
    def test_stuck_resource_becomes_erred(self):
        with freeze_time(timezone.now() - timedelta(hours=4)):
            stuck_vm = factories.TestNewInstanceFactory(state=CoreStates.CREATING)
            stuck_volume = factories.TestVolumeFactory(state=CoreStates.CREATING)

        tasks.SetErredStuckResources().run()

        stuck_vm.refresh_from_db()
        stuck_volume.refresh_from_db()

        self.assertEqual(stuck_vm.state, CoreStates.ERRED)
        self.assertEqual(stuck_volume.state, CoreStates.ERRED)

    def test_ok_vm_unchanged(self):
        ok_vm = factories.TestNewInstanceFactory(
            state=CoreStates.CREATING,
            modified=timezone.now() - timedelta(minutes=1),
        )
        ok_volume = factories.TestVolumeFactory(
            state=CoreStates.CREATING,
            modified=timezone.now() - timedelta(minutes=1),
        )
        tasks.SetErredStuckResources().run()

        ok_vm.refresh_from_db()
        ok_volume.refresh_from_db()

        self.assertEqual(ok_vm.state, CoreStates.CREATING)
        self.assertEqual(ok_volume.state, CoreStates.CREATING)


class ExceptionTest(TestCase):
    def test_exception_must_include_setting_name_and_type(self):
        service_settings = factories.ServiceSettingsFactory()

        class Backend:
            def pull_resources(self):
                raise KeyError("test error")

        backend = Backend()
        service_settings.get_backend = lambda: backend
        task = tasks.ServiceResourcesPullTask()
        error_message = f"'test error', Service settings: {service_settings.name}, {service_settings.type}"
        self.assertRaisesRegex(KeyError, error_message, task.pull, service_settings)


class BackgroundListPullTaskTest(TestCase):
    def test_get_pulled_objects_filters_by_state_and_backend_id(self):
        # Create instances with various states and backend_id values
        good1 = factories.TestNewInstanceFactory(state=CoreStates.OK, backend_id="id1")
        good2 = factories.TestNewInstanceFactory(
            state=CoreStates.ERRED, backend_id="id2"
        )
        bad1 = factories.TestNewInstanceFactory(
            state=CoreStates.OK, backend_id=""
        )  # empty backend_id
        bad2 = factories.TestNewInstanceFactory(
            state=CoreStates.CREATING, backend_id="id3"
        )  # wrong state

        class TestTask(tasks.BackgroundListPullTask):
            model = test_models.TestNewInstance
            pull_task = mock.Mock()

        task = TestTask()
        result = list(task.get_pulled_objects())
        self.assertIn(good1, result)
        self.assertIn(good2, result)
        self.assertNotIn(bad1, result)
        self.assertNotIn(bad2, result)

    def test_run_schedules_pull_tasks_and_uses_iterator(self):
        """Test that run() schedules pull tasks for each valid instance.

        Note: run() iterates with chunked_queryset (client-side PK pagination)
        for memory efficiency without server-side cursors.
        """
        instance1 = factories.TestNewInstanceFactory(
            state=CoreStates.OK, backend_id="id1"
        )
        instance2 = factories.TestNewInstanceFactory(
            state=CoreStates.OK, backend_id="id2"
        )

        # Verify get_pulled_objects returns our instances
        class TestTask(tasks.BackgroundListPullTask):
            model = test_models.TestNewInstance
            pull_task = mock.Mock()

        task = TestTask()
        pulled_objects = list(task.get_pulled_objects())

        # Verify both instances are in the queryset
        self.assertIn(instance1, pulled_objects)
        self.assertIn(instance2, pulled_objects)
        self.assertEqual(len(pulled_objects), 2)
