"""Audit-log coverage for OpenStack port security toggle and allowed_address_pairs updates."""

from unittest.mock import patch

from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType
from waldur_openstack import models

from . import factories, fixtures


class PortSecurityToggleAuditTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.port = factories.PortFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            port_security_enabled=True,
            state=CoreStates.OK,
        )
        self.backend_patcher = patch(
            "waldur_openstack.backend.OpenStackBackend.enable_port_security",
            return_value=None,
        )
        self.backend_patcher.start()
        self.addCleanup(self.backend_patcher.stop)
        self.backend_patcher2 = patch(
            "waldur_openstack.backend.OpenStackBackend.disable_port_security",
            return_value=None,
        )
        self.backend_patcher2.start()
        self.addCleanup(self.backend_patcher2.stop)
        self.client.force_authenticate(self.fixture.admin)

    def _events(self, event_type):
        return logging_models.Event.objects.filter(event_type=event_type)

    def test_disable_port_security_emits_event(self):
        url = factories.PortFactory.get_url(self.port, action="disable_port_security")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        events = self._events(EventType.OPENSTACK_PORT_SECURITY_DISABLED)
        self.assertEqual(events.count(), 1)
        ctx = events.first().context
        self.assertEqual(ctx["enabled"], False)
        self.assertEqual(ctx["port_uuid"], self.port.uuid.hex)
        self.assertEqual(ctx["user_uuid"], self.fixture.admin.uuid.hex)

    def test_enable_port_security_emits_event_only_when_state_flips(self):
        # Currently enabled — calling enable again should NOT emit (no-op).
        url = factories.PortFactory.get_url(self.port, action="enable_port_security")
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self._events(EventType.OPENSTACK_PORT_SECURITY_ENABLED).count(), 0
        )

        # Disable, then enable: enable should emit exactly once.
        self.port.port_security_enabled = False
        self.port.save(update_fields=["port_security_enabled"])

        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self._events(EventType.OPENSTACK_PORT_SECURITY_ENABLED).count(), 1
        )


class AllowedAddressPairsAuditTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        # Pre-seed two pairs to exercise the add/remove/modify diff.
        self.existing_pairs = [
            {"ip_address": "10.0.0.5", "mac_address": "aa:bb:cc:dd:ee:01"},
            {"ip_address": "10.0.0.6", "mac_address": "aa:bb:cc:dd:ee:02"},
        ]
        self.port = factories.PortFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            subnet=self.fixture.subnet,
            instance=self.fixture.instance,
            allowed_address_pairs=self.existing_pairs,
            state=CoreStates.OK,
        )
        self.executor_patcher = patch(
            "waldur_openstack.executors."
            "InstanceAllowedAddressPairsUpdateExecutor.execute",
            return_value=None,
        )
        self.executor_patcher.start()
        self.addCleanup(self.executor_patcher.stop)
        self.client.force_authenticate(self.fixture.admin)
        self.url = factories.InstanceFactory.get_url(
            self.fixture.instance, action="update_allowed_address_pairs"
        )

    def _events(self, event_type):
        return logging_models.Event.objects.filter(event_type=event_type)

    def test_pair_change_emits_aggregate_event(self):
        new_pairs = [
            # KEEP 10.0.0.5 unchanged
            {"ip_address": "10.0.0.5", "mac_address": "aa:bb:cc:dd:ee:01"},
            # MODIFY 10.0.0.6 (new MAC)
            {"ip_address": "10.0.0.6", "mac_address": "aa:bb:cc:dd:ee:99"},
            # ADD 10.0.0.7
            {"ip_address": "10.0.0.7", "mac_address": "aa:bb:cc:dd:ee:03"},
        ]
        response = self.client.post(
            self.url,
            data={
                "subnet": factories.SubNetFactory.get_url(self.fixture.subnet),
                "allowed_address_pairs": new_pairs,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        events = self._events(EventType.OPENSTACK_PORT_ALLOWED_ADDRESS_PAIRS_CHANGED)
        self.assertEqual(events.count(), 1)
        ctx = events.first().context
        self.assertEqual(ctx["added_count"], 1)
        self.assertEqual(ctx["removed_count"], 0)
        self.assertEqual(ctx["modified_count"], 1)
        self.assertEqual(ctx["added_pairs"][0]["ip_address"], "10.0.0.7")
        modified = ctx["modified_pairs"][0]
        self.assertEqual(modified["new"]["mac_address"], "aa:bb:cc:dd:ee:99")
        self.assertEqual(modified["old"]["mac_address"], "aa:bb:cc:dd:ee:02")
        self.assertEqual(ctx["trigger"], "user_action")

    def test_unchanged_pairs_emit_no_event(self):
        response = self.client.post(
            self.url,
            data={
                "subnet": factories.SubNetFactory.get_url(self.fixture.subnet),
                "allowed_address_pairs": self.existing_pairs,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            self._events(
                EventType.OPENSTACK_PORT_ALLOWED_ADDRESS_PAIRS_CHANGED
            ).count(),
            0,
        )

    def test_empty_pairs_remove_all(self):
        response = self.client.post(
            self.url,
            data={
                "subnet": factories.SubNetFactory.get_url(self.fixture.subnet),
                "allowed_address_pairs": [],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        events = self._events(EventType.OPENSTACK_PORT_ALLOWED_ADDRESS_PAIRS_CHANGED)
        self.assertEqual(events.count(), 1)
        ctx = events.first().context
        self.assertEqual(ctx["removed_count"], 2)
        self.assertEqual(ctx["added_count"], 0)
