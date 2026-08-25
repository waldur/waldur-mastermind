"""``scim_pull_user`` — fetch user attributes from a remote SCIM directory.

Examples::

    waldur scim_pull_user --username alice
    waldur scim_pull_user --all --rate 5
    waldur scim_pull_user --username alice --source scim:keycloak

Requires Constance settings ``SCIM_PULL_API_URL`` and ``SCIM_PULL_API_KEY``.
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

from waldur_core.core.models import User
from waldur_core.core.utils import chunked_queryset
from waldur_core.users.scim.pull.client import ScimError
from waldur_core.users.scim.pull.service import (
    ScimPullConfigError,
    build_pull_client,
    pull_user_attributes,
)


class Command(BaseCommand):
    help = "Pull user attributes from a remote SCIM 2.0 directory."

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument(
            "--username",
            help="Pull a single user identified by their Waldur username.",
        )
        target.add_argument(
            "--all",
            action="store_true",
            help="Pull every active user. Rate-limited (see --rate).",
        )
        parser.add_argument(
            "--rate",
            type=float,
            default=5.0,
            help="Maximum requests per second when using --all (default: 5).",
        )
        parser.add_argument(
            "--source",
            help="Override the source label written to attribute_sources. "
            "Defaults to the SCIM_PULL_SOURCE_NAME Constance setting.",
        )

    def handle(self, *args, **options):
        try:
            client = build_pull_client()
        except ScimPullConfigError as exc:
            raise CommandError(str(exc))

        source = options.get("source")

        if options.get("username"):
            user = self._get_user(options["username"])
            changed = self._pull_one(user, client, source)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Pulled {user.username}: changed fields = {sorted(changed) or 'none'}"
                )
            )
            return

        rate = max(options.get("rate") or 1.0, 0.1)
        interval = 1.0 / rate
        qs = User.objects.filter(is_active=True).order_by("id")
        total = qs.count()
        self.stdout.write(f"Pulling SCIM attributes for {total} active users...")
        ok = errors = 0
        # Client-side chunks: the loop sleeps and makes SCIM calls between
        # fetches, so a server-side cursor here would be left open across
        # transaction boundaries and lost by a transaction-mode pooler.
        for user in chunked_queryset(qs):
            try:
                self._pull_one(user, client, source)
                ok += 1
            except ScimError as exc:
                errors += 1
                self.stderr.write(f"SCIM pull failed for {user.username!r}: {exc}")
            time.sleep(interval)
        self.stdout.write(
            self.style.SUCCESS(
                f"SCIM pull complete: ok={ok}, errors={errors}, total={total}"
            )
        )

    def _get_user(self, username: str) -> User:
        try:
            return User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f"No user with username {username!r}.")

    def _pull_one(self, user: User, client, source: str | None) -> set[str]:
        return pull_user_attributes(user, client=client, source=source)
