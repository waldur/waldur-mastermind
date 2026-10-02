"""Create the POSIX project groups of projects that already use a provider.

Groups are created as projects get their first resource at a provider with
``project_groups_enabled``; projects that had resources before the switch was
turned on, or before the provider had a POSIX ID pool, are caught up here.
Groups without a GID get one; pinned GIDs are kept and skipped by the allocator.
"""

from django.core.management.base import BaseCommand

from waldur_mastermind.marketplace import models, project_groups


class Command(BaseCommand):
    help = (
        "Create POSIX project groups for projects that already have resources at "
        "a service provider with project groups enabled, and allocate missing "
        "GIDs."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--provider",
            dest="provider",
            action="append",
            default=None,
            help="Limit the run to the service provider with the given UUID. "
            "May be given multiple times.",
        )
        parser.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            default=False,
            help="Report what would be done without writing anything.",
        )

    def handle(self, *args, **options):
        provider_uuids = options.get("provider")
        if isinstance(provider_uuids, str):
            # call_command(..., provider="<uuid>") bypasses argparse.
            provider_uuids = [provider_uuids]
        providers = models.ServiceProvider.objects.select_related("customer")
        if provider_uuids:
            providers = providers.filter(uuid__in=provider_uuids)
        dry_run = options["dry_run"]

        for provider in providers.order_by("id"):
            if not provider.project_groups_enabled:
                if provider_uuids:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping {provider} ({provider.uuid.hex}): project "
                            "groups are not enabled."
                        )
                    )
                continue
            rows = project_groups.backfill(provider, dry_run=dry_run)
            self.stdout.write(
                f"{provider} ({provider.uuid.hex}): {len(rows)} change(s)"
            )
            for row in rows:
                gid = row["gid"] if row["gid"] is not None else "-"
                verb = {"create": "create", "allocate": "allocate GID for"}[
                    row["action"]
                ]
                self.stdout.write(
                    f"  {verb} {row['name']} (project {row['project'].uuid.hex}): "
                    f"GID {gid}"
                )
        if dry_run:
            self.stdout.write("Dry run: nothing was written.")
