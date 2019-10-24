from csv import DictWriter

from django.core.management.base import BaseCommand, CommandError

from ... import models


class Command(BaseCommand):
    help_text = 'Export catalog of Amazon images.'
    args = '[ami_catalog.csv]'

    def handle(self, *args, **options):
        if len(args) == 0:
            raise CommandError('AMI catalog filename is not specified.')

        with open(args[0], 'w') as csvfile:
            writer = DictWriter(csvfile, fieldnames=('name', 'region', 'backend_id'))
            writer.writeheader()
            rows = [dict(name=image.name, region=image.region.name, backend_id=image.backend_id)
                    for image in models.Image.objects.all()]
            writer.writerows(rows)
