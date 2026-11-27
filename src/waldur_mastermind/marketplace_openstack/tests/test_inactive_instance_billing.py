import datetime
from unittest import mock

from ddt import data, ddt
from django.test import SimpleTestCase
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.callbacks import create_resource_plan_period
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_TENANT_OFFERING,
    BillingTypes,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.utils import create_offering_components
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_TYPE,
    utils,
)
from waldur_mastermind.marketplace_openstack import models as billing_models
from waldur_openstack.backend import OpenStackBackend, OpenStackBackendError
from waldur_openstack.tests import factories as openstack_factories
from waldur_openstack.tests import fixtures as openstack_fixtures
from waldur_openstack.tests.fixtures import mock_session

INACTIVE = ("SHUTOFF", "SUSPENDED", "SHELVED", "SHELVED_OFFLOADED")
STILL_BILLED = ("ACTIVE", "PAUSED", "ERROR", "BUILD", "REBOOT", "RESIZE")


def server(server_id, status, vcpus, ram, updated="2026-11-27T08:00:00Z"):
    return {
        "id": server_id,
        "status": status,
        "vcpus": vcpus,
        "ram": ram,
        "updated": updated,
    }


@ddt
class IdleServerIdsTest(SimpleTestCase):
    @data(*INACTIVE)
    def test_server_unchanged_since_previous_poll_is_idle(self, status):
        servers = [server("a", "ACTIVE", 2, 2048), server("b", status, 4, 4096)]
        previous = {s["id"]: s["updated"] for s in servers}
        self.assertEqual(utils.idle_server_ids(servers, previous), {"b"})

    @data(*STILL_BILLED)
    def test_other_status_is_never_idle(self, status):
        servers = [server("b", status, 4, 4096)]
        previous = {"b": servers[0]["updated"]}
        self.assertEqual(utils.idle_server_ids(servers, previous), set())

    def test_server_changed_since_previous_poll_is_not_idle(self):
        # Read as stopped now, but Nova changed it since the previous poll: it
        # may have run for most of the interval, so it is billed for it.
        servers = [server("b", "SHUTOFF", 4, 4096, updated="2026-11-27T10:50:00Z")]
        previous = {"b": "2026-11-27T08:00:00Z"}
        self.assertEqual(utils.idle_server_ids(servers, previous), set())

    def test_server_the_previous_poll_did_not_see_is_not_idle(self):
        servers = [server("b", "SHUTOFF", 4, 4096)]
        self.assertEqual(utils.idle_server_ids(servers, {}), set())

    def test_server_without_updated_is_not_idle(self):
        servers = [server("b", "SHUTOFF", 4, 4096, updated=None)]
        self.assertEqual(utils.idle_server_ids(servers, {"b": None}), set())

    def test_active_server_compute_leaves_out_idle_servers(self):
        servers = [server("a", "ACTIVE", 2, 2048), server("b", "SHUTOFF", 4, 4096)]
        self.assertEqual(
            utils.active_server_compute(servers, {"b"}),
            {CORES_TYPE: 2, RAM_TYPE: 2048},
        )
        self.assertEqual(
            utils.active_server_compute(servers, set()),
            {CORES_TYPE: 6, RAM_TYPE: 6144},
        )

    def test_no_servers_bill_zero(self):
        self.assertEqual(
            utils.active_server_compute([], set()), {CORES_TYPE: 0, RAM_TYPE: 0}
        )


class InactiveInstanceBillingBase(test.APITestCase):
    plugin_options: dict = {}

    def setUp(self):
        super().setUp()
        mock_session()
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.tenant, plan=marketplace_factories.PlanFactory()
        )
        offering = self.resource.offering
        offering.type = OPENSTACK_TENANT_OFFERING
        offering.plugin_options = dict(self.plugin_options)
        offering.save()
        create_offering_components(offering)
        # Any quota change fires import_usage through a signal, so the server
        # list is mocked before the test touches quotas.
        self.list_servers = mock.patch.object(
            OpenStackBackend, "get_tenant_server_compute"
        ).start()
        self.list_servers.return_value = []

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def prime(self, servers):
        """List these servers now, as the previous poll also saw them."""
        self.list_servers.return_value = servers
        billing_models.TenantServerSnapshot.objects.update_or_create(
            tenant=self.tenant,
            defaults={
                "taken_at": timezone.now(),
                "servers": {s["id"]: s["updated"] for s in servers},
            },
        )

    def set_options(self, **options):
        offering = self.resource.offering
        offering.plugin_options = {**self.plugin_options, **options}
        offering.save()

    def set_quota_usage(self, cores, ram, storage=0):
        self.tenant.set_quota_usage(self.tenant.Quotas.vcpu.name, cores)
        self.tenant.set_quota_usage(self.tenant.Quotas.ram.name, ram)
        self.tenant.set_quota_usage(self.tenant.Quotas.storage.name, storage)

    def usage(self, component_type):
        return (
            marketplace_models.ComponentUsage.objects.filter(
                resource=self.resource, component__type=component_type
            )
            .values_list("usage", flat=True)
            .first()
        )


@ddt
class QuotaSourceInactiveInstanceBillingTest(InactiveInstanceBillingBase):
    plugin_options = {"bill_inactive_instances": False}

    def setUp(self):
        super().setUp()
        # Nova's quota counts every server: one running, one stopped.
        self.set_quota_usage(cores=6, ram=6144, storage=10240)
        marketplace_models.ComponentUsage.objects.filter(
            resource=self.resource
        ).delete()
        marketplace_models.ComponentUsagePollRecord.objects.filter(
            resource=self.resource
        ).delete()

    def get_usage_values(self):
        return utils.get_usage_values(self.resource, self.tenant)

    @data(*INACTIVE)
    def test_inactive_server_is_not_billed(self, status):
        self.prime([server("a", "ACTIVE", 2, 2048), server("b", status, 4, 4096)])
        values = self.get_usage_values()
        self.assertEqual(values[CORES_TYPE], 2)
        self.assertEqual(values[RAM_TYPE], 2048)

    @data(*STILL_BILLED)
    def test_other_status_is_billed(self, status):
        self.prime([server("a", "ACTIVE", 2, 2048), server("b", status, 4, 4096)])
        values = self.get_usage_values()
        self.assertEqual(values[CORES_TYPE], 6)
        self.assertEqual(values[RAM_TYPE], 6144)

    def test_storage_is_billed_regardless(self):
        self.prime([server("b", "SHUTOFF", 4, 4096)])
        values = self.get_usage_values()
        self.assertEqual(values[CORES_TYPE], 0)
        self.assertEqual(values[STORAGE_TYPE], 10240)

    def test_first_poll_bills_stopped_server_and_stores_snapshot(self):
        # No previous poll saw the server, so it may have run: billed.
        self.list_servers.return_value = [
            server("a", "ACTIVE", 2, 2048),
            server("b", "SHUTOFF", 4, 4096),
        ]
        self.assertEqual(self.get_usage_values()[CORES_TYPE], 6)
        utils.import_usage(self.resource)
        snapshot = billing_models.TenantServerSnapshot.objects.get(tenant=self.tenant)
        self.assertEqual(
            snapshot.servers,
            {"a": "2026-11-27T08:00:00Z", "b": "2026-11-27T08:00:00Z"},
        )
        # The next poll sees it unchanged: left out.
        self.assertEqual(self.get_usage_values()[CORES_TYPE], 2)

    def test_server_list_failure_keeps_previous_snapshot(self):
        self.prime([server("b", "SHUTOFF", 4, 4096)])
        self.list_servers.side_effect = OpenStackBackendError("Nova is down")
        with self.assertLogs("waldur_mastermind.marketplace_openstack.utils"):
            utils.import_usage(self.resource)
        snapshot = billing_models.TenantServerSnapshot.objects.get(tenant=self.tenant)
        self.assertEqual(snapshot.servers, {"b": "2026-11-27T08:00:00Z"})

    def test_failed_recording_keeps_previous_snapshot(self):
        # Stored before the usage is recorded, the snapshot would let the next
        # poll treat a run that was never billed as idle.
        self.prime([server("b", "ACTIVE", 4, 4096)])
        self.list_servers.return_value = [
            server("b", "SHUTOFF", 4, 4096, updated="2026-11-27T10:50:00Z")
        ]
        with mock.patch.object(
            utils, "import_current_usages", side_effect=RuntimeError("db")
        ):
            with self.assertRaises(RuntimeError):
                utils.import_usage(self.resource)
        snapshot = billing_models.TenantServerSnapshot.objects.get(tenant=self.tenant)
        self.assertEqual(snapshot.servers, {"b": "2026-11-27T08:00:00Z"})
        # So the next poll still bills the server for the interval it ran in.
        self.assertEqual(self.get_usage_values()[CORES_TYPE], 4)

    def test_import_usage_accumulates_and_shows_filtered_values(self):
        self.prime([server("a", "ACTIVE", 2, 2048), server("b", "SHUTOFF", 4, 4096)])
        utils.import_usage(self.resource)
        self.assertEqual(self.usage(CORES_TYPE), 2)
        self.assertEqual(self.usage(RAM_TYPE), 2048)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages[CORES_TYPE], 2)
        self.assertEqual(self.resource.current_usages[RAM_TYPE], 2048)

    def test_server_list_failure_bills_every_server(self):
        self.list_servers.side_effect = OpenStackBackendError("Nova is down")
        with self.assertLogs(
            "waldur_mastermind.marketplace_openstack.utils", level="WARNING"
        ) as logs:
            values = self.get_usage_values()
        self.assertEqual(values[CORES_TYPE], 6)
        self.assertEqual(values[RAM_TYPE], 6144)
        self.assertTrue(any("billing all of them" in m for m in logs.output))

    # A null can only be written outside the API, and must not switch billing
    # of stopped servers off.
    @data({}, {"bill_inactive_instances": True}, {"bill_inactive_instances": None})
    def test_option_on_or_unset_keeps_quota_values(self, options):
        self.plugin_options = options
        self.set_options()
        self.list_servers.reset_mock()
        values = self.get_usage_values()
        self.list_servers.assert_not_called()
        self.assertEqual(values[CORES_TYPE], 6)
        self.assertEqual(values[RAM_TYPE], 6144)


class PlacementSourceInactiveInstanceBillingTest(InactiveInstanceBillingBase):
    plugin_options = {"billing_source": "placement", "bill_inactive_instances": False}

    def setUp(self):
        super().setUp()
        self.running = self.fixture.instance
        self.running.backend_id = "running"
        self.running.save()
        self.stopped = openstack_factories.InstanceFactory(
            tenant=self.tenant, project=self.fixture.project, backend_id="stopped"
        )
        self.prime(
            [
                server("running", "ACTIVE", 2, 2048),
                server("stopped", "SHUTOFF", 4, 4096),
            ]
        )
        allocations = {
            "running": {"rp": {"resources": {"VCPU": 2, "MEMORY_MB": 2048}}},
            "stopped": {"rp": {"resources": {"VCPU": 4, "MEMORY_MB": 4096, "VGPU": 1}}},
        }
        self.placement = mock.patch(
            "waldur_mastermind.marketplace_openstack.utils.get_placement_client"
        ).start()
        self.placement.return_value.get_allocations.side_effect = (
            lambda consumer: allocations.get(consumer, {})
        )

    def test_stopped_instance_allocations_are_left_out(self):
        values = utils.get_usage_values(self.resource, self.tenant)
        self.assertEqual(values[CORES_TYPE], 2)
        self.assertEqual(values[RAM_TYPE], 2048)
        # The stopped server's vGPU is left out along with its cores and RAM.
        self.assertNotIn("vgpu", values)
        consumers = [
            call.args[0]
            for call in self.placement.return_value.get_allocations.call_args_list
        ]
        self.assertNotIn("stopped", consumers)

    def test_server_list_failure_bills_every_allocation(self):
        self.list_servers.side_effect = OpenStackBackendError("Nova is down")
        with self.assertLogs(
            "waldur_mastermind.marketplace_openstack.utils", level="WARNING"
        ):
            values = utils.get_usage_values(self.resource, self.tenant)
        self.assertEqual(values[CORES_TYPE], 6)
        self.assertEqual(values[RAM_TYPE], 6144)
        self.assertEqual(values["vgpu"], 1)

    def test_placement_failure_falls_back_to_filtered_quota(self):
        # Nova's quota counts both servers; the server list is still known, so
        # the stopped one stays out of the fallback too.
        self.set_quota_usage(cores=6, ram=6144)
        self.placement.return_value.get_allocations.side_effect = OpenStackBackendError(
            "Placement is down"
        )
        with self.assertLogs(
            "waldur_mastermind.marketplace_openstack.utils", level="WARNING"
        ):
            values = utils.get_usage_values(self.resource, self.tenant)
        self.assertEqual(values[CORES_TYPE], 2)
        self.assertEqual(values[RAM_TYPE], 2048)

    def test_stopped_server_changed_since_previous_poll_keeps_its_vgpu(self):
        # Stopped now, but Nova changed it since the previous poll: it may
        # have run, so its allocations, vGPU included, are billed.
        self.list_servers.return_value = [
            server("running", "ACTIVE", 2, 2048),
            server("stopped", "SHUTOFF", 4, 4096, updated="2026-11-27T10:00:00Z"),
        ]
        values = utils.get_usage_values(self.resource, self.tenant)
        self.assertEqual(values["vgpu"], 1)
        self.assertEqual(values[CORES_TYPE], 6)

    def test_option_on_bills_stopped_allocations(self):
        self.set_options(bill_inactive_instances=True)
        self.list_servers.reset_mock()
        values = utils.get_usage_values(self.resource, self.tenant)
        self.list_servers.assert_not_called()
        self.assertEqual(values[CORES_TYPE], 6)
        self.assertEqual(values[RAM_TYPE], 6144)


def at(hhmm):
    hours, minutes = (int(x) for x in hhmm.split(":"))
    return datetime.datetime(2026, 11, 27, hours, minutes, tzinfo=datetime.UTC)


class HourlyAccumulationOfStoppedServersTest(InactiveInstanceBillingBase):
    """Polls in sequence under an hourly-accumulated cores component.

    A poll bills the interval since the previous one at what it reads, so a
    server read as stopped at one instant must not take the interval before it
    off the bill: the tenant can stop servers and trigger a quota pull at will.
    """

    plugin_options = {"bill_inactive_instances": False}

    def setUp(self):
        super().setUp()
        cores = marketplace_models.OfferingComponent.objects.get(
            offering=self.resource.offering, type=CORES_TYPE
        )
        cores.billing_type = BillingTypes.USAGE
        cores.save(update_fields=["billing_type"])
        create_resource_plan_period(self.resource)
        marketplace_models.ComponentUsage.objects.filter(
            resource=self.resource
        ).delete()
        marketplace_models.ComponentUsagePollRecord.objects.filter(
            resource=self.resource
        ).delete()

    def poll(self, when, servers):
        self.list_servers.return_value = servers
        with freeze_time(when):
            utils.import_usage(self.resource)
        return float(self.usage(CORES_TYPE))

    def test_server_is_billed_for_the_interval_it_stops_in(self):
        running = server("a", "ACTIVE", 2, 2048)
        # 10:00, both running; the first poll bills one hour.
        self.assertEqual(
            self.poll(at("10:00"), [running, server("b", "ACTIVE", 4, 4096)]), 6
        )
        # Stopped at 10:50: it ran for most of the hour, so the hour is billed
        # as running.
        stopped = server("b", "SHUTOFF", 4, 4096, updated="2026-11-27T10:50:00Z")
        self.assertEqual(self.poll(at("11:00"), [running, stopped]), 12)
        # Stopped for the whole hour since: only the running server is billed.
        self.assertEqual(self.poll(at("12:00"), [running, stopped]), 14)

    def test_stopping_servers_around_pulls_does_not_avoid_billing(self):
        running = server("a", "ACTIVE", 2, 2048)
        self.poll(at("10:00"), [running, server("b", "ACTIVE", 4, 4096)])
        # The tenant suspends just before each pull and resumes after it. Nova
        # moves `updated` on every suspend, so each interval is billed in full.
        total = 6
        for pull in ("10:20", "10:40", "11:00"):
            suspended = server("b", "SUSPENDED", 4, 4096, updated=f"suspend {pull}")
            total += 6 * (20 / 60)
            self.assertAlmostEqual(
                self.poll(at(pull), [running, suspended]), total, places=1
            )

    def test_nova_clock_behind_waldur_does_not_hide_a_change(self):
        # On a lab cloud Nova's clock ran six minutes behind Waldur's, so a
        # stop right after a poll is stamped before it. Only Nova's own values
        # are compared, so the change is still seen and the interval billed.
        running = server("a", "ACTIVE", 2, 2048)
        self.poll(at("10:00"), [running, server("b", "ACTIVE", 4, 4096)])
        stopped = server("b", "SHUTOFF", 4, 4096, updated="2026-11-27T09:55:00Z")
        self.assertEqual(self.poll(at("11:00"), [running, stopped]), 12)


class PlacementClassOfIdleServerTest(InactiveInstanceBillingBase):
    """A class only idle servers hold keeps being polled, at zero."""

    plugin_options = {"billing_source": "placement", "bill_inactive_instances": False}

    def setUp(self):
        super().setUp()
        offering = self.resource.offering
        marketplace_models.OfferingComponent.objects.filter(offering=offering).update(
            billing_type=BillingTypes.USAGE
        )
        marketplace_factories.OfferingComponentFactory(
            offering=offering,
            type="vgpu",
            name="Virtual GPU",
            billing_type=BillingTypes.USAGE,
        )
        create_resource_plan_period(self.resource)
        marketplace_models.ComponentUsage.objects.filter(
            resource=self.resource
        ).delete()
        marketplace_models.ComponentUsagePollRecord.objects.filter(
            resource=self.resource
        ).delete()
        self.fixture.instance.backend_id = "g"
        self.fixture.instance.save()
        placement = mock.patch(
            "waldur_mastermind.marketplace_openstack.utils.get_placement_client"
        ).start()
        placement.return_value.get_allocations.return_value = {
            "rp": {"resources": {"VCPU": 1, "MEMORY_MB": 512, "VGPU": 1}}
        }

    def poll(self, when, servers):
        self.list_servers.return_value = servers
        with freeze_time(when):
            utils.import_usage(self.resource)
        return float(self.usage("vgpu"))

    def test_restart_after_long_stop_does_not_bill_the_gap(self):
        self.assertEqual(self.poll(at("10:00"), [server("g", "ACTIVE", 1, 512)]), 1)
        stopped = server("g", "SHUTOFF", 1, 512, updated="2026-11-27T10:01:00Z")
        # The hour it stopped in is billed; the idle hours after it are not.
        self.assertEqual(self.poll(at("11:00"), [stopped]), 2)
        for hour in ("12:00", "13:00", "14:00", "15:00", "16:00"):
            self.assertEqual(self.poll(at(hour), [stopped]), 2)
        # Started at 16:04: the five minutes since the last poll are billed,
        # not the six hours since the last nonzero reading.
        started = server("g", "ACTIVE", 1, 512, updated="2026-11-27T16:04:00Z")
        self.assertAlmostEqual(self.poll(at("16:05"), [started]), 2 + 5 / 60, places=1)
