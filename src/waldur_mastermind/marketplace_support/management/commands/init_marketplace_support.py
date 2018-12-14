from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError
from waldur_core.structure import models as structure_models

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_support import utils


class Command(BaseCommand):
    help = 'Import existing support offerings as marketplace resources.'

    def add_arguments(self, parser):
        parser.add_argument('--category', dest='category_uuid', required=True,
                            help='Specify a category to create offerings.')
        parser.add_argument('--customer', dest='customer_uuid', required=True,
                            help='Specify a customer to create offerings.')

    def handle(self, category_uuid, customer_uuid, *args, **options):
        try:
            category = marketplace_models.Category.objects.get(uuid=category_uuid)
        except ObjectDoesNotExist:
            raise CommandError('A category is not found.')
        try:
            customer = structure_models.Customer.objects.get(uuid=customer_uuid)
        except ObjectDoesNotExist:
            raise CommandError('A customer is not found.')

        utils.init_offerings_and_resources(category, customer)
