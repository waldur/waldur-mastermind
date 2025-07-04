import collections
import datetime
import hashlib
import logging
from typing import cast

import requests
from celery import shared_task
from constance import config
from dateutil.relativedelta import relativedelta
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone
from rest_framework import status

from waldur_core import _get_version
from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.log import event_logger
from waldur_core.core.models import User
from waldur_core.logging import models as logging_models
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure import models as structure_models
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices import utils as invoice_utils
from waldur_mastermind.marketplace import exceptions, models, plugins, utils
from waldur_mastermind.marketplace.enums import (
    OfferingStates,
    OrderStates,
    ResourceStates,
    RobotAccountStates,
)
from waldur_mastermind.marketplace.utils import (
    get_consumer_approvers,
    get_provider_approvers,
)
from waldur_mastermind.support.backend import get_active_backend

logger = logging.getLogger(__name__)


def process_order_on_commit(order: models.Order, user):
    serialized_order = core_utils.serialize_instance(order)
    serialized_user = core_utils.serialize_instance(user)
    transaction.on_commit(
        lambda: process_order.delay(serialized_order, serialized_user)
    )


@shared_task
def process_order(serialized_order, serialized_user):
    order = core_utils.deserialize_instance(serialized_order)
    user = core_utils.deserialize_instance(serialized_user)
    utils.process_order(order, user)


@shared_task
def create_screenshot_thumbnail(uuid):
    screenshot = models.Screenshot.objects.get(uuid=uuid)
    utils.create_screenshot_thumbnail(screenshot)


@shared_task
def notify_consumer_about_pending_order(uuid):
    order = models.Order.objects.get(uuid=uuid)
    approvers = get_consumer_approvers(order)

    if not approvers:
        return

    order_link = core_utils.format_homeport_link(
        "marketplace-order-details/{order_uuid}/",
        project_uuid=order.project.uuid,
        order_uuid=order.uuid,
    )

    context = {
        "order_link": order_link,
        "order": order,
        "site_name": config.SITE_NAME,
    }

    logger.info(
        "About to send email regarding order (%s) %s to approvers: %s",
        order.uuid,
        order,
        approvers,
    )

    core_utils.broadcast_mail(
        "marketplace", "notify_consumer_about_pending_order", context, approvers
    )


@shared_task
def notify_provider_about_pending_order(order_uuid):
    order = models.Order.objects.get(uuid=order_uuid)

    approvers = get_provider_approvers(order)
    if not approvers:
        return

    link = core_utils.format_homeport_link(
        "marketplace-order-details/{order_uuid}/",
        order_uuid=order.uuid,
    )

    context = {
        "order_url": link,
        "order": order,
        "site_name": config.SITE_NAME,
    }

    logger.info(
        "About to send email regarding order %s to approvers: %s",
        order,
        approvers,
    )

    core_utils.broadcast_mail(
        "marketplace", "notify_provider_about_pending_order", context, approvers
    )


@shared_task
def notify_about_resource_change(event_type, context, resource_uuid):
    resource = models.Resource.objects.get(uuid=resource_uuid)
    emails = resource.project.get_user_mails()
    core_utils.broadcast_mail("marketplace", event_type, context, emails)


def filter_aggregate_by_scope(queryset, scope):
    scope_path = None

    if isinstance(scope, structure_models.Project):
        scope_path = "resource__project"

    if isinstance(scope, structure_models.Customer):
        scope_path = "resource__project__customer"

    if scope_path:
        queryset = queryset.filter(**{scope_path: scope})

    return queryset


def aggregate_reported_usage(start, end, scope):
    queryset = models.ComponentUsage.objects.filter(
        date__date__gte=start, date__date__lte=end
    ).exclude(component__parent=None)

    queryset = filter_aggregate_by_scope(queryset, scope)

    queryset = queryset.values("component__parent_id").annotate(total=Sum("usage"))

    return {row["component__parent_id"]: row["total"] for row in queryset}


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

    queryset = queryset.values("plan__components__component__parent_id").annotate(
        total=Sum("plan__components__amount")
    )

    return {
        row["plan__components__component__parent_id"]: row["total"] for row in queryset
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
                "reported_usage": reported_usage.get(component_id),
                "fixed_usage": fixed_usage.get(component_id),
            },
        )


@shared_task(name="waldur_mastermind.marketplace.calculate_usage_for_current_month")
def calculate_usage_for_current_month():
    """Calculate marketplace resource usage for the current month across all customers and projects."""
    start = invoice_utils.get_current_month_start()
    end = invoice_utils.get_current_month_end()
    scopes = []

    for customer in structure_models.Customer.objects.all():
        scopes.append(customer)
        for project in customer.projects.all():
            scopes.append(project)

    for scope in scopes:
        calculate_usage_for_scope(start, end, scope)


@shared_task
def terminate_resource(serialized_resource, serialized_user):
    resource = core_utils.deserialize_instance(serialized_resource)
    user = core_utils.deserialize_instance(serialized_user)
    response = utils.terminate_resource(resource, user)

    if response and response.status_code != status.HTTP_200_OK:
        raise exceptions.ResourceTerminateException(response.rendered_content)


@shared_task(
    name="waldur_mastermind.marketplace.terminate_resources_if_project_end_date_has_been_reached"
)
def terminate_resources_if_project_end_date_has_been_reached():
    """Terminate resources when their project has reached its end date."""
    expired_projects = structure_models.Project.available_objects.exclude(
        end_date__isnull=True
    ).filter(end_date__lte=timezone.datetime.today())

    for project in expired_projects:
        project_resources = models.Resource.objects.filter(project=project)
        active_resources = project_resources.exclude(state=ResourceStates.TERMINATED)

        if not active_resources:
            event_logger.info(
                "Project {project_name} is going to be deleted because end date has been reached and there are no active resources.",
                event_type="project_deletion_triggered",
                event_context={"project": project},
                group="project",
            )
            project.delete()
            return

        # We expect that resources with parents will be removed when parents are removed
        terminatable_resources = project_resources.filter(
            state__in=(ResourceStates.OK, ResourceStates.ERRED),
            offering__parent=None,
        )
        logger.info(
            "About to terminate resources from expired project: %s",
            ",".join([f"{r.uuid}, {r.name}" for r in terminatable_resources]),
        )
        utils.schedule_resources_termination(
            terminatable_resources,
            termination_comment=f"Project end date has been reached on {timezone.datetime.today()}",
        )


@shared_task(
    name="waldur_mastermind.marketplace.terminate_resources_in_state_erred_without_backend_id_and_failed_terminate_order"
)
def terminate_resources_in_state_erred_without_backend_id_and_failed_terminate_order():
    """Clean up erred Slurm resources that failed both creation and termination."""
    termination_offerings_types = ["Marketplace.Slurm"]
    resources = models.Resource.objects.filter(
        state=ResourceStates.ERRED,
        backend_id="",
        offering__type__in=termination_offerings_types,
    )
    # Get the uuids of resources that have a failed creation order
    failed_creation_resources = (
        models.Order.objects.filter(
            resource__in=resources,
            type=models.Order.Types.CREATE,
            state=OrderStates.ERRED,
        )
        .order_by("-created")
        .values_list("resource__uuid", flat=True)
    )

    # Get the uuids of resources that have a failed latest termination order
    resources_with_last_termination_order_erred = (
        models.Order.objects.filter(
            resource__in=resources,
            type=models.Order.Types.TERMINATE,
            state=OrderStates.ERRED,
        )
        .order_by("-created")
        .values_list("resource__uuid", flat=True)
    )

    # Get the uuids of resources that have a failed creation order and the latest termination order is in ERRED state
    uuids_resources_to_terminate = list(
        set(failed_creation_resources)
        & set(resources_with_last_termination_order_erred)
    )

    # Get the resources that have a failed creation order and the latest termination order is in ERRED state
    resources_to_terminate = models.Resource.objects.filter(
        uuid__in=uuids_resources_to_terminate
    )
    logger.info(f"Resources to delete during daily cleanup: {resources_to_terminate}")
    # Delete the resources, associated orders should be deleted automatically due to CASCADE delete
    for resource in resources_to_terminate:
        resource.delete()


@shared_task(name="waldur_mastermind.marketplace.notify_about_stale_resource")
def notify_about_stale_resource():
    """Notify customers about resources that have not generated invoice items in the last 3 months."""
    if not config.ENABLE_STALE_RESOURCE_NOTIFICATIONS:
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
            Q(state=ResourceStates.TERMINATED)
            | Q(state=ResourceStates.TERMINATING)
            | Q(state=ResourceStates.CREATING)
        )
        .exclude(offering__billable=False)
    )
    user_resources = collections.defaultdict(list)

    for resource in resources:
        mails = resource.project.customer.get_owner_mails()
        resource_url = core_utils.format_homeport_link(
            "resource-details/{resource_uuid}/",
            project_uuid=resource.project.uuid.hex,
            resource_uuid=resource.uuid.hex,
        )

        for mail in mails:
            user_resources[mail].append(
                {"resource": resource, "resource_url": resource_url}
            )

    for key, value in user_resources.items():
        core_utils.broadcast_mail(
            "marketplace",
            "notification_about_stale_resources",
            {"resources": value},
            [key],
        )


@shared_task(name="waldur_mastermind.marketplace.terminate_expired_resources")
def terminate_expired_resources():
    """Terminate marketplace resources that have reached their end date."""
    expired_resources = models.Resource.objects.filter(
        end_date__lte=timezone.datetime.today(),
        state__in=(ResourceStates.OK, ResourceStates.ERRED),
    )
    logger.info(
        "About to terminate expired resources: %s",
        ",".join([f"{r.uuid}, {r.name}" for r in expired_resources]),
    )
    utils.schedule_resources_termination(
        expired_resources,
        termination_comment=f"Resource expired on {timezone.datetime.today()}",
    )


@shared_task
def notify_about_resource_termination(resource_uuid, user_uuid, is_staff_action=None):
    resource = models.Resource.objects.get(uuid=resource_uuid)
    user = User.objects.get(uuid=user_uuid)
    admin_emails = set(resource.project.get_user_mails(ProjectRole.ADMIN))
    manager_emails = set(resource.project.get_user_mails(ProjectRole.MANAGER))
    emails = admin_emails | manager_emails
    bcc = []
    if user.email and user.notifications_enabled:
        bcc.append(user.email)
    resource_url = core_utils.format_homeport_link(
        "project-resource-details/{resource_uuid}/",
        project_uuid=resource.project.uuid.hex,
        resource_uuid=resource.uuid.hex,
    )
    context = {"resource": resource, "user": user, "resource_url": resource_url}

    if is_staff_action:
        core_utils.broadcast_mail(
            "marketplace",
            "marketplace_resource_termination_scheduled_staff",
            context,
            emails,
            bcc=bcc,
        )
    else:
        core_utils.broadcast_mail(
            "marketplace",
            "marketplace_resource_termination_scheduled",
            context,
            emails,
            bcc=bcc,
        )


@shared_task(name="waldur_mastermind.marketplace.notification_about_project_ending")
def notification_about_project_ending():
    """Send notifications about projects ending in 1 day and 7 days."""
    date_1 = timezone.datetime.today().date() + datetime.timedelta(days=1)
    utils.notification_about_project_ending(date_1)

    date_7 = timezone.datetime.today().date() + datetime.timedelta(days=7)
    utils.notification_about_project_ending(date_7)


@shared_task(name="waldur_mastermind.marketplace.notification_about_resource_ending")
def notification_about_resource_ending():
    """Send notifications about resources ending in 1 day and 7 days."""
    date_1 = timezone.datetime.today().date() + datetime.timedelta(days=1)
    date_7 = timezone.datetime.today().date() + datetime.timedelta(days=7)
    expired_resources = models.Resource.objects.exclude(end_date__isnull=True).filter(
        Q(end_date=date_1) | Q(end_date=date_7)
    )

    for resource in expired_resources:
        users = (
            resource.project.get_users()
            .exclude(email="")
            .exclude(notifications_enabled=False)
        )

        resource_url = core_utils.format_homeport_link(
            "resource-details/{resource_uuid}/",
            resource_uuid=resource.uuid.hex,
        )

        for user in users:
            context = {
                "resource_url": resource_url,
                "resource": resource,
                "user": user,
                "delta": (resource.end_date - timezone.datetime.today().date()).days,
            }
            core_utils.broadcast_mail(
                "marketplace",
                "notification_about_resource_ending",
                context,
                [user.email],
            )


@shared_task(name="waldur_mastermind.marketplace.send_metrics")
def send_metrics():
    """Send anonymous usage metrics and telemetry data to the Waldur team."""
    if not core_models.Feature.objects.filter(key="telemetry.send_metrics").exists():
        return

    site_name = config.HOMEPORT_URL
    deployment_type = core_utils.get_deployment_type()
    first_event = logging_models.Event.objects.order_by("created").first()
    installation_date = (
        first_event.created.strftime("%Y-%m-%d %H:%M:%S.%f%z") if first_event else None
    )
    installation_date_str = str(installation_date) if installation_date else None
    params = {
        "deployment_id": hashlib.sha256(site_name.encode()).hexdigest(),
        "deployment_type": deployment_type,
        "helpdesk_backend": get_active_backend().backend_name,
        "helpdesk_integration_status": config.WALDUR_SUPPORT_ENABLED,
        "number_of_users": core_models.User.objects.filter(is_active=True).count(),
        "number_of_offerings": models.Offering.objects.filter(
            state__in=(
                OfferingStates.ACTIVE,
                OfferingStates.PAUSED,
            )
        ).count(),
        "types_of_offering": list(
            models.Offering.objects.filter(
                state__in=(
                    OfferingStates.ACTIVE,
                    OfferingStates.PAUSED,
                )
            )
            .order_by()
            .values_list("type", flat=True)
            .distinct()
        ),
        "version": _get_version(),
    }
    if installation_date_str:
        params["installation_date"] = installation_date_str
    url = config.TELEMETRY_URL + f"v{config.TELEMETRY_VERSION}/metrics/"
    response = requests.post(url, json=params)

    if response.status_code != 200:
        logger.warning(
            "Error when sending telemetry metrics, status code: %s, text: %s",
            response.status_code,
            response.text,
        )

    return response


def copy_future_price_to_current_price():
    for component in models.PlanComponent.objects.exclude(
        future_price=F("price")
    ).exclude(future_price__isnull=True):
        component.price = component.future_price
        component.save(update_fields=["price"])


@shared_task(name="waldur_mastermind.marketplace.process_pending_project_orders")
def process_pending_project_orders():
    """Process orders for projects that have become active."""
    active_project_ids = structure_models.Project.objects.filter(
        start_date__lte=timezone.now()
    ).values_list("id", flat=True)
    orders = models.Order.objects.filter(
        state=OrderStates.PENDING_PROJECT, project__in=active_project_ids
    )
    for order in orders:
        # Setting the state to PENDING_PROVIDER because direct transition
        # from PENDING_PROJECT to EXECUTING is not supported
        order.state = OrderStates.PENDING_PROVIDER
        order.save(update_fields=["state"])
        if utils.order_should_not_be_reviewed_by_provider(order):
            order.set_state_executing()
            order.save(update_fields=["state"])
            transaction.on_commit(
                lambda: process_order_on_commit(order, order.created_by)
            )
        else:
            transaction.on_commit(
                lambda: notify_provider_about_pending_order.delay(order.uuid)
            )


@shared_task(name="waldur_mastermind.marketplace.mark_resources_as_erred_after_timeout")
def mark_resources_as_erred_after_timeout():
    """Mark stale orders and their resources as erred if they have been executing for more than 2 hours."""
    now = timezone.now()
    two_hours_ago = now - datetime.timedelta(hours=2)
    stale_orders = models.Order.objects.filter(
        offering__type__in=plugins.manager.list_interruptible_offerings(),
        state=OrderStates.EXECUTING,
        modified__lt=two_hours_ago,
    )

    for order in stale_orders:
        order.fail()
        order.error_message = "Execution has timed out."
        order.save(update_fields=["state", "error_message"])
        resource = order.resource
        resource.set_state_erred()
        resource.backend_metadata.update({"state": "Erred"})
        resource.save(update_fields=["state", "backend_metadata"])
        scope = cast(structure_models.BaseResource, resource.scope)
        if scope:
            scope.set_erred()
            scope.save(update_fields=["state"])


@shared_task
def notify_user_that_order_been_rejected(order_uuid):
    try:
        order = models.Order.objects.get(uuid=order_uuid)
    except models.Order.DoesNotExist:
        logger.warning(
            f"Cannot send rejection notification: Order {order_uuid} not found."
        )
        return

    if not order.created_by.email:
        logger.warning(
            f"Cannot send rejection notification: Order {order_uuid} has no valid user email."
        )
        return

    link = core_utils.format_homeport_link(
        "marketplace-order-details/{order_uuid}/",
        order_uuid=order.uuid,
    )

    context = {
        "order_url": link,
        "order": order,
        "site_name": config.SITE_NAME,
        "order_type": order.get_type_display().lower(),
    }

    core_utils.broadcast_mail(
        "marketplace",
        "notification_to_user_that_order_been_rejected",
        context,
        [order.created_by.email],
    )


@shared_task(name="waldur_mastermind.marketplace.remove_deleted_robot_accounts")
def remove_deleted_robot_accounts():
    """
    Remove robot accounts that are in DELETED state.
    This task runs daily to clean up robot accounts that have been marked for deletion.
    """
    logger.info("Daily task: Removing deleted robot accounts")
    deleted_accounts = models.RobotAccount.objects.filter(
        state=RobotAccountStates.DELETED
    )
    count = deleted_accounts.count()
    deleted_accounts.delete()

    if count > 0:
        logger.info(f"Removed {count} robot accounts that were in DELETED state")
