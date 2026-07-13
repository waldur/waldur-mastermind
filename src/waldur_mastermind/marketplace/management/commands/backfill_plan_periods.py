import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from waldur_mastermind.marketplace.models import (
    ComponentUsage,
    Offering,
    Resource,
    ResourcePlanPeriod,
)


class Command(BaseCommand):
    help = (
        "Backfill plan_period on ComponentUsage records where it is NULL. "
        "This fixes incorrect quarterly/annual/total usage calculations caused by "
        "ComponentUsage records created without a plan_period."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only show what would be done without making changes.",
        )
        parser.add_argument(
            "--offering",
            dest="offering_uuid",
            help="Only process resources belonging to the offering with this UUID.",
        )
        parser.add_argument(
            "--resource",
            dest="resource_uuid",
            help="Only process the resource with this UUID.",
        )
        parser.add_argument(
            "--start-date",
            dest="start_date",
            help=(
                "Only backfill ComponentUsage records whose billing_period is on or "
                "after this date (format: YYYY-MM-DD)."
            ),
        )
        parser.add_argument(
            "--end-date",
            dest="end_date",
            help=(
                "Only backfill ComponentUsage records whose billing_period is on or "
                "before this date (format: YYYY-MM-DD)."
            ),
        )

    def _parse_date(self, value, label):
        try:
            return datetime.datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            raise CommandError(
                f"Invalid {label} {value!r}: expected format YYYY-MM-DD."
            )

    def _orphan_qs(self, resource):
        """Orphaned ComponentUsage records for a resource, honouring date filters."""
        orphans = ComponentUsage.objects.filter(resource=resource, plan_period=None)
        if self.start_date:
            orphans = orphans.filter(billing_period__gte=self.start_date)
        if self.end_date:
            orphans = orphans.filter(billing_period__lte=self.end_date)
        return orphans

    def _backfill_for_plan_period(self, plan_period, dry_run):
        """Backfill orphaned ComponentUsage records for a given plan period.

        Deletes duplicates (orphans that already have a matching record with
        the plan_period set) and backfills the rest.

        Returns (updated_count, deleted_count).
        """
        orphans = self._orphan_qs(plan_period.resource)
        if not orphans.exists():
            return 0, 0

        # Find orphans that already have a matching record with this plan_period
        duplicates = orphans.filter(
            billing_period__in=ComponentUsage.objects.filter(
                resource=plan_period.resource, plan_period=plan_period
            ).values("billing_period")
        )
        dup_count = duplicates.count()
        if not dry_run and dup_count:
            duplicates.delete()

        # Backfill remaining orphans
        remaining = self._orphan_qs(plan_period.resource)
        updated_count = remaining.count()
        if not dry_run and updated_count:
            remaining.update(plan_period=plan_period)

        if dup_count or updated_count:
            self.stdout.write(
                f"  {plan_period.resource.name} (uuid={plan_period.resource.uuid.hex}): "
                f"deleted {dup_count} duplicates, backfilled {updated_count} records"
            )

        return updated_count, dup_count

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        self.start_date = (
            self._parse_date(options["start_date"], "--start-date")
            if options["start_date"]
            else None
        )
        self.end_date = (
            self._parse_date(options["end_date"], "--end-date")
            if options["end_date"]
            else None
        )
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise CommandError("--start-date must not be after --end-date.")

        offering = None
        if options["offering_uuid"]:
            try:
                offering = Offering.objects.get(uuid=options["offering_uuid"])
            except (Offering.DoesNotExist, ValueError):
                raise CommandError(
                    f"Offering with UUID {options['offering_uuid']!r} does not exist."
                )

        resource = None
        if options["resource_uuid"]:
            try:
                resource = Resource.objects.get(uuid=options["resource_uuid"])
            except (Resource.DoesNotExist, ValueError):
                raise CommandError(
                    f"Resource with UUID {options['resource_uuid']!r} does not exist."
                )

        total_updated = 0
        total_deleted = 0

        # Step 1: Backfill where ResourcePlanPeriod already exists
        plan_periods = ResourcePlanPeriod.objects.filter(end=None).select_related(
            "resource"
        )
        if offering:
            plan_periods = plan_periods.filter(resource__offering=offering)
        if resource:
            plan_periods = plan_periods.filter(resource=resource)

        for plan_period in plan_periods:
            updated, deleted = self._backfill_for_plan_period(plan_period, dry_run)
            total_updated += updated
            total_deleted += deleted

        # Step 2: Create missing ResourcePlanPeriod for resources that still have orphaned records
        resources_still_orphaned = (
            Resource.objects.filter(
                state__in=(Resource.States.OK, Resource.States.UPDATING)
            )
            .exclude(plan=None)
            .filter(usages__plan_period=None)
            .exclude(
                Q(id__in=ResourcePlanPeriod.objects.filter(end=None).values("resource"))
            )
            .distinct()
        )
        if offering:
            resources_still_orphaned = resources_still_orphaned.filter(
                offering=offering
            )
        if resource:
            resources_still_orphaned = resources_still_orphaned.filter(pk=resource.pk)

        for resource_obj in resources_still_orphaned:
            orphaned = self._orphan_qs(resource_obj)
            count = orphaned.count()
            if not count:
                continue
            self.stdout.write(
                f"  {resource_obj.name} (uuid={resource_obj.uuid.hex}): "
                f"creating ResourcePlanPeriod and backfilling {count} records"
            )
            if not dry_run:
                plan_period = ResourcePlanPeriod.objects.create(
                    resource=resource_obj,
                    plan=resource_obj.plan,
                    start=resource_obj.created,
                    end=None,
                )
                updated, deleted = self._backfill_for_plan_period(
                    plan_period, dry_run=False
                )
                total_updated += updated
                total_deleted += deleted
            else:
                total_updated += count

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Total: {total_updated} backfilled, {total_deleted} duplicates deleted"
            )
        )
