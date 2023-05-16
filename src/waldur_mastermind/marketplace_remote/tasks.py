import collections
import logging
from datetime import datetime, timedelta

import requests
from celery.app import shared_task
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.utils import dateparse, timezone
from rest_framework import exceptions as rf_exceptions
from waldur_client import WaldurClient, WaldurClientException

from waldur_core.core.mixins import ReviewStateMixin
from waldur_core.core.utils import (
    broadcast_mail,
    deserialize_instance,
    format_homeport_link,
    month_start,
    serialize_instance,
)
from waldur_core.structure import models as structure_models
from waldur_core.structure.tasks import BackgroundListPullTask, BackgroundPullTask
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.registrators import RegistrationManager
from waldur_mastermind.invoices.utils import get_previous_month
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.callbacks import sync_order_item_state
from waldur_mastermind.marketplace.utils import create_local_resource
from waldur_mastermind.marketplace_remote import models as remote_models
from waldur_mastermind.marketplace_remote.constants import (
    OFFERING_COMPONENT_FIELDS,
    OFFERING_FIELDS,
    PLAN_FIELDS,
    RESOURCE_FIELDS,
)
from waldur_mastermind.marketplace_remote.utils import (
    get_client_for_offering,
    pull_fields,
    sync_project_permission,
)

from . import PLUGIN_NAME, utils

logger = logging.getLogger(__name__)

OrderItemInvertStates = {key: val for val, key in models.OrderItem.States.CHOICES}


class OfferingPullTask(BackgroundPullTask):
    def pull(self, local_offering: models.Offering):
        client = get_client_for_offering(local_offering)
        remote_offering = client.get_marketplace_public_offering(
            local_offering.backend_id
        )
        pull_fields(OFFERING_FIELDS, local_offering, remote_offering)
        utils.import_offering_thumbnail(local_offering, remote_offering)
        self.sync_offering_components(local_offering, remote_offering)
        self.sync_plans(local_offering, remote_offering)

    def sync_offering_components(
        self, local_offering: models.Offering, remote_offering
    ):
        remote_components = remote_offering['components']
        local_components = local_offering.components.all()
        remote_component_types_map = {item['type']: item for item in remote_components}
        local_component_types = [item.type for item in local_components]

        new_component_types = set(remote_component_types_map.keys()) - set(
            local_component_types
        )
        stale_component_types = set(local_component_types) - set(
            remote_component_types_map.keys()
        )
        existing_component_types = set(local_component_types) & set(
            remote_component_types_map.keys()
        )
        if stale_component_types:
            local_offering.components.filter(type__in=stale_component_types).delete()
            logger.info(
                'Components %s of offering %s have been deleted',
                stale_component_types,
                local_offering,
            )

        utils.import_offering_components(
            local_offering,
            {
                'components': [
                    comp
                    for comp_type, comp in remote_component_types_map.items()
                    if comp_type in new_component_types
                ]
            },
        )

        for existing_component_type in existing_component_types:
            remote_component = remote_component_types_map[existing_component_type]
            local_component: models.OfferingComponent = local_offering.components.get(
                type=existing_component_type
            )
            pull_fields(OFFERING_COMPONENT_FIELDS, local_component, remote_component)
            logger.info(
                'Component %s for offering %s has been updated',
                existing_component_type,
                local_offering,
            )

    def sync_plans(self, local_offering: models.Offering, remote_offering):
        """
        Sync plans for an existing offering
        """
        local_plans = models.Plan.objects.filter(offering=local_offering)
        remote_plans = remote_offering['plans']

        local_plan_uuids = [item.backend_id for item in local_plans]
        remote_plans_map = {item['uuid']: item for item in remote_plans}

        new_plans = set(remote_plans_map.keys()) - set(local_plan_uuids)
        stale_plans = set(local_plan_uuids) - set(remote_plans_map.keys())
        existing_plans = set(local_plan_uuids) & set(remote_plans_map.keys())

        for stale_plan in local_offering.plans.filter(backend_id__in=stale_plans):
            stale_plan.archived = True
            stale_plan.save()
            logger.info(
                'Plan %s of offering %s has been archived',
                stale_plan,
                local_offering,
            )

        local_components_map = {
            item.type: item for item in local_offering.components.all()
        }
        new_remote_plans = {
            'plans': [
                item for item in remote_offering['plans'] if item['uuid'] in new_plans
            ]
        }
        utils.import_plans(local_offering, new_remote_plans, local_components_map)

        for existing_plan_backend_id in existing_plans:
            remote_plan = remote_plans_map[existing_plan_backend_id]
            local_plan: models.Plan = local_offering.plans.get(
                backend_id=existing_plan_backend_id
            )
            updated_fields = pull_fields(PLAN_FIELDS, local_plan, remote_plan)

            self.sync_plan_components(local_plan, remote_plan)

            if updated_fields:
                logger.info(
                    'Plan %s for offering %s has been updated',
                    local_plan.name,
                    local_offering,
                )

    def sync_plan_components(self, local_plan: models.Plan, remote_plan):
        """
        Sync plan componets for an existing plan
        This method skips check of stale plan components, because it assumes they have been already removed in `sync_components` method
        """
        local_offering = local_plan.offering
        local_offering_components = local_offering.components
        local_plan_components = set(
            local_plan.components.all().values_list('component__type', flat=True)
        )
        remote_prices = remote_plan['prices']
        remote_quotas = remote_plan['quotas']
        remote_plan_components = set(remote_prices.keys()) | set(remote_quotas.keys())

        new_plan_components = remote_plan_components - local_plan_components

        existing_plan_components = local_plan_components & remote_plan_components

        for component_type in new_plan_components:
            plan_component = models.PlanComponent.objects.create(
                plan=local_plan,
                component=local_offering_components.get(type=component_type),
                price=remote_prices[component_type],
                amount=remote_quotas[component_type],
            )
            logger.info(
                'Plan component %s of offering %s has been created',
                plan_component,
                local_plan.offering,
            )

        for existing_plan_component in existing_plan_components:
            local_component: models.OfferingComponent = local_offering_components.get(
                type=existing_plan_component
            )
            local_plan_component: models.PlanComponent = local_plan.components.get(
                component=local_component
            )
            changed_fields = pull_fields(
                ['price', 'amount'],
                local_plan_component,
                {
                    'price': remote_prices[existing_plan_component],
                    'amount': remote_quotas[existing_plan_component],
                },
            )

            if changed_fields:
                logger.info(
                    'Plan component %s of offering %s has been updated',
                    existing_plan_component,
                    local_offering,
                )


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
        usernames = set(remote_offering_users.keys()) | set(local_offering_users.keys())
        user_map = {
            user.username: user
            for user in models.User.objects.filter(username__in=usernames)
        }

        missing = set(remote_offering_users.keys()) - set(local_offering_users.keys())
        for local_username in missing:
            if local_username not in user_map:
                logger.debug(
                    'Skipping missing offering user synchronization because user '
                    'with username %s is not available in the local database.',
                    local_username,
                )
                continue
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
            RESOURCE_FIELDS,
            local_resource,
            remote_resource,
        )
        if local_resource.effective_id != remote_resource['backend_id']:
            local_resource.effective_id = remote_resource['backend_id']
            local_resource.save(update_fields=['effective_id'])
        # When pulling resource, if remote state is different from local, import remote order items.
        utils.import_resource_order_items(local_resource)
        if utils.parse_resource_state(remote_resource['state']) != local_resource.state:
            utils.pull_resource_state(local_resource)


class ResourceListPullTask(BackgroundListPullTask):
    name = 'waldur_mastermind.marketplace_remote.pull_resources'
    pull_task = ResourcePullTask

    def get_pulled_objects(self):
        return models.Resource.objects.filter(offering__type=PLUGIN_NAME).exclude(
            backend_id=''
        )


@shared_task
def pull_offering_resources(serialized_offering):
    offering = deserialize_instance(serialized_offering)
    resources = models.Resource.objects.filter(offering=offering)
    for resource in resources:
        ResourcePullTask().delay(serialize_instance(resource))


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
                effective_id = remote_order_item.get('resource_uuid') or ''
                if resource_uuid:
                    create_local_resource(local_order_item, resource_uuid, effective_id)
            sync_order_item_state(local_order_item, new_state)
        pull_fields(('error_message',), local_order_item, remote_order_item)


class OrderItemStatePullTask(OrderItemPullTask):
    def pull(self, local_order_item):
        super().pull(local_order_item)
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


@shared_task
def pull_offering_order_items(serialized_offering):
    offering = deserialize_instance(serialized_offering)
    order_items = (
        models.OrderItem.objects.filter(offering=offering)
        .exclude(state__in=models.OrderItem.States.TERMINAL_STATES)
        .exclude(backend_id='')
    )
    for order_item in order_items:
        OrderItemPullTask().delay(serialize_instance(order_item))


class UsagePullTask(BackgroundPullTask):
    def pull(self, local_resource: models.Resource):
        client = get_client_for_offering(local_resource.offering)

        today = datetime.today()
        four_months_ago = month_start(today - relativedelta(months=4))
        four_months_ago_str = four_months_ago.strftime('%Y-%m-%d')

        remote_usages = client.list_component_usages(
            local_resource.backend_id,
            date_after=four_months_ago_str,
        )

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


@shared_task
def pull_offering_usage(serialized_offering):
    offering = deserialize_instance(serialized_offering)
    resources = models.Resource.objects.filter(offering=offering)
    for resource in resources:
        UsagePullTask().delay(serialize_instance(resource))


class ResourceInvoicePullTask(BackgroundPullTask):
    def pull(self, local_resource: models.Resource):
        for date in (get_previous_month(), timezone.now()):
            self.pull_date(date, local_resource)

    def pull_date(self, date, local_resource):
        client = get_client_for_offering(local_resource.offering)
        local_customer = local_resource.project.customer
        try:
            remote_invoice_items = client.list_invoice_items(
                {
                    "resource_uuid": local_resource.backend_id,
                    "year": date.year,
                    "month": date.month,
                }
            )
        except WaldurClientException as e:
            logger.info(
                f'Unable to get remote invoice items for resource [id={local_resource.backend_id}]: {e}'
            )
            return

        local_invoice, _ = RegistrationManager.get_or_create_invoice(
            local_customer, date
        )
        local_invoice_items = local_invoice.items.filter(resource=local_resource)
        local_invoice_items.filter(backend_uuid=None).delete()

        local_item_ids = {item.backend_uuid.hex for item in local_invoice_items}
        remote_item_ids = {item['uuid'] for item in remote_invoice_items}

        new_item_ids = remote_item_ids - local_item_ids
        stale_item_ids = local_item_ids - remote_item_ids
        existing_item_ids = local_item_ids & remote_item_ids

        if len(stale_item_ids) > 0:
            invoice_models.InvoiceItem.objects.filter(name__in=stale_item_ids).delete()
            logger.info(
                f'The following invoice items for resource [uuid={local_resource.uuid}] have been deleted: {stale_item_ids}'
            )

        new_invoice_items = [
            item for item in remote_invoice_items if item['uuid'] in new_item_ids
        ]
        for item in new_invoice_items:
            invoice_models.InvoiceItem.objects.create(
                backend_uuid=item['uuid'],
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
            item for item in remote_invoice_items if item['uuid'] in existing_item_ids
        ]
        for item in existing_invoice_items:
            local_item = local_invoice_items.get(
                backend_uuid=item['uuid'],
            )
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
        return models.Resource.objects.filter(offering__type=PLUGIN_NAME).exclude(
            state=models.Resource.States.TERMINATED
        )


@shared_task
def pull_offering_invoices(serialized_offering):
    offering = deserialize_instance(serialized_offering)
    resources = models.Resource.objects.filter(offering=offering).exclude(
        state=models.Resource.States.TERMINATED
    )
    for resource in resources:
        ResourceInvoicePullTask().delay(serialize_instance(resource))


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
            client = utils.get_client_for_offering(offering)

            try:
                remote_project = utils.get_remote_project(offering, project, client)
                if not remote_project:
                    if not local_permissions:
                        logger.info(
                            f'Skipping remote project {project} synchronization in '
                            'offering {offering} because there are no users to be synced.'
                        )
                    else:
                        remote_project = utils.create_remote_project(
                            offering, project, client
                        )
                        utils.push_project_users(
                            offering, project, remote_project['uuid']
                        )
                    continue
            except rf_exceptions.ValidationError as e:
                logger.warning(
                    f'Unable to fetch remote project {project} in offering {offering}: {e}'
                )
                continue
            except WaldurClientException as e:
                logger.warning(
                    f'Unable to create remote project {project} in offering {offering}: {e}'
                )
                continue
            else:
                remote_project_uuid = remote_project['uuid']

            try:
                remote_permissions = client.get_project_permissions(remote_project_uuid)
            except WaldurClientException as e:
                logger.warning(
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
                    logger.warning(
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
                        logger.warning(
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
                        logger.warning(
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
                        logger.warning(
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
                        logger.warning(
                            f'Unable to update permission for user [{remote_user_uuid}] with role {old_role} (until {new_expiration_time}) '
                            f'and project [{remote_project_uuid}] in offering [{offering}]: {e}'
                        )

            stale_usernames = set(remote_user_roles.keys()) - set(
                local_permissions.keys()
            )
            for username in stale_usernames:
                old_permission_id = None
                for permission in remote_permissions:
                    if permission['user_username'] == username:
                        old_permission_id = str(permission['pk'])

                if not old_permission_id:
                    continue
                try:
                    client.remove_project_permission(old_permission_id)
                except WaldurClientException as e:
                    logger.warning(
                        f'Unable to remove permission [{old_permission_id}] for user [{username}] in offering [{offering}]: {e}'
                    )


@shared_task
def sync_remote_project(serialized_request):
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
    local_project = structure_models.Project.objects.get(pk=pk)
    backend_id = utils.get_project_backend_id(local_project)
    offering_ids = (
        models.Resource.objects.filter(
            project=local_project,
            offering__type=PLUGIN_NAME,
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
            structure_models.Project.objects.filter(is_removed=True),
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


@shared_task
def trigger_order_item_callback(serialized_order_item):
    order_item = deserialize_instance(serialized_order_item)
    requests.post(order_item.callback_url)


@shared_task(
    name='waldur_mastermind.marketplace_remote.notify_about_pending_project_update_requests'
)
def notify_about_pending_project_update_requests():
    week_ago = datetime.now() - timedelta(weeks=1)
    pending_project_update_requests = (
        remote_models.ProjectUpdateRequest.objects.filter(
            state=ReviewStateMixin.States.PENDING
        )
        .order_by('project_id')
        .distinct('project_id')
        .filter(created__lte=week_ago)
    )

    for pending_project_update_request in pending_project_update_requests:
        mails = pending_project_update_request.project.customer.get_owner_mails()
        project_url = format_homeport_link(
            'projects/{project_uuid}/marketplace-project-update-requests/',
            project_uuid=pending_project_update_request.project.uuid.hex,
        )
        context = {
            'project_update_request': pending_project_update_request,
            'project_url': project_url,
        }
        broadcast_mail(
            'marketplace_remote',
            'notification_about_pending_project_updates',
            context,
            mails,
        )


@shared_task(
    name='waldur_mastermind.marketplace_remote.notify_about_project_details_update'
)
def notify_about_project_details_update(serialized_project_update):
    review_request = deserialize_instance(serialized_project_update)

    context = {}
    if review_request.new_description:
        context['new_description'] = review_request.new_description
        context['old_description'] = review_request.old_description
    if review_request.new_name:
        context['new_name'] = review_request.new_name
        context['old_name'] = review_request.old_name
    if review_request.new_end_date:
        context['new_end_date'] = review_request.new_end_date
        context['old_end_date'] = review_request.old_end_date
    if review_request.new_oecd_fos_2007_code:
        context['new_oecd_fos_2007_code'] = review_request.new_oecd_fos_2007_code
        context['old_oecd_fos_2007_code'] = review_request.old_oecd_fos_2007_code
    if review_request.new_is_industry:
        context['new_is_industry'] = review_request.new_is_industry
        context['old_is_industry'] = review_request.new_is_industry

    context['reviewed_by'] = review_request.reviewed_by
    context['project_url'] = format_homeport_link(
        'projects/{project_uuid}/',
        project_uuid=review_request.project.uuid.hex,
    )
    mails = [
        review_request.reviewed_by,
        review_request.created_by,
    ]

    broadcast_mail(
        'marketplace_remote',
        'notification_about_project_details_update',
        context,
        mails,
    )


class RemoteProjectDataPushTask(BackgroundPullTask):
    def pull(self, instance: models.Offering):
        offering = instance
        project_ids = (
            models.Resource.objects.filter(offering=offering)
            .exclude(state=models.Resource.States.TERMINATED)
            .values_list('project_id', flat=True)
            .distinct()
        )
        for project in structure_models.Project.objects.filter(id__in=project_ids):
            try:
                logger.info("Pushing project %s data to remote Waldur")
                request = remote_models.ProjectUpdateRequest(
                    project=project,
                    offering=offering,
                    new_name=project.name,
                    new_description=project.description,
                    new_end_date=project.end_date,
                    new_oecd_fos_2007_code=project.oecd_fos_2007_code,
                    new_is_industry=project.is_industry,
                )
                utils.update_remote_project(request)
            except WaldurClientException as exc:
                logger.error("Unable to push project data: %s", exc)


class RemoteProjectDataListPushTask(BackgroundPullTask):
    name = 'waldur_mastermind.marketplace_remote.push_remote_project_data'
    pull_task = RemoteProjectDataPushTask

    def get_pulled_objects(self):
        return models.Offering.objects.filter(type=PLUGIN_NAME)
