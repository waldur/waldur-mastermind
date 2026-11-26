import requests
import responses
from django.test import SimpleTestCase

from waldur_firecrest.client import FirecrestClient, FirecrestException
from waldur_firecrest.monitoring import MonitorClient, MonitorException

INTERNAL_ERROR = requests.exceptions.ConnectionError(
    "HTTPSConnectionPool(host='hpc.internal', port=443): Max retries exceeded"
)


class ConnectionErrorTest(SimpleTestCase):
    """The message reaches users and the AI assistant, so it names no host."""

    @responses.activate
    def test_firecrest_error_names_no_host(self):
        responses.get("https://hpc.internal/status/systems", body=INTERNAL_ERROR)
        client = FirecrestClient("https://hpc.internal", "token")

        with self.assertLogs("waldur_firecrest.client", "WARNING") as logs:
            with self.assertRaises(FirecrestException) as raised:
                client.list_systems()

        self.assertEqual(str(raised.exception), "Unable to reach FirecREST.")
        self.assertIn("hpc.internal", logs.output[-1])

    @responses.activate
    def test_monitor_error_names_no_host(self):
        responses.get("https://hpc.internal/cluster/c1/jobs/7", body=INTERNAL_ERROR)
        client = MonitorClient("https://hpc.internal", "c1", "token")

        with self.assertLogs("waldur_firecrest.monitoring", "WARNING") as logs:
            with self.assertRaises(MonitorException) as raised:
                client.get_job(7)

        self.assertEqual(str(raised.exception), "Unable to reach the cluster monitor.")
        self.assertIn("hpc.internal", logs.output[-1])
