from collections import defaultdict

from django.core.management.base import BaseCommand

from waldur_mastermind.marketplace import models, utils
from waldur_mastermind.marketplace.enums import ServiceAccountState


class Command(BaseCommand):
    help = (
        "List open project and customer service accounts whose username is "
        "invalid or shared with another open service account, and those closed "
        "in Waldur only. Read-only."
    )

    def handle(self, *args, **options):
        claimed = defaultdict(list)
        invalid = []
        waldur_only = []
        for model in (models.ProjectServiceAccount, models.CustomerServiceAccount):
            waldur_only.extend(
                self.describe(account)
                for account in model.objects.filter(
                    state=ServiceAccountState.CLOSED,
                    error_message__startswith=utils.CLOSED_IN_WALDUR_ONLY,
                )
            )
            accounts = model.objects.exclude(state=ServiceAccountState.CLOSED)
            for account in accounts:
                row = self.describe(account)
                if not account.username:
                    continue
                try:
                    utils.validate_account_username(account.username)
                except utils.InvalidAccountUsername:
                    invalid.append(row)
                    continue
                claimed[account.username].append(row)

        conflicts = {name: rows for name, rows in claimed.items() if len(rows) > 1}

        self.stdout.write(f"Invalid usernames: {len(invalid)}")
        for row in invalid:
            self.stdout.write(f"  {row}")
        self.stdout.write(
            f"Usernames claimed by more than one account: {len(conflicts)}"
        )
        for name, rows in sorted(conflicts.items()):
            self.stdout.write(f"  {name!r}:")
            for row in rows:
                self.stdout.write(f"    {row}")
        self.stdout.write(f"Closed in Waldur only: {len(waldur_only)}")
        for row in waldur_only:
            self.stdout.write(f"  {row}")
        if invalid or conflicts or waldur_only:
            self.stdout.write(
                "Invalid usernames are closed in Waldur only when the account is "
                "next closed. Close, update and rotate are refused for shared "
                "usernames: compare each account with the backend's record of "
                "that username, and remove the row that doesn't own it. An "
                "account closed in Waldur only may still be open at the backend: "
                "close it there by hand, then delete its row."
            )

    @staticmethod
    def describe(account) -> str:
        scope = getattr(account, "project", None) or getattr(account, "customer", None)
        return (
            f"{type(account).__name__} {account.uuid.hex} "
            f"username={account.username!r} scope={scope} "
            f"state={account.get_state_display()} modified={account.modified:%Y-%m-%d}"
        )
