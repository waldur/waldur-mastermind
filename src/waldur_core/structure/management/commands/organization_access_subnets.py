import ipaddress
from itertools import groupby

from django.core.management.base import BaseCommand

from waldur_core.structure import models


class Command(BaseCommand):
    help = "Dumps information about organization access subnets, merging adjacent or overlapping networks."

    def get_merged_subnets(self):
        subnets_queryset = models.AccessSubnet.objects.all().values_list(
            "inet", flat=True
        )

        # Convert string representations to IPv4Network/IPv6Network objects
        networks = []
        for subnet in subnets_queryset:
            try:
                network = ipaddress.ip_network(subnet)
                networks.append(network)
            except ValueError as e:
                self.stderr.write(f"Error processing subnet {subnet}: {e}")

        # Sort networks by IP version and then by address
        networks.sort(key=lambda x: (x.version, x.network_address))

        # Group networks by IP version (IPv4 or IPv6)
        grouped_networks = {
            k: list(g) for k, g in groupby(networks, key=lambda x: x.version)
        }

        # Merge adjacent or overlapping networks using collapse_addresses
        merged_networks = []
        for version, version_networks in grouped_networks.items():
            merged_version_networks = list(
                ipaddress.collapse_addresses(version_networks)
            )
            merged_networks.extend(merged_version_networks)

        return merged_networks

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
