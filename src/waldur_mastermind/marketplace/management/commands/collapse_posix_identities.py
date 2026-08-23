"""Retrofit command: give a user one POSIX identity per pool.

Before identities became principal-scoped, a user with accounts on two offerings
of one service provider was allocated two UIDs from the same pool. Both accounts
resolve to the same username, the same DN and the same home directory, so the
provider's LDAP tree ends up with one entry whose ``uidNumber`` depends on
whichever site agent wrote last. This command picks one canonical identity per
``(pool, user)``, rewrites the other accounts onto it and prints the UID -> UID
and GID -> GID map so the operator can drive ``chown`` before flipping.

Dry run by default: nothing is written without ``--apply``.
"""

from django.core.management.base import BaseCommand

from waldur_core.core.models import User
from waldur_mastermind.marketplace import models, posix_ids, posix_maintenance

RECONCILE_HINT = {posix_ids.UID: "chown required", posix_ids.GID: "chgrp required"}


class Command(BaseCommand):
    help = (
        "Collapse per-offering POSIX identities of a user into one identity per "
        "POSIX ID pool. Dry run unless --apply is given."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--pool",
            dest="pool",
            action="append",
            default=None,
            help="Limit the run to the POSIX ID pool with the given UUID. "
            "May be given multiple times.",
        )
        parser.add_argument(
            "--apply",
            dest="apply",
            action="store_true",
            default=False,
            help="Perform the collapse. Without it the command only reports.",
        )

    def handle(self, *args, **options):
        pool_uuids = options.get("pool")
        if isinstance(pool_uuids, str):
            # call_command(..., pool="<uuid>") bypasses argparse.
            pool_uuids = [pool_uuids]
        pools = models.PosixIdPool.objects.all()
        if pool_uuids:
            pools = pools.filter(uuid__in=pool_uuids)

        plan = posix_maintenance.plan_collapse(pools)
        self.report(plan)
        if not plan["groups"]:
            return
        if not options["apply"]:
            self.stdout.write("")
            self.stdout.write("Re-run with --apply to perform the change.")
            return
        posix_maintenance.apply_collapse(plan)
        self.stdout.write(self.style.SUCCESS("Collapse applied."))

    def report(self, plan):
        if not plan["groups"]:
            self.stdout.write(
                "Nothing to collapse: every user already has one POSIX identity "
                "per pool."
            )
            return

        users = {}
        offering_users = set()
        for group in plan["groups"]:
            pool = group["pool"]
            user = self.describe_user(group["user_id"])
            users[group["user_id"]] = user
            self.stdout.write(f"Pool {pool.uuid.hex} ({pool})")
            canonical = group["canonical"]
            self.stdout.write(
                f"  user {user}   uid {canonical.uid} gid {canonical.gid}   <- canonical"
            )
            for change in group["changes"]:
                offering_users.add(change["offering_user_uuid"])
                hint = RECONCILE_HINT[change["namespace"]]
                self.stdout.write(
                    f"    {change['offering_name']}: {change['namespace']} "
                    f"{change['old_value']} -> {change['new_value']}   [{hint}]"
                )
            for warning in group["warnings"]:
                self.stdout.write(self.style.WARNING(f"    WARNING {warning}"))

        if plan["withheld"]:
            self.stdout.write("")
            self.stdout.write(
                "Values that will be freed but NOT recycled (return them to the "
                "pool from the POSIX identity admin once the filesystem has been "
                "reconciled):"
            )
            for namespace in ("uid", "gid"):
                values = sorted(
                    row["value"]
                    for row in plan["withheld"]
                    if row["namespace"] == namespace
                )
                if values:
                    joined = ", ".join(str(value) for value in values)
                    self.stdout.write(f"  {namespace}: {joined}")

        self.stdout.write("")
        self.stdout.write(
            f"{len(users)} user(s), {len(offering_users)} offering user(s) affected."
        )

    def describe_user(self, user_id):
        # all_objects: a leaver whose identity still needs collapsing is exactly
        # the case an operator wants named, and the default manager hides
        # deactivated users.
        user = User.all_objects.filter(id=user_id).first()
        return user.username if user is not None else str(user_id)
