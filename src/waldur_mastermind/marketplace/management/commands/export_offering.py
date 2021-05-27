import copy
import json
import os

from django.core.management.base import BaseCommand

from waldur_core.core.utils import is_uuid_like
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.serializers import ExportImportOfferingSerializer


def export_offering(offering, path):
    serializer = ExportImportOfferingSerializer(offering)
    data = copy.copy(serializer.data)

    if offering.thumbnail:
        filename, file_extension = os.path.splitext(offering.thumbnail.file.name)
        pic_path = os.path.join(path, offering.uuid.hex + file_extension)

        with open(pic_path, 'wb') as pic_res:
            pic_res.write(offering.thumbnail.file.file.read())

        data['thumbnail'] = os.path.basename(pic_path)
    else:
        pic_path = None

    json_path = os.path.join(path, offering.uuid.hex + '.json')

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    return json_path, pic_path


class Command(BaseCommand):
    help = (
        'Export an offering from Waldur. '
        'Export data includes JSON file with an offering data and a thumbnail. '
        'Names of this files include offering ID.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '-o',
            '--offering',
            dest='offering',
            type=str,
            help='An offering UUID.',
            required=True,
        )
        parser.add_argument(
            '-p',
            '--path',
            dest='path',
            type=str,
            help='Path to the folder where the export data will be saved.',
            required=True,
        )

    def handle(self, *args, **options):
        offering_uuid = options['offering']
        path = options['path']

        if not is_uuid_like(offering_uuid):
            self.stdout.write(self.style.ERROR('Offering UUID is not valid.'))
            return

        if not os.path.exists(path):
            self.stdout.write(self.style.ERROR('The file path does not exist.'))
            return

        try:
            offering = models.Offering.objects.get(uuid=offering_uuid)
        except models.Offering.DoesNotExist:
            self.stdout.write(
                self.style.ERROR('Offering with UUID: %s is not found.' % offering_uuid)
            )
            return

        json_path, pic_path = export_offering(offering, path)
        self.stdout.write(
            self.style.SUCCESS('Offering data has been exported to %s.' % json_path)
        )

        if pic_path:
            self.stdout.write(
                self.style.SUCCESS(
                    'Offering thumbnail has been exported to %s.' % pic_path
                )
            )
