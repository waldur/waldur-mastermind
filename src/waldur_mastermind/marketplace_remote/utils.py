import logging
from collections import defaultdict

from django.utils import dateparse
from rest_framework.exceptions import ValidationError
from waldur_client import WaldurClient, WaldurClientException

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models

from . import PLUGIN_NAME

logger = logging.getLogger(__name__)

INVALID_RESOURCE_STATES = (
    marketplace_models.Resource.States.CREATING,
    marketplace_models.Resource.States.TERMINATED,
)


def get_client_for_offering(offering):
    options = offering.secret_options
    api_url = options['api_url']
    token = options['token']
    return WaldurClient(api_url, token)


def pull_fields(fields, local_object, remote_object):
    changed_fields = set()
    for field in fields:
        if remote_object[field] != getattr(local_object, field):
            setattr(local_object, field, remote_object[field])
            changed_fields.add(field)
    if changed_fields:
        local_object.save(update_fields=changed_fields)


def get_remote_offerings_for_project(project):
    offering_ids = (
        marketplace_models.Resource.objects.filter(
            project=project,
            offering__type=PLUGIN_NAME,
            offering__state=marketplace_models.Offering.States.ACTIVE,
        )
        .exclude(state__in=INVALID_RESOURCE_STATES)
        .values_list('offering', flat=True)
        .distinct()
    )
    return marketplace_models.Offering.objects.filter(pk__in=offering_ids)


def get_projects_with_remote_offerings():
    pairs = (
        marketplace_models.Resource.objects.filter(offering__type=PLUGIN_NAME)
        .exclude(state__in=INVALID_RESOURCE_STATES)
        .values('offering', 'project')
        .distinct()
    )
    projects_with_offerings = defaultdict(set)
    for pair in pairs:
        project = structure_models.Project.objects.get(pk=pair['project'])
        offering = marketplace_models.Offering.objects.get(pk=pair['offering'])
        projects_with_offerings[project].add(offering)
    return projects_with_offerings


def get_or_create_remote_project(offering, project, client=None):
    if not client:
        client = get_client_for_offering(offering)
    options = offering.secret_options
    remote_customer_uuid = options['customer_uuid']
    remote_project_name = f'{project.customer.name} / {project.name}'
    remote_project_uuid = f'{project.customer.uuid}_{project.uuid}'
    remote_projects = client.list_projects(
        query_params={'backend_id': remote_project_uuid}
    )
    if len(remote_projects) == 0:
        response = client.create_project(
            customer_uuid=remote_customer_uuid,
            name=remote_project_name,
            backend_id=remote_project_uuid,
        )
        return response, True
    elif len(remote_projects) == 1:
        return remote_projects[0], False
    else:
        raise ValidationError('There are multiple projects in remote Waldur.')


def create_or_update_project_permission(
    client, remote_project_uuid, remote_user_uuid, role, expiration_time
):
    permissions = client.get_project_permissions(
        remote_project_uuid, remote_user_uuid, role
    )
    if not permissions:
        return client.create_project_permission(
            remote_user_uuid,
            remote_project_uuid,
            role,
            expiration_time.isoformat() if expiration_time else expiration_time,
        )
    permission = permissions[0]
    old_expiration_time = (
        dateparse.parse_datetime(permission['expiration_time'])
        if permission['expiration_time']
        else permission['expiration_time']
    )
    if old_expiration_time != expiration_time:
        return client.update_project_permission(
            permission['pk'],
            expiration_time.isoformat() if expiration_time else expiration_time,
        )


def remove_project_permission(client, remote_project_uuid, remote_user_uuid, role):
    remote_permissions = client.get_project_permissions(
        remote_project_uuid, remote_user_uuid, role
    )
    if remote_permissions:
        client.remove_project_permission(remote_permissions[0]['pk'])
        return True
    return False


def sync_project_permission(grant, project, role, user, expiration_time):
    for offering in get_remote_offerings_for_project(project):
        client = get_client_for_offering(offering)
        try:
            remote_user_uuid = client.get_remote_eduteams_user(user.username)['uuid']
        except WaldurClientException as e:
            logger.debug(
                f'Unable to fetch remote user {user.username} in offering {offering}: {e}'
            )
            continue

        try:
            remote_project, _ = get_or_create_remote_project(offering, project, client)
            remote_project_uuid = remote_project['uuid']
        except WaldurClientException as e:
            logger.debug(
                f'Unable to create remote project {project} in offering {offering}: {e}'
            )
            continue

        if grant:
            try:
                create_or_update_project_permission(
                    client, remote_project_uuid, remote_user_uuid, role, expiration_time
                )
            except WaldurClientException as e:
                logger.debug(
                    f'Unable to create permission for user [{remote_user_uuid}] with role {role} (until {expiration_time}) '
                    f'and project [{remote_project_uuid}] in offering [{offering}]: {e}'
                )
        else:
            try:
                remove_project_permission(
                    client, remote_project_uuid, remote_user_uuid, role
                )
            except WaldurClientException as e:
                logger.debug(
                    f'Unable to remove permission for user [{remote_user_uuid}] with role {role} '
                    f'and project [{remote_project_uuid}] in offering [{offering}]: {e}'
                )


def push_project_users(offering, project, remote_project_uuid):
    client = get_client_for_offering(offering)

    permissions = collect_local_user_roles(project)

    for username, roles in permissions.items():
        try:
            remote_user_uuid = client.get_remote_eduteams_user(username)['uuid']
        except WaldurClientException as e:
            logger.debug(
                f'Unable to fetch remote user {username} in offering {offering}: {e}'
            )
            continue

        for role, expiration_time in roles:
            try:
                create_or_update_project_permission(
                    client, remote_project_uuid, remote_user_uuid, role, expiration_time
                )
            except WaldurClientException as e:
                logger.debug(
                    f'Unable to create permission for user [{remote_user_uuid}] with role {role} '
                    f'and project [{remote_project_uuid}] in offering [{offering}]: {e}'
                )


def collect_local_user_roles(project):
    permissions = defaultdict(set)
    for permission in structure_models.ProjectPermission.objects.filter(
        project=project, is_active=True,
    ):
        permissions[permission.user.username].add(
            (permission.role, permission.expiration_time)
        )
    for permission in structure_models.CustomerPermission.objects.filter(
        customer=project.customer,
        is_active=True,
        role=structure_models.CustomerRole.OWNER,
    ):
        # Organization owner is mapped to project manager in remote Waldur
        permissions[permission.user.username].add(
            (structure_models.ProjectRole.MANAGER, permission.expiration_time)
        )
    return permissions
