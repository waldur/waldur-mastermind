from waldur_core.core.utils import DryRunCommand
from waldur_mastermind.marketplace_openstack import utils


class Command(DryRunCommand):
    help = """Import OpenStack tenant resources as marketplace resources.
    It is expected that offerings for OpenStack tenant service settings are imported before this command is ran.
    """

    def handle(self, dry_run, *args, **options):
        resources_counter = utils.import_openstack_instances_and_volumes(dry_run)
        self.stdout.write(self.style.SUCCESS('%s resources have been created.' % resources_counter))
