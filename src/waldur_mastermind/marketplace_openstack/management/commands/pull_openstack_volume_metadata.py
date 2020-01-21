from django.contrib.contenttypes.models import ContentType

from waldur_core.core.utils import DryRunCommand
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_openstack import utils
from waldur_openstack.openstack_tenant import models as tenant_models


class Command(DryRunCommand):
    help = """Pull OpenStack volumes metadata to marketplace."""

    def handle(self, dry_run, *args, **options):
        content_type = ContentType.objects.get_for_model(tenant_models.Volume)
        resources = marketplace_models.Resource.objects.filter(content_type=content_type).exclude(object_id=None)
        for resource in resources:
            if resource.scope:
                utils.import_volume_metadata(resource)
        self.stdout.write(self.style.SUCCESS('%s resources have been processed.' % resources.count()))
