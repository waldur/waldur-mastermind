from unittest import mock

from django.test import TransactionTestCase
from django.test.utils import override_settings

from waldur_core.core import utils
from waldur_core.logging.tests import factories as logging_factories

from .. import tasks


class TestDetectEventLocationTask(TransactionTestCase):
    @mock.patch("waldur_geo_ip.handlers.tasks")
    def test_handler(self, mock_tasks):
        logging_factories.EventFactory(
            context={"ip_address": "127.0.0.1", "location": "pending"}
        )
        mock_tasks.detect_event_location.delay.assert_called_once()

    @mock.patch("requests.get")
    @override_settings(IPSTACK_ACCESS_KEY="IPSTACK_ACCESS_KEY")
    def test_task(self, mock_request_get):
        event = logging_factories.EventFactory(
            context={"ip_address": "127.0.0.1", "location": "pending"}
        )
        mock_request_get.return_value.ok = True
        response = {
            "ip": "127.0.0.1",
            "country_name": "Country",
        }
        mock_request_get.return_value.json.return_value = response
        tasks.detect_event_location(utils.serialize_instance(event))

        event.refresh_from_db()
        self.assertEqual(event.context.get("location"), "Country")
