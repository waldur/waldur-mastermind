from unittest import mock

from django.test import TransactionTestCase
from django.test.utils import override_settings

from waldur_core.core import utils
from waldur_core.structure.tests import factories

from .. import tasks


class TestDetectVMCoordinatesTask(TransactionTestCase):
    @mock.patch('requests.get')
    @override_settings(IPSTACK_ACCESS_KEY='IPSTACK_ACCESS_KEY')
    def test_task_sets_coordinates(self, mock_request_get):
        ip_address = "127.0.0.1"
        expected_latitude = 20
        expected_longitude = 20
        instance = factories.TestNewInstanceFactory()

        mock_request_get.return_value.ok = True
        response = {
            "ip": ip_address,
            "latitude": expected_latitude,
            "longitude": expected_longitude,
        }
        mock_request_get.return_value.json.return_value = response
        tasks.detect_vm_coordinates(utils.serialize_instance(instance))

        instance.refresh_from_db()
        self.assertEqual(instance.latitude, expected_latitude)
        self.assertEqual(instance.longitude, expected_longitude)

    @mock.patch('requests.get')
    def test_task_does_not_set_coordinates_if_response_is_not_ok(
        self, mock_request_get
    ):
        instance = factories.TestNewInstanceFactory()

        mock_request_get.return_value.ok = False
        tasks.detect_vm_coordinates(utils.serialize_instance(instance))

        instance.refresh_from_db()
        self.assertIsNone(instance.latitude)
        self.assertIsNone(instance.longitude)
