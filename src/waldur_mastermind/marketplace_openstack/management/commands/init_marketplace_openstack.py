from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ObjectDoesNotExist

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_openstack import utils


class Command(BaseCommand):
    help = 'Init marketplace offerings for provisioning OpenStack virtual machines and volumes.'

    def add_arguments(self, parser):
        parser.add_argument('-c', '--category', dest='category_uuid', required=True)
        parser.add_argument('-t', '--tenants', dest='tenants', nargs='+', type=str, required=False)

    def handle(self, category_uuid, *args, **options):
        try:
            category = marketplace_models.Category.objects.get(uuid=category_uuid)
        except ObjectDoesNotExist:
            raise CommandError('A category is not found.')

        utils.create_missing_offerings(category, options['tenants'])
