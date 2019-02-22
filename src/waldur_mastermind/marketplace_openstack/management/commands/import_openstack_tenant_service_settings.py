from django.core.management.base import CommandError

from waldur_core.core.utils import DryRunCommand
from waldur_mastermind.marketplace.models import Category
from waldur_mastermind.marketplace_openstack import utils


class Command(DryRunCommand):
    help = """Import OpenStack tenant service settings as marketplace offerings."""

    def handle(self, dry_run, *args, **options):
        try:
            offerings_counter, plans_counter = utils.import_openstack_tenant_service_settings(dry_run)
            self.stdout.write(self.style.SUCCESS('%s offerings have been created.' % offerings_counter))
            self.stdout.write(self.style.SUCCESS('%s plans have been created.' % plans_counter))
        except Category.DoesNotExist:
            raise CommandError('Please ensure that WALDUR_MARKETPLACE_OPENSTACK.INSTANCE_CATEGORY_UUID '
                               'and WALDUR_MARKETPLACE_OPENSTACK.VOLUME_CATEGORY_UUID'
                               'setting has valid value.')
