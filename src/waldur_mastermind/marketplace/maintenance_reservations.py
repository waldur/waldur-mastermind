"""Maintenance announcements as Slurm reservations, through the site agent.

Opt-in per offering (plugin option ``slurm_maintenance_sync_enabled``). Waldur
decides which reservations should exist; the site agent only reconciles the
cluster with that list, so the rules live here:

- only published announcements (scheduled or in progress) are reserved;
- a full outage reserves all of the offering's nodes, a partial outage only
  the partitions it names (none named: it stays a notice, as do degraded
  performance and no impact);
- every reservation Waldur asks for is named ``waldur-maint-<id>``, and the
  agent manages only reservations with that prefix, never the site's own.

The reverse direction turns maintenance reservations made on the cluster into
draft announcements, which the provider reviews before anyone is told.
"""

import datetime
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from . import models
from .enums import ImpactLevel, MaintenanceState, MaintenanceType

OPTION = "slurm_maintenance_sync_enabled"
RESERVATION_PREFIX = "waldur-maint-"
RESERVED_STATES = (MaintenanceState.SCHEDULED, MaintenanceState.IN_PROGRESS)
RESERVED_IMPACTS = (ImpactLevel.FULL_OUTAGE, ImpactLevel.PARTIAL_OUTAGE)
IMPORT_BACKEND_PREFIX = "slurm-reservation"


def is_enabled(offering) -> bool:
    return bool((offering.plugin_options or {}).get(OPTION))


def reservation_name(maintenance) -> str:
    return f"{RESERVATION_PREFIX}{maintenance.uuid.hex[:12]}"


def reserved_partitions(row) -> list[str] | None:
    """Partitions to reserve for an affected offering; [] = all, None = none."""
    if row.impact_level == ImpactLevel.FULL_OUTAGE:
        return list(row.affected_partitions or [])
    if row.impact_level == ImpactLevel.PARTIAL_OUTAGE and row.affected_partitions:
        return list(row.affected_partitions)
    return None


def window(maintenance) -> tuple[datetime.datetime, datetime.datetime]:
    start = maintenance.scheduled_start
    if maintenance.state == MaintenanceState.IN_PROGRESS and maintenance.actual_start:
        start = min(start, maintenance.actual_start)
    return start, maintenance.scheduled_end


def desired_reservations(offering, now=None) -> list[dict]:
    """The reservations the cluster should hold for this offering's maintenance."""
    if not is_enabled(offering):
        return []
    now = now or timezone.now()
    rows = (
        models.MaintenanceAnnouncementOffering.objects.filter(
            offering=offering,
            maintenance__state__in=RESERVED_STATES,
            impact_level__in=RESERVED_IMPACTS,
            maintenance__scheduled_end__gt=now,
        )
        .select_related("maintenance")
        .order_by("maintenance__scheduled_start")
    )
    result = []
    for row in rows:
        partitions = reserved_partitions(row)
        if partitions is None:
            continue
        maintenance = row.maintenance
        start, end = window(maintenance)
        result.append(
            {
                "name": reservation_name(maintenance),
                "maintenance_uuid": maintenance.uuid.hex,
                "maintenance_name": maintenance.name,
                "state": maintenance.get_state_display(),
                "impact_level": row.get_impact_level_display(),
                "start": start,
                "end": end,
                "partitions": partitions,
                "flags": ["MAINT"],
            }
        )
    return result


@dataclass
class SiteReservation:
    name: str
    start: datetime.datetime
    end: datetime.datetime
    partitions: list[str]
    node_list: str = ""
    flags: tuple[str, ...] = ()


def import_backend_id(offering, name: str) -> str:
    return f"{IMPORT_BACKEND_PREFIX}:{offering.uuid.hex}:{name}"


@transaction.atomic
def import_site_reservations(offering, reservations: list[SiteReservation], user):
    """Draft announcements for the cluster's own maintenance reservations.

    Our own (prefixed) reservations and ones without the MAINT flag are
    skipped. A draft is created once per reservation and follows later
    changes to it while it is still a draft; once the provider has
    published or discarded it, Waldur's copy is theirs and is left alone.
    """
    service_provider = models.ServiceProvider.objects.filter(
        customer=offering.customer
    ).first()
    result = {"created": [], "updated": [], "unchanged": [], "skipped": []}
    # A reservation listing every node arrives as every partition; that is
    # the whole cluster, i.e. a full outage.
    published = set(
        models.OfferingPartition.objects.filter(offering=offering).values_list(
            "partition_name", flat=True
        )
    )
    if service_provider is None:
        result["skipped"] = [r.name for r in reservations]
        return result
    for reservation in reservations:
        flags = {flag.upper() for flag in reservation.flags}
        if reservation.name.startswith(RESERVATION_PREFIX) or not (
            flags & {"MAINT", "MAINTENANCE"}
        ):
            result["skipped"].append(reservation.name)
            continue
        partitions = sorted(set(reservation.partitions))
        if published and published <= set(partitions):
            partitions = []
        impact = ImpactLevel.PARTIAL_OUTAGE if partitions else ImpactLevel.FULL_OUTAGE
        backend_id = import_backend_id(offering, reservation.name)
        existing = (
            models.MaintenanceAnnouncement.objects.select_for_update()
            .filter(service_provider=service_provider, backend_id=backend_id)
            .first()
        )
        if existing is None:
            maintenance = models.MaintenanceAnnouncement.objects.create(
                name=f"{offering.name}: maintenance {reservation.name}",
                message=(
                    f"Maintenance on {', '.join(partitions) or 'all nodes'} of "
                    f"{offering.name}. Imported from the cluster's reservation "
                    f"'{reservation.name}'; review the message before publishing."
                ),
                internal_notes=f"Slurm reservation {reservation.name} "
                f"({reservation.node_list or 'all nodes'}).",
                maintenance_type=MaintenanceType.SCHEDULED,
                scheduled_start=reservation.start,
                scheduled_end=reservation.end,
                service_provider=service_provider,
                created_by=user,
                backend_id=backend_id,
            )
            models.MaintenanceAnnouncementOffering.objects.create(
                maintenance=maintenance,
                offering=offering,
                impact_level=impact,
                affected_partitions=partitions,
            )
            result["created"].append(reservation.name)
            continue
        if existing.state != MaintenanceState.DRAFT:
            result["unchanged"].append(reservation.name)
            continue
        changed = (existing.scheduled_start, existing.scheduled_end) != (
            reservation.start,
            reservation.end,
        )
        existing.scheduled_start = reservation.start
        existing.scheduled_end = reservation.end
        existing.save(update_fields=["scheduled_start", "scheduled_end", "modified"])
        row, _ = models.MaintenanceAnnouncementOffering.objects.get_or_create(
            maintenance=existing, offering=offering
        )
        if (row.impact_level, row.affected_partitions) != (impact, partitions):
            row.impact_level = impact
            row.affected_partitions = partitions
            row.save(update_fields=["impact_level", "affected_partitions", "modified"])
            changed = True
        result["updated" if changed else "unchanged"].append(reservation.name)
    return result
