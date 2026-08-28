from django.core.management.base import BaseCommand
from rest_framework.authtoken.models import Token

from waldur_core.core.models import User
from waldur_core.passkeys.models import PasskeyVerifiedSession


class Command(BaseCommand):
    help = (
        "Delete API tokens held by staff and support accounts that were not "
        "issued behind a passkey. Part of turning on PASSKEY_ENFORCED_FOR_STAFF."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting anything.",
        )

    def handle(self, *args, **options):
        """Enforcement is retrospective or it is nothing.

        Every privileged token that already exists was issued without a
        passkey, so leaving them in place means the accounts enforcement is
        meant to protect keep working exactly as before — the setting would
        change nothing until each of those sessions happened to expire.

        This logs every affected staff member out. That is the cost of the
        switch meaning something on the day it is flipped, and it belongs in
        the release notes rather than in a surprise.
        """
        dry_run = options["dry_run"]

        # all_objects: a deactivated staff account still holds a token, and
        # leaving it behind is precisely the kind of forgotten credential this
        # command exists to clear.
        privileged = User.all_objects.filter(is_staff=True) | User.all_objects.filter(
            is_support=True
        )
        verified_token_ids = PasskeyVerifiedSession.objects.values_list(
            "token_id", flat=True
        )
        tokens = Token.objects.filter(user__in=privileged).exclude(
            pk__in=verified_token_ids
        )

        count = tokens.count()
        if not count:
            self.stdout.write(self.style.SUCCESS("No unverified privileged tokens."))
            return

        for token in tokens.select_related("user"):
            self.stdout.write(
                f"  {token.user.username} (staff={token.user.is_staff}, "
                f"support={token.user.is_support}, issued {token.created:%Y-%m-%d})"
            )

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"Would delete {count} token(s). Nothing changed.")
            )
            return

        tokens.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {count} token(s). Those users must sign in again, "
                "with a passkey."
            )
        )
