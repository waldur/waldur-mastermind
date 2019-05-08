from datetime import timedelta

from ddt import ddt, data
from django.test import TestCase
from django.utils import timezone
from django.test.utils import override_settings
from six.moves import mock

from waldur_core.core import utils
from waldur_core.structure import tasks
from waldur_core.structure.tests import factories, models


class TestDetectVMCoordinatesTask(TestCase):

    @mock.patch('requests.get')
    @override_settings(IPSTACK_ACCESS_KEY='IPSTACK_ACCESS_KEY')
    def test_task_sets_coordinates(self, mock_request_get):
        ip_address = "127.0.0.1"
        expected_latitude = 20
        expected_longitude = 20
        instance = factories.TestNewInstanceFactory()

        mock_request_get.return_value.ok = True
        response = {"ip": ip_address, "latitude": expected_latitude, "longitude": expected_longitude}
        mock_request_get.return_value.json.return_value = response
        tasks.detect_vm_coordinates(utils.serialize_instance(instance))

        instance.refresh_from_db()
        self.assertEqual(instance.latitude, expected_latitude)
        self.assertEqual(instance.longitude, expected_longitude)

    @mock.patch('requests.get')
    def test_task_does_not_set_coordinates_if_response_is_not_ok(self, mock_request_get):
        instance = factories.TestNewInstanceFactory()

        mock_request_get.return_value.ok = False
        tasks.detect_vm_coordinates(utils.serialize_instance(instance))

        instance.refresh_from_db()
        self.assertIsNone(instance.latitude)
        self.assertIsNone(instance.longitude)


@ddt
class ThrottleProvisionTaskTest(TestCase):

    @data(
        dict(size=tasks.ThrottleProvisionTask.DEFAULT_LIMIT + 1, retried=True),
        dict(size=tasks.ThrottleProvisionTask.DEFAULT_LIMIT - 1, retried=False),
    )
    def test_if_limit_is_reached_provisioning_is_delayed(self, params):
        link = factories.TestServiceProjectLinkFactory()
        factories.TestNewInstanceFactory.create_batch(
            size=params['size'],
            state=models.TestNewInstance.States.CREATING,
            service_project_link=link)
        vm = factories.TestNewInstanceFactory(
            state=models.TestNewInstance.States.CREATION_SCHEDULED,
            service_project_link=link)
        serialized_vm = utils.serialize_instance(vm)
        mocked_retry = mock.Mock()
        tasks.ThrottleProvisionTask.retry = mocked_retry
        tasks.ThrottleProvisionTask().si(
            serialized_vm,
            'create',
            state_transition='begin_starting').apply()
        self.assertEqual(mocked_retry.called, params['retried'])


class SetErredProvisioningResourcesTaskTest(TestCase):
    def test_stuck_resource_becomes_erred(self):
        with mock.patch('model_utils.fields.now') as mocked_now:
            mocked_now.return_value = timezone.now() - timedelta(hours=4)
            stuck_vm = factories.TestNewInstanceFactory(state=models.TestNewInstance.States.CREATING)
            stuck_volume = factories.TestVolumeFactory(state=models.TestVolume.States.CREATING)

        tasks.SetErredStuckResources().run()

        stuck_vm.refresh_from_db()
        stuck_volume.refresh_from_db()

        self.assertEqual(stuck_vm.state, models.TestNewInstance.States.ERRED)
        self.assertEqual(stuck_volume.state, models.TestVolume.States.ERRED)

    def test_ok_vm_unchanged(self):
        ok_vm = factories.TestNewInstanceFactory(
            state=models.TestNewInstance.States.CREATING,
            modified=timezone.now() - timedelta(minutes=1)
        )
        ok_volume = factories.TestVolumeFactory(
            state=models.TestVolume.States.CREATING,
            modified=timezone.now() - timedelta(minutes=1)
        )
        tasks.SetErredStuckResources().run()

        ok_vm.refresh_from_db()
        ok_volume.refresh_from_db()

        self.assertEqual(ok_vm.state, models.TestNewInstance.States.CREATING)
        self.assertEqual(ok_volume.state, models.TestVolume.States.CREATING)
