from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from waldur_core.core.enums import CoreStates
from waldur_core.structure.utils import RESOURCE_MISSING_MESSAGE
from waldur_openstack import models

RESOURCE_MODELS = (
    models.Tenant,
    models.Instance,
    models.Volume,
    models.Snapshot,
)


class Command(BaseCommand):
    help = (
        "List OpenStack resources which are marked as missing at the backend. "
        "Deletion is left to the operator: each resource is linked to a marketplace "
        "resource, invoice items and order history."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="Only report resources missing for at least this many days (default: 7).",
        )

    def handle(self, *args, **options):
        threshold = timezone.now() - timedelta(days=options["days"])
        total = 0
        untracked = 0
        self.stdout.write(
            "\t".join(
                ["TYPE", "UUID", "NAME", "CUSTOMER", "PROJECT", "BACKEND ID", "AGE"]
            )
        )

        for model in RESOURCE_MODELS:
            queryset = (
                model.objects.filter(
                    state=CoreStates.ERRED,
                    error_message__contains=RESOURCE_MISSING_MESSAGE,
                )
                # A resource which never got a backend id was not provisioned at
                # all, rather than lost, so it is not an operator's concern here.
                .exclude(backend_id="")
                .select_related("project__customer")
                .order_by("backend_missing_since", "id")
            )

            for resource in queryset:
                missing_since = resource.backend_missing_since
                if missing_since is None:
                    # Marked as missing before the timestamp was tracked.
                    untracked += 1
                elif missing_since > threshold:
                    continue
                else:
                    total += 1
                self.stdout.write(self.format_row(resource, missing_since))

        self.stdout.write("")
        self.stdout.write(
            f"{total} resource(s) missing for at least {options['days']} day(s)."
        )
        if untracked:
            self.stdout.write(
                f"{untracked} resource(s) were marked as missing before the "
                f"timestamp was tracked, their age is unknown."
            )

    def format_row(self, resource, missing_since):
        if missing_since is None:
            age = "unknown"
        else:
            age = f"{(timezone.now() - missing_since).days}d"
        return "\t".join(
            [
                resource.__class__.__name__,
                str(resource.uuid),
                resource.name,
                resource.project.customer.name,
                resource.project.name,
                resource.backend_id,
                age,
            ]
        )
