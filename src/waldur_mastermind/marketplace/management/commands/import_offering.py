import json
import os

from django.core.management.base import BaseCommand
from django.db.transaction import atomic

from waldur_core.core.utils import is_uuid_like
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.serializers import ExportImportOfferingSerializer


def _create_or_update_offering(serializer, data):
    serializer.is_valid(raise_exception=True)
    offering = serializer.save()

    thumbnail = data.get('thumbnail')

    if thumbnail:
        offering.thumbnail = thumbnail
        offering.save()

    return offering


def update_offering(offering, data):
    serializer = ExportImportOfferingSerializer(offering, data=data)
    return _create_or_update_offering(serializer, data)


def create_offering(data, customer):
    data['customer_id'] = customer.id
    serializer = ExportImportOfferingSerializer(data=data)
    return _create_or_update_offering(serializer, data)


class Command(BaseCommand):
    help = (
        'Import or update an offering in Waldur. '
        'You must define offering for updating or category and customer for creating.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '-p',
            '--path',
            dest='path',
            type=str,
            help='File path to offering data.',
            required=True,
        )
        parser.add_argument(
            '-c',
            '--customer',
            dest='customer',
            type=str,
            help='Customer UUID.',
            required=False,
        )
        parser.add_argument(
            '-ct',
            '--category',
            dest='category',
            type=str,
            help='Category UUID.',
            required=False,
        )
        parser.add_argument(
            '-o',
            '--offering',
            dest='offering',
            type=str,
            help='Updated offering UUID.',
            required=False,
        )

    def handle(self, *args, **options):
        customer_uuid = options.get('customer')
        offering_uuid = options.get('offering')
        category_uuid = options.get('category')
        path = options['path']

        if not os.path.exists(path):
            self.stdout.write(self.style.ERROR('File path does not exist.'))
            return
        else:
            with open(path, 'r') as f:
                data = json.load(f)

        if not offering_uuid and not customer_uuid:
            self.stdout.write(
                self.style.ERROR(
                    'You must define customer if you want to create an offering.'
                )
            )
            return

        if not offering_uuid and not category_uuid:
            self.stdout.write(
                self.style.ERROR(
                    'You must define category if you want to create an offering.'
                )
            )
            return

        if not offering_uuid and not category_uuid and not customer_uuid:
            self.stdout.write(
                self.style.ERROR(
                    'You must define offering for its updating or category and customer for an offering creating.'
                )
            )
            return

        def get_object(uuid, model, model_name):
            if uuid:
                if not is_uuid_like(uuid):
                    self.stdout.write(
                        self.style.ERROR('%s UUID is not valid.' % model_name)
                    )
                    return

                try:
                    return model.objects.get(uuid=uuid)
                except model.DoesNotExist:
                    self.stdout.write(
                        self.style.ERROR(
                            '%s with UUID: %s is not found.' % (model_name, uuid)
                        )
                    )

        offering = get_object(offering_uuid, models.Offering, 'Offering')
        customer = get_object(customer_uuid, structure_models.Customer, 'Customer')
        category = get_object(category_uuid, models.Category, 'Category')

        if category:
            data['category_id'] = category.id

        thumbnail = data.get('thumbnail')
        if thumbnail:
            data['thumbnail'] = os.path.join(os.path.dirname(path), thumbnail)

        with atomic():
            if offering:
                update_offering(offering, data)
                self.stdout.write(self.style.SUCCESS('An offering has been updated.'))
            else:
                create_offering(data, customer)
                self.stdout.write(self.style.SUCCESS('An offering has been imported.'))
