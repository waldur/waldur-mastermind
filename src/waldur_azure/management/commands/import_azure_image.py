from django.core.management.base import BaseCommand

from waldur_azure.models import Image
from waldur_core.structure.models import ServiceSettings


class Command(BaseCommand):
    help = 'Import Azure image'

    def add_arguments(self, parser):
        parser.add_argument('--sku')
        parser.add_argument('--publisher')
        parser.add_argument('--offer')

    def handle(self, *args, **options):
        for settings in ServiceSettings.objects.filter(type='Azure'):
            Image.objects.update_or_create(
                settings=settings,
                name=options['offer'],
                sku=options['sku'],
                publisher=options['publisher'],
                version='latest',
            )
