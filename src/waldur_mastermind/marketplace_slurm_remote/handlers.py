import logging

from django.core import exceptions as django_exceptions
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.utils import timezone

from waldur_core.core.utils import month_start
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME, utils

logger = logging.getLogger(__name__)

COMPONENT_FIELDS = {
    'cpu_usage',
    'gpu_usage',
    'ram_usage',
    'cpu_limit',
    'gpu_limit',
    'ram_limit',
}


def update_component_quota(sender, instance, created=False, **kwargs):
    if created:
        return

    if not set(instance.tracker.changed()) & COMPONENT_FIELDS:
        return

    allocation = instance

    try:
        resource = marketplace_models.Resource.objects.get(scope=allocation)
    except django_exceptions.ObjectDoesNotExist:
        return

    for component in manager.get_components(PLUGIN_NAME):
        usage = getattr(allocation, component.type + '_usage')
        limit = getattr(allocation, component.type + '_limit')

        try:
            offering_component = marketplace_models.OfferingComponent.objects.get(
                offering=resource.offering, type=component.type
            )
        except marketplace_models.OfferingComponent.DoesNotExist:
            logger.warning(
                'Skipping Allocation synchronization because this '
                'marketplace.OfferingComponent does not exist.'
                'Allocation ID: %s',
                allocation.id,
            )
        else:
            marketplace_models.ComponentQuota.objects.update_or_create(
                resource=resource,
                component=offering_component,
                defaults={'limit': limit, 'usage': usage},
            )
            try:
                plan_period = marketplace_models.ResourcePlanPeriod.objects.get(
                    resource=resource, end=None
                )
            except (ObjectDoesNotExist, MultipleObjectsReturned):
                logger.warning(
                    'Skipping component usage synchronization because valid'
                    'ResourcePlanPeriod is not found.'
                    'Allocation ID: %s',
                    allocation.id,
                )
            else:
                date = timezone.now()
                marketplace_models.ComponentUsage.objects.update_or_create(
                    resource=resource,
                    component=offering_component,
                    billing_period=month_start(date),
                    plan_period=plan_period,
                    defaults={'usage': usage, 'date': date},
                )


def terminate_allocation_when_resource_is_terminated(sender, instance, **kwargs):
    resource: marketplace_models.Resource = instance
    if resource.offering.type != PLUGIN_NAME:
        return

    allocation = resource.scope
    allocation.begin_deleting()
    allocation.save(update_fields=['state'])


def create_offering_users_when_project_role_granted(sender, structure, user, **kwargs):
    project = structure
    resources = project.resource_set.filter(
        state=marketplace_models.Resource.States.OK, offering__type=PLUGIN_NAME
    )
    offering_ids = set(resources.values_list('offering_id', flat=True))
    offerings = marketplace_models.Offering.objects.filter(id__in=offering_ids)

    for offering in offerings:
        if not offering.secret_options.get('service_provider_can_create_offering_user'):
            logger.info(
                'It is not allowed to create users for current offering %s.', offering
            )
            continue

        if marketplace_models.OfferingUser.objects.filter(
            offering=offering,
            user=user,
        ).exists():
            logger.info('An offering user for %s in %s already exists', user, offering)
            continue

        username = utils.generate_username(user, offering)

        offering_user = marketplace_models.OfferingUser.objects.create(
            offering=offering,
            user=user,
            username=username,
        )

        (
            uidnumber,
            primarygroup,
        ) = marketplace_utils.generate_uidnumber_and_primary_group(offering)
        offering_user.backend_metadata.update(
            {
                'uidnumber': uidnumber,
                'primarygroup': primarygroup,
                'loginShell': "/bin/sh",
                'homeDir': f"/home/{offering_user.username}",
            }
        )

        offering_user.save(update_fields=['backend_metadata'])


def create_offering_user_for_new_resource(sender, instance, **kwargs):
    resource: marketplace_models.Resource = instance
    project = resource.project
    users = project.get_users()
    offering = resource.offering
    if not offering.secret_options.get('service_provider_can_create_offering_user'):
        logger.info(
            'It is not allowed to create users for current offering %s.', offering
        )
        return

    for user in users:
        if marketplace_models.OfferingUser.objects.filter(
            offering=offering,
            user=user,
        ).exists():
            logger.info('An offering user for %s in %s already exists', user, offering)
            continue

        username = utils.generate_username(user, offering)

        offering_user = marketplace_models.OfferingUser.objects.create(
            offering=offering,
            user=user,
            username=username,
        )

        offering_user.set_propagation_date()

        (
            uidnumber,
            primarygroup,
        ) = marketplace_utils.generate_uidnumber_and_primary_group(offering)
        offering_user.backend_metadata.update(
            {
                'uidnumber': uidnumber,
                'primarygroup': primarygroup,
                'loginShell': "/bin/sh",
                'homeDir': f"/home/{offering_user.username}",
            }
        )

        offering_user.save(update_fields=['propagation_date', 'backend_metadata'])

        logger.info('The offering user %s has been created', offering_user)


def update_offering_user_username_after_offering_settings_change(
    sender, instance, created=False, **kwargs
):
    if created:
        return

    offering = instance

    if offering.type != PLUGIN_NAME or not offering.tracker.has_changed(
        'plugin_options'
    ):
        return

    offering_users = marketplace_models.OfferingUser.objects.filter(offering=offering)

    for offering_user in offering_users:
        new_username = utils.generate_username(offering_user.user, offering)
        logger.info('New username for %s is %s', offering_user, new_username)
        offering_user.username = new_username
        offering_user.save(update_fields=['username'])
