from datetime import timedelta
from unittest import mock

from ddt import data, ddt
from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time

from waldur_core.core import utils
from waldur_core.structure import tasks
from waldur_core.structure.tests import factories, models


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
            state=models.TestNewInstance.States.CREATING,
            service_settings=service_settings,
            project=project,
        )
        vm = factories.TestNewInstanceFactory(
            state=models.TestNewInstance.States.CREATION_SCHEDULED,
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
            stuck_vm = factories.TestNewInstanceFactory(
                state=models.TestNewInstance.States.CREATING
            )
            stuck_volume = factories.TestVolumeFactory(
                state=models.TestVolume.States.CREATING
            )

        tasks.SetErredStuckResources().run()

        stuck_vm.refresh_from_db()
        stuck_volume.refresh_from_db()

        self.assertEqual(stuck_vm.state, models.TestNewInstance.States.ERRED)
        self.assertEqual(stuck_volume.state, models.TestVolume.States.ERRED)

    def test_ok_vm_unchanged(self):
        ok_vm = factories.TestNewInstanceFactory(
            state=models.TestNewInstance.States.CREATING,
            modified=timezone.now() - timedelta(minutes=1),
        )
        ok_volume = factories.TestVolumeFactory(
            state=models.TestVolume.States.CREATING,
            modified=timezone.now() - timedelta(minutes=1),
        )
        tasks.SetErredStuckResources().run()

        ok_vm.refresh_from_db()
        ok_volume.refresh_from_db()

        self.assertEqual(ok_vm.state, models.TestNewInstance.States.CREATING)
        self.assertEqual(ok_volume.state, models.TestVolume.States.CREATING)


class ExceptionTest(TestCase):
    def test_exception_must_include_setting_name_and_type(self):
        service_settings = factories.ServiceSettingsFactory()

        class Backend:
            def pull_resources(self):
                raise KeyError("test error")

        backend = Backend()
        service_settings.get_backend = lambda: backend
        task = tasks.ServiceResourcesPullTask()
        error_message = "'test error', Service settings: {}, {}".format(
            service_settings.name,
            service_settings.type,
        )
        self.assertRaisesRegex(KeyError, error_message, task.pull, service_settings)
