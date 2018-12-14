from django.core.management.base import BaseCommand, CommandError

from waldur_mastermind.marketplace.models import Category
from waldur_mastermind.marketplace_openstack import utils


class Command(BaseCommand):
    help = """Import OpenStack tenant service settings as marketplace offerings."""

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Don\'t make any changes, instead show what objects would be created.')

    def handle(self, dry_run, *args, **options):
        try:
            offerings_counter, plans_counter = utils.import_openstack_tenant_service_settings(dry_run)
            self.stdout.write(self.style.SUCCESS('%s offerings have been created.' % offerings_counter))
            self.stdout.write(self.style.SUCCESS('%s plans have been created.' % plans_counter))
        except Category.DoesNotExist:
            raise CommandError('Please ensure that WALDUR_MARKETPLACE_OPENSTACK.INSTANCE_CATEGORY_UUID '
                               'and WALDUR_MARKETPLACE_OPENSTACK.VOLUME_CATEGORY_UUID'
                               'setting has valid value.')
