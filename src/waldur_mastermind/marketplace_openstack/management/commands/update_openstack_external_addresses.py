from django.core.management.base import BaseCommand

from waldur_mastermind.marketplace_openstack import utils
from waldur_openstack import models as openstack_models


class Command(BaseCommand):
    help = """Recompute the mapped public IPs of all floating IPs and the
    addresses by which global search finds tenants and VMs. Run once after an
    upgrade, and whenever mapped addresses look out of date."""

    def handle(self, *args, **options):
        tenants = openstack_models.Tenant.objects.all()
        for tenant in tenants.iterator():
            utils.update_external_addresses_of_tenant(tenant)
        self.stdout.write(
            self.style.SUCCESS("%s tenants have been processed." % tenants.count())
        )
