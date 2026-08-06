from django.core.management.base import BaseCommand

from waldur_core.core import utils as core_utils
from waldur_core.structure import models


class Command(BaseCommand):
    help = (
        "Dumps the addresses allowed to sign in to the portal on behalf of an "
        "organization, merging adjacent or overlapping networks. Entries that "
        "apply only to resources of an offering are excluded — those are "
        "exported by resource_access_subnets."
    )

    def get_merged_subnets(self):
        # Only portal-scoped entries. The same table now also holds addresses
        # trusted purely for reaching resources; emitting those here would widen
        # the sign-in allow-list with addresses never meant to grant it.
        subnets = (
            models.AccessSubnet.objects.exclude(inet__isnull=True)
            .filter(applies_to_portal=True)
            .values_list("inet", flat=True)
        )
        return core_utils.merge_access_subnets(subnets)

    def add_arguments(self, parser):
        parser.add_argument(
            "-o",
            "--output",
            dest="output",
            default=None,
            help="Specifies file to which the merged subnets will be written. "
            "The output will be printed to stdout by default.",
        )

    def handle(self, *args, **options):
        merged_subnets = self.get_merged_subnets()

        if options["output"] is None:
            for subnet in merged_subnets:
                self.stdout.write(str(subnet))
        else:
            with open(options["output"], "w") as output_file:
                output_file.write("\n".join([str(s) for s in merged_subnets]))
