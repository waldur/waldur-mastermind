import collections
import datetime
import hashlib
import logging

import requests
from celery import shared_task
from constance import config
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework import status

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.logging import models as logging_models
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.utils import get_users, role_has_permission
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure.log import event_logger
from waldur_mastermind import __version__ as mastermind_version
from waldur_mastermind.common.utils import create_request
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices import utils as invoice_utils
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_slurm_remote import (
    PLUGIN_NAME as SLURM_REMOTE_PLUGIN_NAME,
)
from waldur_mastermind.support.backend import get_active_backend

from . import PLUGIN_NAME, exceptions, models, utils, views

logger = logging.getLogger(__name__)


User = get_user_model()


def approve_order(order, user):
    order.approve()
    order.approved_by = user
    order.approved_at = timezone.now()
    order.save()

    serialized_order = core_utils.serialize_instance(order)
    serialized_user = core_utils.serialize_instance(user)
    transaction.on_commit(
        lambda: process_order.delay(serialized_order, serialized_user)
    )

    # Send emails to service provider users in case if order item is Markeplace.Basic ot Marketplace.Remote
    from waldur_mastermind.marketplace_remote import PLUGIN_NAME as REMOTE_PLUGIN_NAME

    for order_item in order.items.filter(
        offering__type__in=[PLUGIN_NAME, REMOTE_PLUGIN_NAME]
    ).exclude(
        offering__plugin_options__has_key='auto_approve_remote_orders',
        offering__plugin_options__auto_approve_remote_orders=True,
    ):
        transaction.on_commit(
            lambda: notify_provider_about_order_item_pending_approval.delay(
                order_item.uuid
            )
        )


@shared_task
def process_order(serialized_order, serialized_user):
    order = core_utils.deserialize_instance(serialized_order)
    user = core_utils.deserialize_instance(serialized_user)

    # Skip remote plugin because it is going to processed
    # only after it gets approved by service provider
    from waldur_mastermind.marketplace_remote import PLUGIN_NAME as REMOTE_PLUGIN_NAME

    for item in order.items.exclude(offering__type=SLURM_REMOTE_PLUGIN_NAME):
        if item.offering.type == REMOTE_PLUGIN_NAME:
            # If an offering has auto_approve_remote_orders flag set to True, an item can be processed without approval
            auto_approve_remote_orders = item.offering.plugin_options.get(
                'auto_approve_remote_orders', False
            )
            # A service provider owner or a service manager is not required to approve an order item manually
            user_is_service_provider_owner = structure_permissions._has_owner_access(
                user, item.offering.customer
            )
            user_is_service_provider_offering_manger = (
                structure_permissions._has_service_manager_access(
                    user, item.offering.customer
                )
                and item.offering.has_user(user)
            )
            # If any condition is not met, the order item is requested for manual approval
            if (
                auto_approve_remote_orders
                or user_is_service_provider_owner
                or user_is_service_provider_offering_manger
            ):
                pass
            else:
                continue

        item.set_state_executing()
        item.save(update_fields=['state'])
        utils.process_order_item(item, user)


@shared_task
def process_order_item(serialized_order_item, serialized_user):
    order_item = core_utils.deserialize_instance(serialized_order_item)
    user = core_utils.deserialize_instance(serialized_user)
    utils.process_order_item(order_item, user)


@shared_task
def create_screenshot_thumbnail(uuid):
    screenshot = models.Screenshot.objects.get(uuid=uuid)
    utils.create_screenshot_thumbnail(screenshot)


@shared_task
def notify_order_approvers(uuid):
    order = models.Order.objects.get(uuid=uuid)
    users = User.objects.none()

    if settings.WALDUR_MARKETPLACE['NOTIFY_STAFF_ABOUT_APPROVALS']:
        users |= User.objects.filter(is_staff=True, is_active=True)

    if role_has_permission(RoleEnum.CUSTOMER_OWNER, PermissionEnum.APPROVE_ORDER):
        users |= get_users(order.project.customer, RoleEnum.CUSTOMER_OWNER)

    if role_has_permission(RoleEnum.PROJECT_MANAGER, PermissionEnum.APPROVE_ORDER):
        users |= get_users(order.project, RoleEnum.PROJECT_MANAGER)

    if role_has_permission(RoleEnum.PROJECT_ADMIN, PermissionEnum.APPROVE_ORDER):
        users |= get_users(order.project, RoleEnum.PROJECT_ADMIN)

    approvers = (
        users.distinct()
        .exclude(email='')
        .exclude(notifications_enabled=False)
        .values_list('email', flat=True)
    )

    if not approvers:
        return

    link = core_utils.format_homeport_link(
        'projects/{project_uuid}/marketplace-order-details/{order_uuid}/',
        project_uuid=order.project.uuid,
        order_uuid=order.uuid,
    )

    context = {
        'order_url': link,
        'order': order,
        'site_name': config.SITE_NAME,
    }

    logger.info(
        'About to send email regarding order %s to approvers: %s', order, approvers
    )

    core_utils.broadcast_mail(
        'marketplace', 'notification_approval', context, approvers
    )


@shared_task
def notify_provider_about_order_item_pending_approval(order_item_uuid):
    order_item: models.OrderItem = models.OrderItem.objects.get(uuid=order_item_uuid)
    # EXECUTING is for Marketplace.Basic
    # PENDING is for Marketplace.Remote
    if order_item.state not in [
        models.OrderItem.States.EXECUTING,
        models.OrderItem.States.PENDING,
    ]:
        return

    service_provider_org = order_item.offering.customer
    approvers = service_provider_org.get_owner_mails()
    approvers |= (
        order_item.offering.get_users()
        .exclude(email='')
        .exclude(notifications_enabled=False)
        .values_list('email', flat=True)
    )

    link = core_utils.format_homeport_link(
        'providers/{organization_uuid}/marketplace-order-items/',
        organization_uuid=service_provider_org.uuid,
    )

    context = {
        'order_item_url': link,
        'order': order_item.order,
        'site_name': config.SITE_NAME,
    }

    logger.info(
        'About to send email regarding order item %s to approvers: %s',
        order_item,
        approvers,
    )

    core_utils.broadcast_mail(
        'marketplace', 'notification_service_provider_approval', context, approvers
    )


@shared_task
def notify_about_resource_change(event_type, context, resource_uuid):
    resource = models.Resource.objects.get(uuid=resource_uuid)
    emails = resource.project.get_user_mails()
    core_utils.broadcast_mail('marketplace', event_type, context, emails)


def filter_aggregate_by_scope(queryset, scope):
    scope_path = None

    if isinstance(scope, structure_models.Project):
        scope_path = 'resource__project'

    if isinstance(scope, structure_models.Customer):
        scope_path = 'resource__project__customer'

    if scope_path:
        queryset = queryset.filter(**{scope_path: scope})

    return queryset


def aggregate_reported_usage(start, end, scope):
    queryset = models.ComponentUsage.objects.filter(
        date__date__gte=start, date__date__lte=end
    ).exclude(component__parent=None)

    queryset = filter_aggregate_by_scope(queryset, scope)

    queryset = queryset.values('component__parent_id').annotate(total=Sum('usage'))

    return {row['component__parent_id']: row['total'] for row in queryset}


def aggregate_fixed_usage(start, end, scope):
    queryset = models.ResourcePlanPeriod.objects.filter(
        # Resource has been active during billing period
        Q(start__gte=start, end__lte=end)
        | Q(end__isnull=True)  # Resource is still active
        | Q(
            end__gte=start, end__lte=end
        )  # Resource has been launched in previous billing period and stopped in current
    )
    queryset = filter_aggregate_by_scope(queryset, scope)

    queryset = queryset.values('plan__components__component__parent_id').annotate(
        total=Sum('plan__components__amount')
    )

    return {
        row['plan__components__component__parent_id']: row['total'] for row in queryset
    }


def calculate_usage_for_scope(start, end, scope):
    reported_usage = aggregate_reported_usage(start, end, scope)
    fixed_usage = aggregate_fixed_usage(start, end, scope)
    # It needs to cover a case when a key is None because OfferingComponent.parent can be None.
    fixed_usage.pop(None, None)
    components = set(reported_usage.keys()) | set(fixed_usage.keys())
    content_type = ContentType.objects.get_for_model(scope)

    for component_id in components:
        models.CategoryComponentUsage.objects.update_or_create(
            content_type=content_type,
            object_id=scope.id,
            component_id=component_id,
            date=start,
            defaults={
                'reported_usage': reported_usage.get(component_id),
                'fixed_usage': fixed_usage.get(component_id),
            },
        )


@shared_task(name='waldur_mastermind.marketplace.calculate_usage_for_current_month')
def calculate_usage_for_current_month():
    start = invoice_utils.get_current_month_start()
    end = invoice_utils.get_current_month_end()
    scopes = []

    for customer in structure_models.Customer.objects.all():
        scopes.append(customer)
        for project in customer.projects.all():
            scopes.append(project)

    for scope in scopes:
        calculate_usage_for_scope(start, end, scope)


@shared_task(name='waldur_mastermind.marketplace.send_notifications_about_usages')
def send_notifications_about_usages():
    for warning in utils.get_info_about_missing_usage_reports():
        customer = warning['customer']
        emails = customer.get_owner_mails()
        warning['public_resources_url'] = utils.get_public_resources_url(customer)

        if customer.serviceprovider.enable_notifications and emails:
            core_utils.broadcast_mail(
                'marketplace', 'notification_usages', warning, emails
            )


@shared_task
def terminate_resource(serialized_resource, serialized_user):
    resource = core_utils.deserialize_instance(serialized_resource)
    user = core_utils.deserialize_instance(serialized_user)
    view = views.ResourceViewSet.as_view({'post': 'terminate'})
    response = create_request(view, user, {}, uuid=resource.uuid.hex)

    if response.status_code != status.HTTP_200_OK:
        raise exceptions.ResourceTerminateException(response.rendered_content)


@shared_task(
    name='waldur_mastermind.marketplace.terminate_resources_if_project_end_date_has_been_reached'
)
def terminate_resources_if_project_end_date_has_been_reached():
    expired_projects = structure_models.Project.available_objects.exclude(
        end_date__isnull=True
    ).filter(end_date__lte=timezone.datetime.today())

    for project in expired_projects:
        project_resources = models.Resource.objects.filter(project=project)
        active_resources = project_resources.exclude(
            state=models.Resource.States.TERMINATED
        )

        if not active_resources:
            event_logger.project.info(
                'Project {project_name} is going to be deleted because end date has been reached and there are no active resources.',
                event_type='project_deletion_triggered',
                event_context={'project': project},
            )
            project.delete()
            return

        terminatable_resources = project_resources.filter(
            state__in=(models.Resource.States.OK, models.Resource.States.ERRED)
        )
        utils.schedule_resources_termination(
            terminatable_resources,
            termination_comment=f"Project end date has been reached on {timezone.datetime.today()}",
        )


@shared_task(name='waldur_mastermind.marketplace.notify_about_stale_resource')
def notify_about_stale_resource():
    if not settings.WALDUR_MARKETPLACE['ENABLE_STALE_RESOURCE_NOTIFICATIONS']:
        return

    today = datetime.datetime.today()
    prev_1 = today - relativedelta(months=1)
    prev_2 = today - relativedelta(months=2)
    items = invoices_models.InvoiceItem.objects.filter(
        Q(
            invoice__month=today.month,
            invoice__year=today.year,
        )
        | Q(invoice__month=prev_1.month, invoice__year=prev_1.year)
        | Q(invoice__month=prev_2.month, invoice__year=prev_2.year)
    )
    actual_resources_ids = []

    for item in items:
        if item.price:
            actual_resources_ids.append(item.resource.id)

    resources = (
        models.Resource.objects.exclude(id__in=actual_resources_ids)
        .exclude(
            Q(state=models.Resource.States.TERMINATED)
            | Q(state=models.Resource.States.TERMINATING)
            | Q(state=models.Resource.States.CREATING)
        )
        .exclude(offering__billable=False)
    )
    user_resources = collections.defaultdict(list)

    for resource in resources:
        mails = resource.project.customer.get_owner_mails()
        resource_url = core_utils.format_homeport_link(
            'projects/{project_uuid}/marketplace-project-resource-details/{resource_uuid}/',
            project_uuid=resource.project.uuid.hex,
            resource_uuid=resource.uuid.hex,
        )

        for mail in mails:
            user_resources[mail].append(
                {'resource': resource, 'resource_url': resource_url}
            )

    for key, value in user_resources.items():
        core_utils.broadcast_mail(
            'marketplace',
            'notification_about_stale_resources',
            {'resources': value},
            [key],
        )


@shared_task(name='waldur_mastermind.marketplace.terminate_expired_resources')
def terminate_expired_resources():
    expired_resources = models.Resource.objects.filter(
        end_date__lte=timezone.datetime.today(),
        state__in=(models.Resource.States.OK, models.Resource.States.ERRED),
    )
    utils.schedule_resources_termination(
        expired_resources,
        termination_comment=f"Resource expired on {timezone.datetime.today()}",
    )


@shared_task
def notify_about_resource_termination(resource_uuid, user_uuid, is_staff_action=None):
    resource = models.Resource.objects.get(uuid=resource_uuid)
    user = User.objects.get(uuid=user_uuid)
    admin_emails = set(
        resource.project.get_user_mails(structure_models.ProjectRole.ADMINISTRATOR)
    )
    manager_emails = set(
        resource.project.get_user_mails(structure_models.ProjectRole.MANAGER)
    )
    emails = admin_emails | manager_emails
    bcc = []
    if user.email and user.notifications_enabled:
        bcc.append(user.email)
    resource_url = core_utils.format_homeport_link(
        'projects/{project_uuid}/marketplace-project-resource-details/{resource_uuid}/',
        project_uuid=resource.project.uuid.hex,
        resource_uuid=resource.uuid.hex,
    )
    context = {'resource': resource, 'user': user, 'resource_url': resource_url}

    if is_staff_action:
        core_utils.broadcast_mail(
            'marketplace',
            'marketplace_resource_termination_scheduled_staff',
            context,
            emails,
            bcc=bcc,
        )
    else:
        core_utils.broadcast_mail(
            'marketplace',
            'marketplace_resource_termination_scheduled',
            context,
            emails,
            bcc=bcc,
        )


@shared_task(name='waldur_mastermind.marketplace.notification_about_project_ending')
def notification_about_project_ending():
    date_1 = timezone.datetime.today().date() + datetime.timedelta(days=1)
    date_7 = timezone.datetime.today().date() + datetime.timedelta(days=7)
    expired_projects = structure_models.Project.available_objects.exclude(
        end_date__isnull=True
    ).filter(Q(end_date=date_1) | Q(end_date=date_7))

    for project in expired_projects:
        managers = (
            project.get_users(structure_models.ProjectRole.MANAGER)
            .exclude(email='')
            .exclude(notifications_enabled=False)
        )
        owners = (
            project.customer.get_owners()
            .exclude(email='')
            .exclude(notifications_enabled=False)
        )
        users = set(managers) | set(owners)

        project_url = core_utils.format_homeport_link(
            'projects/{project_uuid}/',
            project_uuid=project.uuid.hex,
        )

        for user in users:
            context = {
                'project_url': project_url,
                'project': project,
                'user': user,
                'delta': (project.end_date - timezone.datetime.today().date()).days,
            }
            core_utils.broadcast_mail(
                'marketplace',
                'notification_about_project_ending',
                context,
                [user.email],
            )


@shared_task(name='waldur_mastermind.marketplace.send_metrics')
def send_metrics():
    if not settings.WALDUR_MARKETPLACE['TELEMETRY_ENABLED']:
        return

    site_name = settings.WALDUR_CORE['HOMEPORT_URL']
    deployment_type = core_utils.get_deployment_type()
    first_event = logging_models.Event.objects.order_by('created').first()
    installation_date = (
        first_event.created.strftime('%Y-%m-%d %H:%M:%S.%f%z') if first_event else None
    )
    installation_date_str = str(installation_date) if installation_date else None
    params = {
        'deployment_id': hashlib.sha256(site_name.encode()).hexdigest(),
        'deployment_type': deployment_type,
        'helpdesk_backend': get_active_backend().backend_name,
        'helpdesk_integration_status': settings.WALDUR_SUPPORT['ENABLED'],
        'number_of_users': core_models.User.objects.filter(is_active=True).count(),
        'number_of_offerings': marketplace_models.Offering.objects.filter(
            state__in=(
                marketplace_models.Offering.States.ACTIVE,
                marketplace_models.Offering.States.PAUSED,
            )
        ).count(),
        'types_of_offering': list(
            marketplace_models.Offering.objects.filter(
                state__in=(
                    marketplace_models.Offering.States.ACTIVE,
                    marketplace_models.Offering.States.PAUSED,
                )
            )
            .order_by()
            .values_list('type', flat=True)
            .distinct()
        ),
        'version': mastermind_version,
    }
    if installation_date_str:
        params['installation_date'] = installation_date_str
    url = (
        settings.WALDUR_MARKETPLACE['TELEMETRY_URL']
        + f"v{settings.WALDUR_MARKETPLACE['TELEMETRY_VERSION']}/metrics/"
    )
    response = requests.post(url, json=params)

    if response.status_code != 200:
        logger.warning(
            'An error of metrics sending, status code: %s, text: %s',
            response.status_code,
            response.text,
        )

    return response
