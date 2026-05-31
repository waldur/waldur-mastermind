from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_openstack import models
from waldur_openstack.diagnose import run_diagnose

from . import factories, fixtures


class _BaseDiagnoseTest(test.APITestCase):
    """Common fixture: an instance with one port on a subnet."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.subnet = self.fixture.subnet
        self.network = self.subnet.network
        models.SubNet.objects.filter(pk=self.subnet.pk).update(cidr="10.0.0.0/24")
        self.subnet.refresh_from_db()

        self.instance = factories.InstanceFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            state=CoreStates.OK,
            runtime_state=models.Instance.RuntimeStates.ACTIVE,
        )
        self.port = factories.PortFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
            instance=self.instance,
            network=self.network,
            subnet=self.subnet,
            admin_state_up=True,
            port_security_enabled=True,
            state=CoreStates.OK,
        )
        self.port.security_groups.add(self.fixture.security_group)

    def _status_for(self, report, check_name):
        for c in report.checks:
            if c.check == check_name:
                return c.status
        self.fail(
            f"Check {check_name!r} not in report: {[c.check for c in report.checks]}"
        )


class DiagnoseHappyPathTest(_BaseDiagnoseTest):
    def setUp(self):
        super().setUp()
        self.ext_net = factories.ExternalNetworkFactory(
            settings=self.fixture.settings, backend_id="ext-net"
        )
        factories.ExternalSubnetFactory(
            network=self.ext_net,
            backend_id="ext-subnet",
            cidr="192.168.240.96/28",
            allocation_pools=[{"start": "192.168.240.98", "end": "192.168.240.110"}],
        )
        self.router = factories.RouterFactory(
            tenant=self.fixture.tenant,
            external_network_ref=self.ext_net,
            external_fixed_ips=[
                {
                    "subnet_id": "ext-subnet",
                    "ip_address": "192.168.240.104",
                }
            ],
        )
        self.router.ports.add(self.port)

    def test_running_active_instance_with_router_passes(self):
        report = run_diagnose(self.instance, target="external")
        self.assertEqual(self._status_for(report, "instance_running"), "ok")
        self.assertEqual(self._status_for(report, "port_admin_state"), "ok")
        self.assertEqual(self._status_for(report, "subnet_on_router"), "ok")
        self.assertEqual(self._status_for(report, "router_external_gateway"), "ok")
        self.assertIsNone(report.root_cause)


class DiagnoseFailPathsTest(_BaseDiagnoseTest):
    def test_stopped_instance_fails_running_check(self):
        models.Instance.objects.filter(pk=self.instance.pk).update(
            runtime_state=models.Instance.RuntimeStates.SHUTOFF
        )
        self.instance.refresh_from_db()
        report = run_diagnose(self.instance, target="external")
        self.assertEqual(self._status_for(report, "instance_running"), "fail")
        self.assertIsNotNone(report.root_cause)

    def test_admin_down_port_fails(self):
        models.Port.objects.filter(pk=self.port.pk).update(admin_state_up=False)
        self.port.refresh_from_db()
        report = run_diagnose(self.instance, target="external")
        self.assertEqual(self._status_for(report, "port_admin_state"), "fail")

    def test_port_security_without_sg_warns(self):
        self.port.security_groups.clear()
        report = run_diagnose(self.instance, target="external")
        self.assertEqual(self._status_for(report, "port_security_groups"), "warn")

    def test_no_router_fails_external_target(self):
        # No router on the path — external traffic blocked.
        report = run_diagnose(self.instance, target="external")
        self.assertEqual(self._status_for(report, "subnet_on_router"), "fail")
        self.assertIn("No subnet", report.root_cause)

    def test_internal_target_skips_router_checks(self):
        report = run_diagnose(self.instance, target="internal:10.0.0.42")
        self.assertEqual(self._status_for(report, "subnet_on_router"), "skip")
        self.assertEqual(self._status_for(report, "router_external_gateway"), "skip")
        # Target in same subnet → ok
        self.assertEqual(self._status_for(report, "internal_target_reachable"), "ok")


class DiagnoseDirectExternalTest(_BaseDiagnoseTest):
    """When a VM's only port is directly attached to an external network
    (no tenant router on the path), outbound connectivity works without
    a router. Diagnose must report ``ok``, not ``fail``."""

    def test_direct_external_port_does_not_fail(self):
        # Flip the network to external (mirrors Neutron router:external=True).
        self.network.is_external = True
        self.network.save(update_fields=["is_external"])

        report = run_diagnose(self.instance, target="external")
        self.assertEqual(self._status_for(report, "subnet_on_router"), "ok")
        # No router on the path → gateway check is correctly skipped.
        self.assertEqual(self._status_for(report, "router_external_gateway"), "skip")


class DiagnoseFipTargetTest(_BaseDiagnoseTest):
    def test_unattached_fip_fails(self):
        report = run_diagnose(self.instance, target="fip:1.2.3.4")
        self.assertEqual(self._status_for(report, "floating_ip_path"), "fail")
        # The root cause may come from an earlier failing check (no router).
        self.assertIsNotNone(report.root_cause)

    def test_attached_fip_passes(self):
        factories.FloatingIPFactory(
            tenant=self.fixture.tenant,
            address="192.168.240.99",
            backend_network_id="ext-net",
            port=self.port,
        )
        report = run_diagnose(self.instance, target="fip:192.168.240.99")
        self.assertEqual(self._status_for(report, "floating_ip_path"), "ok")


class DiagnoseEndpointTest(_BaseDiagnoseTest):
    """HTTP smoke test."""

    def test_diagnose_returns_documented_shape(self):
        url = factories.InstanceFactory.get_url(self.instance, "diagnose_connectivity")
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(url, {"target": "external"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.data
        self.assertEqual(body["target"], "external")
        self.assertIn("checks", body)
        self.assertGreaterEqual(len(body["checks"]), 5)
        # Each check must carry the documented fields.
        for check in body["checks"]:
            self.assertIn("check", check)
            self.assertIn("status", check)
            self.assertIn(check["status"], ("ok", "warn", "fail", "skip"))
            self.assertIn("detail", check)
