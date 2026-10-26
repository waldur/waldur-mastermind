import datetime
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from freezegun import freeze_time
from keystoneauth1 import exceptions as keystone_exceptions
from novaclient import exceptions as nova_exceptions

from waldur_core.core.enums import CoreStates
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_TENANT_OFFERING,
    OPENSTACK_VOLUME_OFFERING,
    OfferingStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_metrics import enums as metrics_enums
from waldur_mastermind.marketplace_metrics import ingest
from waldur_mastermind.marketplace_metrics import models as metrics_models
from waldur_mastermind.marketplace_openstack import metrics, models, tasks
from waldur_mastermind.marketplace_openstack.tests.utils import BaseOpenStackTest
from waldur_openstack import models as openstack_models
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.exceptions import OpenStackBackendError
from waldur_openstack.tests import factories as openstack_factories
from waldur_openstack.tests.fixtures import OpenStackFixture

DIAGNOSTICS = "waldur_openstack.backend.OpenStackBackend.get_instance_diagnostics"
DEVICES = "waldur_openstack.backend.OpenStackBackend.get_instance_volume_devices"
T0 = datetime.datetime(2026, 10, 26, 8, 0, tzinfo=datetime.UTC)


def diagnostics(
    cpu_seconds,
    rx=0,
    tx=0,
    read=0,
    write=0,
    reads=0,
    writes=0,
    memory=None,
    disks=None,
):
    """Legacy (libvirt) diagnostics: one vCPU, one port and the given disks."""
    reading = {
        "cpu0_time": cpu_seconds * 10**9,
        "tap1_rx": rx,
        "tap1_rx_packets": 1,
        "tap1_rx_errors": 0,
        "tap1_rx_drop": 0,
        "tap1_tx": tx,
        "tap1_tx_packets": 1,
        "tap1_tx_errors": 0,
        "tap1_tx_drop": 0,
    }
    disks = disks or {"vda": (read, write, reads, writes)}
    for device, (
        device_read,
        device_write,
        device_reads,
        device_writes,
    ) in disks.items():
        reading.update(
            {
                f"{device}_read": device_read,
                f"{device}_write": device_write,
                f"{device}_read_req": device_reads,
                f"{device}_write_req": device_writes,
                f"{device}_errors": -1,
            }
        )
    if memory:
        available, usable = memory
        reading.update(
            {
                "memory": available,
                "memory-actual": available,
                "memory-available": available,
                "memory-usable": usable,
            }
        )
    return reading


class BaseInstanceMetricsTest(BaseOpenStackTest):
    def setUp(self):
        super().setUp()
        # Poll locks and Nova backoff live in the cache.
        cache.clear()
        self.fixture = OpenStackFixture()
        self.instance = self.fixture.instance
        self.instance.runtime_state = openstack_models.Instance.RuntimeStates.ACTIVE
        # Started just before its first poll: that poll counts all it did.
        self.instance.start_time = T0 - datetime.timedelta(minutes=5)
        self.instance.save()
        # The provider turns metrics on for its tenants and picks three; the
        # tenant's instance offering adopts them when it is created.
        self.provider = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING,
            plugin_options={
                metrics.INSTANCE_METRICS_ENABLED_OPTION: True,
                metrics.POLL_INTERVAL_OPTION: 15,
                metrics.INSTANCE_METRICS_OPTION: [
                    metrics.CPU_TIME,
                    metrics.CPU_UTILIZATION,
                    metrics.NETWORK_IO,
                ],
            },
        )
        self.offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING,
            scope=self.fixture.tenant,
            parent=self.provider,
        )
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            scope=self.instance,
            state=ResourceStates.OK,
        )

    def configure(self, **options):
        """Change the provider's settings, as its update_integration call would."""
        self.provider.plugin_options.update(options)
        self.provider.save()
        tasks.sync_instance_metric_offerings(self.provider.id)

    def states(self, offering=None):
        return dict(
            metrics_models.OfferingMetric.objects.filter(
                offering=offering or self.offering
            ).values_list("definition__key", "state")
        )

    def instance_resource(self, offering, runtime_state):
        instance = openstack_factories.InstanceFactory(
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            state=CoreStates.OK,
            runtime_state=runtime_state,
        )
        return marketplace_factories.ResourceFactory(
            offering=offering,
            project=self.fixture.project,
            scope=instance,
            state=ResourceStates.OK,
        )

    def poll(self, at, reading, devices=None):
        with (
            freeze_time(at),
            mock.patch(DIAGNOSTICS, return_value=reading),
            mock.patch(DEVICES, return_value=devices or {}) as get_devices,
        ):
            metrics.collect(self.resource)
        return get_devices

    def values(self, key, **attributes):
        return list(
            metrics_models.MetricPoint.objects.filter(
                series__resource=self.resource,
                series__offering_metric__definition__key=key,
                series__attributes=attributes,
            )
            .order_by("timestamp")
            .values_list("value", flat=True)
        )

    def dispatched(self, minutes):
        """Resources sent for polling over this many one-minute beat ticks."""
        with mock.patch(
            "waldur_mastermind.marketplace_openstack.tasks.collect_resource_metrics"
        ) as task:
            for minute in range(minutes):
                with freeze_time(T0 + datetime.timedelta(minutes=minute)):
                    tasks.collect_instance_metrics()
        return sorted(
            call.kwargs["args"][0] for call in task.apply_async.call_args_list
        )


class InstanceMetricsTest(BaseInstanceMetricsTest):
    def test_counters_are_recorded_as_increments_between_polls(self):
        self.poll(T0, diagnostics(100, rx=1000, tx=2000))
        self.poll(
            T0 + datetime.timedelta(minutes=5), diagnostics(130, rx=1500, tx=2600)
        )

        self.assertEqual(self.values(metrics.CPU_TIME), [100, 30])
        self.assertEqual(
            self.values(metrics.NETWORK_IO, direction="receive"), [1000, 500]
        )
        self.assertEqual(
            self.values(metrics.NETWORK_IO, direction="transmit"), [2000, 600]
        )

    def test_utilization_is_cpu_time_over_the_interval(self):
        self.poll(T0, diagnostics(100))
        self.assertEqual(self.values(metrics.CPU_UTILIZATION), [])

        self.poll(T0 + datetime.timedelta(minutes=5), diagnostics(130))

        # 30 s of CPU over 300 s on one vCPU.
        self.assertEqual(self.values(metrics.CPU_UTILIZATION), [Decimal(10)])

    def test_first_reading_of_a_long_running_instance_is_a_baseline(self):
        self.instance.start_time = T0 - datetime.timedelta(days=30)
        self.instance.save()

        self.poll(T0, diagnostics(100000, rx=10**9))
        self.assertEqual(self.values(metrics.CPU_TIME), [])

        self.poll(
            T0 + datetime.timedelta(minutes=5), diagnostics(100030, rx=10**9 + 500)
        )
        self.assertEqual(self.values(metrics.CPU_TIME), [30])
        self.assertEqual(self.values(metrics.NETWORK_IO, direction="receive"), [500])

    def test_a_first_reading_after_an_interval_is_a_baseline(self):
        # Running for more than an interval: its totals may have been counted
        # under a sample since lost, or belong to before metrics were on.
        self.instance.start_time = T0 - datetime.timedelta(hours=2)
        self.instance.save()

        self.poll(T0, diagnostics(100, rx=1000))
        self.poll(T0 + datetime.timedelta(minutes=5), diagnostics(130, rx=1500))

        self.assertEqual(self.values(metrics.CPU_TIME), [30])
        self.assertEqual(self.values(metrics.NETWORK_IO, direction="receive"), [500])

    def test_a_lower_cpu_time_is_a_restart_and_counts_from_zero(self):
        self.poll(T0, diagnostics(100, rx=1000))
        restart = T0 + datetime.timedelta(minutes=5)
        self.poll(restart, diagnostics(5, rx=50))

        self.assertEqual(self.values(metrics.CPU_TIME), [100, 5])
        self.assertEqual(
            self.values(metrics.NETWORK_IO, direction="receive"), [1000, 50]
        )
        # No utilization across a restart: the interval is not known.
        self.assertEqual(self.values(metrics.CPU_UTILIZATION), [])

    def test_a_restart_the_cpu_time_hides_shows_on_every_device(self):
        self.show(metrics.CPU_TIME, metrics.NETWORK_IO, metrics.DISK_IO)
        self.poll(T0, diagnostics(30, rx=10**6, read=10**6))
        # Restarted and busy since: more CPU time than at the last poll.
        self.poll(T0 + datetime.timedelta(hours=6), diagnostics(50, rx=1000, read=2000))

        self.assertEqual(self.values(metrics.CPU_TIME), [30, 50])
        self.assertEqual(
            self.values(metrics.NETWORK_IO, direction="receive"), [10**6, 1000]
        )
        self.assertEqual(self.values(metrics.DISK_IO, direction="read"), [10**6, 2000])

    def test_one_device_starting_over_is_not_a_restart(self):
        self.show(metrics.CPU_TIME, metrics.CPU_UTILIZATION, metrics.DISK_IO)
        self.poll(T0, diagnostics(100, rx=1000, read=5000))
        # The port was attached anew; the disk kept counting.
        self.poll(
            T0 + datetime.timedelta(minutes=5),
            diagnostics(130, rx=50, read=5100),
        )

        self.assertEqual(self.values(metrics.CPU_TIME), [100, 30])
        self.assertEqual(self.values(metrics.DISK_IO, direction="read"), [5000, 100])
        self.assertEqual(self.values(metrics.CPU_UTILIZATION), [Decimal(10)])

    def test_a_disk_going_away_does_not_count_the_others_again(self):
        self.show(metrics.DISK_IO)
        disks = {"vda": (100, 0, 1, 0), "vdb": (1000, 0, 10, 0)}
        self.poll(T0, diagnostics(100, disks=disks))
        self.poll(
            T0 + datetime.timedelta(minutes=5),
            diagnostics(130, disks={"vda": (150, 0, 2, 0)}),
        )

        self.assertEqual(self.values(metrics.DISK_IO, direction="read"), [1100, 50])

    def test_a_disk_missing_from_one_reading_is_not_counted_twice(self):
        self.show(metrics.DISK_IO)
        self.poll(
            T0, diagnostics(100, disks={"vda": (100, 0, 1, 0), "vdb": (1000, 0, 1, 0)})
        )
        self.poll(
            T0 + datetime.timedelta(minutes=5),
            diagnostics(130, disks={"vda": (150, 0, 1, 0)}),
        )
        self.poll(
            T0 + datetime.timedelta(minutes=10),
            diagnostics(160, disks={"vda": (160, 0, 1, 0), "vdb": (1100, 0, 1, 0)}),
        )

        self.assertEqual(
            self.values(metrics.DISK_IO, direction="read"), [1100, 50, 110]
        )

    def test_only_adopted_metrics_are_reported(self):
        self.poll(T0, diagnostics(100, read=4096, writes=3))

        self.assertFalse(
            metrics_models.MetricSeries.objects.filter(
                offering_metric__definition__key__in=[
                    metrics.DISK_IO,
                    metrics.DISK_OPERATIONS,
                ]
            ).exists()
        )

    def test_an_offering_without_vm_metrics_is_not_polled(self):
        metrics_models.OfferingMetric.objects.filter(offering=self.offering).delete()

        with mock.patch(DIAGNOSTICS) as get_diagnostics:
            self.assertIsNone(metrics.collect(self.resource))

        get_diagnostics.assert_not_called()

    def test_only_running_instances_of_reporting_offerings_are_polled(self):
        self.instance_resource(
            self.offering, openstack_models.Instance.RuntimeStates.SHUTOFF
        )
        self.instance_resource(
            marketplace_factories.OfferingFactory(type=OPENSTACK_INSTANCE_OFFERING),
            openstack_models.Instance.RuntimeStates.ACTIVE,
        )

        self.assertEqual(self.dispatched(15), [self.resource.id])

    def test_only_offerings_in_use_are_polled(self):
        for state in (
            OfferingStates.DRAFT,
            OfferingStates.ARCHIVED,
            OfferingStates.UNAVAILABLE,
        ):
            for offering in (self.offering, self.provider):
                offering.state = state
                offering.save()
                self.assertEqual(self.dispatched(15), [], (offering, state))
                offering.state = OfferingStates.ACTIVE
                offering.save()
                cache.clear()

        self.assertEqual(self.dispatched(15), [self.resource.id])

    def test_paused_offerings_are_still_polled(self):
        # Paused takes no new orders; the instances it has keep running.
        for offering in (self.offering, self.provider):
            offering.state = OfferingStates.PAUSED
            offering.save()

        self.assertEqual(self.dispatched(15), [self.resource.id])

    def test_instances_with_a_broken_record_are_not_polled(self):
        settings = self.instance.tenant.service_settings
        breakages = (
            (self.instance, "backend_id", ""),
            (self.instance, "state", CoreStates.ERRED),
            (settings, "is_active", False),
            (settings, "state", CoreStates.ERRED),
        )
        for record, field, broken in breakages:
            sound = getattr(record, field)
            setattr(record, field, broken)
            record.save()
            self.assertEqual(self.dispatched(15), [], field)
            setattr(record, field, sound)
            record.save()
            cache.clear()

        self.assertEqual(self.dispatched(15), [self.resource.id])

    def test_a_poll_queued_before_the_offering_was_archived_reads_nothing(self):
        self.offering.state = OfferingStates.ARCHIVED
        self.offering.save()

        with mock.patch(DIAGNOSTICS) as get_diagnostics:
            tasks.collect_resource_metrics(self.resource.id)

        get_diagnostics.assert_not_called()

    def test_values_that_are_not_counters_are_left_out(self):
        reading = diagnostics(100, rx=1000)
        reading.update({"tap1_tx": "n/a", "cpu1_time": None, "vda_read": True})

        self.poll(T0, reading)

        self.assertEqual(self.values(metrics.CPU_TIME), [100])
        self.assertEqual(self.values(metrics.NETWORK_IO, direction="receive"), [1000])
        self.assertEqual(self.values(metrics.NETWORK_IO, direction="transmit"), [])

    def test_a_damaged_sample_starts_the_counting_over(self):
        self.poll(T0, diagnostics(100, rx=1000))
        models.InstanceMetricSample.objects.filter(instance=self.instance).update(
            counters={"nics": "damaged"}
        )

        self.poll(T0 + datetime.timedelta(minutes=5), diagnostics(130, rx=1500))
        self.poll(T0 + datetime.timedelta(minutes=10), diagnostics(160, rx=1600))

        # The damaged reading counts nothing; the next counts from it.
        self.assertEqual(
            self.values(metrics.NETWORK_IO, direction="receive"), [1000, 100]
        )

    def test_each_instance_is_polled_once_per_interval(self):
        other = self.instance_resource(
            self.offering, openstack_models.Instance.RuntimeStates.ACTIVE
        )

        self.assertEqual(
            self.dispatched(30),
            sorted([self.resource.id, other.id] * 2),
        )

    def test_the_provider_sets_the_interval_on_its_tenant_offering(self):
        self.configure(**{metrics.POLL_INTERVAL_OPTION: 60})

        self.assertEqual(metrics.poll_interval(self.offering), 60)
        self.assertEqual(self.dispatched(60), [self.resource.id])

    def test_the_interval_stays_within_bounds(self):
        for configured, expected in ((None, 360), (1, 5), (10**6, 1440)):
            self.provider.plugin_options[metrics.POLL_INTERVAL_OPTION] = configured
            self.assertEqual(metrics.poll_interval(self.offering), expected)

    def test_a_tenant_adopts_the_metrics_its_provider_shows(self):
        active = metrics_enums.OfferingMetricStates.ACTIVE
        self.assertEqual(
            self.states(),
            {
                metrics.CPU_TIME: active,
                metrics.CPU_UTILIZATION: active,
                metrics.NETWORK_IO: active,
            },
        )
        definition = metrics_models.MetricDefinition.objects.get(key=metrics.NETWORK_IO)
        self.assertEqual(definition.unit, "By")
        self.assertEqual(definition.retention_policy.raw_days, 14)

    def test_a_metric_the_provider_drops_is_archived_and_comes_back(self):
        archived = metrics_enums.OfferingMetricStates.ARCHIVED
        active = metrics_enums.OfferingMetricStates.ACTIVE

        self.configure(**{metrics.INSTANCE_METRICS_OPTION: [metrics.CPU_TIME]})
        self.assertEqual(self.states()[metrics.NETWORK_IO], archived)
        self.assertEqual(self.states()[metrics.CPU_TIME], active)

        # Empty picks all of them, the two never adopted included.
        self.configure(**{metrics.INSTANCE_METRICS_OPTION: []})
        self.assertEqual(set(self.states().values()), {active})
        self.assertEqual(set(self.states()), set(metrics.INSTANCE_METRIC_KEYS))

    def test_turning_metrics_off_hides_them_and_stops_polling(self):
        self.configure(**{metrics.INSTANCE_METRICS_ENABLED_OPTION: False})

        self.assertEqual(
            set(self.states().values()),
            {metrics_enums.OfferingMetricStates.ARCHIVED},
        )
        self.assertEqual(self.dispatched(15), [])

    def test_a_new_tenant_gets_its_providers_metrics(self):
        other = marketplace_factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING, parent=self.provider
        )

        self.assertEqual(set(self.states(other)), set(self.states()))

    @mock.patch(
        "waldur_mastermind.marketplace_openstack.tasks.sync_instance_metric_offerings"
    )
    def test_changing_the_settings_resyncs_the_tenants(self, sync):
        with self.captureOnCommitCallbacks(execute=True):
            self.provider.plugin_options[metrics.INSTANCE_METRICS_ENABLED_OPTION] = (
                False
            )
            self.provider.save()

        sync.delay.assert_called_once_with(self.provider.id)

    @mock.patch(
        "waldur_mastermind.marketplace_openstack.tasks.sync_instance_metric_offerings"
    )
    def test_other_offering_changes_do_not_resync(self, sync):
        with self.captureOnCommitCallbacks(execute=True):
            self.provider.name = "Renamed"
            self.provider.save()

        sync.delay.assert_not_called()

    def test_polls_run_on_the_background_queue(self):
        self.assertTrue(tasks.collect_resource_metrics.is_background)

    def test_a_nova_error_does_not_fail_the_task(self):
        with mock.patch(DIAGNOSTICS, side_effect=OpenStackBackendError("409")):
            tasks.collect_resource_metrics(self.resource.id)

        self.assertFalse(
            models.InstanceMetricSample.objects.filter(instance=self.instance).exists()
        )

    def test_the_sample_follows_the_last_poll(self):
        later = timezone.now()
        self.poll(later, diagnostics(42))

        sample = models.InstanceMetricSample.objects.get(instance=self.instance)
        self.assertEqual(sample.cpu_time, 42)
        self.assertEqual(sample.counters["nics"]["tap1"], {"receive": 0, "transmit": 0})

    def show(self, *keys):
        self.configure(**{metrics.INSTANCE_METRICS_OPTION: list(keys)})

    def volume(self, backend_id):
        """A volume in the tenant, as its tenant's volume offering provides it."""
        if not hasattr(self, "volume_offering"):
            self.volume_offering = marketplace_factories.OfferingFactory(
                type=OPENSTACK_VOLUME_OFFERING,
                scope=self.fixture.tenant,
                parent=self.provider,
            )
        volume = openstack_factories.VolumeFactory(
            tenant=self.fixture.tenant, backend_id=backend_id
        )
        return marketplace_factories.ResourceFactory(
            offering=self.volume_offering,
            project=self.fixture.project,
            scope=volume,
            state=ResourceStates.OK,
        )

    def resource_values(self, resource, key, **attributes):
        return list(
            metrics_models.MetricPoint.objects.filter(
                series__resource=resource,
                series__offering_metric__definition__key=key,
                series__attributes=attributes,
            )
            .order_by("timestamp")
            .values_list("value", flat=True)
        )

    def test_memory_in_use_leaves_reclaimable_cache_out(self):
        self.show(metrics.MEMORY_UTILIZATION)

        self.poll(T0, diagnostics(100, memory=(2_000_000, 1_500_000)))

        self.assertEqual(self.values(metrics.MEMORY_UTILIZATION), [Decimal(25)])

    def test_no_memory_figure_without_the_guests_balloon_statistics(self):
        self.show(metrics.MEMORY_UTILIZATION)

        self.poll(T0, diagnostics(100))

        self.assertEqual(self.values(metrics.MEMORY_UTILIZATION), [])

    def test_a_counter_the_driver_does_not_keep_is_left_out(self):
        self.show(metrics.DISK_IO)

        self.poll(T0, diagnostics(100, disks={"vda": (-1, 300, 1, 2)}))

        self.assertEqual(self.values(metrics.DISK_IO, direction="read"), [])
        self.assertEqual(self.values(metrics.DISK_IO, direction="write"), [300])

    def test_each_attached_volume_gets_its_own_figures(self):
        self.show(metrics.DISK_IO, metrics.VOLUME_IO, metrics.VOLUME_OPERATIONS)
        data = self.volume("vol-1")

        self.poll(
            T0,
            diagnostics(
                100, disks={"vda": (100, 200, 1, 2), "vdb": (1000, 2000, 10, 20)}
            ),
            devices={"vdb": "vol-1"},
        )

        self.assertEqual(
            self.resource_values(data, metrics.VOLUME_IO, direction="read"), [1000]
        )
        self.assertEqual(
            self.resource_values(data, metrics.VOLUME_IO, direction="write"), [2000]
        )
        self.assertEqual(
            self.resource_values(data, metrics.VOLUME_OPERATIONS, direction="write"),
            [20],
        )
        # The instance still counts all its disks, the root disk included.
        self.assertEqual(self.values(metrics.DISK_IO, direction="read"), [1100])

    def test_a_volume_attached_again_counts_from_zero(self):
        self.show(metrics.VOLUME_IO)
        data = self.volume("vol-1")

        self.poll(
            T0, diagnostics(100, disks={"vdb": (1000, 0, 1, 0)}), {"vdb": "vol-1"}
        )
        self.poll(
            T0 + datetime.timedelta(minutes=15),
            diagnostics(130, disks={"vdb": (50, 0, 1, 0)}),
            {"vdb": "vol-1"},
        )

        self.assertEqual(
            self.resource_values(data, metrics.VOLUME_IO, direction="read"),
            [1000, 50],
        )

    def test_a_volume_on_a_new_device_was_attached_anew(self):
        self.show(metrics.VOLUME_IO)
        data = self.volume("vol-1")

        self.poll(
            T0, diagnostics(100, disks={"vdb": (1000, 0, 1, 0)}), {"vdb": "vol-1"}
        )
        # Nova keeps a device name for the life of an attachment.
        self.poll(
            T0 + datetime.timedelta(minutes=15),
            diagnostics(130, disks={"vdc": (1500, 0, 2, 0)}),
            {"vdc": "vol-1"},
        )

        self.assertEqual(
            self.resource_values(data, metrics.VOLUME_IO, direction="read"),
            [1000, 1500],
        )

    def test_another_volume_on_the_device_counts_from_zero(self):
        self.show(metrics.VOLUME_IO)
        first, second = self.volume("vol-1"), self.volume("vol-2")

        self.poll(
            T0, diagnostics(100, disks={"vdb": (1000, 0, 1, 0)}), {"vdb": "vol-1"}
        )
        self.poll(
            T0 + datetime.timedelta(minutes=15),
            diagnostics(130, disks={"vdb": (1200, 0, 2, 0)}),
            {"vdb": "vol-2"},
        )

        self.assertEqual(
            self.resource_values(first, metrics.VOLUME_IO, direction="read"), [1000]
        )
        self.assertEqual(
            self.resource_values(second, metrics.VOLUME_IO, direction="read"), [1200]
        )

    def test_volumes_are_not_looked_up_unless_their_metrics_are_shown(self):
        get_devices = self.poll(T0, diagnostics(100))

        get_devices.assert_not_called()

    def test_an_instance_is_polled_for_its_volumes_alone(self):
        self.show(metrics.VOLUME_IO)
        self.volume("vol-1")

        self.assertEqual(self.dispatched(15), [self.resource.id])

    def test_a_reading_older_than_the_last_recorded_is_dropped(self):
        self.poll(T0, diagnostics(100, tx=1000))
        # Read second, recorded first.
        self.poll(
            T0 + datetime.timedelta(minutes=5, seconds=1), diagnostics(130, tx=2000)
        )
        # Read first, by a poll that reaches the database last: its lower
        # totals would otherwise look like a restart and count again.
        self.poll(T0 + datetime.timedelta(minutes=5), diagnostics(129, tx=1900))

        self.assertEqual(
            self.values(metrics.NETWORK_IO, direction="transmit"), [1000, 1000]
        )
        sample = models.InstanceMetricSample.objects.get(instance=self.instance)
        self.assertEqual(sample.cpu_time, 130)

    def test_a_second_poll_skips_while_one_is_running(self):
        with metrics.poll_lock(self.instance):
            with mock.patch(DIAGNOSTICS) as get_diagnostics:
                metrics.collect(self.resource)

        get_diagnostics.assert_not_called()

    def test_the_lock_is_released_after_a_poll_and_after_a_failure(self):
        self.poll(T0, diagnostics(100))
        with mock.patch(DIAGNOSTICS, side_effect=OpenStackBackendError("timed out")):
            with self.assertRaises(OpenStackBackendError):
                metrics.collect(self.resource)

        with metrics.poll_lock(self.instance) as acquired:
            self.assertTrue(acquired)

    def test_queued_polls_expire_within_their_interval(self):
        self.assertEqual(metrics.poll_expiry(5), 300)
        self.assertEqual(metrics.poll_expiry(360), 15 * 60)
        with mock.patch(
            "waldur_mastermind.marketplace_openstack.tasks.collect_resource_metrics"
        ) as task:
            for minute in range(15):
                with freeze_time(T0 + datetime.timedelta(minutes=minute)):
                    tasks.collect_instance_metrics()

        self.assertEqual(task.apply_async.call_args.kwargs["expires"], 15 * 60)

    def test_a_missed_tick_is_dropped_rather_than_run_late(self):
        schedule = settings.CELERY_BEAT_SCHEDULE[
            "marketplace-openstack.collect-instance-metrics"
        ]

        self.assertLess(schedule["options"]["expires"], 60)

    def fail_polls(self, count, error):
        for _ in range(count):
            with mock.patch(DIAGNOSTICS, side_effect=error):
                tasks.collect_resource_metrics(self.resource.id)

    def test_polls_pause_when_nova_keeps_failing(self):
        self.fail_polls(metrics.FAILURES_BEFORE_BACKOFF, OpenStackBackendError("504"))

        self.assertTrue(metrics.is_backing_off(self.offering))
        self.assertEqual(self.dispatched(15), [])

    def test_an_answer_about_one_server_is_not_a_nova_failure(self):
        # Not running, deleted outside Waldur, no diagnostics on its hypervisor.
        for cause in (
            nova_exceptions.Conflict(409),
            nova_exceptions.NotFound(404),
            nova_exceptions.HTTPNotImplemented(501),
        ):
            error = OpenStackBackendError(str(cause))
            error.__cause__ = cause
            self.fail_polls(metrics.FAILURES_BEFORE_BACKOFF, error)

        self.assertFalse(metrics.is_backing_off(self.offering))

    def test_an_instance_gone_before_its_poll_is_skipped(self):
        self.resource.object_id = 10**6
        self.resource.save()

        with mock.patch(DIAGNOSTICS) as get_diagnostics:
            tasks.collect_resource_metrics(self.resource.id)

        get_diagnostics.assert_not_called()

    def run_dispatcher(self, *minutes):
        """Resources sent for polling by runs at these minutes after T0."""
        with mock.patch(
            "waldur_mastermind.marketplace_openstack.tasks.collect_resource_metrics"
        ) as task:
            for minute in minutes:
                with freeze_time(T0 + datetime.timedelta(minutes=minute)):
                    tasks.collect_instance_metrics()
        return [call.kwargs["args"][0] for call in task.apply_async.call_args_list]

    def test_a_minute_the_dispatcher_missed_is_caught_up(self):
        start = int(T0.timestamp() // 60)
        # The resource's turn comes three minutes after a run, and the runs
        # in between never happen.
        due = next(
            minute
            for minute in range(1, 16)
            if metrics.is_due(self.resource.uuid, 15, start + minute)
        )
        first_run = due - 3 if due >= 3 else due + 12

        dispatched = self.run_dispatcher(first_run, first_run + 5, first_run + 5)

        self.assertEqual(dispatched, [self.resource.id])

    def test_metrics_waldur_measures_cannot_be_reported(self):
        self.assertTrue(
            set(metrics.KEYS) <= ingest.RESERVED_KEYS,
        )

    def test_a_paused_metric_stays_paused_when_the_choice_changes(self):
        paused = metrics_enums.OfferingMetricStates.PAUSED
        metrics_models.OfferingMetric.objects.filter(
            offering=self.offering, definition__key=metrics.NETWORK_IO
        ).update(state=paused)

        self.configure(
            **{
                metrics.INSTANCE_METRICS_OPTION: [
                    metrics.CPU_TIME,
                    metrics.NETWORK_IO,
                ]
            }
        )

        self.assertEqual(self.states()[metrics.NETWORK_IO], paused)

    def test_a_good_poll_resets_the_failure_count(self):
        self.fail_polls(
            metrics.FAILURES_BEFORE_BACKOFF - 1, OpenStackBackendError("504")
        )
        self.poll(T0, diagnostics(100))
        self.fail_polls(
            metrics.FAILURES_BEFORE_BACKOFF - 1, OpenStackBackendError("504")
        )

        self.assertFalse(metrics.is_backing_off(self.offering))


class InstanceMetricsPollingApiTest(BaseInstanceMetricsTest):
    """Faster reads asked for by someone who manages the instance."""

    def setUp(self):
        super().setUp()
        ProjectRole.MANAGER.add_permission(PermissionEnum.CAN_MANAGE_OPENSTACK_INSTANCE)

    def url(self, action=None):
        url = f"/api/marketplace-openstack-instance-metrics/{self.resource.uuid.hex}/"
        return url + f"{action}/" if action else url

    def post(self, action, user, data=None):
        self.client.force_authenticate(user)
        with mock.patch(
            "waldur_mastermind.marketplace_openstack.views.tasks.collect_resource_metrics"
        ) as task:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(self.url(action), data or {}, format="json")
        return response, task

    def test_status_shows_the_interval_and_who_may_change_it(self):
        self.poll(T0, diagnostics(100))
        self.client.force_authenticate(self.fixture.manager)

        with freeze_time(T0 + datetime.timedelta(minutes=1)):
            response = self.client.get(self.url())

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["interval_minutes"], 15)
        self.assertTrue(response.data["can_manage"])
        self.assertIsNotNone(response.data["next_poll_at"])
        self.assertIsNotNone(response.data["last_polled_at"])

        self.client.force_authenticate(self.fixture.member)
        self.assertFalse(self.client.get(self.url()).data["can_manage"])

    def test_faster_updates_read_now_and_then_often(self):
        response, task = self.post(
            "faster_updates", self.fixture.manager, {"interval_minutes": 5, "hours": 1}
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["interval_minutes"], 5)
        self.assertEqual(task.apply_async.call_args.kwargs["args"], (self.resource.id,))
        schedule = models.InstanceMetricSchedule.objects.get(instance=self.instance)
        self.assertEqual(schedule.requested_by, self.fixture.manager)

        with freeze_time(timezone.now()):
            self.assertEqual(metrics.current_interval(15, schedule, timezone.now()), 5)

    def test_faster_updates_lapse_on_their_own(self):
        models.InstanceMetricSchedule.objects.create(
            instance=self.instance, interval=5, until=T0
        )

        self.assertEqual(self.dispatched(15), [self.resource.id])

    def test_back_to_normal_drops_the_faster_interval(self):
        self.post(
            "faster_updates", self.fixture.manager, {"interval_minutes": 5, "hours": 1}
        )
        response, _ = self.post("normal_updates", self.fixture.manager)

        self.assertEqual(response.data["interval_minutes"], 15)
        self.assertFalse(models.InstanceMetricSchedule.objects.exists())

    def test_someone_who_cannot_manage_the_instance_is_refused(self):
        response, task = self.post(
            "faster_updates", self.fixture.member, {"interval_minutes": 5, "hours": 1}
        )

        self.assertEqual(response.status_code, 403)
        task.apply_async.assert_not_called()

    def test_reading_now_is_refused_right_after_a_read(self):
        self.poll(timezone.now() - datetime.timedelta(minutes=1), diagnostics(100))

        response, task = self.post("update_now", self.fixture.manager)

        self.assertEqual(response.status_code, 400)
        task.apply_async.assert_not_called()

    def test_reading_now(self):
        response, task = self.post("update_now", self.fixture.manager)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(task.apply_async.call_args.kwargs["args"], (self.resource.id,))

    def test_reading_now_twice_before_the_read_queues_one_read(self):
        self.post("update_now", self.fixture.manager)
        response, task = self.post("update_now", self.fixture.manager)

        self.assertEqual(response.status_code, 400)
        task.apply_async.assert_not_called()

    def test_asking_for_faster_updates_again_does_not_read_again(self):
        faster = {"interval_minutes": 5, "hours": 1}
        self.post("faster_updates", self.fixture.manager, faster)
        response, task = self.post(
            "faster_updates", self.fixture.manager, {**faster, "hours": 6}
        )

        self.assertEqual(response.status_code, 200, response.data)
        task.apply_async.assert_not_called()
        schedule = models.InstanceMetricSchedule.objects.get(instance=self.instance)
        self.assertGreater(schedule.until, timezone.now() + datetime.timedelta(hours=5))

    def test_only_offered_intervals_and_durations_are_accepted(self):
        for data in (
            {"interval_minutes": 7, "hours": 1},
            {"interval_minutes": 5, "hours": 25},
        ):
            response, _ = self.post("faster_updates", self.fixture.manager, data)
            self.assertEqual(response.status_code, 400, data)

    def test_a_stopped_instance_is_not_read_on_request(self):
        self.instance.runtime_state = openstack_models.Instance.RuntimeStates.SHUTOFF
        self.instance.save()

        for action, data in (
            ("update_now", None),
            ("faster_updates", {"interval_minutes": 5, "hours": 1}),
        ):
            response, task = self.post(action, self.fixture.manager, data)
            self.assertEqual(response.status_code, 400, action)
            task.apply_async.assert_not_called()
        self.assertFalse(models.InstanceMetricSchedule.objects.exists())

    def test_faster_updates_are_faster_than_the_provider(self):
        for interval in (15, 60):
            response, task = self.post(
                "faster_updates",
                self.fixture.manager,
                {"interval_minutes": interval, "hours": 1},
            )
            self.assertEqual(response.status_code, 400, interval)
            self.assertIn("interval_minutes", response.data)
            task.apply_async.assert_not_called()

    def volume_resource(self, instance=None):
        volume_offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_VOLUME_OFFERING,
            scope=self.fixture.tenant,
            parent=self.provider,
        )
        volume = openstack_factories.VolumeFactory(
            tenant=self.fixture.tenant, instance=instance
        )
        return marketplace_factories.ResourceFactory(
            offering=volume_offering,
            project=self.fixture.project,
            scope=volume,
            state=ResourceStates.OK,
        )

    def post_for(self, resource, action, user, data=None):
        self.client.force_authenticate(user)
        url = (
            f"/api/marketplace-openstack-instance-metrics/{resource.uuid.hex}/{action}/"
        )
        with mock.patch(
            "waldur_mastermind.marketplace_openstack.views.tasks.collect_resource_metrics"
        ) as task:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(url, data or {}, format="json")
        return response, task

    def test_a_volume_is_read_with_its_instance(self):
        volume = self.volume_resource(instance=self.instance)
        self.client.force_authenticate(self.fixture.manager)

        response = self.client.get(
            f"/api/marketplace-openstack-instance-metrics/{volume.uuid.hex}/"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["instance_name"], self.resource.name)
        self.assertEqual(
            response.data["instance_resource_uuid"], self.resource.uuid.hex
        )
        self.assertTrue(response.data["can_manage"])

    def test_faster_updates_on_a_volume_speed_up_its_instance(self):
        volume = self.volume_resource(instance=self.instance)

        response, task = self.post_for(
            volume,
            "faster_updates",
            self.fixture.manager,
            {"interval_minutes": 5, "hours": 1},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["interval_minutes"], 5)
        self.assertTrue(
            models.InstanceMetricSchedule.objects.filter(
                instance=self.instance
            ).exists()
        )
        self.assertEqual(task.apply_async.call_args.kwargs["args"], (self.resource.id,))

    def test_a_detached_volume_is_not_read(self):
        volume = self.volume_resource()
        self.client.force_authenticate(self.fixture.manager)

        status = self.client.get(
            f"/api/marketplace-openstack-instance-metrics/{volume.uuid.hex}/"
        ).data
        response, task = self.post_for(volume, "update_now", self.fixture.manager)

        self.assertIsNone(status["instance_name"])
        self.assertIsNone(status["next_poll_at"])
        self.assertFalse(status["can_manage"])
        self.assertEqual(response.status_code, 400)
        task.apply_async.assert_not_called()

    def test_an_instance_of_an_archived_offering_is_not_read(self):
        self.offering.state = OfferingStates.ARCHIVED
        self.offering.save()
        self.client.force_authenticate(self.fixture.manager)

        status = self.client.get(self.url()).data
        response, task = self.post("update_now", self.fixture.manager)

        self.assertFalse(status["enabled"])
        self.assertEqual(response.status_code, 400)
        task.apply_async.assert_not_called()

    def test_nothing_to_speed_up_where_the_provider_collects_nothing(self):
        self.configure(**{metrics.INSTANCE_METRICS_ENABLED_OPTION: False})

        response, _ = self.post(
            "faster_updates", self.fixture.manager, {"interval_minutes": 5, "hours": 1}
        )

        self.assertEqual(response.status_code, 400)


class InstanceDiagnosticsBackendTest(BaseOpenStackTest):
    def setUp(self):
        super().setUp()
        self.fixture = OpenStackFixture()
        self.backend = OpenStackBackend(self.fixture.tenant.service_settings)

    @mock.patch("waldur_openstack.backend.get_keystone_session")
    @mock.patch("waldur_openstack.backend.get_nova_client")
    def test_metrics_calls_give_up_after_a_timeout(self, get_nova, _):
        get_nova.return_value.servers.diagnostics.side_effect = (
            keystone_exceptions.ConnectTimeout("timed out")
        )

        with self.assertRaises(OpenStackBackendError):
            self.backend.get_instance_diagnostics(self.fixture.instance)
        session = get_nova.call_args.args[0]
        self.assertEqual(session.timeout, OpenStackBackend.METRICS_CALL_TIMEOUT)

    @mock.patch("waldur_openstack.backend.get_keystone_session")
    @mock.patch("waldur_openstack.backend.get_nova_client")
    def test_diagnostics_are_asked_for_in_libvirts_own_terms(self, get_nova, _):
        get_nova.return_value.servers.diagnostics.return_value = (None, {"x": 1})

        self.assertEqual(
            self.backend.get_instance_diagnostics(self.fixture.instance), {"x": 1}
        )
        self.assertEqual(get_nova.call_args.kwargs["version"], "2.47")

    @mock.patch("waldur_openstack.backend.get_keystone_session")
    @mock.patch("waldur_openstack.backend.get_nova_client")
    def test_each_device_names_its_volume(self, get_nova, _):
        get_nova.return_value.volumes.get_server_volumes.return_value = [
            mock.Mock(device="/dev/vda", volumeId="root"),
            mock.Mock(device="/dev/vdb", volumeId="data"),
            mock.Mock(device=None, volumeId="pending"),
        ]

        self.assertEqual(
            self.backend.get_instance_volume_devices(self.fixture.instance),
            {"vda": "root", "vdb": "data"},
        )
