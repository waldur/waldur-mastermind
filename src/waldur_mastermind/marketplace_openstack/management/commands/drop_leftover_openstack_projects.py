from django.core.exceptions import MultipleObjectsReturned
from django.core.management.base import BaseCommand

from waldur_mastermind.marketplace.models import Offering, Resource
from waldur_openstack.openstack_base.session import get_keystone_client


class Command(BaseCommand):
    help = """
    Drop leftover projects from remote OpenStack deployment.
    Leftovers are resources marked as terminated in Waldur but still present in the remote OpenStack.
    Such inconsistency may be caused by split brain problem in the distributed database.
    """

    def add_arguments(self, parser):
        parser.add_argument(
            "--offering",
            help="Target marketplace offering name where leftover projects are located.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Don't make any changes, instead show what projects would be deleted.",
        )
        parser.add_argument(
            "--fuzzy-matching",
            action="store_true",
            help="Try to detect leftovers by name.",
        )

    def collect_leftovers_by_id(self, offering, remote_projects):
        remote_project_ids = {project.id for project in remote_projects}
        local_project_ids = set(
            Resource.objects.filter(offering=offering, state=Resource.States.TERMINATED)
            .exclude(backend_id="")
            .values_list("backend_id", flat=True)
        )
        return local_project_ids & remote_project_ids

    def collect_leftovers_by_name(
        self,
        offering,
        remote_projects,
    ):
        # Some resources do not have backend_id so we use name instead
        # Since name can be reused names of existing resources are filtered out
        local_resources = Resource.objects.filter(
            offering=offering, state=Resource.States.TERMINATED
        )
        local_project_names = set(local_resources.values_list("name", flat=True)) - set(
            Resource.objects.filter(
                offering=offering, state=Resource.States.OK
            ).values_list("name", flat=True)
        )
        leftovers = set()
        remote_project_names = {project.name for project in remote_projects}
        remote_project_ids = {project.id for project in remote_projects}
        for project_name in local_project_names & remote_project_names:
            try:
                resource = local_resources.get(name=project_name)
            except MultipleObjectsReturned:
                pass
            else:
                if not resource.backend_id:
                    pass
                else:
                    leftovers.add(resource.backend_id)
        return leftovers & remote_project_ids

    def handle(self, *args, **options):
        try:
            offering = Offering.objects.get(name=options["offering"])
        except Offering.DoesNotExist:
            self.stdout.write(
                self.style.ERROR("Offering with given name is not found.")
            )
            return

        backend = offering.scope.get_backend()
        if not backend.ping():
            self.stdout.write(
                self.style.ERROR("Remote OpenStack API does not respond.")
            )
            return

        keystone = get_keystone_client(backend.session)
        remote_projects = keystone.projects.list()

        leftovers = self.collect_leftovers_by_id(offering, remote_projects)
        if options["fuzzy_matching"]:
            leftovers |= self.collect_leftovers_by_name(offering, remote_projects)

        if leftovers:
            self.stdout.write(
                "Projects with following IDs are going to be deleted %s"
                % ", ".join(leftovers)
            )
            if not options["dry_run"]:
                for project_id in leftovers:
                    keystone.projects.delete(project_id)
