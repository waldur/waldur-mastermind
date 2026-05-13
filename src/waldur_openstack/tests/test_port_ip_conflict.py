from unittest import mock

from django.test import TestCase
from neutronclient.client import exceptions as neutron_exceptions

from waldur_openstack.backend import OpenStackBackendError

from . import fixtures


class CreateInstancePortIpConflictTest(TestCase):
    """Regression tests for IpAddressAlreadyAllocatedClient handling.

    When Neutron rejects port creation because the requested fixed IP is
    already in use on the subnet (HTTP 409), this is a recoverable
    user/race condition rather than an internal failure. The backend
    should:

    - log at WARNING level (not ERROR) in backend.py;
    - raise OpenStackBackendError with a human-readable message so the
      reason surfaces in instance.error_message instead of the default
      cryptic exception repr.
    """

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.port = self.fixture.port

    def _run_create_port(self, mock_get_neutron_client):
        mock_neutron = mock_get_neutron_client.return_value
        mock_neutron.create_port.side_effect = (
            neutron_exceptions.IpAddressAlreadyAllocatedClient()
        )
        backend = self.port.get_backend()

        with self.assertLogs("waldur_openstack.backend", level="WARNING") as cm:
            with self.assertRaises(OpenStackBackendError) as ctx:
                backend.create_instance_port(self.port, ["security-group-id"])
        return cm, ctx.exception

    @mock.patch("waldur_openstack.backend.get_neutron_client")
    @mock.patch("waldur_openstack.backend.OpenStackBackend.admin_session")
    def test_ip_conflict_is_logged_as_warning_and_raises_backend_error(
        self, mock_admin_session, mock_get_neutron_client
    ):
        cm, _exc = self._run_create_port(mock_get_neutron_client)

        # At least one WARNING-level log must mention the IP-conflict situation.
        warning_lines = [line for line in cm.output if line.startswith("WARNING")]
        self.assertTrue(
            any("Failed to create port" in line for line in warning_lines),
            f"Expected a WARNING about IP allocation, got: {cm.output}",
        )

        # No ERROR-level log lines should be emitted for this recoverable case.
        error_lines = [line for line in cm.output if line.startswith("ERROR")]
        self.assertEqual(
            error_lines,
            [],
            f"Expected no ERROR logs for IP-conflict, got: {error_lines}",
        )

    @mock.patch("waldur_openstack.backend.get_neutron_client")
    @mock.patch("waldur_openstack.backend.OpenStackBackend.admin_session")
    def test_ip_conflict_surfaces_human_readable_user_message(
        self, mock_admin_session, mock_get_neutron_client
    ):
        """OpenStackBackendError must carry an actionable message so it shows
        up in instance.error_message (visible to the end user) instead of the
        default ``IpAddressAlreadyAllocatedClient: An unknown exception occurred.``
        """
        _cm, exc = self._run_create_port(mock_get_neutron_client)

        message = str(exc)
        self.assertIn("Failed to create port on subnet", message)
        # One of the two branches: "requested fixed IP <ip> is already allocated"
        # (user-supplied IP) or "the IP allocated for this port is already in use"
        # (auto-assigned race). Both contain "already".
        self.assertIn("already", message)
        # Should hint at the remediation.
        self.assertIn("Choose a different IP", message)
        # Should NOT just be the bare exception repr.
        self.assertNotIn("An unknown exception occurred", message)
