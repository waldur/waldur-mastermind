from django.core.management.base import BaseCommand

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.utils import MoveResourceException, move_resource


class Command(BaseCommand):
    help = "Move a marketplace resource to a different project."

    def add_arguments(self, parser):
        parser.add_argument(
            '-p',
            '--project',
            dest='project_uuid',
            required=True,
            help='Target project UUID',
        )
        parser.add_argument(
            '-r',
            '--resource',
            dest='resource_uuid',
            required=True,
            help='UUID of a marketplace resource to move.',
        )

    def handle(self, project_uuid, resource_uuid, *args, **options):
        try:
            project = structure_models.Project.objects.get(uuid=project_uuid)
        except structure_models.Project.DoesNotExist:
            self.stdout.write(self.style.ERROR('Project is not found.'))
            return
        except ValueError:
            self.stdout.write(self.style.ERROR('Project UUID is not valid.'))
            return

        try:
            resource = marketplace_models.Resource.objects.get(uuid=resource_uuid)
        except marketplace_models.Resource.DoesNotExist:
            self.stdout.write(self.style.ERROR('Resource is not found.'))
            return
        except ValueError:
            self.stdout.write(self.style.ERROR('Resource UUID is not valid.'))
            return

        try:
            move_resource(resource, project)
            self.stdout.write(
                self.style.SUCCESS('Resource has been moved to another project.')
            )
        except MoveResourceException as e:
            self.stdout.write(self.style.ERROR(e))
