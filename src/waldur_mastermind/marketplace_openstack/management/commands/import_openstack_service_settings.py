from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import CommandError

from waldur_core.core.utils import DryRunCommand
from waldur_core.structure.models import Customer
from waldur_mastermind.marketplace.models import Category
from waldur_mastermind.marketplace_openstack import utils


class Command(DryRunCommand):
    help = """Import OpenStack service settings as marketplace offerings."""

    def add_arguments(self, parser):
        super(Command, self).add_arguments(parser)
        parser.add_argument('--customer', dest='customer_uuid', required=True,
                            help='Default customer argument is used for shared service setting.')

        parser.add_argument('--require-templates', action='store_true',
                            help='Skip service settings without package template.')

    def handle(self, customer_uuid, dry_run, require_templates, *args, **options):
        try:
            customer = Customer.objects.get(uuid=customer_uuid)
        except ObjectDoesNotExist:
            raise CommandError('A customer is not found.')

        try:
            offerings_counter, plans_counter = utils.import_openstack_service_settings(
                customer, dry_run, require_templates)
            self.stdout.write(self.style.SUCCESS('%s offerings have been created.' % offerings_counter))
            self.stdout.write(self.style.SUCCESS('%s plans have been created.' % plans_counter))
        except Category.DoesNotExist:
            raise CommandError('Please ensure that WALDUR_MARKETPLACE_OPENSTACK.TENANT_CATEGORY_UUID '
                               'setting has valid value.')
