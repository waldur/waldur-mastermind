from django.core.management.base import BaseCommand

from waldur_core.core import utils as core_utils
from waldur_core.structure import models


class Command(BaseCommand):
    help = "Dumps information about organization access subnets, merging adjacent or overlapping networks."

    def get_merged_subnets(self):
        subnets = models.AccessSubnet.objects.exclude(inet__isnull=True).values_list(
            "inet", flat=True
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
