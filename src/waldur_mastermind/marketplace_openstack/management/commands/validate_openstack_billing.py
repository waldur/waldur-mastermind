import logging
import sys
from collections import defaultdict
from datetime import datetime

from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError

from waldur_core.structure.models import ServiceSettings
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_MODE_DYNAMIC,
    STORAGE_MODE_FIXED,
    utils,
)
from waldur_openstack import models as openstack_models
from waldur_openstack.backend import OpenStackBackendError
from waldur_openstack.session import get_placement_client

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Audit OpenStack instance flavor-derived billing against Placement "
        "allocations. Read-only: reports drift, changes nothing."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--month",
            help="Reporting period as YYYY-MM. Defaults to the current month. "
            "Instances created after the end of the month are excluded.",
        )
        parser.add_argument(
            "--service-settings",
            help="Limit the audit to a single OpenStack ServiceSettings UUID.",
        )
        parser.add_argument(
            "--flag-untracked",
            action="store_true",
            help="Also report Placement resource classes (e.g. VGPU) that have "
            "no matching OfferingComponent on the plan — i.e. silent under-billing.",
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Only print drift rows and the summary, suppress progress output.",
        )

    def handle(self, *args, **options):
        flag_untracked = options["flag_untracked"]
        quiet = options["quiet"]
        period_end = self._parse_month(options.get("month"))

        # backend_id is nullable (null=True), so exclude("") alone lets NULL
        # rows through to get_allocations(None); exclude both.
        instances = (
            openstack_models.Instance.objects.exclude(backend_id="")
            .exclude(backend_id__isnull=True)
            .order_by("tenant_id")
        )
        if period_end is not None:
            instances = instances.filter(created__lt=period_end)

        settings_uuid = options.get("service_settings")
        if settings_uuid:
            try:
                service_settings = ServiceSettings.objects.get(uuid=settings_uuid)
            except (ServiceSettings.DoesNotExist, ValueError):
                raise CommandError(
                    f"ServiceSettings with UUID {settings_uuid} does not exist."
                )
            instances = instances.filter(tenant__service_settings=service_settings)

        # Group by tenant so the Placement client and the tenant's tracked
        # components are resolved once per tenant rather than per instance.
        by_tenant = defaultdict(list)
        for instance in instances:
            by_tenant[instance.tenant].append(instance)

        rows: list[dict] = []
        for tenant, tenant_instances in by_tenant.items():
            rows.extend(self._audit_tenant(tenant, tenant_instances, flag_untracked))

        return self._report(rows, quiet)

    def _parse_month(self, month):
        if not month:
            return None
        try:
            start = datetime.strptime(month, "%Y-%m")
        except ValueError:
            raise CommandError("--month must be in YYYY-MM format.")
        if start.month == 12:
            return start.replace(year=start.year + 1, month=1)
        return start.replace(month=start.month + 1)

    def _billing_context(self, tenant):
        """Return ``(tracked_component_types, storage_mode)`` for the tenant's
        marketplace plan, or ``None`` if the tenant has no marketplace resource.
        """
        try:
            resource = marketplace_models.Resource.objects.get(scope=tenant)
        except ObjectDoesNotExist:
            return None
        tracked = set(resource.offering.components.values_list("type", flat=True))
        storage_mode = (
            resource.offering.plugin_options.get("storage_mode") or STORAGE_MODE_FIXED
        )
        return tracked, storage_mode

    def _audit_tenant(self, tenant, tenant_instances, flag_untracked):
        context = self._billing_context(tenant)
        if context is None:
            logger.warning("Skipping tenant %s: no marketplace resource found.", tenant)
            return []
        tracked, storage_mode = context
        if not tracked:
            tracked = {CORES_TYPE, RAM_TYPE}

        try:
            backend = tenant.get_backend()
            placement = get_placement_client(backend.admin_session)
        except OpenStackBackendError as e:
            logger.warning("Skipping tenant %s: cannot reach Placement: %s", tenant, e)
            return []

        rows = []
        for instance in tenant_instances:
            try:
                allocations = placement.get_allocations(instance.backend_id)
            except OpenStackBackendError as e:
                logger.warning("Skipping instance %s: Placement error: %s", instance, e)
                continue
            if not allocations:
                # No Placement record: instance is transient (just created) or
                # was never scheduled. Nothing to reconcile against.
                logger.debug(
                    "Instance %s has no Placement allocation; skipping.", instance
                )
                continue
            # Storage is intentionally absent: instance.disk is Cinder volumes,
            # which Placement does not track (see PLACEMENT_CLASSES_IGNORED).
            flavor_values = {
                CORES_TYPE: instance.cores,
                RAM_TYPE: instance.ram,
            }
            placement_resources = utils.aggregate_placement_allocations(allocations)
            drifts = utils.reconcile_instance_allocation(
                flavor_values, placement_resources, tracked, flag_untracked
            )
            for drift in drifts:
                rows.append({"instance": instance, "tenant": tenant, **drift})

        # Storage twin of the VGPU check: Cinder volume types in use that no
        # component bills for. Only in dynamic mode — fixed mode bills all
        # storage under the single `storage` component, so there is no per-type gap.
        if flag_untracked and storage_mode == STORAGE_MODE_DYNAMIC:
            volume_drifts = utils.detect_untracked_volume_types(
                tenant.quota_usages, tracked
            )
            for drift in volume_drifts:
                rows.append({"instance": None, "tenant": tenant, **drift})
        return rows

    def _report(self, rows, quiet):
        if not rows:
            self.stdout.write(self.style.SUCCESS("No drift detected."))
            return

        for row in rows:
            instance = row["instance"]
            scope = (
                f"{instance} ({instance.backend_id})"
                if instance is not None
                else f"tenant {row['tenant']}"
            )
            line = (
                f"[{row['severity']}] {scope} "
                f"{row['resource_class']}: billed={row['billed']} "
                f"actual={row['actual']} — {row['tag']}"
            )
            style = (
                self.style.ERROR
                if row["severity"] == utils.DriftSeverity.HIGH
                else self.style.WARNING
            )
            self.stdout.write(style(line))

        high = sum(1 for r in rows if r["severity"] == utils.DriftSeverity.HIGH)
        medium = len(rows) - high
        if not quiet:
            self.stdout.write(
                f"\nDrift summary: {len(rows)} finding(s) — "
                f"{high} HIGH, {medium} MEDIUM."
            )
        if high:
            # Non-zero exit so cron / CI can alert on under-billing.
            sys.exit(1)
