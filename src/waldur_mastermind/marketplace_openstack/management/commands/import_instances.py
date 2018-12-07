from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_openstack.utils import create_missing_resources_for_instances


class Command(BaseCommand):
    help = 'Import existing OpenStack instances as marketplace resources.'

    def add_arguments(self, parser):
        parser.add_argument('--category', dest='category_uuid', required=True,
                            help='Specify a category to create offerings.')
        parser.add_argument('--dry_run', dest='dry_run', required=False,
                            help='Show what objects would be created.')

    def handle(self, category_uuid, customer_uuid, dry_run, *args, **options):
        try:
            category = marketplace_models.Category.objects.get(uuid=category_uuid)
        except ObjectDoesNotExist:
            raise CommandError('A category is not found.')

        create_missing_resources_for_instances(category, dry_run, self.stdout)
