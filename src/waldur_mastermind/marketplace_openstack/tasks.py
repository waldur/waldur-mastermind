from celery import shared_task

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_openstack.utils import import_limits

States = marketplace_models.Resource.States


@shared_task(name='waldur_mastermind.marketplace_openstack.synchronize_limits')
def synchronize_limits(offering_uuid):
    offering = marketplace_models.Offering.objects.get(uuid=offering_uuid)

    resources = marketplace_models.Resource.objects\
        .filter(offering=offering)\
        .exclude(state__in=(States.TERMINATED, States.TERMINATING))

    for resource in resources:
        import_limits(resource)
