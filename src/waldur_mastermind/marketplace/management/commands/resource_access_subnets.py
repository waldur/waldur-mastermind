from django.core.management.base import BaseCommand

from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import models


class Command(BaseCommand):
    help = (
        "Dumps per-resource access subnets for consumption by external firewalls, "
        "merging adjacent or overlapping networks. Only resources of offerings that "
        "opt in via the enable_resource_access_subnets plugin option have subnets."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "-r",
            "--offering",
            dest="offering",
            default=None,
            help="Limit the dump to resources of the offering with the given UUID.",
        )
        parser.add_argument(
            "-o",
            "--output",
            dest="output",
            default=None,
            help="Specifies file to which the merged subnets will be written. "
            "The output will be printed to stdout by default.",
        )

    def get_merged_subnets(self, offering_uuid=None):
        queryset = models.ResourceAccessSubnet.objects.exclude(inet__isnull=True)
        if offering_uuid:
            queryset = queryset.filter(resource__offering__uuid=offering_uuid)
        return core_utils.merge_access_subnets(queryset.values_list("inet", flat=True))

    def handle(self, *args, **options):
        merged_subnets = self.get_merged_subnets(options.get("offering"))

        if options["output"] is None:
            for subnet in merged_subnets:
                self.stdout.write(str(subnet))
        else:
            with open(options["output"], "w") as output_file:
                output_file.write("\n".join([str(s) for s in merged_subnets]))
