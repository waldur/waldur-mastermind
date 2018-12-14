from django.core.management.base import BaseCommand

from waldur_mastermind.marketplace_openstack import utils


class Command(BaseCommand):
    help = """Import OpenStack tenants as marketplace resources.
    It is expected that offerings for OpenStack service settings are imported before this command is ran.
    """

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Don\'t make any changes, instead show what objects would be created.')

    def handle(self, dry_run, *args, **options):
        resource_counter = utils.import_openstack_tenants(dry_run)
        self.stdout.write(self.style.SUCCESS('%s resources have been created.' % resource_counter))
