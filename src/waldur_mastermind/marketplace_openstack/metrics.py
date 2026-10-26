"""Custom metrics for OpenStack instances and their volumes, read from Nova.

The provider turns them on, and picks which ones, on its tenant offering; the
choice is adopted on the instance and volume offerings of every tenant of that
offering. Each poll reads one instance's diagnostics and the volumes attached
to it, and records the instance's figures on its resource and each volume's on
the volume's resource. Nova gives running totals per disk and port; what each
of them added since the last poll is recorded, so that a device coming or
going, or starting over on its own, doesn't count the others again.
"""

import contextlib
import dataclasses
import datetime
import decimal
import logging
import re

from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from waldur_core.core.enums import CoreStates
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_VOLUME_OFFERING,
    ResourceStates,
)
from waldur_mastermind.marketplace_metrics import enums as metrics_enums
from waldur_mastermind.marketplace_metrics import ingest
from waldur_mastermind.marketplace_metrics import models as metrics_models

# models is imported absolutely: tests that import this package's tasks by a
# relative path would otherwise load a second copy of the model class.
from waldur_mastermind.marketplace_openstack import models
from waldur_openstack import models as openstack_models
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.exceptions import OpenStackBackendError

from . import (
    INSTANCE_METRIC_KEYS,
    INSTANCE_METRICS_ENABLED_OPTION,
    INSTANCE_METRICS_OPTION,
    VOLUME_METRIC_KEYS,
)

logger = logging.getLogger(__name__)

(
    CPU_UTILIZATION,
    CPU_TIME,
    MEMORY_UTILIZATION,
    NETWORK_IO,
    DISK_IO,
    DISK_OPERATIONS,
) = INSTANCE_METRIC_KEYS
VOLUME_IO, VOLUME_OPERATIONS = VOLUME_METRIC_KEYS
KEYS = INSTANCE_METRIC_KEYS + VOLUME_METRIC_KEYS
KEYS_BY_OFFERING_TYPE = {
    OPENSTACK_INSTANCE_OFFERING: INSTANCE_METRIC_KEYS,
    OPENSTACK_VOLUME_OFFERING: VOLUME_METRIC_KEYS,
}

# The catalogue entries these metrics report against, created the first time a
# provider turns them on. Staff may edit them afterwards, as any other entry.
DEFINITIONS = {
    CPU_UTILIZATION: dict(
        name="CPU utilization",
        description="Share of the instance's vCPUs in use between two polls.",
        unit="%",
        kind=metrics_enums.MetricKinds.GAUGE,
        attribute_keys=[],
    ),
    CPU_TIME: dict(
        name="CPU time",
        description="CPU time the instance's vCPUs consumed.",
        unit="s",
        kind=metrics_enums.MetricKinds.COUNTER,
        attribute_keys=[],
    ),
    MEMORY_UTILIZATION: dict(
        name="Memory utilization",
        description="Share of the guest's memory in use when it was read, "
        "reclaimable page cache left out. Needs the guest's balloon driver.",
        unit="%",
        kind=metrics_enums.MetricKinds.GAUGE,
        attribute_keys=[],
    ),
    NETWORK_IO: dict(
        name="Network traffic",
        description="Bytes the instance received and transmitted on its ports.",
        unit="By",
        kind=metrics_enums.MetricKinds.COUNTER,
        attribute_keys=["direction"],
    ),
    DISK_IO: dict(
        name="Disk throughput",
        description="Bytes the instance read from and wrote to all its disks.",
        unit="By",
        kind=metrics_enums.MetricKinds.COUNTER,
        attribute_keys=["direction"],
    ),
    DISK_OPERATIONS: dict(
        name="Disk I/O operations",
        description="Read and write requests the instance made to all its disks.",
        # Per second, as charts show it over hours, this is IOPS.
        unit="{I/O operation}",
        kind=metrics_enums.MetricKinds.COUNTER,
        attribute_keys=["direction"],
    ),
    VOLUME_IO: dict(
        name="Volume throughput",
        description="Bytes read from and written to the volume by the instance "
        "it is attached to.",
        unit="By",
        kind=metrics_enums.MetricKinds.COUNTER,
        attribute_keys=["direction"],
    ),
    VOLUME_OPERATIONS: dict(
        name="Volume I/O operations",
        description="Read and write requests made to the volume by the instance "
        "it is attached to.",
        unit="{I/O operation}",
        kind=metrics_enums.MetricKinds.COUNTER,
        attribute_keys=["direction"],
    ),
}
# Polls are hours apart, so an hour holds a point at most and hourly roll-ups
# would only copy the raw points: both are kept two weeks, the daily ones the
# charts and goals read for a little over a year.
RETENTION_POLICY = dict(
    name="OpenStack instance metrics", raw_days=14, hourly_days=14, daily_days=400
)

NANOSECONDS = decimal.Decimal(10) ** 9
# A reading's devices, and the sample's counters, by kind.
DEVICE_KINDS = ("nics", "disks")
HUNDRED = decimal.Decimal(100)

POLL_INTERVAL_OPTION = "instance_metrics_poll_interval_minutes"
DEFAULT_POLL_INTERVAL = 360
MIN_POLL_INTERVAL = 5
MAX_POLL_INTERVAL = 1440
# Faster polling someone may ask for on one instance, and for how long at most.
FAST_INTERVALS = (5, 15, 60)
MAX_FAST_HOURS = 24

# A queued poll not started within this many minutes, or its interval if that
# is shorter, is dropped: after an outage the backlog would otherwise reach
# Nova all at once, several polls of one instance among it.
MAX_POLL_WAIT = 15
# One poll of an instance at a time, across workers: the cache is shared (in
# production it is the database). The lock outlives the two Nova calls'
# timeouts, so a poll that dies without releasing it does not block the
# instance for longer than that. The lock is a best effort: the database
# cache can hand out an expired key twice, and a key can be culled early.
# Recording a reading locks the instance and checks the last one recorded,
# so a second poll still never counts twice.
LOCK_SECONDS = 4 * OpenStackBackend.METRICS_CALL_TIMEOUT
# A read someone asks for, at most one per instance this often.
REQUEST_SECONDS = MIN_POLL_INTERVAL * 60
# Minutes the dispatcher catches up on when a run was late or skipped.
DISPATCH_CATCH_UP = MAX_POLL_WAIT
DISPATCHED_KEY = "marketplace-openstack:instance-metrics:dispatched"
# Nova's answers about one server rather than about Nova: not running (409),
# gone (404), or a hypervisor that keeps no diagnostics (501).
INSTANCE_ERROR_CODES = (404, 409, 501)
# What is read: offerings in use, paused ones too (they only take no new
# orders), and resources and OpenStack records in a working state.
READ_OFFERING_STATES = (
    marketplace_models.Offering.States.ACTIVE,
    marketplace_models.Offering.States.PAUSED,
)
READ_RESOURCE_STATES = (ResourceStates.OK, ResourceStates.UPDATING)
READ_INSTANCE_STATES = (CoreStates.OK, CoreStates.UPDATING)
# When a provider's Nova fails this many polls in a row, its instances are
# left alone for a while instead of adding load to it.
FAILURES_BEFORE_BACKOFF = 5
BACKOFF_SECONDS = 600


def poll_interval(offering):
    """Minutes between polls of one instance.

    Set on the provider's tenant offering, the parent of every tenant's
    instance offering, so one setting covers all of the provider's tenants.
    """
    source = offering.parent or offering
    minutes = source.plugin_options.get(POLL_INTERVAL_OPTION) or DEFAULT_POLL_INTERVAL
    return max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, int(minutes)))


def current_interval(offering_interval, schedule, now):
    """The instance's own faster interval while it lasts, else the offering's."""
    if schedule is not None and schedule.until > now:
        return min(schedule.interval, offering_interval)
    return offering_interval


def next_poll(resource, interval, now):
    """The start of the next minute that is the resource's turn."""
    minute = int(now.timestamp() // 60)
    wait = (resource.uuid.int - minute) % interval or interval
    return datetime.datetime.fromtimestamp((minute + wait) * 60, tz=datetime.UTC)


def is_due(uuid, interval, minute):
    """Whether a minute, counted from the epoch, is the resource's turn.

    Every resource has a fixed minute within the interval, taken from its
    UUID, so polls spread evenly over the interval instead of reaching Nova
    together on one tick, and need no countdown or stored schedule.
    """
    return minute % interval == uuid.int % interval


def minutes_to_dispatch(now):
    """The minutes to poll for: this one, and those earlier runs missed.

    Beat's ticks drift and a queued run may start late or be dropped, so a run
    may not land in its own minute; skipping one would leave its instances
    unread for a whole interval. Each minute is claimed in the cache, so that
    runs close together don't poll for one twice.
    """
    current = int(now.timestamp() // 60)
    last = cache.get(DISPATCHED_KEY)
    if last is None or last >= current:
        first = current
    else:
        first = max(last + 1, current - DISPATCH_CATCH_UP + 1)
    claimed = [
        minute
        for minute in range(first, current + 1)
        if cache.add(f"{DISPATCHED_KEY}:{minute}", True, timeout=3600)
    ]
    if claimed:
        cache.set(DISPATCHED_KEY, current, timeout=86400)
    return claimed


def poll_expiry(interval):
    """Seconds a queued poll may wait to start."""
    return min(interval, MAX_POLL_WAIT) * 60


@contextlib.contextmanager
def poll_lock(instance):
    key = f"marketplace-openstack:instance-metrics:poll:{instance.id}"
    acquired = cache.add(key, True, timeout=LOCK_SECONDS)
    try:
        yield acquired
    finally:
        if acquired:
            cache.delete(key)


def _provider_key(offering, what):
    provider_id = offering.parent_id or offering.id
    return f"marketplace-openstack:instance-metrics:{what}:{provider_id}"


def is_backing_off(offering):
    return bool(cache.get(_provider_key(offering, "backoff")))


def _failure_keys(offering):
    return [
        _provider_key(offering, f"failure:{slot}")
        for slot in range(FAILURES_BEFORE_BACKOFF)
    ]


def _note_failure(offering):
    """Count a failed poll of the provider's Nova; pause polling after too many.

    Each failure claims the first free numbered slot with add(), which one
    caller wins, across workers too (an expired slot still in the database can
    go to two, which only delays the pause). A counter would not do:
    production caches in the database, where incr() is a read and a write,
    and the polls of many instances fail together when Nova does, so most
    failures went uncounted.
    """
    keys = _failure_keys(offering)
    for slot, key in enumerate(keys):
        if cache.add(key, True, timeout=BACKOFF_SECONDS):
            break
    else:
        return
    if slot < len(keys) - 1:
        return
    cache.set(_provider_key(offering, "backoff"), True, timeout=BACKOFF_SECONDS)
    cache.delete_many(keys)
    logger.warning(
        "Nova failed %s instance metrics polls in a row for offering %s; "
        "pausing them for %s minutes.",
        len(keys),
        offering.parent or offering,
        BACKOFF_SECONDS // 60,
    )


def _note_success(offering):
    cache.delete_many(_failure_keys(offering))


def is_instance_error(error):
    """Nova's answer about one server, such as not running: no fault of Nova's.

    The backend error keeps only the message of what it wraps, so the status
    is read from the exception it was raised from.
    """
    cause = error.__cause__ or error.__context__
    return getattr(cause, "code", None) in INSTANCE_ERROR_CODES


def is_enabled(offering):
    """Whether the provider collects metrics for this instance or volume offering.

    Only an offering in use is read, active or paused, of a provider offering
    in use: a draft, archived or unavailable one is left alone.
    """
    parent = offering.parent
    return (
        parent is not None
        and offering.state in READ_OFFERING_STATES
        and parent.state in READ_OFFERING_STATES
        and bool(parent.plugin_options.get(INSTANCE_METRICS_ENABLED_OPTION))
    )


def selected_keys(tenant_offering):
    """The metrics the provider shows; all of them unless it picked some."""
    if not tenant_offering.plugin_options.get(INSTANCE_METRICS_ENABLED_OPTION):
        return set()
    chosen = tenant_offering.plugin_options.get(INSTANCE_METRICS_OPTION)
    return set(chosen or KEYS) & set(KEYS)


def catalogue_definition(key):
    policy, _ = metrics_models.RetentionPolicy.objects.get_or_create(
        name=RETENTION_POLICY["name"], defaults=RETENTION_POLICY
    )
    definition, _ = metrics_models.MetricDefinition.objects.get_or_create(
        key=key,
        owner_customer=None,
        defaults=dict(DEFINITIONS[key], retention_policy=policy),
    )
    return definition


def sync_tenant_offering(offering):
    """Adopt on a tenant's instance or volume offering what its provider shows.

    Metrics the provider no longer shows are archived: hidden from projects,
    and purged after the grace period unless shown again before that.
    """
    own_keys = KEYS_BY_OFFERING_TYPE[offering.type]
    parent = offering.parent
    wanted = (selected_keys(parent) if parent else set()) & set(own_keys)
    adopted = {
        offering_metric.definition.key: offering_metric
        for offering_metric in metrics_models.OfferingMetric.objects.filter(
            offering=offering, definition__key__in=own_keys
        ).select_related("definition")
    }
    for key in wanted:
        offering_metric = adopted.get(key)
        if offering_metric is None:
            definition = catalogue_definition(key)
            # get_or_create: two syncs of one offering may run at once.
            metrics_models.OfferingMetric.objects.get_or_create(
                offering=offering,
                definition=definition,
                defaults=dict(
                    project_aggregation=(
                        metrics_enums.ProjectAggregations.MEAN
                        if definition.kind == metrics_enums.MetricKinds.GAUGE
                        else metrics_enums.ProjectAggregations.SUM
                    )
                ),
            )
        elif offering_metric.state == metrics_enums.OfferingMetricStates.ARCHIVED:
            # Shown again. A metric staff paused stays paused.
            offering_metric.state = metrics_enums.OfferingMetricStates.ACTIVE
            offering_metric.save(update_fields=["state", "modified"])
    for key, offering_metric in adopted.items():
        if (
            key not in wanted
            and offering_metric.state != metrics_enums.OfferingMetricStates.ARCHIVED
        ):
            offering_metric.state = metrics_enums.OfferingMetricStates.ARCHIVED
            offering_metric.save(update_fields=["state", "modified"])


def adopted_keys(offering):
    return set(
        metrics_models.OfferingMetric.objects.filter(
            offering=offering,
            state=metrics_enums.OfferingMetricStates.ACTIVE,
            definition__key__in=KEYS,
        ).values_list("definition__key", flat=True)
    )


@dataclasses.dataclass
class Reading:
    """One diagnostics reading, by device: cpu0_time, vda_read, tapXXX_rx."""

    cpu_time: decimal.Decimal | None
    vcpus: int
    memory_available: int | None
    memory_usable: int | None
    nics: dict
    disks: dict


def _number(value):
    """A counter's value, or None: missing, not a number, or -1, which libvirt
    reports for a counter the driver does not keep."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value if value >= 0 else None


def _value(diagnostics, key):
    return _number(diagnostics.get(key))


def parse(diagnostics):
    cpu_ns = [
        _number(value)
        for key, value in diagnostics.items()
        if re.fullmatch(r"cpu\d+_time", key) and _number(value) is not None
    ]
    nics = {
        key[: -len("_rx_packets")]: {
            "receive": _value(diagnostics, key[: -len("_packets")]),
            "transmit": _value(diagnostics, key[: -len("_rx_packets")] + "_tx"),
        }
        for key in diagnostics
        if key.endswith("_rx_packets")
    }
    disks = {}
    for key in diagnostics:
        if not key.endswith("_read_req"):
            continue
        device = key[: -len("_read_req")]
        disks[device] = {
            "read": _value(diagnostics, f"{device}_read"),
            "write": _value(diagnostics, f"{device}_write"),
            "read_req": _value(diagnostics, f"{device}_read_req"),
            "write_req": _value(diagnostics, f"{device}_write_req"),
        }
    return Reading(
        cpu_time=decimal.Decimal(sum(cpu_ns)) / NANOSECONDS if cpu_ns else None,
        vcpus=len(cpu_ns),
        memory_available=_value(diagnostics, "memory-available"),
        memory_usable=_value(diagnostics, "memory-usable"),
        nics=nics,
        disks=disks,
    )


def _total(items, field):
    """Sum a counter's increments over NICs or disks; None if none reports it."""
    values = [item[field] for item in items if item.get(field) is not None]
    return sum(values) if values else None


def _increase(value, last):
    """What a counter added since it was last read; all of it if it started over."""
    if value is None:
        return None
    if last is None or value < last:
        return value
    return value - last


def _went_back(last, counters):
    """Whether any of a device's counters is lower than when last read."""
    return any(
        value is not None and last.get(field) is not None and value < last[field]
        for field, value in counters.items()
    )


def _restarted(sample, reading):
    """Whether the server's process restarted since the last reading.

    Its CPU time never goes back while it runs. A restarted server that has
    already used more CPU than the old one had when last read still shows
    it: every device it had then counts from zero again. One device starting
    over is only a volume or port attached anew.
    """
    if reading.cpu_time < sample.cpu_time:
        return True
    devices = [
        _went_back(sample.counters[kind][device], counters)
        for kind in DEVICE_KINDS
        for device, counters in getattr(reading, kind).items()
        if device in sample.counters.get(kind, {})
    ]
    return bool(devices) and all(devices)


def _device_increments(last_devices, devices, restarted, volumes):
    """What each device's counters added since the last reading.

    A device counts from zero after a restart, when it is new, or when the
    volume on it is not the one that was: then it was attached anew. volumes
    is None when the attachments were not looked up.
    """
    increments = {}
    for device, counters in devices.items():
        last = last_devices.get(device)
        attached_anew = (
            volumes is not None
            and last is not None
            and "volume" in last
            and last["volume"] != volumes.get(device)
        )
        start_over = restarted or last is None or attached_anew
        increments[device] = {
            field: _increase(value, None if start_over else last.get(field))
            for field, value in counters.items()
        }
    return increments


def _device_state(last_devices, devices, volumes):
    """The devices' counters to compare the next reading with.

    A device missing from one reading, as when the driver cannot read it,
    keeps its last counters, so that coming back it is not counted in full
    again; missing from two, it is taken to be gone.
    """
    state = {
        device: dict(counters, volume=volumes.get(device))
        if volumes is not None
        else dict(counters)
        for device, counters in devices.items()
    }
    for device, last in last_devices.items():
        if device not in devices and not last.get("missing"):
            state[device] = dict(last, missing=True)
    return state


@dataclasses.dataclass
class Increments:
    """What a reading added to the last one: CPU time and each device's counters."""

    cpu_time: decimal.Decimal
    restarted: bool
    nics: dict
    disks: dict


def _is_sound(counters):
    """Whether a sample's counters are as written: devices by kind, as dicts."""
    return isinstance(counters, dict) and all(
        isinstance(counters.get(kind, {}), dict)
        and all(isinstance(last, dict) for last in counters.get(kind, {}).values())
        for kind in DEVICE_KINDS
    )


def count(sample, reading, instance, now, interval, volumes=None):
    """What this reading added since the last one, or None for a baseline.

    interval is the instance's polling interval, in minutes. A damaged sample
    is a baseline too: this reading starts the counting over.
    """
    if sample is not None and not _is_sound(sample.counters):
        logger.warning(
            "Damaged metrics sample of instance %s; starting over.", instance
        )
        return None
    if sample is None:
        # Nothing is known about earlier restarts. A server started since the
        # last round of polls is new: its totals are all its activity. Those
        # of one running for longer are a baseline: read for the first time,
        # or again after its sample was lost, they hold earlier activity,
        # perhaps counted already, that doesn't belong to this moment.
        started = instance.start_time or instance.created
        new_since = now - datetime.timedelta(minutes=interval + MAX_POLL_WAIT)
        if started < new_since:
            return None
        restarted, last = True, {}
    else:
        restarted, last = _restarted(sample, reading), sample.counters
    return Increments(
        cpu_time=(
            reading.cpu_time if restarted else reading.cpu_time - sample.cpu_time
        ),
        restarted=restarted,
        nics=_device_increments(last.get("nics", {}), reading.nics, restarted, None),
        disks=_device_increments(
            last.get("disks", {}), reading.disks, restarted, volumes
        ),
    )


def _utilization(sample, cpu_time, now, vcpus):
    elapsed = decimal.Decimal((now - sample.sampled_at).total_seconds())
    if elapsed <= 0 or not vcpus:
        return None
    used = cpu_time / elapsed / vcpus * HUNDRED
    return min(max(used, decimal.Decimal(0)), HUNDRED)


def _memory_utilization(reading):
    """In use: the guest's memory less what it could hand out without swapping."""
    if not reading.memory_available or reading.memory_usable is None:
        return None
    used = reading.memory_available - reading.memory_usable
    share = decimal.Decimal(used) / decimal.Decimal(reading.memory_available)
    return min(max(share * HUNDRED, decimal.Decimal(0)), HUNDRED)


class _Points(list):
    """The points of one resource, for the metrics its offering adopted.

    Counters go in as what they added since the last reading.
    """

    def __init__(self, resource, keys, timestamp):
        super().__init__()
        self.resource = resource
        self.keys = keys
        self.timestamp = timestamp

    def add(self, key, value, attributes=None):
        if key not in self.keys or value is None:
            return
        self.append(
            ingest.Point(
                resource_uuid=self.resource.uuid.hex,
                metric=key,
                timestamp=self.timestamp,
                value=decimal.Decimal(value),
                attributes=attributes or {},
            )
        )

    def add_disk(self, io_key, operations_key, disks):
        self.add(io_key, _total(disks, "read"), {"direction": "read"})
        self.add(io_key, _total(disks, "write"), {"direction": "write"})
        self.add(operations_key, _total(disks, "read_req"), {"direction": "read"})
        self.add(operations_key, _total(disks, "write_req"), {"direction": "write"})


def build_points(resource, instance, reading, sample, now, keys, volumes=None):
    """The instance's points for one reading, what it added, and the sample to keep.

    volumes maps the instance's disk devices to the volumes on them, when
    they were looked up.
    """
    points = _Points(resource, keys, now)
    if reading.cpu_time is None:
        return points, None, None
    increments = count(
        sample, reading, instance, now, poll_interval(resource.offering), volumes
    )
    points.add(MEMORY_UTILIZATION, _memory_utilization(reading))
    if increments is not None:
        points.add(CPU_TIME, increments.cpu_time)
        if not increments.restarted:
            points.add(
                CPU_UTILIZATION,
                _utilization(sample, increments.cpu_time, now, reading.vcpus),
            )
        nics = increments.nics.values()
        points.add(NETWORK_IO, _total(nics, "receive"), {"direction": "receive"})
        points.add(NETWORK_IO, _total(nics, "transmit"), {"direction": "transmit"})
        points.add_disk(DISK_IO, DISK_OPERATIONS, increments.disks.values())
    last = sample.counters if sample is not None and _is_sound(sample.counters) else {}
    next_sample = models.InstanceMetricSample(
        instance=instance,
        sampled_at=now,
        cpu_time=reading.cpu_time,
        counters={
            "nics": _device_state(last.get("nics", {}), reading.nics, None),
            "disks": _device_state(last.get("disks", {}), reading.disks, volumes),
        },
    )
    return points, next_sample, increments


def volume_points(instance, increments, devices, now):
    """Each attached volume's points, from what its disk device added."""
    volumes = {
        volume.backend_id: volume
        for volume in openstack_models.Volume.objects.filter(
            tenant=instance.tenant, backend_id__in=devices.values()
        )
    }
    resources = {
        resource.object_id: resource
        for resource in marketplace_models.Resource.objects.filter(
            offering__type=OPENSTACK_VOLUME_OFFERING,
            offering__state__in=READ_OFFERING_STATES,
            state__in=READ_RESOURCE_STATES,
            content_type=ContentType.objects.get_for_model(openstack_models.Volume),
            object_id__in=[volume.id for volume in volumes.values()],
        ).select_related("offering")
    }
    keys_of = {}
    for device, added in increments.disks.items():
        volume = volumes.get(devices.get(device))
        resource = resources.get(volume.id) if volume else None
        if resource is None:
            continue
        if resource.offering_id not in keys_of:
            keys_of[resource.offering_id] = adopted_keys(resource.offering)
        points = _Points(resource, keys_of[resource.offering_id], now)
        points.add_disk(VOLUME_IO, VOLUME_OPERATIONS, [added])
        if points:
            yield points


def _record(points, instance):
    result = ingest.record(points.resource, points)
    for rejected in result.rejected:
        logger.warning(
            "Metric point %s of %s on instance %s was refused: %s",
            points[rejected["index"]].metric,
            points.resource,
            instance,
            rejected["reason"],
        )
    return result


def _read(backend, instance, volumes_wanted, offering):
    """Diagnostics, when they were taken, and the volume on each device.

    The volumes are None when not looked up.
    """
    try:
        reading = parse(backend.get_instance_diagnostics(instance))
        # Stamped when Nova answered, not after the next call: the order of
        # readings is what tells a restart from a late arrival.
        read_at = timezone.now()
        devices = (
            backend.get_instance_volume_devices(instance) if volumes_wanted else None
        )
    except OpenStackBackendError as e:
        if not is_instance_error(e):
            _note_failure(offering)
        raise
    _note_success(offering)
    return reading, read_at, devices


def _store(resource, instance, reading, read_at, devices, keys):
    """Record one reading, unless a later one is already recorded.

    The instance's row is locked, so that two polls of it are recorded one
    after the other even without a sample to lock yet. A reading older than
    the last recorded one would count its devices from zero again; it is
    dropped instead.
    """
    with transaction.atomic():
        openstack_models.Instance.objects.select_for_update().only("pk").get(
            pk=instance.pk
        )
        sample = models.InstanceMetricSample.objects.filter(instance=instance).first()
        if sample is not None and sample.sampled_at >= read_at:
            logger.info(
                "Dropping a reading of instance %s taken before the last one.",
                instance,
            )
            return None
        points, next_sample, increments = build_points(
            resource, instance, reading, sample, read_at, keys, devices
        )
        if next_sample is None:
            logger.info("Instance %s reported no CPU time; nothing recorded.", instance)
            return None
        result = _record(points, instance)
        if increments is not None and devices:
            for points_of_volume in volume_points(
                instance, increments, devices, read_at
            ):
                _record(points_of_volume, instance)
        models.InstanceMetricSample.objects.update_or_create(
            instance=instance,
            defaults={
                "sampled_at": next_sample.sampled_at,
                "cpu_time": next_sample.cpu_time,
                "counters": next_sample.counters,
            },
        )
    return result


def collect(resource):
    """Read one instance and record what its and its volumes' offerings adopted."""
    instance = resource.scope
    if not isinstance(instance, openstack_models.Instance) or not instance.backend_id:
        # Gone, or not in OpenStack, since the poll was queued.
        return None
    if not is_enabled(resource.offering):
        # Turned off, or the offering is no longer active.
        return None
    keys = adopted_keys(resource.offering)
    parent = resource.offering.parent
    volumes_wanted = bool(parent and selected_keys(parent) & set(VOLUME_METRIC_KEYS))
    if not keys and not volumes_wanted:
        return None
    with poll_lock(instance) as acquired:
        if not acquired:
            logger.info("Instance %s is already being read; skipping.", instance)
            return None
        backend = OpenStackBackend(instance.tenant.service_settings)
        reading, read_at, devices = _read(
            backend, instance, volumes_wanted, resource.offering
        )
        return _store(resource, instance, reading, read_at, devices, keys)
