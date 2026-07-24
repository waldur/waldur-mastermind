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
  orders, plans, billing periods and usage records onto the keeper inside a
  transaction before deleting it.

The merge itself lives in ``utils.merge_duplicate_offering`` so the staff API
action and this command cannot drift apart.
"""

import logging

from django.core.management.base import BaseCommand

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
            plan = utils.plan_offering_merge(duplicate, keeper)

            if plan["is_empty"]:
                self._delete_empty_duplicate(duplicate, apply_changes)
            elif merge:
                self._merge_duplicate(duplicate, keeper, plan, apply_changes)
            else:
                self.stdout.write(
                    self.style.ERROR(
                        f"  SKIP duplicate id={duplicate.id}: still owns "
                        f"{plan['resource_count']} resource(s) and "
                        f"{plan['order_count']} order(s). "
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
        try:
            utils.delete_empty_duplicate_offering(duplicate)
        except utils.OfferingMergeBlocked as e:
            self.stdout.write(
                self.style.ERROR(f"  SKIP duplicate id={duplicate.id}: {e}")
            )
            return
        self.stdout.write(
            self.style.SUCCESS(f"  deleted empty duplicate id={duplicate.id}")
        )

    def _merge_duplicate(self, duplicate, keeper, plan, apply_changes):
        if plan["blockers"]:
            self.stdout.write(
                self.style.ERROR(
                    f"  SKIP duplicate id={duplicate.id}: cannot merge without "
                    f"losing history — {'; '.join(plan['blockers'])}"
                )
            )
            return

        if not apply_changes:
            self.stdout.write(
                f"  would merge duplicate id={duplicate.id} into keeper "
                f"id={keeper.id}: re-point {plan['resource_count']} resource(s), "
                f"{plan['order_count']} order(s), {plan['plan_period_count']} billing "
                f"period(s), {plan['component_usage_count']} usage(s) and "
                f"{plan['component_quota_count']} quota(s), then delete."
            )
            return

        try:
            result = utils.merge_duplicate_offering(duplicate, keeper)
        except utils.OfferingMergeBlocked as e:
            self.stdout.write(
                self.style.ERROR(f"  SKIP duplicate id={duplicate.id}: {e}")
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"  merged duplicate id={duplicate.id} into keeper id={keeper.id} "
                f"({result['resource_count']} resources, {result['order_count']} orders, "
                f"{result['plan_period_count']} billing periods, "
                f"{result['component_usage_count']} usages re-pointed)"
            )
        )
