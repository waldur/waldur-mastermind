from django.core.management.base import BaseCommand

from waldur_mastermind.marketplace import utils


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
            action="append",
            default=None,
            help="Limit the dump to resources of the offering with the given UUID. "
            "May be given multiple times.",
        )
        parser.add_argument(
            "--include-organization-subnets",
            dest="include_organization_subnets",
            action="store_true",
            default=False,
            help="Also merge in the organization-level access subnets of customers "
            "owning non-terminated resources of the selected offerings.",
        )
        parser.add_argument(
            "-o",
            "--output",
            dest="output",
            default=None,
            help="Specifies file to which the merged subnets will be written. "
            "The output will be printed to stdout by default.",
        )

    def get_merged_subnets(
        self, offering_uuids=None, include_organization_subnets=False
    ):
        return utils.aggregate_access_subnets(
            offering_uuids=offering_uuids,
            include_organization_subnets=include_organization_subnets,
        )["packed"]

    def handle(self, *args, **options):
        offering_uuids = options.get("offering")
        # call_command(..., offering="<uuid>") bypasses argparse, so the append
        # action never runs and a plain string arrives here.
        if isinstance(offering_uuids, str):
            offering_uuids = [offering_uuids]
        merged_subnets = self.get_merged_subnets(
            offering_uuids=offering_uuids,
            include_organization_subnets=options["include_organization_subnets"],
        )

        if options["output"] is None:
            for subnet in merged_subnets:
                self.stdout.write(str(subnet))
        else:
            with open(options["output"], "w") as output_file:
                output_file.write("\n".join([str(s) for s in merged_subnets]))
