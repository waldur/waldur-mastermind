from django.contrib.contenttypes.models import ContentType

from waldur_core.core.utils import DryRunCommand
from waldur_mastermind.marketplace.models import Resource
from waldur_mastermind.marketplace_openstack import utils
from waldur_openstack.openstack.models import Tenant


class Command(DryRunCommand):
    help = """Push OpenStack tenant quotas from marketplace to backend."""

    def handle(self, dry_run, *args, **options):
        ct = ContentType.objects.get_for_model(Tenant)
        for resource in Resource.objects.filter(content_type=ct).exclude(
            state__in=(
                Resource.States.TERMINATED,
                Resource.States.TERMINATING,
            )
        ):
            utils.push_tenant_limits(resource)
