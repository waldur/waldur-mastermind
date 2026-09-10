"""Re-apply auto-provisioning rules to existing users.

Rules are normally evaluated when identity data arrives — at OIDC login or a
SCIM pull. After an administrator edits a rule, that leaves existing users out
of step until each of them logs in again. This command closes that window.

Examples::

    # Preview what a rule change would do to one user
    waldur reconcile_autoprovisioned_roles --username alice --dry-run

    # Apply to everyone
    waldur reconcile_autoprovisioned_roles --all
"""

import time

from django.core.management.base import BaseCommand, CommandError

from waldur_autoprovisioning.reconciliation import reconcile_autoprovisioned_roles
from waldur_core.core.models import User
from waldur_core.core.utils import chunked_queryset


class Command(BaseCommand):
    help = "Re-apply auto-provisioning rules, granting and revoking roles."

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument(
            "--username",
            help="Reconcile a single user identified by their Waldur username.",
        )
        target.add_argument(
            "--all",
            action="store_true",
            help="Reconcile every active user.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )
        parser.add_argument(
            "--rate",
            type=float,
            default=0,
            help="Users per second when using --all. 0 (default) means no limit.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        if options["username"]:
            try:
                users = [User.objects.get(username=options["username"])]
            except User.DoesNotExist:
                raise CommandError(f"No user with username {options['username']!r}.")
        else:
            users = chunked_queryset(User.objects.filter(is_active=True).order_by("id"))

        rate = options["rate"]
        changed = 0
        seen = 0
        for user in users:
            seen += 1
            result = reconcile_autoprovisioned_roles(user, dry_run=dry_run)
            if result["granted"] or result["revoked"]:
                changed += 1
                self.stdout.write(
                    f"{user.username}: "
                    f"granted {result['granted'] or 'nothing'}, "
                    f"revoked {result['revoked'] or 'nothing'}"
                )
            if rate:
                time.sleep(1.0 / rate)

        verb = "would change" if dry_run else "changed"
        self.stdout.write(
            self.style.SUCCESS(f"Reconciled {seen} user(s); {verb} {changed}.")
        )
