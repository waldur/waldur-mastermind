from django.core.management.base import BaseCommand

from waldur_mastermind.marketplace_openstack import utils


class Command(BaseCommand):
    help = """Import OpenStack tenant service settings as marketplace offerings."""

    def add_arguments(self, parser):
        parser.add_argument('--dry_run', dest='dry_run', required=False,
                            help='Don\'t make any changes, instead show what objects would be created.')

    def handle(self, dry_run, *args, **options):
        utils.import_openstack_tenant_service_settings(dry_run)
