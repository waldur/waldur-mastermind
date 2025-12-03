import collections
import datetime
import hashlib
import logging
from datetime import timedelta
from typing import cast

import requests
from celery import shared_task
from constance import config
from dateutil.relativedelta import relativedelta
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Q, Sum
from django.utils import timezone
from rest_framework import status

from waldur_core import _get_version
from waldur_core.checklist import models as checklist_models
from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.models import User
from waldur_core.logging import event_logger
from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.permissions.models import UserRole
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import get_connected_projects
from waldur_core.structure.models import Project
from waldur_mastermind.analytics import models as analytics_models
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices import utils as invoice_utils
from waldur_mastermind.marketplace import exceptions, models, plugins, utils
from waldur_mastermind.marketplace.catalog_loaders.eessi import EESSICatalogLoader
from waldur_mastermind.marketplace.catalog_loaders.spack import SpackCatalogLoader
from waldur_mastermind.marketplace.enums import (
    OfferingStates,
    OfferingUserStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
    RobotAccountStates,
)

# Delayed import to avoid circular import with handlers.py
from waldur_mastermind.marketplace.utils import (
    get_consumer_approvers,
    get_provider_approvers,
)

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
    """Create a thumbnail for a screenshot."""
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
        for project in structure_models.Project.available_objects.filter(
            customer=customer
        ):
            scopes.append(project)

    for scope in scopes:
        calculate_usage_for_scope(start, end, scope)


@shared_task
def terminate_resource(serialized_resource, serialized_user):
    """Terminate a resource."""
    resource = core_utils.deserialize_instance(serialized_resource)
    user = core_utils.deserialize_instance(serialized_user)
    response = utils.terminate_resource(resource, user)

    if response and response.status_code != status.HTTP_200_OK:
        raise exceptions.ResourceTerminateException(response.rendered_content)


@shared_task(
    name="waldur_mastermind.marketplace.terminate_resources_if_project_end_date_has_been_reached"
)
def terminate_resources_if_project_end_date_has_been_reached():
    """Terminate resources when their project has reached its end date (including grace period)."""
    today = timezone.datetime.today().date()

    # Find projects where the effective end date (including grace period) has passed
    expired_projects = []
    for project in structure_models.Project.available_objects.exclude(
        end_date__isnull=True
    ):
        if (
            project.get_effective_end_date()
            and project.get_effective_end_date() <= today
        ):
            expired_projects.append(project)

    for project in expired_projects:
        project_resources = models.Resource.objects.filter(project=project)
        active_resources = project_resources.exclude(state=ResourceStates.TERMINATED)

        if not active_resources:
            event_logger.emit(
                "Project {project_name} is going to be deleted because end date has been reached and there are no active resources.",
                event_type=EventType.PROJECT_DELETION_TRIGGERED,
                event_context={"project": project},
                scopes=[project, project.customer],
            )
            project.delete()
            continue

        # We expect that resources with parents will be removed when parents are removed
        terminatable_resources = project_resources.filter(
            state__in=(ResourceStates.OK, ResourceStates.ERRED),
            offering__parent=None,
        )
        logger.info(
            "About to terminate resources from expired project: %s",
            ",".join([f"{r.uuid}, {r.name}" for r in terminatable_resources]),
        )
        project.get_effective_end_date()
        grace_days = project.get_grace_period_days()
        termination_comment = f"Project effective end date (including {grace_days} day grace period) has been reached on {timezone.datetime.today()}"
        utils.schedule_resources_termination(
            terminatable_resources,
            termination_comment=termination_comment,
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
            type=OrderTypes.CREATE,
            state=OrderStates.ERRED,
        )
        .order_by("-created")
        .values_list("resource__uuid", flat=True)
    )

    # Get the uuids of resources that have a failed latest termination order
    resources_with_last_termination_order_erred = (
        models.Order.objects.filter(
            resource__in=resources,
            type=OrderTypes.TERMINATE,
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

    # skip sending if setting is unset
    if not config.TELEMETRY_URL:
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
        "helpdesk_backend": config.WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE,
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


@shared_task(name="waldur_mastermind.marketplace.process_pending_start_date_orders")
def process_pending_start_date_orders():
    """
    Finds orders that are pending activation due to a future start date
    and moves them to the EXECUTING state if the start date has been reached.
    """
    today = timezone.now().date()
    orders_to_process = models.Order.objects.filter(
        state=OrderStates.PENDING_START_DATE,
        start_date__lte=today,
    )

    for order in orders_to_process:
        logger.info(
            "Processing order %s (%s) as its start date %s has been reached.",
            order,
            order.id,
            order.start_date,
        )
        order.set_state_executing()
        order.save(update_fields=["state"])
        # Use transaction.on_commit to ensure the state change is saved
        # before the processing task is queued.
        transaction.on_commit(lambda: process_order_on_commit(order, order.created_by))


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
        continue_order_processing(order)


def continue_order_processing(order: models.Order):
    """
    Advances an order to the next logical state after consumer/project approval.
    Checks for provider review and the order's own start_date.
    """
    if utils.order_should_not_be_reviewed_by_provider(order):
        if order.start_date and order.start_date > timezone.now().date():
            order.state = models.OrderStates.PENDING_START_DATE
            order.save(update_fields=["state"])
        else:
            order.set_state_executing()
            order.save(update_fields=["state"])
            transaction.on_commit(
                lambda: process_order_on_commit(order, order.created_by)
            )
    else:
        order.state = models.OrderStates.PENDING_PROVIDER
        order.save(update_fields=["state"])
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


@shared_task(name="waldur_mastermind.marketplace.update_daily_consent_history")
def update_daily_consent_history():
    """
    Daily task to update consent history statistics for dashboard reporting.
    Uses quota system + DailyQuotaHistory for historical tracking.
    """
    today = timezone.now().date()
    yesterday = today - timedelta(days=1)

    offerings_with_tos = (
        models.Offering.objects.filter(terms_of_service_configs__is_active=True)
        .distinct()
        .annotate(
            active_users_count=Count(
                "offeringuser",
                filter=Q(offeringuser__state=models.OfferingUserStates.OK),
                distinct=True,
            ),
            total_users_count=Count("offeringuser", distinct=True),
            accepted_consents_count=Count(
                "user_consents",
                filter=Q(user_consents__revocation_date__isnull=True),
                distinct=True,
            ),
            revoked_consents_count=Count(
                "user_consents",
                filter=Q(user_consents__revocation_date__isnull=False),
                distinct=True,
            ),
            revoked_consents_today=Count(
                "user_consents",
                filter=Q(user_consents__revocation_date__date=yesterday),
                distinct=True,
            ),
        )
    )

    quota_records = []

    for offering in offerings_with_tos:
        total_consents_count = (
            offering.accepted_consents_count + offering.revoked_consents_count
        )

        offering.set_quota_usage("active_users_count", offering.active_users_count)
        offering.set_quota_usage("total_users_count", offering.total_users_count)
        offering.set_quota_usage(
            "accepted_consents_count", offering.accepted_consents_count
        )
        offering.set_quota_usage(
            "revoked_consents_count", offering.revoked_consents_count
        )
        offering.set_quota_usage("total_consents_count", total_consents_count)
        offering.set_quota_usage(
            "revoked_consents_today", offering.revoked_consents_today
        )

        quota_records.extend(
            [
                analytics_models.DailyQuotaHistory(
                    scope=offering,
                    name="active_users_count",
                    usage=offering.active_users_count,
                    date=today,
                ),
                analytics_models.DailyQuotaHistory(
                    scope=offering,
                    name="total_users_count",
                    usage=offering.total_users_count,
                    date=today,
                ),
                analytics_models.DailyQuotaHistory(
                    scope=offering,
                    name="accepted_consents_count",
                    usage=offering.accepted_consents_count,
                    date=today,
                ),
                analytics_models.DailyQuotaHistory(
                    scope=offering,
                    name="revoked_consents_count",
                    usage=offering.revoked_consents_count,
                    date=today,
                ),
                analytics_models.DailyQuotaHistory(
                    scope=offering,
                    name="total_consents_count",
                    usage=total_consents_count,
                    date=today,
                ),
                analytics_models.DailyQuotaHistory(
                    scope=offering,
                    name="revoked_consents_today",
                    usage=offering.revoked_consents_today,
                    date=today,
                ),
            ]
        )

    # Bulk create all quota history records
    analytics_models.DailyQuotaHistory.objects.bulk_create(
        quota_records, ignore_conflicts=True
    )

    updated_count = offerings_with_tos.count()

    if updated_count == 0:
        logger.info("No offerings with ToS found")
    else:
        logger.info(f"Updated consent history for {updated_count} offerings")


@shared_task(name="waldur_mastermind.marketplace.send_tos_consent_notification")
def send_tos_consent_notification(offering_uuid, user_uuid):
    """Send notification to user about required ToS consent."""
    try:
        offering = models.Offering.objects.get(uuid=offering_uuid)
        user = core_models.User.objects.get(uuid=user_uuid)
    except (models.Offering.DoesNotExist, core_models.User.DoesNotExist):
        logger.warning(
            "Cannot send ToS consent notification. Offering %s or User %s not found.",
            offering_uuid,
            user_uuid,
        )
        return

    if not offering.has_terms_of_service():
        return

    if offering.check_user_consent(user):
        return

    active_tos = offering.terms_of_service_configs.filter(is_active=True).first()
    if not active_tos:
        return

    tos_management_url = core_utils.format_homeport_link("profile/tos-management/")

    tos_link = active_tos.terms_of_service_link
    if not tos_link:
        tos_link = core_utils.format_homeport_link(
            "marketplace-public-offering/{offering_uuid}/",
            offering_uuid=offering.uuid.hex,
        )

    context = {
        "user": user,
        "offering": offering,
        "terms_of_service_link": tos_link,
        "tos_management_url": tos_management_url,
        "version": active_tos.version,
        "site_name": config.SITE_NAME,
    }

    logger.info(
        "Sending ToS consent notification to %s for offering %s",
        user.email,
        offering.name,
    )

    core_utils.broadcast_mail(
        "marketplace", "tos_consent_required", context, [user.email]
    )


@shared_task(name="waldur_mastermind.marketplace.send_tos_reconsent_notification")
def send_tos_reconsent_notification(offering_uuid, old_version, new_version):
    """Notify users about ToS update requiring re-consent."""
    try:
        offering = models.Offering.objects.get(uuid=offering_uuid)
    except models.Offering.DoesNotExist:
        logger.warning(
            "Cannot notify about ToS update. Offering %s not found.", offering_uuid
        )
        return

    active_tos = offering.terms_of_service_configs.filter(is_active=True).first()
    if not active_tos or not active_tos.requires_reconsent:
        return

    offering_users = models.OfferingUser.objects.filter(
        offering=offering
    ).select_related("user")

    tos_management_url = core_utils.format_homeport_link("profile/tos-management/")

    tos_link = active_tos.terms_of_service_link
    if not tos_link:
        tos_link = core_utils.format_homeport_link(
            "marketplace-public-offering/{offering_uuid}/",
            offering_uuid=offering.uuid.hex,
        )

    for offering_user in offering_users:
        user = offering_user.user

        if user.is_staff or user.is_support:
            continue

        consent = models.UserOfferingConsent.objects.filter(
            user=user, offering=offering, revocation_date__isnull=True
        ).first()

        if consent and consent.version == active_tos.version:
            continue

        context = {
            "user": user,
            "offering": offering,
            "terms_of_service_link": tos_link,
            "tos_management_url": tos_management_url,
            "old_version": old_version,
            "new_version": new_version,
            "site_name": config.SITE_NAME,
        }

        logger.info(
            "Sending ToS re-consent notification to %s for offering %s (v%s -> v%s)",
            user.email,
            offering.name,
            old_version,
            new_version,
        )

        core_utils.broadcast_mail(
            "marketplace", "tos_reconsent_required", context, [user.email]
        )


@shared_task(name="waldur_mastermind.marketplace.revoke_outdated_consents")
def revoke_outdated_consents():
    """
    Revoke consents for users who haven't re-consented within grace period.

    Finds all active ToS with requires_reconsent=True where grace period has expired,
    and revokes all consents that don't match the current active ToS version.
    """
    now = timezone.now()
    tos_with_expired_grace_period = [
        tos
        for tos in models.OfferingTermsOfService.objects.filter(
            is_active=True,
            requires_reconsent=True,
        ).select_related("offering")
        if tos.grace_period_end and tos.grace_period_end <= now
    ]
    if not tos_with_expired_grace_period:
        logger.info("No expired ToS found")
        return

    tos_with_expired_grace_period_by_offering = {
        tos.offering.id: tos for tos in tos_with_expired_grace_period
    }
    outdated_consents = list(
        models.UserOfferingConsent.objects.filter(
            offering__id__in=tos_with_expired_grace_period_by_offering.keys(),
            revocation_date__isnull=True,
        ).select_related("user", "offering", "offering__customer")
    )
    if not outdated_consents:
        logger.info("No outdated consents found to revoke")
        return

    consents_to_revoke = []
    for consent in outdated_consents:
        tos = tos_with_expired_grace_period_by_offering[consent.offering.id]
        if consent.version != tos.version:
            consents_to_revoke.append((consent, tos))

    if not consents_to_revoke:
        logger.info("All consents are up to date nothing to revoke")
        return

    # Add consents by offering for better logging
    consents_by_offering = {}
    for consent, tos in consents_to_revoke:
        if tos.offering.id not in consents_by_offering:
            consents_by_offering[tos.offering.id] = {
                "tos": tos,
                "consents": [],
            }
        consents_by_offering[tos.offering.id]["consents"].append(consent)

    consents_ids = [consent.id for consent, _ in consents_to_revoke]
    models.UserOfferingConsent.objects.filter(id__in=consents_ids).update(
        revocation_date=now
    )

    revoked_count = len(consents_to_revoke)

    for offering_id, data in consents_by_offering.items():
        tos = data["tos"]
        count = len(data["consents"])
        logger.info(
            "Revoked %d outdated consents for offering %s "
            "(ToS version %s, grace period expired on %s)",
            count,
            tos.offering.name,
            tos.version,
            tos.grace_period_end.isoformat(),
        )

    for consent, tos in consents_to_revoke:
        event_logger.emit(
            "User {user_name} consent for offering {offering_name} has been revoked "
            "due to expired grace period. Grace period ended on {grace_end}.",
            event_type=EventType.TERMS_OF_SERVICE_CONSENT_REVOKED,
            event_context={
                "user": consent.user,
                "offering": consent.offering,
                "consent": consent,
                "version": consent.version,
                "old_version": consent.version,
                "new_version": tos.version,
                "grace_period_end": tos.grace_period_end.isoformat(),
                "user_name": consent.user.full_name or consent.user.username,
                "offering_name": consent.offering.name,
                "revocation_date": now,
                "grace_end": tos.grace_period_end.isoformat(),
            },
            scopes=[consent.offering, consent.offering.customer],
        )

    logger.info(
        f"Total of {revoked_count} outdated consents revoked across all offerings"
    )


@shared_task(
    name="waldur_mastermind.marketplace.create_checklist_completions_for_offering_users"
)
def create_checklist_completions_for_offering_users(offering_id, checklist_id):
    """Background task to create checklist completions for existing offering users."""
    try:
        offering = models.Offering.objects.get(id=offering_id)
        checklist = checklist_models.Checklist.objects.get(id=checklist_id)
    except (models.Offering.DoesNotExist, checklist_models.Checklist.DoesNotExist) as e:
        logger.error(
            f"Failed to find offering {offering_id} or checklist {checklist_id}: {e}"
        )
        return

    logger.info(
        f"Starting checklist completion creation for offering '{offering.name}' "
        f"with checklist '{checklist.name}'"
    )

    # Get content type for OfferingUser
    offering_user_content_type = ContentType.objects.get_for_model(models.OfferingUser)

    # Get all existing offering users for this offering
    offering_user_ids = list(
        models.OfferingUser.objects.filter(offering=offering).values_list(
            "id", flat=True
        )
    )

    total_users = len(offering_user_ids)
    logger.info(f"Found {total_users} offering users to process")

    if total_users == 0:
        logger.info("No offering users found, nothing to do")
        return

    # Process in batches to avoid memory issues
    batch_size = 100
    created_count = 0
    skipped_count = 0

    for i in range(0, len(offering_user_ids), batch_size):
        batch_ids = offering_user_ids[i : i + batch_size]
        offering_users = models.OfferingUser.objects.filter(id__in=batch_ids)

        completions_to_create = []
        for offering_user in offering_users:
            # Check if completion already exists to avoid duplicates
            existing_completion = checklist_models.ChecklistCompletion.objects.filter(
                scope_content_type=offering_user_content_type,
                scope_object_id=offering_user.id,
                checklist=checklist,
            ).exists()

            if not existing_completion:
                completions_to_create.append(
                    checklist_models.ChecklistCompletion(
                        scope_content_type=offering_user_content_type,
                        scope_object_id=offering_user.id,
                        checklist=checklist,
                    )
                )
            else:
                skipped_count += 1

        # Bulk create for efficiency
        if completions_to_create:
            checklist_models.ChecklistCompletion.objects.bulk_create(
                completions_to_create, ignore_conflicts=True
            )
            created_count += len(completions_to_create)

        # Log progress for large batches
        batch_num = i // batch_size + 1
        total_batches = (len(offering_user_ids) - 1) // batch_size + 1
        logger.info(
            f"Processed batch {batch_num}/{total_batches}: "
            f"created {len(completions_to_create)} completions"
        )

    logger.info(
        f"Checklist completion creation completed for offering '{offering.name}': "
        f"created {created_count}, skipped {skipped_count} (already existed), "
        f"total {total_users} users processed"
    )


@shared_task(
    name="waldur_mastermind.marketplace.remove_checklist_completions_for_offering_users"
)
def remove_checklist_completions_for_offering_users(offering_id, checklist_id):
    """Background task to remove checklist completions when compliance is removed."""
    try:
        offering = models.Offering.objects.get(id=offering_id)
        checklist = checklist_models.Checklist.objects.get(id=checklist_id)
    except (models.Offering.DoesNotExist, checklist_models.Checklist.DoesNotExist) as e:
        logger.error(
            f"Failed to find offering {offering_id} or checklist {checklist_id}: {e}"
        )
        return

    logger.info(
        f"Starting checklist completion removal for offering '{offering.name}' "
        f"with checklist '{checklist.name}'"
    )

    # Get content type for OfferingUser
    offering_user_content_type = ContentType.objects.get_for_model(models.OfferingUser)

    # Get all offering users for this offering
    offering_user_ids = list(
        models.OfferingUser.objects.filter(offering=offering).values_list(
            "id", flat=True
        )
    )

    total_users = len(offering_user_ids)
    logger.info(f"Found {total_users} offering users to process for removal")

    if total_users == 0:
        logger.info("No offering users found, nothing to remove")
        return

    # Remove completions in batches to avoid memory issues
    batch_size = 100
    deleted_count = 0

    for i in range(0, len(offering_user_ids), batch_size):
        batch_ids = offering_user_ids[i : i + batch_size]

        # Delete completions for this batch
        deleted_in_batch = checklist_models.ChecklistCompletion.objects.filter(
            scope_content_type=offering_user_content_type,
            scope_object_id__in=batch_ids,
            checklist=checklist,
        ).delete()[0]

        deleted_count += deleted_in_batch

        # Log progress for large batches
        batch_num = i // batch_size + 1
        total_batches = (len(offering_user_ids) - 1) // batch_size + 1
        logger.info(
            f"Processed batch {batch_num}/{total_batches}: "
            f"deleted {deleted_in_batch} completions"
        )

    logger.info(
        f"Checklist completion removal completed for offering '{offering.name}': "
        f"deleted {deleted_count} completions for {total_users} users"
    )


@shared_task(
    name="waldur_mastermind.marketplace.replace_checklist_completions_for_offering_users"
)
def replace_checklist_completions_for_offering_users(
    offering_id, old_checklist_id, new_checklist_id
):
    """Background task to replace checklist completions when checklist is changed."""
    try:
        offering = models.Offering.objects.get(id=offering_id)
        old_checklist = checklist_models.Checklist.objects.get(id=old_checklist_id)
        new_checklist = checklist_models.Checklist.objects.get(id=new_checklist_id)
    except (
        models.Offering.DoesNotExist,
        checklist_models.Checklist.DoesNotExist,
    ) as e:
        logger.error(
            f"Failed to find offering {offering_id} or checklists {old_checklist_id}/{new_checklist_id}: {e}"
        )
        return

    logger.info(
        f"Starting checklist completion replacement for offering '{offering.name}': "
        f"'{old_checklist.name}' → '{new_checklist.name}'"
    )

    # Get content type for OfferingUser
    offering_user_content_type = ContentType.objects.get_for_model(models.OfferingUser)

    # Get all offering users for this offering
    offering_user_ids = list(
        models.OfferingUser.objects.filter(offering=offering).values_list(
            "id", flat=True
        )
    )

    total_users = len(offering_user_ids)
    logger.info(f"Found {total_users} offering users to process for replacement")

    if total_users == 0:
        logger.info("No offering users found, nothing to replace")
        return

    # Process in batches
    batch_size = 100
    deleted_count = 0
    created_count = 0

    for i in range(0, len(offering_user_ids), batch_size):
        batch_ids = offering_user_ids[i : i + batch_size]

        # First, remove old completions
        deleted_in_batch = checklist_models.ChecklistCompletion.objects.filter(
            scope_content_type=offering_user_content_type,
            scope_object_id__in=batch_ids,
            checklist=old_checklist,
        ).delete()[0]

        deleted_count += deleted_in_batch

        # Then create new completions for users in this batch
        offering_users = models.OfferingUser.objects.filter(id__in=batch_ids)
        completions_to_create = []

        for offering_user in offering_users:
            completions_to_create.append(
                checklist_models.ChecklistCompletion(
                    scope_content_type=offering_user_content_type,
                    scope_object_id=offering_user.id,
                    checklist=new_checklist,
                )
            )

        # Bulk create new completions
        if completions_to_create:
            checklist_models.ChecklistCompletion.objects.bulk_create(
                completions_to_create, ignore_conflicts=True
            )
            created_count += len(completions_to_create)

        # Log progress
        batch_num = i // batch_size + 1
        total_batches = (len(offering_user_ids) - 1) // batch_size + 1
        logger.info(
            f"Processed batch {batch_num}/{total_batches}: "
            f"deleted {deleted_in_batch}, created {len(completions_to_create)} completions"
        )

    logger.info(
        f"Checklist completion replacement completed for offering '{offering.name}': "
        f"deleted {deleted_count} old completions, created {created_count} new completions "
        f"for {total_users} users"
    )


@shared_task(
    name="waldur_mastermind.marketplace.request_offering_user_deletion_for_user"
)
def request_offering_user_deletion_for_user(user_uuid: str):
    """
    Request offering user deletion when user loses project access.
    Handles both provisioned (OK state) and unprovisioned (creation states) offering users.
    """
    user = User.objects.get(uuid=user_uuid)
    connected_projects = get_connected_projects(user)
    offering_users_to_request_deletion = (
        models.OfferingUser.objects.filter(
            user=user,
            state__in=[
                OfferingUserStates.OK,
                OfferingUserStates.CREATING,
                OfferingUserStates.PENDING_ACCOUNT_LINKING,
                OfferingUserStates.PENDING_ADDITIONAL_VALIDATION,
                OfferingUserStates.ERROR_CREATING,
            ],
        )
        .annotate(
            has_resources=Exists(
                models.Resource.objects.filter(
                    offering=OuterRef("offering"),
                    project__in=connected_projects,
                ).exclude(state=ResourceStates.TERMINATED)
            )
        )
        .filter(has_resources=False)
    )

    for offering_user in offering_users_to_request_deletion:
        logger.info(
            "User %s has no access to resources in offering %s, requesting deletion.",
            user,
            offering_user.offering,
        )
        offering_user.request_deletion()
        offering_user.save(update_fields=["state"])

    offering_users_not_provisioned = (
        models.OfferingUser.objects.filter(
            user=user,
            state__in=[
                OfferingUserStates.CREATION_REQUESTED,
            ],
        )
        .annotate(
            has_resources=Exists(
                models.Resource.objects.filter(
                    offering=OuterRef("offering"),
                    project__in=connected_projects,
                ).exclude(state=ResourceStates.TERMINATED)
            )
        )
        .filter(has_resources=False)
    )

    for offering_user in offering_users_not_provisioned:
        logger.info(
            "User %s has no access to resources in offering %s (state: %s), marking as DELETED.",
            user,
            offering_user.offering,
            offering_user.get_state_display(),
        )
        offering_user.set_deleted()
        offering_user.save(update_fields=["state"])


@shared_task(
    name="waldur_mastermind.marketplace.create_or_restore_offering_users_for_user"
)
def create_or_restore_offering_users_for_user(user_uuid: str, project_uuid: str):
    """
    Create or restore offering users when user gains project access.
    Handles both new creation and restoration of offering users in deletion states.
    """
    from waldur_mastermind.marketplace.handlers import (
        OFFERING_USER_ALLOWED_OFFERING_TYPES,
    )

    user = User.objects.get(uuid=user_uuid)
    project = structure_models.Project.objects.get(uuid=project_uuid)

    resources = project.resource_set.filter(
        state=ResourceStates.OK,
        offering__type__in=OFFERING_USER_ALLOWED_OFFERING_TYPES,
    )
    offering_ids = set(resources.values_list("offering_id", flat=True))
    offerings = models.Offering.objects.filter(id__in=offering_ids)

    for offering in offerings:
        if not offering.plugin_options.get("service_provider_can_create_offering_user"):
            logger.info(
                "It is not allowed to create users for current offering %s.", offering
            )
            continue

        offering_user = models.OfferingUser.objects.filter(
            offering=offering,
            user=user,
        ).first()

        if offering_user:
            # Restore offering user if it's in deletion flow
            if offering_user.state in [
                OfferingUserStates.DELETION_REQUESTED,
                OfferingUserStates.DELETING,
                OfferingUserStates.ERROR_DELETING,
            ]:
                old_state = offering_user.get_state_display()
                if offering_user.username:
                    # Account exists on service provider - restore to OK
                    offering_user.set_ok()
                    offering_user.save(update_fields=["state"])
                    event_logger.emit(
                        f"Account for user {offering_user.user.username} in offering {offering_user.offering.name} has been restored from {old_state} to OK because user regained project access.",
                        event_type=EventType.MARKETPLACE_OFFERING_USER_UPDATED,
                        event_context={"offering_user": offering_user},
                        scopes=[
                            offering_user.offering,
                            offering_user.offering.customer,
                        ],
                    )
                else:
                    offering_user.state = OfferingUserStates.CREATION_REQUESTED
                    offering_user.save(update_fields=["state"])
                    event_logger.emit(
                        f"Account creation for user {offering_user.user.username} in offering {offering_user.offering.name} has been requested (was in {old_state}) because user regained project access.",
                        event_type=EventType.MARKETPLACE_OFFERING_USER_UPDATED,
                        event_context={"offering_user": offering_user},
                        scopes=[
                            offering_user.offering,
                            offering_user.offering.customer,
                        ],
                    )
            elif offering_user.state == OfferingUserStates.DELETED:
                # DELETED state - request new account creation
                offering_user.state = OfferingUserStates.CREATION_REQUESTED
                offering_user.save(update_fields=["state"])
                event_logger.emit(
                    f"New account creation for user {offering_user.user.username} in offering {offering_user.offering.name} has been requested because user regained project access after offering user was deleted.",
                    event_type=EventType.MARKETPLACE_OFFERING_USER_UPDATED,
                    event_context={"offering_user": offering_user},
                    scopes=[offering_user.offering, offering_user.offering.customer],
                )
            else:
                logger.info(
                    "An offering user for %s in %s already exists", user, offering
                )
            continue

        # Create new offering user
        username = utils.generate_username(user, offering)
        state = (
            OfferingUserStates.OK if username else OfferingUserStates.CREATION_REQUESTED
        )
        offering_user = models.OfferingUser.objects.create(
            offering=offering,
            user=user,
            username=username,
            state=state,
        )
        utils.setup_linux_related_data(offering_user, offering)
        offering_user.save(update_fields=["backend_metadata"])

        logger.info("The offering user %s has been created", offering_user)


@shared_task(name="waldur_mastermind.marketplace.cleanup_stale_offering_users")
def cleanup_stale_offering_users():
    """
    Periodic task to clean up offering users who no longer have project access.
    """
    logger.info("Starting cleanup of stale offering users")

    user_ids = (
        models.OfferingUser.objects.exclude(
            state__in=[
                OfferingUserStates.DELETION_REQUESTED,
                OfferingUserStates.DELETING,
                OfferingUserStates.DELETED,
            ]
        )
        .values_list("user_id", flat=True)
        .distinct()
    )
    users = User.objects.filter(id__in=user_ids)
    for user in users:
        request_offering_user_deletion_for_user.delay(user.uuid.hex)

    logger.info(f"Scheduled cleanup tasks for {len(users)} users with offering users")


@shared_task(name="marketplace.update_software_catalogs")
def update_software_catalogs():
    """
    Daily task to update all enabled software catalogs.

    Updates EESSI, Spack, and other configured catalogs independently.
    Each catalog is processed in isolation - if one fails, others continue.
    """
    logger.info("Starting software catalogs update")

    results = {}

    # Define catalog configurations
    catalog_configs = [
        {
            "name": "EESSI",
            "enabled_setting": "SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED",
            "loader_class": EESSICatalogLoader,
            "loader_kwargs": {
                "catalog_name": "EESSI",
                "catalog_version": config.SOFTWARE_CATALOG_EESSI_VERSION or "auto",
                "api_base_url": config.SOFTWARE_CATALOG_EESSI_API_URL,
                "include_extensions": config.SOFTWARE_CATALOG_EESSI_INCLUDE_EXTENSIONS,
            },
            "catalog_type": "binary_runtime",
        },
        {
            "name": "Spack",
            "enabled_setting": "SOFTWARE_CATALOG_SPACK_UPDATE_ENABLED",
            "loader_class": SpackCatalogLoader,
            "loader_kwargs": {
                "catalog_name": "Spack",
                "catalog_version": config.SOFTWARE_CATALOG_SPACK_VERSION or "auto",
                "data_url": config.SOFTWARE_CATALOG_SPACK_DATA_URL,
            },
            "catalog_type": "source_package",
        },
    ]

    # Process each catalog independently with full exception isolation
    for catalog_config in catalog_configs:
        catalog_name = catalog_config["name"]

        try:
            # Check if catalog is enabled
            enabled = getattr(config, catalog_config["enabled_setting"], False)
            if not enabled:
                logger.info(f"{catalog_name} catalog update is disabled via settings")
                results[catalog_name.lower()] = {
                    "status": "skipped",
                    "reason": "disabled",
                }
                continue

            # Validate configuration before proceeding
            validation_errors = _validate_catalog_config(catalog_config)
            if validation_errors:
                raise Exception(
                    f"Configuration validation failed: {', '.join(validation_errors)}"
                )

            logger.info(f"Updating {catalog_name} catalog")

            # Create loader instance with error handling
            try:
                loader_class = catalog_config["loader_class"]
                loader_kwargs = catalog_config["loader_kwargs"]
                loader = loader_class(**loader_kwargs)
            except Exception as loader_error:
                raise Exception(
                    f"Failed to initialize {catalog_name} loader: {loader_error}"
                ) from loader_error

            # Update catalog with full error isolation
            try:
                catalog = _update_catalog_with_error_handling(
                    loader=loader,
                    catalog_name=catalog_name,
                    catalog_type=catalog_config["catalog_type"],
                )
            except Exception as update_error:
                raise Exception(
                    f"Failed to update {catalog_name} catalog: {update_error}"
                ) from update_error

            # Record success
            results[catalog_name.lower()] = {
                "status": "success",
                "catalog_uuid": str(catalog.uuid),
                "catalog_name": catalog.name,
                "catalog_version": catalog.version,
                "last_update": catalog.last_successful_update.isoformat()
                if catalog.last_successful_update
                else None,
            }
            logger.info(f"{catalog_name} catalog update completed successfully")

        except Exception as e:
            # Log error but continue with next catalog
            error_msg = f"{catalog_name} catalog update failed: {e}"
            logger.error(error_msg, exc_info=True)

            results[catalog_name.lower()] = {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
            }

            # Continue processing other catalogs
            continue

    # Aggregate final results
    success_count = sum(1 for r in results.values() if r.get("status") == "success")
    error_count = sum(1 for r in results.values() if r.get("status") == "error")
    skipped_count = sum(1 for r in results.values() if r.get("status") == "skipped")

    # Determine overall task status
    overall_status = "completed"
    if error_count > 0 and success_count == 0:
        overall_status = "failed"  # All catalogs failed
    elif error_count > 0:
        overall_status = "partial"  # Some catalogs failed

    summary = {
        "status": overall_status,
        "catalogs_updated": success_count,
        "catalogs_failed": error_count,
        "catalogs_skipped": skipped_count,
        "total_catalogs": len(catalog_configs),
        "update_time": timezone.now().isoformat(),
        "results": results,
    }

    # Log appropriate level based on results
    if overall_status == "failed":
        logger.error(f"All software catalog updates failed: {summary}")
    elif overall_status == "partial":
        logger.warning(f"Some software catalog updates failed: {summary}")
    else:
        logger.info(f"Software catalog updates completed successfully: {summary}")

    return summary


def _update_catalog_with_error_handling(loader, catalog_name: str, catalog_type: str):
    """
    Helper to update catalog with proper error handling and logging.

    Args:
        loader: Catalog loader instance
        catalog_name: Name of the catalog
        catalog_type: Type of the catalog

    Returns:
        Updated SoftwareCatalog instance

    Raises:
        Exception: If catalog update fails
    """
    catalog = None
    catalog_created = False

    try:
        # Find or create catalog record for tracking
        catalog, catalog_created = models.SoftwareCatalog.objects.get_or_create(
            name=catalog_name,
            version=loader.catalog_version,
            catalog_type=catalog_type,
            defaults={
                "description": f"{catalog_name} software catalog",
                "auto_update_enabled": True,
            },
        )

        # Update attempt timestamp
        catalog.last_update_attempt = timezone.now()
        catalog.save(update_fields=["last_update_attempt"])

        # Perform the actual update
        update_existing = config.SOFTWARE_CATALOG_UPDATE_EXISTING_PACKAGES
        stats = loader.load_catalog(update_existing=update_existing, dry_run=False)

        # Update success timestamp and clear errors
        catalog.last_successful_update = timezone.now()
        catalog.update_errors = ""
        catalog.save(update_fields=["last_successful_update", "update_errors"])

        logger.info(f"Successfully updated {catalog_name} catalog: {stats}")
        return catalog

    except Exception as e:
        # Handle catalog cleanup on failure
        error_msg = f"Catalog update failed: {e}"
        if catalog:
            if catalog_created:
                # If we created the catalog object and update failed, remove it
                catalog.delete()
                logger.debug(f"Removed failed catalog object for {catalog_name}")
            else:
                # If catalog existed before, just log the error
                catalog.update_errors = error_msg
                catalog.save(update_fields=["update_errors"])

        raise e


def _validate_catalog_config(catalog_config):
    """
    Validate catalog configuration to prevent runtime errors.

    Returns:
        List of validation error messages (empty if valid)
    """
    errors = []
    catalog_name = catalog_config["name"]
    loader_kwargs = catalog_config.get("loader_kwargs", {})

    # Validate EESSI-specific configuration
    if catalog_name == "EESSI":
        api_url = loader_kwargs.get("api_base_url", "")
        if not api_url or not api_url.startswith("http"):
            errors.append("EESSI API URL must be a valid HTTP/HTTPS URL")

    # Validate Spack-specific configuration
    elif catalog_name == "Spack":
        data_url = loader_kwargs.get("data_url", "")
        if not data_url or not data_url.startswith("http"):
            errors.append("Spack data URL must be a valid HTTP/HTTPS URL")

    # Validate common fields
    if not loader_kwargs.get("catalog_name"):
        errors.append("Catalog name is required")

    return errors


@shared_task(name="waldur_mastermind.marketplace.remove_users_from_robot_accounts")
def remove_users_from_robot_accounts_on_permission_loss(user_role_id):
    """
    Remove users from robot accounts when they lose active membership in a project.

    This task is triggered when a user's role is revoked and checks if they should
    be removed from robot accounts they no longer have access to.

    Args:
        user_role_id: ID of the UserRole that was revoked
    """
    logger.info(
        f"Processing robot account cleanup for revoked user role {user_role_id}"
    )

    try:
        # Get the revoked user role
        try:
            user_role = UserRole.objects.get(id=user_role_id)
        except UserRole.DoesNotExist:
            logger.warning(
                f"UserRole {user_role_id} not found, skipping robot account cleanup"
            )
            return

        user = user_role.user
        scope = user_role.scope

        # Only process project-related role revocations
        if not isinstance(scope, Project):
            logger.debug(f"Role revocation for {user} is not project-related, skipping")
            return

        project = scope
        logger.info(
            f"Processing robot account cleanup for user {user} in project {project}"
        )

        # Check if user still has any active roles in this project
        remaining_roles = UserRole.objects.filter(
            user=user, scope=project, is_active=True
        ).exists()

        if remaining_roles:
            logger.debug(
                f"User {user} still has active roles in project {project}, keeping robot account access"
            )
            return

        # Find all robot accounts in this project that the user has access to
        robot_accounts = models.RobotAccount.objects.filter(
            resource__project=project, users=user
        )

        removed_count = 0
        for robot_account in robot_accounts:
            # Remove user from robot account
            robot_account.users.remove(user)
            removed_count += 1
            logger.info(f"Removed user {user} from robot account {robot_account}")

            # Log the user removal event
            event_logger.emit(
                "User {user_username} has been removed from robot account {robot_account_username} due to loss of project access.",
                event_type=EventType.RESOURCE_ROBOT_ACCOUNT_UPDATED,
                event_context={
                    "robot_account": robot_account,
                    "user": user,
                    "project": project,
                    "reason": "project_access_revoked",
                },
                scopes=[robot_account, robot_account.resource, project, user],
            )

            # Also check if user was the responsible_user and remove if they no longer have access
            if robot_account.responsible_user == user:
                robot_account.responsible_user = None
                robot_account.save(update_fields=["responsible_user"])
                logger.info(
                    f"Cleared responsible_user {user} from robot account {robot_account}"
                )

                # Log the responsible user removal event
                event_logger.emit(
                    "Responsible user {user_username} has been removed from robot account {robot_account_username} due to loss of project access.",
                    event_type=EventType.RESOURCE_ROBOT_ACCOUNT_UPDATED,
                    event_context={
                        "robot_account": robot_account,
                        "user": user,
                        "project": project,
                        "reason": "project_access_revoked",
                        "action": "responsible_user_cleared",
                    },
                    scopes=[robot_account, robot_account.resource, project, user],
                )

        if removed_count > 0:
            logger.info(
                f"Successfully removed user {user} from {removed_count} robot accounts in project {project}"
            )
        else:
            logger.debug(
                f"No robot accounts found for user {user} in project {project}"
            )

    except Exception as e:
        logger.error(
            f"Error removing users from robot accounts for role {user_role_id}: {e}",
            exc_info=True,
        )
        raise


@shared_task(name="waldur_mastermind.marketplace.reconcile_robot_account_access")
def reconcile_robot_account_access():
    """
    Reconciliation task to ensure robot account access is properly maintained.

    This task periodically checks all robot accounts and removes users who
    no longer have active project access, serving as a backup to the
    signal-driven cleanup.
    """
    logger.info("Starting robot account access reconciliation")

    try:
        total_accounts_processed = 0
        total_users_removed = 0

        # Get all robot accounts with users
        robot_accounts = (
            models.RobotAccount.objects.filter(users__isnull=False)
            .prefetch_related("users", "resource__project")
            .distinct()
        )

        for robot_account in robot_accounts:
            total_accounts_processed += 1
            project = robot_account.resource.project
            users_to_remove = []

            # Check each user's access to the project
            for user in robot_account.users.all():
                # Check if user has any active roles in this project
                has_active_access = UserRole.objects.filter(
                    user=user, scope=project, is_active=True
                ).exists()

                if not has_active_access:
                    users_to_remove.append(user)

            # Remove users who no longer have access
            for user in users_to_remove:
                robot_account.users.remove(user)
                total_users_removed += 1
                logger.info(
                    f"Reconciliation: Removed user {user} from robot account {robot_account}"
                )

                # Log the reconciliation event
                event_logger.emit(
                    "User {user_username} has been removed from robot account {robot_account_username} during access reconciliation.",
                    event_type=EventType.RESOURCE_ROBOT_ACCOUNT_UPDATED,
                    event_context={
                        "robot_account": robot_account,
                        "user": user,
                        "project": project,
                        "reason": "access_reconciliation",
                    },
                    scopes=[robot_account, robot_account.resource, project, user],
                )

                # Also check responsible_user during reconciliation
                if robot_account.responsible_user == user:
                    robot_account.responsible_user = None
                    robot_account.save(update_fields=["responsible_user"])
                    logger.info(
                        f"Reconciliation: Cleared responsible_user {user} from robot account {robot_account}"
                    )

                    # Log the responsible user reconciliation event
                    event_logger.emit(
                        "Responsible user {user_username} has been removed from robot account {robot_account_username} during access reconciliation.",
                        event_type=EventType.RESOURCE_ROBOT_ACCOUNT_UPDATED,
                        event_context={
                            "robot_account": robot_account,
                            "user": user,
                            "project": project,
                            "reason": "access_reconciliation",
                            "action": "responsible_user_cleared",
                        },
                        scopes=[robot_account, robot_account.resource, project, user],
                    )

        logger.info(
            f"Robot account access reconciliation completed. "
            f"Processed {total_accounts_processed} accounts, "
            f"removed {total_users_removed} users."
        )

        return {
            "accounts_processed": total_accounts_processed,
            "users_removed": total_users_removed,
        }

    except Exception as e:
        logger.error(
            f"Error during robot account access reconciliation: {e}", exc_info=True
        )
        raise
