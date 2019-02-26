from waldur_core.core.utils import DryRunCommand
from waldur_mastermind.marketplace_openstack import utils


class Command(DryRunCommand):
    help = """Import OpenStack tenants as marketplace resources.
    It is expected that offerings for OpenStack service settings are imported before this command is ran.
    """

    def handle(self, dry_run, *args, **options):
        resource_counter = utils.import_openstack_tenants(dry_run)
        self.stdout.write(self.style.SUCCESS('%s resources have been created.' % resource_counter))
