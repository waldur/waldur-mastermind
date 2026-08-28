from django.core.management.base import BaseCommand
from rest_framework.authtoken.models import Token

from waldur_core.core.models import PersonalAccessToken, User
from waldur_core.passkeys.models import PasskeyVerifiedSession


class Command(BaseCommand):
    help = (
        "Delete API tokens held by staff and support accounts that were not "
        "issued behind a passkey, and report the personal access tokens that "
        "predate enforcement. Part of turning on PASSKEY_ENFORCED_FOR_STAFF."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting anything.",
        )
        parser.add_argument(
            "--revoke-personal-access-tokens",
            action="store_true",
            help=(
                "Also revoke personal access tokens held by staff and support "
                "accounts. Off by default: these typically drive CI and "
                "automation, so revoking them without warning breaks "
                "pipelines rather than merely logging somebody out."
            ),
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

        revoke_pats = options["revoke_personal_access_tokens"]

        count = tokens.count()
        if not count:
            self.stdout.write(self.style.SUCCESS("No unverified privileged tokens."))
            self.handle_personal_access_tokens(privileged, dry_run, revoke_pats)
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
            self.handle_personal_access_tokens(privileged, dry_run, revoke_pats)
            return

        tokens.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {count} token(s). Those users must sign in again, "
                "with a passkey."
            )
        )
        self.handle_personal_access_tokens(privileged, dry_run, revoke_pats)

    def handle_personal_access_tokens(self, privileged, dry_run, revoke):
        """Personal access tokens predating enforcement are the same problem.

        A staff PAT scoped STAFF.ACCESS is a long-lived privileged credential
        minted before the policy existed, exactly like the session tokens
        above. Creating a new one now requires a passkey-verified session, but
        the ones already issued keep working until they expire.

        They are reported rather than revoked by default. Unlike a browser
        session, a PAT usually drives CI, so deleting it without warning takes
        pipelines down — a much larger blast radius than a forced re-login,
        and a decision about timing that belongs to the operator.
        """
        pats = PersonalAccessToken.objects.filter(
            user__in=privileged, is_active=True
        ).select_related("user")

        count = pats.count()
        if not count:
            self.stdout.write(
                self.style.SUCCESS("No active privileged personal access tokens.")
            )
            return

        self.stdout.write("")
        self.stdout.write(
            f"{count} active personal access token(s) held by staff or support:"
        )
        for pat in pats:
            self.stdout.write(
                f"  {pat.user.username}: {pat.name} ({pat.token_prefix}..., "
                f"scopes={pat.scopes or 'unrestricted'}, "
                f"expires {pat.expires_at:%Y-%m-%d})"
            )

        if not revoke:
            self.stdout.write(
                self.style.WARNING(
                    "These predate enforcement and keep working without a "
                    "passkey until they expire. Re-run with "
                    "--revoke-personal-access-tokens to revoke them, once you "
                    "have checked what depends on them."
                )
            )
            return

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Would revoke {count} personal access token(s). Nothing changed."
                )
            )
            return

        pats.update(is_active=False)
        self.stdout.write(
            self.style.SUCCESS(
                f"Revoked {count} personal access token(s). Anything using "
                "them — CI, scripts, integrations — stops working now."
            )
        )
