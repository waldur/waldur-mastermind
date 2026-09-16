from constance import config
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from waldur_core.users.scim.server.exceptions import ScimError
from waldur_sram import models, rules
from waldur_sram.views import apply_group


class Command(BaseCommand):
    help = (
        "Re-apply the last SCIM payload SRAM pushed for every group, e.g. after "
        "upgrading or changing SRAM settings. SRAM itself only re-sends changed "
        "groups."
    )

    def handle(self, *args, **options):
        if not config.SRAM_INTEGRATION_ENABLED:
            raise CommandError(
                "SRAM_INTEGRATION_ENABLED is off: SRAM data is frozen. Enable it first."
            )
        failed = 0
        groups = models.SramGroup.objects.order_by("id")
        for group in groups:
            try:
                with transaction.atomic():
                    apply_group(group, group.payload)
            except ScimError as exc:
                failed += 1
                self.stderr.write(f"{group}: {exc.detail}")
        orphans = rules.revoke_orphans()
        self.stdout.write(
            f"Re-applied {groups.count() - failed} SRAM groups, {failed} failed."
        )
        if orphans:
            self.stdout.write(f"Revoked {orphans} grants of deleted rules or groups.")
