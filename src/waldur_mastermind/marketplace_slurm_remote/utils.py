import logging
import re
import unicodedata
from enum import Enum

from waldur_core.structure import models as structure_models
from waldur_freeipa import models as freeipa_models
from waldur_mastermind.marketplace import models as marketplace_models

logger = logging.getLogger(__name__)
USERNAME_ANONYMIZED_POSTFIX_LENGTH = 5
USERNAME_POSTFIX_LENGTH = 2


class UsernameGenerationPolicy(Enum):
    SERVICE_PROVIDER = (
        'service_provider'  # SP should manually submit username for the offering users
    )
    ANONYMIZED = 'anonymized'  # Usernames are generated with <prefix>_<number>, e.g. "anonym_00001".
    # The prefix must be specified in offering.plugin_options as "username_anonymized_prefix"
    FULL_NAME = 'full_name'  # Usernames are constructed using first and last name of users with numerical suffix, e.g. "john_doe_01"
    WALDUR_USERNAME = 'waldur_username'  # Using username field of User model
    FREEIPA = 'freeipa'  # Using username field of waldur_freeipa.Profile model


def sanitize_name(name):
    name = name.strip().lower()
    name = re.sub(r'\s+', '_', name)
    name = re.sub(r'\W+', '', name)
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()
    return name


def create_anonymized_username(offering):
    prefix = offering.plugin_options.get('username_anonymized_prefix', 'walduruser_')
    previous_users = marketplace_models.OfferingUser.objects.filter(
        offering=offering, username__istartswith=prefix
    ).order_by('username')

    if previous_users.exists():
        last_username = previous_users.last().username
        last_number = int(last_username[-USERNAME_ANONYMIZED_POSTFIX_LENGTH:])
        number = str(last_number + 1).zfill(USERNAME_ANONYMIZED_POSTFIX_LENGTH)
    else:
        number = '0'.zfill(USERNAME_ANONYMIZED_POSTFIX_LENGTH)

    return f"{prefix}{number}"


def create_username_from_full_name(user, offering):
    first_name = sanitize_name(user.first_name)
    last_name = sanitize_name(user.last_name)

    username_raw = f"{first_name}_{last_name}"
    previous_users = marketplace_models.OfferingUser.objects.filter(
        offering=offering, username__istartswith=username_raw
    ).order_by('username')

    if previous_users.exists():
        last_username = previous_users.last().username
        last_number = int(last_username[-USERNAME_POSTFIX_LENGTH:])
        number = str(last_number + 1).zfill(USERNAME_POSTFIX_LENGTH)
    else:
        number = '0'.zfill(USERNAME_POSTFIX_LENGTH)

    return f"{username_raw}_{number}"


def create_username_from_freeipa_profile(user):
    profiles = freeipa_models.Profile.objects.filter(user=user)
    if profiles.count() == 0:
        logger.warning('There is no FreeIPA profile for user %s', user)
        return ''
    else:
        return profiles.first().username


def generate_username(user, offering):
    username_generation_policy = offering.plugin_options.get(
        'username_generation_policy', UsernameGenerationPolicy.SERVICE_PROVIDER.value
    )

    if username_generation_policy == UsernameGenerationPolicy.SERVICE_PROVIDER.value:
        return ''

    if username_generation_policy == UsernameGenerationPolicy.ANONYMIZED.value:
        return create_anonymized_username(offering)

    if username_generation_policy == UsernameGenerationPolicy.FULL_NAME.value:
        return create_username_from_full_name(user, offering)

    if username_generation_policy == UsernameGenerationPolicy.WALDUR_USERNAME.value:
        return user.username

    if username_generation_policy == UsernameGenerationPolicy.FREEIPA.value:
        return create_username_from_freeipa_profile(user)

    return ''


def user_offerings_mapping(offerings):
    resources = marketplace_models.Resource.objects.filter(
        state=marketplace_models.Resource.States.OK, offering__in=offerings
    )
    resource_ids = resources.values_list('id', flat=True)

    project_ids = resources.values_list('project_id', flat=True)
    projects = structure_models.Project.objects.filter(id__in=project_ids)

    user_offerings_set = set()

    for project in projects:
        users = project.get_users()

        project_resources = project.resource_set.filter(id__in=resource_ids)
        project_offering_ids = project_resources.values_list('offering_id', flat=True)
        project_offerings = marketplace_models.Offering.objects.filter(
            id__in=project_offering_ids
        )

        for user in users:
            for offering in project_offerings:
                user_offerings_set.add((user, offering))

    for user, offering in user_offerings_set:
        if not marketplace_models.OfferingUser.objects.filter(
            user=user, offering=user
        ).exists():
            username = generate_username(user, offering)
            offering_user = marketplace_models.OfferingUser.objects.create(
                user=user, offering=offering, username=username
            )
            offering_user.set_propagation_date()
            offering_user.save()
            logger.info('Offering user %s has been created.')
