from django.core.management.base import BaseCommand
from django.db.models import Q

from waldur_mastermind.marketplace.models import (
    ComponentUsage,
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

    def _backfill_for_plan_period(self, plan_period, dry_run):
        """Backfill orphaned ComponentUsage records for a given plan period.

        Deletes duplicates (orphans that already have a matching record with
        the plan_period set) and backfills the rest.

        Returns (updated_count, deleted_count).
        """
        orphans = ComponentUsage.objects.filter(
            resource=plan_period.resource, plan_period=None
        )
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
        remaining = ComponentUsage.objects.filter(
            resource=plan_period.resource, plan_period=None
        )
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
        total_updated = 0
        total_deleted = 0

        # Step 1: Backfill where ResourcePlanPeriod already exists
        for plan_period in ResourcePlanPeriod.objects.filter(end=None).select_related(
            "resource"
        ):
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

        for resource in resources_still_orphaned:
            orphaned = ComponentUsage.objects.filter(
                resource=resource, plan_period=None
            )
            count = orphaned.count()
            self.stdout.write(
                f"  {resource.name} (uuid={resource.uuid.hex}): "
                f"creating ResourcePlanPeriod and backfilling {count} records"
            )
            if not dry_run:
                plan_period = ResourcePlanPeriod.objects.create(
                    resource=resource,
                    plan=resource.plan,
                    start=resource.created,
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
