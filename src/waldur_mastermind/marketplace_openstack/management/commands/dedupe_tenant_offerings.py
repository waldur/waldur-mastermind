"""Detect and resolve duplicate per-tenant OpenStack offerings.

The self-heal job (``self_heal_tenant_offerings``) expects exactly one
per-tenant ``OpenStack.Instance`` and one ``OpenStack.Volume`` offering per
tenant. When a second offering of the same type gets created for a tenant
(observed in production after offering-side state drift), self-heal can no
longer decide which offering owns the tenant's resources, logs
``skipped_multiple`` and leaves orphan VMs/volumes unlinked from the
marketplace.

This command reports those duplicate groups with enough detail for an
operator to act, and — when asked — collapses each group down to a single
keeper offering so the next self-heal run can link the orphans.

Safety model (the offering -> resource/order FK is ``on_delete=CASCADE``, so a
naive delete would take resources and orders with it):

* dry-run by default — nothing is changed unless ``--apply`` is given;
* the keeper is the offering that owns the tenant's non-terminated resources
  (falling back to the oldest active offering when none are in use);
* with ``--apply`` only *empty* duplicates (no resources, no orders) are
  deleted — this is safe and covers the common "a stray offering was created
  but nothing ever attached to it" case;
* a duplicate that still owns resources or orders is left untouched and
  reported unless ``--merge`` is also given, which re-points its resources,
  orders and plans onto the keeper inside a transaction before deleting it.
"""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_openstack import utils

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Report and optionally resolve tenants that have more than one "
        "per-tenant OpenStack.Instance/Volume offering."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            type=int,
            dest="tenant_id",
            help="Restrict to a single OpenStack tenant (by numeric id). "
            "Matches the tenant id printed in the self-heal ERROR log.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete empty duplicate offerings. Without this flag the "
            "command only reports (dry-run).",
        )
        parser.add_argument(
            "--merge",
            action="store_true",
            help="Also resolve duplicates that still own resources/orders by "
            "re-pointing them onto the keeper before deletion. Requires --apply.",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        merge = options["merge"]
        tenant_id = options["tenant_id"]

        if merge and not apply_changes:
            self.stderr.write(
                self.style.ERROR("--merge has no effect without --apply.")
            )
            return

        groups = utils.collect_duplicate_offering_groups(tenant_id)
        if not groups:
            self.stdout.write(
                self.style.SUCCESS(
                    "No duplicate per-tenant offerings found"
                    + (f" for tenant {tenant_id}." if tenant_id else ".")
                )
            )
            return

        self.stdout.write(
            f"Found {len(groups)} duplicate offering group(s)"
            f"{'' if apply_changes else ' (dry-run — pass --apply to change anything)'}:"
        )

        for (group_tenant_id, offering_type), offerings in groups.items():
            self._handle_group(
                group_tenant_id, offering_type, offerings, apply_changes, merge
            )

    def _handle_group(self, tenant_id, offering_type, offerings, apply_changes, merge):
        keeper = utils.pick_keeper_offering(offerings)
        duplicates = [o for o in offerings if o.id != keeper.id]

        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(
                f"Tenant {tenant_id} — {offering_type}: "
                f"{len(offerings)} offerings, keeping id={keeper.id}."
            )
        )
        self.stdout.write(
            f"  Candidates: {utils.describe_offering_candidates(offerings)}"
        )

        for duplicate in duplicates:
            resource_count = marketplace_models.Resource.objects.filter(
                offering=duplicate
            ).count()
            order_count = marketplace_models.Order.objects.filter(
                offering=duplicate
            ).count()
            empty = resource_count == 0 and order_count == 0

            if empty:
                self._delete_empty_duplicate(duplicate, apply_changes)
            elif merge:
                self._merge_duplicate(duplicate, keeper, apply_changes)
            else:
                self.stdout.write(
                    self.style.ERROR(
                        f"  SKIP duplicate id={duplicate.id}: still owns "
                        f"{resource_count} resource(s) and {order_count} order(s). "
                        f"Re-run with --apply --merge to re-point them onto the keeper."
                    )
                )

    def _delete_empty_duplicate(self, duplicate, apply_changes):
        if not apply_changes:
            self.stdout.write(
                f"  would delete empty duplicate id={duplicate.id} "
                f"(name={duplicate.name!r})"
            )
            return
        logger.info(
            "dedupe_tenant_offerings: deleting empty duplicate offering "
            "id=%s name=%r for tenant %s",
            duplicate.id,
            duplicate.name,
            duplicate.object_id,
        )
        duplicate.delete()
        self.stdout.write(
            self.style.SUCCESS(f"  deleted empty duplicate id={duplicate.id}")
        )

    def _merge_duplicate(self, duplicate, keeper, apply_changes):
        resources = marketplace_models.Resource.objects.filter(offering=duplicate)
        orders = marketplace_models.Order.objects.filter(offering=duplicate)
        resource_count = resources.count()
        order_count = orders.count()

        if not apply_changes:
            self.stdout.write(
                f"  would merge duplicate id={duplicate.id} into keeper "
                f"id={keeper.id}: re-point {resource_count} resource(s) and "
                f"{order_count} order(s), then delete."
            )
            return

        # Map each duplicate plan to a keeper plan with the same name so
        # re-pointed resources/orders keep a consistent offering<->plan pair.
        # Resource.plan / Order.plan are nullable, so an unmatched plan falls
        # back to null rather than leaving a dangling cross-offering reference.
        keeper_plans_by_name = {plan.name: plan for plan in keeper.plans.all()}

        def remap_plan(plan):
            if plan is None:
                return None
            return keeper_plans_by_name.get(plan.name)

        with transaction.atomic():
            for resource in resources.select_related("plan"):
                resource.offering = keeper
                resource.plan = remap_plan(resource.plan)
                resource.save(update_fields=["offering", "plan"])
            for order in orders.select_related("plan"):
                order.offering = keeper
                order.plan = remap_plan(order.plan)
                order.save(update_fields=["offering", "plan"])
            logger.info(
                "dedupe_tenant_offerings: merged duplicate offering id=%s into "
                "keeper id=%s for tenant %s (%s resources, %s orders re-pointed)",
                duplicate.id,
                keeper.id,
                duplicate.object_id,
                resource_count,
                order_count,
            )
            duplicate.delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"  merged duplicate id={duplicate.id} into keeper id={keeper.id} "
                f"({resource_count} resources, {order_count} orders re-pointed)"
            )
        )
