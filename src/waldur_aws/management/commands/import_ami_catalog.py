from __future__ import unicode_literals

from django.core.management.base import BaseCommand, CommandError

from waldur_core.core.csv import UnicodeDictReader

from ... import models


class Command(BaseCommand):
    help_text = "Import catalog of Amazon images."

    def add_arguments(self, parser):
        parser.add_argument(
            'file',
            type=file,
            metavar='FILE',
            help='AMI catalog file.'
        )
        parser.add_argument(
            '-y', '--yes',
            action='store_true',
            dest='yes',
            default=False,
            help='The answer to any question which would be asked will be yes.'
        )

    def handle(self, *args, **options):
        data = list(UnicodeDictReader(options['file']))

        csv_regions = set([image['region'] for image in data])
        nc_regions = {region.name: region.id for region in models.Region.objects.all()}
        new_regions = csv_regions - set(nc_regions.keys())
        if new_regions:
            raise CommandError('%s regions are missing in the database.' % ', '.join(new_regions))

        csv_images = {image['backend_id']: image for image in data}
        csv_ids = set(csv_images.keys())

        nc_images = {image.backend_id: image for image in models.Image.objects.all()}
        nc_ids = set(nc_images.keys())

        new_ids = csv_ids - nc_ids
        if new_ids:
            new_ids_list = ', '.join(sorted(new_ids))
            self.stdout.write('The following AMIs would be created: {}.'.format(new_ids_list))

        common_ids = nc_ids & csv_ids
        updated_ids = set()
        for image_id in common_ids:
            csv_image = csv_images[image_id]
            nc_image = nc_images[image_id]
            csv_region = nc_regions.get(csv_image['region'])
            if nc_image.name != csv_image['name'] or nc_image.region_id != csv_region:
                updated_ids.add(image_id)

        if updated_ids:
            updated_ids_list = ', '.join(sorted(updated_ids))
            self.stdout.write('The following AMIs would be updated: {}'.format(updated_ids_list))

        stale_ids = nc_ids - csv_ids
        if stale_ids:
            stale_ids_list = ', '.join(sorted(stale_ids))
            self.stdout.write('The following AMIs would be deleted: {}'.format(stale_ids_list))

        if not new_ids and not stale_ids and not updated_ids:
            self.stdout.write('There are no changes to apply.')
            return

        if not options['yes']:
            confirm = raw_input('Enter [y] to continue: ')
            if confirm.strip().lower() != 'y':
                self.stdout.write('Changes are not applied.')
                return

        for image_id in new_ids:
            csv_image = csv_images[image_id]
            models.Image.objects.create(name=csv_image['name'],
                                        backend_id=csv_image['backend_id'],
                                        region_id=nc_regions.get(csv_image['region']))

        for image_id in updated_ids:
            csv_image = csv_images[image_id]
            models.Image.objects.filter(backend_id=image_id).update(
                name=csv_image['name'],
                region_id=nc_regions.get(csv_image['region']))

        models.Image.objects.filter(backend_id__in=stale_ids).delete()
        self.stdout.write('All changes are applied.')
