from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError

from waldur_core.structure.models import Customer
from waldur_mastermind.marketplace_openstack import utils


class Command(BaseCommand):
    help = """Import OpenStack service settings as marketplace offerings."""

    def add_arguments(self, parser):
        parser.add_argument('--customer', dest='customer_uuid', required=True,
                            help='Default customer argument is used for shared service setting.')

        parser.add_argument('--dry_run', dest='dry_run', required=False,
                            help='Don\'t make any changes, instead show what objects would be created.')

    def handle(self, customer_uuid, dry_run, *args, **options):
        try:
            customer = Customer.objects.get(uuid=customer_uuid)
        except ObjectDoesNotExist:
            raise CommandError('A customer is not found.')

        utils.import_openstack_service_settings(customer, dry_run)
