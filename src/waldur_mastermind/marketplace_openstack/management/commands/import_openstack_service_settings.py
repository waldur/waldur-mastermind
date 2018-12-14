from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError

from waldur_core.structure.models import Customer
from waldur_mastermind.marketplace.models import Category
from waldur_mastermind.marketplace_openstack import utils


class Command(BaseCommand):
    help = """Import OpenStack service settings as marketplace offerings."""

    def add_arguments(self, parser):
        parser.add_argument('--customer', dest='customer_uuid', required=True,
                            help='Default customer argument is used for shared service setting.')

        parser.add_argument('--dry-run', action='store_true',
                            help='Don\'t make any changes, instead show what objects would be created.')

    def handle(self, customer_uuid, dry_run, *args, **options):
        try:
            customer = Customer.objects.get(uuid=customer_uuid)
        except ObjectDoesNotExist:
            raise CommandError('A customer is not found.')

        try:
            offerings_counter, plans_counter = utils.import_openstack_service_settings(customer, dry_run)
            self.stdout.write(self.style.SUCCESS('%s offerings have been created.' % offerings_counter))
            self.stdout.write(self.style.SUCCESS('%s plans have been created.' % plans_counter))
        except Category.DoesNotExist:
            raise CommandError('Please ensure that WALDUR_MARKETPLACE_OPENSTACK.TENANT_CATEGORY_UUID '
                               'setting has valid value.')
