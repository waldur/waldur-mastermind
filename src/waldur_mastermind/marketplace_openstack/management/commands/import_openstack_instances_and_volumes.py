from django.core.management.base import BaseCommand

from waldur_mastermind.marketplace_openstack import utils


class Command(BaseCommand):
    help = """Import OpenStack tenant resources as marketplace resources.
    It is expected that offerings for OpenStack tenant service settings are imported before this command is ran.
    """

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', dest='dry_run', required=False,
                            help='Don\'t make any changes, instead show what objects would be created.')

    def handle(self, dry_run, *args, **options):
        utils.import_openstack_instances_and_volumes(dry_run)
