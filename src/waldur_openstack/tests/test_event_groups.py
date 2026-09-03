"""Validate that the OPENSTACK_* EventGroup chips resolve via the
existing /api/events/?feature=<group> machinery.

T5 originally added a standalone /openstack-tenants/{uuid}/network_events/
endpoint. Code review (the user's call) pointed out the existing global
events endpoint already supports scope + feature filtering with proper
permission gating, so we now publish per-category chips as EventGroup
entries and let the frontend hit /api/events/ directly. This file is
the contract test for that integration.
"""

from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EVENT_GROUP_MAPPING, EventGroup, EventType
from waldur_core.logging.event_logger import (
    expand_event_groups,
    get_event_groups,
)

from . import factories, fixtures


class OpenstackEventGroupMappingTest(test.APISimpleTestCase):
    """Pure unit tests for the chip → event-type map."""

    EXPECTED_CHIPS = {
        EventGroup.OPENSTACK_ROUTER,
        EventGroup.OPENSTACK_NETWORK,
        EventGroup.OPENSTACK_SUBNET,
        EventGroup.OPENSTACK_PORT,
        EventGroup.OPENSTACK_FLOATING_IP,
        EventGroup.OPENSTACK_RBAC,
        EventGroup.OPENSTACK_SECURITY_GROUP,
    }

    def test_all_seven_chips_registered(self):
        for chip in self.EXPECTED_CHIPS:
            self.assertIn(chip, EVENT_GROUP_MAPPING, f"{chip} missing from map")

    def test_rbac_chip_contains_create_and_delete(self):
        types = EVENT_GROUP_MAPPING[EventGroup.OPENSTACK_RBAC]
        self.assertIn(EventType.OPENSTACK_RBAC_POLICY_CREATED, types)
        self.assertIn(EventType.OPENSTACK_RBAC_POLICY_DELETED, types)

    def test_port_chip_includes_aap_changed(self):
        types = EVENT_GROUP_MAPPING[EventGroup.OPENSTACK_PORT]
        self.assertIn(EventType.OPENSTACK_PORT_ALLOWED_ADDRESS_PAIRS_CHANGED, types)

    def test_router_chip_includes_new_interface_events(self):
        types = EVENT_GROUP_MAPPING[EventGroup.OPENSTACK_ROUTER]
        self.assertIn(EventType.OPENSTACK_ROUTER_INTERFACE_ADDED, types)
        self.assertIn(EventType.OPENSTACK_ROUTER_INTERFACE_REMOVED, types)

    def test_subnet_chip_includes_host_routes_change(self):
        types = EVENT_GROUP_MAPPING[EventGroup.OPENSTACK_SUBNET]
        self.assertIn(EventType.OPENSTACK_SUBNET_HOST_ROUTES_CHANGED, types)

    def test_expand_event_groups_returns_strings(self):
        expanded = expand_event_groups([EventGroup.OPENSTACK_RBAC])
        self.assertIn(EventType.OPENSTACK_RBAC_POLICY_CREATED.value, expanded)
        self.assertIn(EventType.OPENSTACK_RBAC_POLICY_DELETED.value, expanded)

    def test_groups_present_in_the_full_catalogue(self):
        """The chips must stay in the static mapping, which is what dispatch and
        ?feature= filtering resolve against.

        Whether the /api/events/event_groups/ endpoint *advertises* them is a
        separate, deployment-dependent question -- it does so only where an
        OpenStack offering exists. See waldur_core.logging.availability and
        waldur_core/logging/tests/test_availability.py.
        """
        groups = get_event_groups()
        for chip in self.EXPECTED_CHIPS:
            self.assertIn(chip.value, groups)


class OpenstackEventsViaGlobalEndpointTest(test.APITestCase):
    """End-to-end: filter the global /api/events/ via the new chips."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.client.force_authenticate(self.fixture.owner)

        # Insert a handful of events directly into the audit store,
        # mirroring what event_logger.emit() would do. Each Event has a
        # Feed entry scoping it to the tenant so /api/events/?scope=...
        # picks it up.
        self.router_event = self._emit(
            EventType.OPENSTACK_ROUTER_UPDATED, "router updated"
        )
        self.network_event = self._emit(
            EventType.OPENSTACK_NETWORK_CREATED, "network created"
        )
        self.rbac_event = self._emit(
            EventType.OPENSTACK_RBAC_POLICY_CREATED, "rbac created"
        )

        from waldur_openstack.tests import factories as factories_mod

        self.tenant_url = factories_mod.TenantFactory.get_url(self.tenant)

    def _emit(self, event_type, message):
        event = logging_models.Event.objects.create(
            event_type=event_type, message=message, context={}
        )
        logging_models.Feed.objects.create(
            scope=self.tenant,
            event=event,
            content_type=ContentType.objects.get_for_model(self.tenant),
            object_id=self.tenant.id,
        )
        return event

    def test_feature_router_returns_router_events_only(self):
        response = self.client.get(
            "/api/events/",
            {"scope": self.tenant_url, "feature": "openstack_router"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        types = [row["event_type"] for row in response.data]
        self.assertIn(EventType.OPENSTACK_ROUTER_UPDATED.value, types)
        self.assertNotIn(EventType.OPENSTACK_NETWORK_CREATED.value, types)
        self.assertNotIn(EventType.OPENSTACK_RBAC_POLICY_CREATED.value, types)

    def test_multi_chip_filter_accumulates(self):
        # Multi-feature query reproduces multi-select chips on the FE.
        response = self.client.get(
            f"/api/events/?scope={self.tenant_url}"
            "&feature=openstack_router&feature=openstack_rbac",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        types = [row["event_type"] for row in response.data]
        self.assertIn(EventType.OPENSTACK_ROUTER_UPDATED.value, types)
        self.assertIn(EventType.OPENSTACK_RBAC_POLICY_CREATED.value, types)
        self.assertNotIn(EventType.OPENSTACK_NETWORK_CREATED.value, types)

    def test_unrelated_tenant_does_not_see_events(self):
        """A different tenant's scope must not leak this tenant's events."""
        from waldur_core.structure.tests import (
            fixtures as structure_fixtures,
        )

        other_fixture = structure_fixtures.ProjectFixture()
        other_tenant = factories.TenantFactory(
            service_settings=self.fixture.settings,
            project=other_fixture.project,
            backend_id="other",
        )
        # Emit a router event scoped to the OTHER tenant; our authenticated
        # user has no role on that project.
        other_event = logging_models.Event.objects.create(
            event_type=EventType.OPENSTACK_ROUTER_UPDATED,
            message="other tenant router updated",
            context={},
        )
        logging_models.Feed.objects.create(
            scope=other_tenant,
            event=other_event,
            content_type=ContentType.objects.get_for_model(other_tenant),
            object_id=other_tenant.id,
        )

        # Hit /api/events/ with feature=router but no scope. The backend
        # filter denies non-staff non-support callers without a scope, so
        # the queryset is .none() — confirms we cannot harvest the global
        # event stream by category alone.
        response = self.client.get("/api/events/", {"feature": "openstack_router"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])
