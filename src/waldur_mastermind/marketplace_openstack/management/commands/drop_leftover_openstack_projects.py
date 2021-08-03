from django.core.exceptions import MultipleObjectsReturned
from django.core.management.base import BaseCommand

from waldur_mastermind.marketplace.models import Offering, Resource


class Command(BaseCommand):
    help = """
    Drop leftover projects from remote OpenStack deployment.
    Leftovers are resources marked as terminated in Waldur but still present in the remote OpenStack.
    Such inconsistency may be caused by split brain problem in the distributed database.
    """

    def add_arguments(self, parser):
        parser.add_argument(
            '--offering',
            help='Target marketplace offering name where leftover projects are located.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Don\'t make any changes, instead show what projects would be deleted.',
        )

    def handle(self, *args, **options):
        try:
            offering = Offering.objects.get(name=options['offering'])
        except Offering.DoesNotExist:
            self.stdout.write(
                self.style.ERROR('Offering with given name is not found.')
            )
            return

        backend = offering.scope.get_backend()
        if not backend.ping():
            self.stdout.write(
                self.style.ERROR('Remote OpenStack API does not respond.')
            )
            return

        local_resources = Resource.objects.filter(
            offering=offering, state=Resource.States.TERMINATED
        )
        keystone = backend.keystone_admin_client
        remote_projects = keystone.projects.list()

        # First let's check projects by their IDs since they are guaranteed to be unique
        remote_project_ids = {project.id for project in remote_projects}
        local_project_ids = set(
            local_resources.exclude(backend_id='').values_list('backend_id', flat=True)
        )
        leftovers = local_project_ids & remote_project_ids
        if leftovers:
            self.stdout.write(
                'Projects with following IDs are going to be deleted %s'
                % ', '.join(leftovers)
            )
            if not options['dry_run']:
                for project_id in leftovers:
                    keystone.projects.delete(project_id)

        # Some resources do not have backend_id so let's use name instead
        # since project names are unique to their domain
        local_project_names = set(
            local_resources.exclude(name='').values_list('name', flat=True)
        )
        remote_project_names = {project.name for project in remote_projects}
        leftovers = local_project_names & remote_project_names
        if leftovers:
            self.stdout.write(
                'Projects with following names are going to be deleted %s'
                % ', '.join(leftovers)
            )
            if not options['dry_run']:
                for project_name in leftovers:
                    try:
                        resource = local_resources.get(name=project_name)
                    except MultipleObjectsReturned:
                        self.stdout.write(
                            self.style.ERROR(
                                'Skipping deletion of resource because its name is not unique: %s.'
                                % project_name
                            )
                        )
                    else:
                        if not resource.backend_id:
                            self.stdout.write(
                                self.style.ERROR(
                                    'Skipping deletion of resource because it does not have backend_id: %s.'
                                    % project_name
                                )
                            )
                        else:
                            keystone.projects.delete(resource.backend_id)
