import collections
import logging

from celery.app import shared_task
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.utils import dateparse, timezone
from waldur_client import WaldurClient, WaldurClientException

from waldur_core.core.utils import deserialize_instance
from waldur_core.structure import models as structure_models
from waldur_core.structure.tasks import BackgroundListPullTask, BackgroundPullTask
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.registrators import RegistrationManager
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.callbacks import sync_order_item_state
from waldur_mastermind.marketplace.utils import create_local_resource
from waldur_mastermind.marketplace_remote.constants import OFFERING_FIELDS
from waldur_mastermind.marketplace_remote.utils import (
    get_client_for_offering,
    pull_fields,
    sync_project_permission,
)

from . import PLUGIN_NAME, utils

logger = logging.getLogger(__name__)

OrderItemInvertStates = {key: val for val, key in models.OrderItem.States.CHOICES}


class OfferingPullTask(BackgroundPullTask):
    def pull(self, local_offering):
        client = get_client_for_offering(local_offering)
        remote_offering = client.get_marketplace_offering(local_offering.backend_id)
        pull_fields(OFFERING_FIELDS, local_offering, remote_offering)


class OfferingListPullTask(BackgroundListPullTask):
    name = 'waldur_mastermind.marketplace_remote.pull_offerings'
    pull_task = OfferingPullTask

    def get_pulled_objects(self):
        return models.Offering.objects.filter(type=PLUGIN_NAME)


class OfferingUserPullTask(BackgroundPullTask):
    def pull(self, local_offering):
        client = get_client_for_offering(local_offering)
        remote_offering_users = {
            remote_offering_user['user_username']: remote_offering_user['username']
            for remote_offering_user in client.list_remote_offering_users(
                {'offering_uuid': local_offering.backend_id}
            )
        }
        local_offering_users = {
            offering_user.user.username: offering_user.username
            for offering_user in models.OfferingUser.objects.filter(
                offering=local_offering
            )
        }
        usernames = set(remote_offering_users.values()) | set(
            local_offering_users.keys()
        )
        user_map = {
            user.username: user
            for user in models.User.objects.filter(username__in=usernames)
        }

        missing = set(remote_offering_users.keys()) - set(local_offering_users.keys())
        for local_username in missing:
            user = user_map[local_username]
            models.OfferingUser.objects.create(
                user=user,
                offering=local_offering,
                username=remote_offering_users[local_username],
            )

        stale = set(local_offering_users.keys()) - set(remote_offering_users.keys())
        for local_username in stale:
            user = user_map[local_username]
            offering_user = models.OfferingUser.objects.get(
                user=user, offering=local_offering
            )
            offering_user.delete()

        common = set(local_offering_users.keys()) & set(remote_offering_users.keys())
        for local_username in common:
            remote_username = remote_offering_users[local_username]
            if local_offering_users[local_username] == remote_username:
                continue
            user = user_map[local_username]
            offering_user = models.OfferingUser.objects.get(
                user=user, offering=local_offering
            )
            offering_user.username = remote_username
            offering_user.save(update_fields=['username'])


class OfferingUserListPullTask(BackgroundListPullTask):
    name = 'waldur_mastermind.marketplace_remote.pull_offering_users'
    pull_task = OfferingUserPullTask

    def get_pulled_objects(self):
        return models.Offering.objects.filter(type=PLUGIN_NAME)


class ResourcePullTask(BackgroundPullTask):
    def pull(self, local_resource):
        client = get_client_for_offering(local_resource.offering)
        remote_resource = client.get_marketplace_resource(local_resource.backend_id)
        pull_fields(
            ['report',], local_resource, remote_resource,
        )


class ResourceListPullTask(BackgroundListPullTask):
    name = 'waldur_mastermind.marketplace_remote.pull_resources'
    pull_task = ResourcePullTask

    def get_pulled_objects(self):
        return models.Resource.objects.filter(offering__type=PLUGIN_NAME).exclude(
            backend_id=''
        )


class OrderItemPullTask(BackgroundPullTask):
    def pull(self, local_order_item):
        if not local_order_item.backend_id:
            return
        client = get_client_for_offering(local_order_item.offering)
        remote_order = client.get_order(local_order_item.backend_id)
        remote_order_item = remote_order['items'][0]

        if remote_order_item['state'] != local_order_item.get_state_display():
            new_state = OrderItemInvertStates[remote_order_item['state']]
            if (
                not local_order_item.resource
                and local_order_item.type == models.OrderItem.Types.CREATE
            ):
                resource_uuid = remote_order_item.get('marketplace_resource_uuid')
                if resource_uuid:
                    create_local_resource(local_order_item, resource_uuid)
            sync_order_item_state(local_order_item, new_state)
        pull_fields(('error_message',), local_order_item, remote_order_item)


class OrderItemStatePullTask(OrderItemPullTask):
    def pull(self, local_order_item):
        super(OrderItemStatePullTask, self).pull(local_order_item)
        local_order_item.refresh_from_db()
        if local_order_item.state not in models.OrderItem.States.TERMINAL_STATES:
            self.retry()


class OrderItemListPullTask(BackgroundListPullTask):
    name = 'waldur_mastermind.marketplace_remote.pull_order_items'
    pull_task = OrderItemPullTask

    def get_pulled_objects(self):
        return (
            models.OrderItem.objects.filter(offering__type=PLUGIN_NAME)
            .exclude(state__in=models.OrderItem.States.TERMINAL_STATES)
            .exclude(backend_id='')
        )


class UsagePullTask(BackgroundPullTask):
    def pull(self, local_resource: models.Resource):
        client = get_client_for_offering(local_resource.offering)

        remote_usages = client.list_component_usages(local_resource.backend_id)

        for remote_usage in remote_usages:
            try:
                offering_component = models.OfferingComponent.objects.get(
                    offering=local_resource.offering, type=remote_usage['type']
                )
            except ObjectDoesNotExist:
                continue
            defaults = {
                'usage': remote_usage['usage'],
                'description': remote_usage['description'],
                'created': remote_usage['created'],
                'date': remote_usage['date'],
                'billing_period': remote_usage['billing_period'],
            }
            models.ComponentUsage.objects.update_or_create(
                resource=local_resource,
                backend_id=remote_usage['uuid'],
                component=offering_component,
                defaults=defaults,
            )


class UsageListPullTask(BackgroundListPullTask):
    name = 'waldur_mastermind.marketplace_remote.pull_usage'
    pull_task = UsagePullTask

    def get_pulled_objects(self):
        return models.Resource.objects.filter(offering__type=PLUGIN_NAME)


class ResourceInvoicePullTask(BackgroundPullTask):
    def pull(self, local_resource: models.Resource):
        client = get_client_for_offering(local_resource.offering)
        remote_customer_uuid = local_resource.offering.secret_options['customer_uuid']
        local_customer = local_resource.project.customer
        now = timezone.now()

        try:
            invoice = client.get_invoice_for_customer(
                remote_customer_uuid, now.year, now.month
            )
        except WaldurClientException as e:
            logger.info(
                f'Unable to get remote invoice for customer [id={remote_customer_uuid}]: {e}'
            )
            return

        # TODO: drop this in favor of backend filtering: https://opennode.atlassian.net/browse/WAL-4268
        remote_invoice_items = [
            item
            for item in invoice['items']
            if item['resource_uuid'] == local_resource.backend_id
        ]

        local_invoice, _ = RegistrationManager.get_or_create_invoice(
            local_customer, now
        )
        local_invoice_items = local_invoice.items.filter(resource=local_resource)
        local_item_names = set([item.name for item in local_invoice_items])
        remote_item_names = set([item['name'] for item in remote_invoice_items])
        new_item_names = remote_item_names - local_item_names
        stale_item_names = local_item_names - remote_item_names
        existing_item_names = local_item_names & remote_item_names

        if len(stale_item_names) > 0:
            invoice_models.InvoiceItem.objects.filter(
                name__in=stale_item_names
            ).delete()
            logger.info(
                f'The following invoice items for resource [uuid={local_resource.uuid}] have been deleted: {stale_item_names}'
            )

        new_invoice_items = [
            item for item in remote_invoice_items if item['name'] in new_item_names
        ]
        for item in new_invoice_items:
            invoice_models.InvoiceItem.objects.create(
                resource=local_resource,
                invoice=local_invoice,
                start=dateparse.parse_datetime(item['start']),
                end=dateparse.parse_datetime(item['end']),
                name=item['name'],
                project=local_resource.project,
                unit=item['unit'],
                measured_unit=item['measured_unit'],
                article_code=item['article_code'],
                unit_price=item['unit_price'],
                details=item['details'],
                quantity=item['quantity'],
            )

        existing_invoice_items = [
            item for item in remote_invoice_items if item['name'] in existing_item_names
        ]
        for item in existing_invoice_items:
            local_item = local_invoice_items.get(name=item['name'],)
            local_item.start = dateparse.parse_datetime(item['start'])
            local_item.end = dateparse.parse_datetime(item['end'])
            local_item.measured_unit = item['measured_unit']
            local_item.details = item['details']
            local_item.quantity = item['quantity']
            local_item.article_code = item['article_code']
            local_item.unit_price = item['unit_price']
            local_item.unit = item['unit']
            local_item.save(
                update_fields=[
                    'start',
                    'end',
                    'measured_unit',
                    'details',
                    'quantity',
                    'article_code',
                    'unit_price',
                    'unit',
                ]
            )


class ResourceInvoiceListPullTask(BackgroundListPullTask):
    name = 'waldur_mastermind.marketplace_remote.pull_invoices'
    pull_task = ResourceInvoicePullTask

    def get_pulled_objects(self):
        return models.Resource.objects.filter(offering__type=PLUGIN_NAME)


@shared_task(
    name='waldur_mastermind.marketplace_remote.update_remote_project_permissions'
)
def update_remote_project_permissions(
    serialized_project, serialized_user, role, grant=True, expiration_time=None
):
    project = deserialize_instance(serialized_project)
    user = deserialize_instance(serialized_user)
    new_expiration_time = (
        dateparse.parse_datetime(expiration_time)
        if expiration_time
        else expiration_time
    )

    sync_project_permission(grant, project, role, user, new_expiration_time)


@shared_task(
    name='waldur_mastermind.marketplace_remote.update_remote_customer_permissions'
)
def update_remote_customer_permissions(
    serialized_customer, serialized_user, role, grant=True, expiration_time=None
):
    customer = deserialize_instance(serialized_customer)
    user = deserialize_instance(serialized_user)
    new_expiration_time = (
        dateparse.parse_datetime(expiration_time)
        if expiration_time
        else expiration_time
    )

    for project in customer.projects.all():
        sync_project_permission(grant, project, role, user, new_expiration_time)


@shared_task(
    name='waldur_mastermind.marketplace_remote.sync_remote_project_permissions'
)
def sync_remote_project_permissions():
    if not settings.WALDUR_AUTH_SOCIAL['ENABLE_EDUTEAMS_SYNC']:
        return

    for project, offerings in utils.get_projects_with_remote_offerings().items():

        for offering in offerings:

            local_permissions = utils.collect_local_permissions(offering, project)
            if not local_permissions:
                continue

            client = utils.get_client_for_offering(offering)
            try:
                remote_project, created = utils.get_or_create_remote_project(
                    offering, project, client
                )
                remote_project_uuid = remote_project['uuid']
            except WaldurClientException as e:
                logger.debug(
                    f'Unable to create remote project {project} in offering {offering}: {e}'
                )
                continue

            if created:
                utils.push_project_users(offering, project, remote_project_uuid)
                continue

            try:
                remote_permissions = client.get_project_permissions(remote_project_uuid)
            except WaldurClientException as e:
                logger.debug(
                    f'Unable to get project permissions for project {project} in offering {offering}: {e}'
                )
                continue

            remote_user_roles = collections.defaultdict()
            for remote_permission in remote_permissions:
                remote_expiration_time = remote_permission['expiration_time']
                remote_user_roles[remote_permission['user_username']] = (
                    remote_permission['role'],
                    dateparse.parse_datetime(remote_expiration_time)
                    if remote_expiration_time
                    else remote_expiration_time,
                )

            for username, (new_role, new_expiration_time) in local_permissions.items():
                try:
                    remote_user_uuid = client.get_remote_eduteams_user(username)['uuid']
                except WaldurClientException as e:
                    logger.debug(
                        f'Unable to fetch remote user {username} in offering {offering}: {e}'
                    )
                    continue

                if username not in remote_user_roles:
                    try:
                        client.create_project_permission(
                            remote_user_uuid,
                            remote_project_uuid,
                            new_role,
                            new_expiration_time.isoformat()
                            if new_expiration_time
                            else new_expiration_time,
                        )
                    except WaldurClientException as e:
                        logger.debug(
                            f'Unable to create permission for user [{remote_user_uuid}] with role {new_role} (until {new_expiration_time}) '
                            f'and project [{remote_project_uuid}] in offering [{offering}]: {e}'
                        )
                    continue

                old_role, old_expiration_time = remote_user_roles[username]

                old_permission_id = None
                for permission in remote_permissions:
                    if permission['role'] == old_role:
                        old_permission_id = str(permission['pk'])

                if not old_permission_id:
                    continue

                if old_role != new_role:
                    try:
                        client.remove_project_permission(old_permission_id)
                    except WaldurClientException as e:
                        logger.debug(
                            f'Unable to remove permission for user [{remote_user_uuid}] with role {old_role} '
                            f'and project [{remote_project_uuid}] in offering [{offering}]: {e}'
                        )
                    try:
                        client.create_project_permission(
                            remote_user_uuid,
                            remote_project_uuid,
                            new_role,
                            new_expiration_time.isoformat()
                            if new_expiration_time
                            else new_expiration_time,
                        )
                    except WaldurClientException as e:
                        logger.debug(
                            f'Unable to create permission for user [{remote_user_uuid}] with role {new_role} (until {new_expiration_time}) '
                            f'and project [{remote_project_uuid}] in offering [{offering}]: {e}'
                        )
                    continue

                if old_expiration_time != new_expiration_time:
                    try:
                        client.update_project_permission(
                            old_permission_id,
                            new_expiration_time.isoformat()
                            if new_expiration_time
                            else new_expiration_time,
                        )
                    except WaldurClientException as e:
                        logger.debug(
                            f'Unable to update permission for user [{remote_user_uuid}] with role {old_role} (until {new_expiration_time}) '
                            f'and project [{remote_project_uuid}] in offering [{offering}]: {e}'
                        )


@shared_task
def sync_remote_project(serialized_request):
    if not settings.WALDUR_AUTH_SOCIAL['ENABLE_EDUTEAMS_SYNC']:
        return
    request = deserialize_instance(serialized_request)
    try:
        utils.update_remote_project(request)
    except WaldurClientException:
        logger.exception(
            f'Unable to update remote project {request.project} in offering {request.offering}'
        )


@shared_task
def delete_remote_project(serialized_project):
    model_name, pk = serialized_project.split(':')
    local_project = structure_models.Project.all_objects.get(pk=pk)
    backend_id = utils.get_project_backend_id(local_project)
    offering_ids = (
        models.Resource.objects.filter(
            project=local_project, offering__type=PLUGIN_NAME,
        )
        .values_list('offering_id', flat=True)
        .distinct()
    )
    offerings = models.Offering.objects.filter(pk__in=offering_ids)
    clients = {}

    for offering in offerings:
        if (
            'api_url' not in offering.secret_options.keys()
            or 'token' not in offering.secret_options.keys()
        ):
            continue

        clients[offering.secret_options['api_url']] = offering.secret_options['token']

    for api_url, token in clients.items():
        client = WaldurClient(api_url, token)

        try:
            remote_project = client.list_projects({'backend_id': backend_id})

            if len(remote_project) != 1:
                continue

        except WaldurClientException as e:
            logger.debug(
                f'Unable to get remote project (backend_id: {backend_id}): {e}'
            )
            continue

        try:
            client.delete_project(remote_project[0]['uuid'])
        except WaldurClientException as e:
            logger.debug(
                f'Unable to delete remote project {remote_project[0]["uuid"]} (api_url: {api_url}): {e}'
            )
            continue


@shared_task
def clean_remote_projects():
    clients = {}
    projects_backend_ids = set(
        map(
            lambda project: utils.get_project_backend_id(project),
            structure_models.Project.all_objects.filter(is_removed=True),
        )
    )

    for offering in models.Offering.objects.filter(
        type=PLUGIN_NAME,
        state__in=(models.Offering.States.ACTIVE, models.Offering.States.PAUSED),
    ):
        if (
            'api_url' not in offering.secret_options.keys()
            or 'token' not in offering.secret_options.keys()
        ):
            continue

        clients[offering.secret_options['api_url']] = offering.secret_options['token']

    for api_url, token in clients.items():
        client = WaldurClient(api_url, token)

        try:
            remote_projects = client.list_projects()
        except WaldurClientException as e:
            logger.debug(f'Unable to get remote projects (api_url: {api_url}): {e}')
            continue

        for remote_project in remote_projects:
            if remote_project['backend_id'] in projects_backend_ids:
                try:
                    client.delete_project(remote_project['uuid'])
                except WaldurClientException as e:
                    logger.debug(
                        f'Unable to delete remote project '
                        f'(backend_id: {remote_project["backend_id"]}, api_url: {api_url}): {e}'
                    )
                    continue
