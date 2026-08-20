import collections
import datetime
import decimal
import hashlib
import logging
import uuid as uuid_mod
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import cast

import httpx
import requests
from celery import shared_task
from constance import config
from dateutil.relativedelta import relativedelta
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Q, Subquery, Sum
from django.utils import timezone
from django_fsm import TransitionNotAllowed
from rest_framework import status

from waldur_core import _get_version
from waldur_core.checklist import models as checklist_models
from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.models import User
from waldur_core.logging import event_logger
from waldur_core.logging import models as logging_models
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging.enums import EventType, ObservableObjectType
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.permissions.models import RoleAvailability, UserRole
from waldur_core.structure import models as structure_models
from waldur_core.structure import tasks as structure_tasks
from waldur_core.structure.managers import get_connected_projects
from waldur_core.structure.models import Project
from waldur_mastermind.analytics import models as analytics_models
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices import utils as invoice_utils
from waldur_mastermind.invoices.models import InvoiceItem
from waldur_mastermind.marketplace import (
    exceptions,
    models,
    plugins,
    utils,
)
from waldur_mastermind.marketplace.catalog_loaders.eessi import EESSICatalogLoader
from waldur_mastermind.marketplace.catalog_loaders.spack import SpackCatalogLoader
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    LimitPeriods,
    MaintenanceState,
    OfferingStates,
    OfferingUserStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
    RobotAccountStates,
    UsageLimitAction,
)

# Delayed import to avoid circular import with handlers.py
from waldur_mastermind.marketplace.utils import (
    evaluate_usage_limit_restriction,
    get_consumer_approvers,
    get_new_order_notification_recipients,
    get_provider_approvers,
)

logger = logging.getLogger(__name__)


@shared_task(name="waldur_mastermind.marketplace.evaluate_usage_limit_restriction")
def evaluate_usage_limit_restriction_task(resource_id):
    """Re-evaluate the usage-limit restriction for a single resource."""
    resource = models.Resource.objects.filter(pk=resource_id).first()
    if resource is not None:
        evaluate_usage_limit_restriction(resource)


@shared_task(name="waldur_mastermind.marketplace.re_evaluate_usage_limit_restrictions")
def re_evaluate_usage_limit_restrictions():
    """Lift usage-limit restrictions when a new period resets reported usage.

    A restriction applied by ``evaluate_usage_limit_restriction`` is normally
    lifted on the next usage report. When a new billing period begins and no new
    usage is reported, this periodic task re-evaluates every currently restricted
    resource so month/quarter/annual restrictions clear on rollover. ``total``
    restrictions never roll over and are only lifted when the limit is raised.
    """
    restricted = models.Resource.objects.exclude(usage_limit_restriction="").filter(
        offering__plugin_options__action_on_usage_limit__in=(
            UsageLimitAction.PAUSE,
            UsageLimitAction.DOWNSCALE,
        )
    )
    for resource in restricted:
        evaluate_usage_limit_restriction(resource)


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
def create_course_account_task(course_account_uuid_hex: str, owner_username: str):
    """Create a single course account via the external API.

    Called per-item during bulk creation so each account is processed
    independently — a failure on one does not block the others.
    """
    try:
        course_account = models.CourseAccount.objects.get(uuid=course_account_uuid_hex)
    except models.CourseAccount.DoesNotExist:
        logger.error(
            "CourseAccount %s not found, skipping task", course_account_uuid_hex
        )
        return

    try:
        response_data = utils.create_course_account(
            {"email": course_account.email, "project": course_account.project},
            owner_username,
        )
        if response_data is None:
            # API disabled in settings — leave record in PENDING state
            return
        temp_account = response_data.get("tempAccount", {})
        user, _ = core_models.User.objects.get_or_create(
            username=temp_account["username"],
            defaults={
                "email": temp_account.get("email", course_account.email),
                "description": "Course Account",
            },
        )
        course_account.user = user
        course_account.set_state_ok()
        course_account.save(update_fields=["user", "state"])
    except Exception as exc:
        logger.error(
            "Failed to create course account %s: %s", course_account_uuid_hex, exc
        )
        if isinstance(exc, httpx.HTTPStatusError):
            error_message = str(utils.extract_error_details_from_httpx_error(exc))
        else:
            error_message = str(exc)
        course_account.error_message = error_message
        course_account.set_state_erred()
        course_account.save(update_fields=["error_message", "state"])


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
def notify_about_new_order(order_uuid):
    order = models.Order.objects.get(uuid=order_uuid)

    recipients = get_new_order_notification_recipients(order)
    if not recipients:
        return

    link = core_utils.format_homeport_link(
        "marketplace-order-details/{order_uuid}/",
        order_uuid=order.uuid,
    )

    context = {
        "order_url": link,
        "order": order,
        "order_type": order.get_type_display().lower(),
        "site_name": config.SITE_NAME,
    }

    logger.info(
        "About to send email regarding new order %s to recipients: %s",
        order,
        recipients,
    )

    core_utils.broadcast_mail(
        "marketplace", "notify_about_new_order", context, recipients
    )


@shared_task
def notify_consumer_about_provider_info(order_uuid):
    order = models.Order.objects.get(uuid=order_uuid)

    # Send pubsub message regardless of email notification settings
    messages = utils.prepare_messages(
        order.offering,
        {
            "order_uuid": order.uuid.hex,
            "action": "provider_info_set",
        },
        ObservableObjectType.ORDER,
    )
    if messages:
        logging_tasks.publish_messages.delay(messages)

    if not order.offering.plugin_options.get("notify_about_provider_consumer_messages"):
        return

    recipients = set()
    if order.created_by and order.created_by.email:
        recipients.add(order.created_by.email)
    if (
        order.consumer_reviewed_by
        and order.consumer_reviewed_by.email
        and order.consumer_reviewed_by.notifications_enabled
    ):
        recipients.add(order.consumer_reviewed_by.email)

    if not recipients:
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
        "Sending provider info notification for order %s to %s",
        order,
        recipients,
    )

    core_utils.broadcast_mail(
        "marketplace",
        "notify_consumer_about_provider_info",
        context,
        list(recipients),
    )


@shared_task
def notify_provider_about_consumer_info(order_uuid):
    order = models.Order.objects.get(uuid=order_uuid)

    # Send pubsub message regardless of email notification settings
    messages = utils.prepare_messages(
        order.offering,
        {
            "order_uuid": order.uuid.hex,
            "action": "consumer_info_set",
        },
        ObservableObjectType.ORDER,
    )
    if messages:
        logging_tasks.publish_messages.delay(messages)

    if not order.offering.plugin_options.get("notify_about_provider_consumer_messages"):
        return

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
        "Sending consumer info notification for order %s to %s",
        order,
        approvers,
    )

    core_utils.broadcast_mail(
        "marketplace",
        "notify_provider_about_consumer_info",
        context,
        list(approvers),
    )


@shared_task
def notify_about_resource_change(event_type, context, resource_uuid):
    resource = models.Resource.objects.get(uuid=resource_uuid)
    emails = resource.project.get_user_mails()
    core_utils.broadcast_mail("marketplace", event_type, context, emails)


def _bulk_aggregate_reported_usage(start, end, scope_field):
    """Aggregate reported usage in bulk, grouped by scope and component."""
    queryset = (
        models.ComponentUsage.objects.filter(date__date__gte=start, date__date__lte=end)
        .exclude(component__parent=None)
        .values(scope_id=F(scope_field), component_parent_id=F("component__parent_id"))
        .annotate(total=Sum("usage"))
    )
    result = collections.defaultdict(dict)
    for row in queryset:
        result[row["scope_id"]][row["component_parent_id"]] = row["total"]
    return result


def _bulk_aggregate_fixed_usage(start, end, scope_field):
    """Aggregate fixed usage in bulk, grouped by scope and component."""
    queryset = (
        models.ResourcePlanPeriod.objects.filter(
            Q(start__gte=start, end__lte=end)
            | Q(end__isnull=True)
            | Q(end__gte=start, end__lte=end)
        )
        .values(
            scope_id=F(scope_field),
            component_parent_id=F("plan__components__component__parent_id"),
        )
        .annotate(total=Sum("plan__components__amount"))
    )
    result = collections.defaultdict(dict)
    for row in queryset:
        result[row["scope_id"]][row["component_parent_id"]] = row["total"]
    return result


@shared_task(name="waldur_mastermind.marketplace.calculate_usage_for_current_month")
def calculate_usage_for_current_month():
    """Calculate marketplace resource usage for the current month across all customers and projects."""
    start = invoice_utils.get_current_month_start()
    end = invoice_utils.get_current_month_end()

    # Bulk aggregate for projects (4 queries instead of 2*N)
    project_reported = _bulk_aggregate_reported_usage(
        start, end, "resource__project_id"
    )
    project_fixed = _bulk_aggregate_fixed_usage(start, end, "resource__project_id")

    customer_reported = _bulk_aggregate_reported_usage(
        start, end, "resource__project__customer_id"
    )
    customer_fixed = _bulk_aggregate_fixed_usage(
        start, end, "resource__project__customer_id"
    )

    project_ct = ContentType.objects.get_for_model(structure_models.Project)
    customer_ct = ContentType.objects.get_for_model(structure_models.Customer)

    # Collect the target usage values keyed by (content_type_id, object_id,
    # component_id) so the writes can be performed in bulk instead of issuing a
    # SELECT ... FOR UPDATE + UPDATE/INSERT per scope/component pair (N+1).
    desired = {}

    def collect(content_type, object_id, reported_usage, fixed_usage):
        fixed_usage.pop(None, None)
        for component_id in set(reported_usage.keys()) | set(fixed_usage.keys()):
            desired[(content_type.id, object_id, component_id)] = (
                reported_usage.get(component_id),
                fixed_usage.get(component_id),
            )

    for customer in structure_models.Customer.objects.all():
        collect(
            customer_ct,
            customer.id,
            customer_reported.get(customer.id, {}),
            customer_fixed.get(customer.id, {}),
        )

    # Every available project belongs to a customer, so iterating them once is
    # equivalent to the previous per-customer filtering without the extra query.
    for project in structure_models.Project.available_objects.all():
        collect(
            project_ct,
            project.id,
            project_reported.get(project.id, {}),
            project_fixed.get(project.id, {}),
        )

    existing = {
        (usage.content_type_id, usage.object_id, usage.component_id): usage
        for usage in models.CategoryComponentUsage.objects.filter(
            date=start,
            content_type__in=[customer_ct, project_ct],
        )
    }

    to_create = []
    to_update = []
    for (content_type_id, object_id, component_id), (
        reported,
        fixed,
    ) in desired.items():
        usage = existing.get((content_type_id, object_id, component_id))
        if usage is None:
            to_create.append(
                models.CategoryComponentUsage(
                    content_type_id=content_type_id,
                    object_id=object_id,
                    component_id=component_id,
                    date=start,
                    reported_usage=reported,
                    fixed_usage=fixed,
                )
            )
        else:
            usage.reported_usage = reported
            usage.fixed_usage = fixed
            to_update.append(usage)

    if to_create:
        models.CategoryComponentUsage.objects.bulk_create(to_create)
    if to_update:
        models.CategoryComponentUsage.objects.bulk_update(
            to_update, ["reported_usage", "fixed_usage"]
        )


@shared_task(name="waldur_mastermind.marketplace.sync_component_usage_summaries")
def sync_component_usage_summaries():
    """
    Runs nightly to keep the current month's ComponentUsageMonthly records up to date.
    """
    from django.core.management import call_command

    # Re-use the command, but only calculate the current month (months=0)
    call_command("init_component_usage_reporting", months=0)


def calculate_consumed_for_month(
    component: models.OfferingComponent, year: int, month: int
) -> Decimal:
    """
    Calculates the total consumption for a component in a specific billing period.
    Source of truth: ComponentUsage records for the specific month.
    """
    consumption_agg = models.ComponentUsage.objects.filter(
        component=component,
        billing_period__year=year,
        billing_period__month=month,
    ).aggregate(total=Sum("usage"))

    total_consumed = consumption_agg["total"]

    if total_consumed is not None and total_consumed > 0:
        return Decimal(total_consumed)

    now = timezone.now()
    is_current_month = year == now.year and month == now.month

    if is_current_month:
        resources = models.Resource.objects.filter(
            offering=component.offering,
            modified__year=year,
            modified__month=month,
        ).only("current_usages")

        fallback_total = Decimal("0")
        for resource in resources:
            # Safely check if current_usages is a dictionary and has the key
            if not resource.current_usages or not isinstance(
                resource.current_usages, dict
            ):
                continue

            if component.type in resource.current_usages:
                usage_val = resource.current_usages.get(component.type, 0)
                try:
                    fallback_total += Decimal(str(usage_val))
                except (ValueError, TypeError, decimal.InvalidOperation):
                    continue

        return fallback_total

    return Decimal("0")


def calculate_allocated_for_month(
    component: models.OfferingComponent, year: int, month: int
) -> Decimal:
    """
    Calculates the total allocated limit for a component in a specific billing period.

    Logic mapping:
    - Current Month: Reads live `Resource.limits` (JSONB). Falls back to `OfferingComponent.limit_amount`.
    - Historical TOTAL: Cumulative sum of all incremental `InvoiceItem.quantity` up to the target month.
    - Historical QUARTERLY/ANNUAL: Finds the latest `InvoiceItem` for each resource within the cycle window.
    - Historical MONTH: Sums `InvoiceItem.quantity` strictly for the target month.
    """
    now = timezone.now()
    is_current_month = year == now.year and month == now.month
    target_date = datetime.date(year, month, 1)

    # Global cap defined on the offering component level (common for USAGE billing types)
    global_limit = Decimal(str(component.limit_amount or 0))

    # --- 1. CURRENT MONTH LOGIC ---
    if is_current_month:
        valid_states = [
            ResourceStates.OK,
            ResourceStates.UPDATING,
            ResourceStates.TERMINATING,
            ResourceStates.ERRED,
        ]

        # We query resources that have an explicit limit set for this component
        resources = models.Resource.objects.filter(
            offering=component.offering,
            state__in=valid_states,
            limits__has_key=component.type,
        ).only("limits")

        total_allocated = Decimal("0")
        has_custom_limits = False

        for resource in resources:
            limit_val = resource.limits.get(component.type, 0)
            try:
                total_allocated += Decimal(str(limit_val))
                has_custom_limits = True
            except (ValueError, TypeError, InvalidOperation):
                continue

        # If no individual resources have custom limits set, and the component provides
        # a global limit_amount (e.g., hard cap for USAGE components), we return that.
        if total_allocated == Decimal("0") and not has_custom_limits:
            return global_limit

        return total_allocated

    # --- 2. HISTORICAL MONTH LOGIC ---
    # For past months, the immutable InvoiceItem ledger is the source of truth.

    # If the component is not a LIMIT type, it doesn't generate limit-based InvoiceItems.
    # Therefore, its historical limit is simply the static global limit.
    if component.billing_type != BillingTypes.LIMIT:
        return global_limit

    # Rule A: TOTAL Limits (Incremental billing)
    # The system writes positive/negative diffs. We must calculate the cumulative sum.
    if component.limit_period == LimitPeriods.TOTAL:
        items_agg = (
            InvoiceItem.objects.filter(plan_component__component=component)
            .filter(
                # All years before the target year, OR earlier/same month in the target year
                Q(invoice__year__lt=year)
                | Q(invoice__year=year, invoice__month__lte=month)
            )
            .aggregate(total=Sum("quantity"))
        )

        return Decimal(str(items_agg["total"] or 0))

    # Rule B: QUARTERLY or ANNUAL Limits
    # Invoice items are only generated in the first month of the cycle.
    # To find the limit for an off-cycle month, we look back through the cycle window.
    elif component.limit_period in (LimitPeriods.QUARTERLY, LimitPeriods.ANNUAL):
        months_back = 3 if component.limit_period == LimitPeriods.QUARTERLY else 12
        cycle_start = target_date - relativedelta(months=months_back - 1)

        # Fetch items from the start of the cycle year onwards to ensure we catch the invoice
        items = InvoiceItem.objects.filter(
            plan_component__component=component,
            invoice__year__gte=cycle_start.year,
        ).select_related("invoice")

        # We need to map {resource_id: latest_quantity}
        # because a resource limit might have been updated during the cycle.
        latest_allocations = {}

        for item in items:
            item_date = datetime.date(item.invoice.year, item.invoice.month, 1)

            # Only consider items that fall in the lookback window exactly preceding the target month
            if cycle_start <= item_date <= target_date:
                resource_id = item.resource_id

                if resource_id not in latest_allocations:
                    latest_allocations[resource_id] = {
                        "date": item_date,
                        "qty": item.quantity,
                    }
                elif item_date > latest_allocations[resource_id]["date"]:
                    latest_allocations[resource_id] = {
                        "date": item_date,
                        "qty": item.quantity,
                    }

        total_allocated = sum(data["qty"] for data in latest_allocations.values())
        return Decimal(str(total_allocated or 0))

    # Rule C: MONTH Limits (or unhandled fallback)
    # Items are billed strictly every single month. We just sum the exact target month.
    else:
        items_agg = InvoiceItem.objects.filter(
            plan_component__component=component,
            invoice__year=year,
            invoice__month=month,
        ).aggregate(total=Sum("quantity"))

        return Decimal(str(items_agg["total"] or 0))


@shared_task
def terminate_resource(serialized_resource, serialized_user):
    """Terminate a resource."""
    resource = core_utils.deserialize_instance(serialized_resource)
    user = core_utils.deserialize_instance(serialized_user)
    response = utils.terminate_resource(resource, user)

    if response and response.status_code != status.HTTP_200_OK:
        raise exceptions.ResourceTerminateException(response.rendered_content)


def _ready_for_scheduled_termination_filter() -> Q:
    """Resources eligible for the daily end-date termination sweep.

    Includes resources still in OK/ERRED, plus resources already stuck in
    TERMINATING because an earlier termination request is awaiting consumer
    approval (state=PENDING_CONSUMER) that the requester couldn't grant
    themselves — otherwise such a resource would never be revisited by the
    sweep, since it no longer matches state__in=(OK, ERRED).
    """
    return Q(state__in=(ResourceStates.OK, ResourceStates.ERRED)) | Q(
        state=ResourceStates.TERMINATING,
        order__type=OrderTypes.TERMINATE,
        order__state=OrderStates.PENDING_CONSUMER,
    )


@shared_task(
    name="waldur_mastermind.marketplace.terminate_resources_if_project_end_date_has_been_reached"
)
def terminate_resources_if_project_end_date_has_been_reached():
    """Terminate resources when their project has reached its end date (including grace period).

    Also pauses resources for projects currently in the grace period,
    if the offering has supports_pausing=True in plugin_options.
    """
    today = timezone.datetime.today().date()

    # Single pass over projects with an end date: pause resources that are inside
    # the grace period, and terminate resources whose offering opts out of the
    # grace period once the raw end date is reached.
    for project in structure_models.Project.available_objects.exclude(
        end_date__isnull=True
    ):
        # Pause resources for projects currently IN grace period.
        # Offerings that disable the grace period are excluded here — their
        # resources are terminated on the project end date (see below), not paused.
        if project.is_in_grace_period:
            resources_to_pause = (
                models.Resource.objects.filter(
                    project=project,
                    offering__plugin_options__supports_pausing=True,
                    paused=False,
                )
                .exclude(
                    state__in=(ResourceStates.TERMINATED, ResourceStates.TERMINATING),
                )
                # A plain .exclude(disable_grace_period=True) would also drop rows
                # where the key is absent: the JSON lookup is SQL NULL and NOT(NULL)
                # is NULL, so guard the value check with has_key to exclude only
                # offerings that actually opted in.
                .exclude(
                    offering__plugin_options__has_key="disable_grace_period",
                    offering__plugin_options__disable_grace_period=True,
                )
            )
            for resource in resources_to_pause:
                resource.paused = True
                resource.save(update_fields=["paused"])
                logger.info(
                    "Resource %s paused due to project %s entering grace period",
                    resource.uuid,
                    project.uuid,
                )
                event_logger.emit(
                    "Resource {resource_name} has been paused because "
                    "project {project_name} has entered the grace period.",
                    event_type=EventType.MARKETPLACE_RESOURCE_PAUSED,
                    event_context={
                        "resource": resource,
                        "project": project,
                    },
                    scopes=[resource, project, project.customer],
                )

        # Terminate resources whose offering disables the grace period as soon as
        # the project end date is reached, ignoring the grace window. Projects
        # whose effective end date (incl. grace) has fully passed are handled by
        # the expired-projects loop below (which terminates every resource), so
        # restrict this to projects still inside their grace window to avoid
        # scheduling the same resource twice.
        effective_end_date = project.get_effective_end_date()
        if (
            project.end_date <= today
            and effective_end_date
            and effective_end_date > today
        ):
            grace_disabled_resources = models.Resource.objects.filter(
                _ready_for_scheduled_termination_filter(),
                project=project,
                offering__parent=None,
                offering__plugin_options__disable_grace_period=True,
            ).distinct()
            # schedule_resources_termination is a no-op on an empty queryset, so
            # no explicit emptiness guard is needed here.
            termination_comment = (
                f"Project end date has been reached on {timezone.datetime.today()}; "
                "grace period disabled for this offering."
            )
            utils.schedule_resources_termination(
                grace_disabled_resources,
                termination_comment=termination_comment,
            )

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
            _ready_for_scheduled_termination_filter(),
            offering__parent=None,
        ).distinct()
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


@shared_task(name="waldur_mastermind.marketplace.reset_stuck_updating_resources")
def reset_stuck_updating_resources():
    """
    Reset marketplace resources stuck in UPDATING state.

    This task handles two scenarios where a resource remains in UPDATING state:

    1. The resource's UPDATE order has been completed (state=DONE) but the resource
       state wasn't transitioned to OK due to a race condition.

    2. The resource was set to UPDATING by a backend operation (e.g., sync/pull)
       without an order, but the operation finished without updating the state.
       In this case, if no UPDATE order exists or is executing, and the resource
       has been stuck for more than 1 hour, it is reset to OK.

    For each stuck resource, the task transitions it to OK state.
    """
    # Subquery to get the latest UPDATE order state for each resource
    latest_update_order_state = (
        models.Order.objects.filter(
            resource=OuterRef("pk"),
            type=OrderTypes.UPDATE,
        )
        .order_by("-created")
        .values("state")[:1]
    )

    # Check if there's any executing UPDATE order for the resource
    has_executing_update_order = models.Order.objects.filter(
        resource=OuterRef("pk"),
        type=OrderTypes.UPDATE,
        state=OrderStates.EXECUTING,
    )

    # Find all resources stuck in UPDATING state
    stuck_resources = (
        models.Resource.objects.filter(
            state=ResourceStates.UPDATING,
        )
        .annotate(
            latest_update_order_state=Subquery(latest_update_order_state),
            has_executing_order=Exists(has_executing_update_order),
        )
        .filter(
            # Case 1: Latest UPDATE order is completed (DONE)
            # If the most recent order is done, reset regardless of older orders
            Q(latest_update_order_state=OrderStates.DONE)
            |
            # Case 2: No executing order and stuck for more than 1 hour
            # This handles resources set to UPDATING by backend operations without orders
            Q(
                has_executing_order=False,
                modified__lt=timezone.now() - datetime.timedelta(hours=1),
            )
        )
    )

    for resource in stuck_resources:
        try:
            reason = (
                "UPDATE order is completed"
                if resource.latest_update_order_state == OrderStates.DONE
                else "no active UPDATE order and stuck for over 1 hour"
            )
            logger.info(
                "Resetting stuck resource %s (UUID: %s) from UPDATING to OK "
                "because %s.",
                resource.name,
                resource.uuid.hex,
                reason,
            )
            resource.set_state_ok()
            resource.save(update_fields=["state"])
        except Exception as e:
            logger.exception(
                "Failed to reset stuck resource %s (UUID: %s): %s",
                resource.name,
                resource.uuid.hex,
                str(e),
            )


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
        _ready_for_scheduled_termination_filter(),
        end_date__lte=timezone.datetime.today(),
    ).distinct()
    logger.info(
        "About to terminate expired resources: %s",
        ",".join([f"{r.uuid}, {r.name}" for r in expired_resources]),
    )
    utils.schedule_resources_termination(
        expired_resources,
        termination_comment=f"Resource expired on {timezone.datetime.today()}",
    )


@shared_task(
    name="waldur_mastermind.marketplace.process_maintenance_announcement_transitions"
)
def process_maintenance_announcement_transitions():
    """Advance scheduled maintenance announcements based on their time window.

    Auto-starts scheduled announcements once their start time has passed and
    auto-completes in-progress announcements once their end time has passed.
    Mirrors the manual start/complete actions so the AdminAnnouncement banner is
    refreshed by the existing post_save handler.
    """
    now = timezone.now()

    # .order_by() clears the model's default ordering; row order is irrelevant.
    to_start = models.MaintenanceAnnouncement.objects.filter(
        state=MaintenanceState.SCHEDULED, scheduled_start__lte=now
    ).order_by()
    for maintenance in to_start:
        try:
            # Use the scheduled time, not now(): the task may run late or catch
            # up after downtime, where now() would misrepresent the window.
            maintenance.actual_start = maintenance.scheduled_start
            maintenance.start_maintenance()
            maintenance.save(update_fields=["state", "actual_start", "modified"])
            logger.info(
                "Auto-started maintenance announcement %s (%s).",
                maintenance.uuid,
                maintenance.name,
            )
        except TransitionNotAllowed:
            # Benign race with a concurrent run or manual action; already moved on.
            logger.debug(
                "Skipping auto-start of maintenance announcement %s: no longer schedulable.",
                maintenance.uuid,
            )
        except Exception:
            logger.exception(
                "Failed to auto-start maintenance announcement %s.", maintenance.uuid
            )

    to_complete = models.MaintenanceAnnouncement.objects.filter(
        state=MaintenanceState.IN_PROGRESS, scheduled_end__lte=now
    ).order_by()
    for maintenance in to_complete:
        try:
            maintenance.actual_end = maintenance.scheduled_end
            maintenance.complete_maintenance()
            maintenance.save(update_fields=["state", "actual_end", "modified"])
            logger.info(
                "Auto-completed maintenance announcement %s (%s).",
                maintenance.uuid,
                maintenance.name,
            )
        except TransitionNotAllowed:
            logger.debug(
                "Skipping auto-complete of maintenance announcement %s: no longer in progress.",
                maintenance.uuid,
            )
        except Exception:
            logger.exception(
                "Failed to auto-complete maintenance announcement %s.", maintenance.uuid
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
    """Send notifications about resources ending in 1 day and 7 days.

    "Ending" is the resource's effective end date — the earliest of its own end
    date and the project-driven termination date. Resources whose offering
    disables the grace period terminate on the raw project end date (earlier than
    the project's effective end date that notification_about_project_ending
    announces), so they are pulled in by their project's raw end date even without
    an own end date — but only when the project has an actual grace window,
    otherwise raw == effective and the project-ending notice already covers them.
    """
    today = timezone.datetime.today().date()
    date_1 = today + datetime.timedelta(days=1)
    date_7 = today + datetime.timedelta(days=7)

    candidate_resources = (
        models.Resource.objects.filter(
            Q(end_date__in=(date_1, date_7))
            | Q(
                project__end_date__in=(date_1, date_7),
                offering__parent=None,
                offering__plugin_options__disable_grace_period=True,
            )
        )
        .exclude(state__in=(ResourceStates.TERMINATED, ResourceStates.TERMINATING))
        .select_related("project", "project__customer", "offering")
    )

    for resource in candidate_resources:
        effective_end_date = resource.effective_end_date
        if effective_end_date not in (date_1, date_7):
            continue
        # Resources pulled in only by their project's raw end date are
        # grace-disabled; skip them when the project has no grace window, since
        # then raw == the project's effective end date and the project-ending
        # notice already covers them on the same day.
        own_ending = resource.end_date in (date_1, date_7)
        if not own_ending and resource.project.get_grace_period_days() <= 0:
            continue

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
                "delta": (effective_end_date - today).days,
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
                OfferingStates.UNAVAILABLE,
            )
        ).count(),
        "types_of_offering": list(
            models.Offering.objects.filter(
                state__in=(
                    OfferingStates.ACTIVE,
                    OfferingStates.PAUSED,
                    OfferingStates.UNAVAILABLE,
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
    try:
        response = requests.post(url, json=params, timeout=30)
    except requests.RequestException as e:
        # Telemetry is best-effort; network failures should not surface as
        # task errors.
        logger.warning("Failed to send telemetry metrics to %s: %s", url, e)
        return None

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
            offering__plugin_options__offering_user_auto_deletion=True,
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
            offering__plugin_options__offering_user_auto_deletion=True,
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


def _get_eligible_offerings_for_project(project):
    """Return offerings in a project that support offering user creation."""
    from waldur_mastermind.marketplace.handlers import (
        OFFERING_USER_ALLOWED_OFFERING_TYPES,
    )

    resources = (
        project.resource_set.filter(
            offering__type__in=OFFERING_USER_ALLOWED_OFFERING_TYPES,
        )
        .filter(
            Q(state=ResourceStates.OK)
            | Q(
                state=ResourceStates.CREATING,
                order__type=OrderTypes.CREATE,
                order__state__in=[
                    OrderStates.PENDING_PROVIDER,
                    OrderStates.EXECUTING,
                ],
            )
        )
        .distinct()
    )
    offering_ids = set(resources.values_list("offering_id", flat=True))
    offerings = models.Offering.objects.filter(id__in=offering_ids)

    return [
        o
        for o in offerings
        if o.plugin_options.get("service_provider_can_create_offering_user")
    ]


def _create_or_restore_offering_user(user, offering):
    """Create or restore a single offering user for a given user and offering."""
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
            logger.info("An offering user for %s in %s already exists", user, offering)
        return

    # Create new offering user
    username = utils.generate_username(user, offering)
    state = OfferingUserStates.OK if username else OfferingUserStates.CREATION_REQUESTED
    offering_user, created = models.OfferingUser.objects.get_or_create(
        offering=offering,
        user=user,
        defaults={
            "username": username,
            "state": state,
        },
    )
    if not created:
        logger.info("An offering user for %s in %s already exists", user, offering)
        return
    utils.setup_linux_related_data(offering_user, offering)
    offering_user.save(update_fields=["backend_metadata"])

    logger.info("The offering user %s has been created", offering_user)


@shared_task(
    name="waldur_mastermind.marketplace.create_or_restore_offering_users_for_user"
)
def create_or_restore_offering_users_for_user(user_uuid: str, project_uuid: str):
    """
    Create or restore offering users when user gains project access.
    Handles both new creation and restoration of offering users in deletion states.
    """
    user = User.objects.get(uuid=user_uuid)
    project = structure_models.Project.objects.get(uuid=project_uuid)

    for offering in _get_eligible_offerings_for_project(project):
        _create_or_restore_offering_user(user, offering)


@shared_task(
    name="waldur_mastermind.marketplace.create_or_restore_offering_users_for_project"
)
def create_or_restore_offering_users_for_project(project_uuid: str):
    """
    Create or restore offering users for ALL users in a project.
    More efficient than dispatching one task per user — queries
    resources and offerings once instead of N times.
    """
    project = structure_models.Project.objects.get(uuid=project_uuid)
    eligible_offerings = _get_eligible_offerings_for_project(project)

    if not eligible_offerings:
        return

    users = project.get_users()
    for user in users:
        for offering in eligible_offerings:
            _create_or_restore_offering_user(user, offering)


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

            if catalog_name == "EESSI":
                # EESSI: update ALL existing catalogs, each with its own version
                eessi_catalogs = list(
                    models.SoftwareCatalog.objects.filter(
                        name="EESSI",
                        catalog_type=catalog_config["catalog_type"],
                    )
                )
                if not eessi_catalogs:
                    results[catalog_name.lower()] = {
                        "status": "skipped",
                        "reason": "no_existing_catalog",
                    }
                    continue

                updated_catalogs = []
                for eessi_catalog in eessi_catalogs:
                    try:
                        loader_kwargs = dict(catalog_config["loader_kwargs"])
                        loader_kwargs["catalog_version"] = eessi_catalog.version
                        loader = catalog_config["loader_class"](**loader_kwargs)
                    except Exception as loader_error:
                        raise Exception(
                            f"Failed to initialize {catalog_name} loader for version {eessi_catalog.version}: {loader_error}"
                        ) from loader_error

                    try:
                        update_existing = (
                            config.SOFTWARE_CATALOG_UPDATE_EXISTING_PACKAGES
                        )
                        eessi_catalog.last_update_attempt = timezone.now()
                        eessi_catalog.save(update_fields=["last_update_attempt"])

                        loader.load_catalog(
                            update_existing=update_existing,
                            dry_run=False,
                            catalog=eessi_catalog,
                            sync=True,
                        )

                        eessi_catalog.last_successful_update = timezone.now()
                        eessi_catalog.update_errors = ""
                        eessi_catalog.save(
                            update_fields=["last_successful_update", "update_errors"]
                        )
                        updated_catalogs.append(eessi_catalog)
                    except Exception as update_error:
                        eessi_catalog.update_errors = (
                            f"Catalog update failed: {update_error}"
                        )
                        eessi_catalog.save(update_fields=["update_errors"])
                        raise Exception(
                            f"Failed to update {catalog_name} catalog version {eessi_catalog.version}: {update_error}"
                        ) from update_error

                # Record success for all EESSI catalogs
                results[catalog_name.lower()] = {
                    "status": "success",
                    "catalogs_updated": len(updated_catalogs),
                    "catalog_versions": [c.version for c in updated_catalogs],
                }
                logger.info(
                    f"{catalog_name} catalog update completed for {len(updated_catalogs)} catalog(s)"
                )
            else:
                # Non-EESSI catalogs: use standard single-catalog update
                try:
                    loader_class = catalog_config["loader_class"]
                    loader_kwargs = catalog_config["loader_kwargs"]
                    loader = loader_class(**loader_kwargs)
                except Exception as loader_error:
                    raise Exception(
                        f"Failed to initialize {catalog_name} loader: {loader_error}"
                    ) from loader_error

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

                if catalog is None:
                    results[catalog_name.lower()] = {
                        "status": "skipped",
                        "reason": "no_existing_catalog",
                    }
                    continue

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

    Only updates existing catalogs — does not create new ones.
    If no catalog exists for the given name+type, returns None so the
    daily task skips it instead of producing orphaned catalog records.

    Args:
        loader: Catalog loader instance
        catalog_name: Name of the catalog
        catalog_type: Type of the catalog

    Returns:
        Updated SoftwareCatalog instance, or None if no existing catalog found
    """
    # Lookup by name + catalog_type only - version is updated, not used as lookup key.
    # Use filter().first() instead of get_or_create because the unique constraint
    # includes version, so multiple catalogs with the same name+type but different
    # versions may exist (PUHURI-PORTALS-EF7).
    catalog = (
        models.SoftwareCatalog.objects.filter(
            name=catalog_name,
            catalog_type=catalog_type,
        )
        .order_by("-modified")
        .first()
    )

    if catalog is None:
        logger.warning(
            f"No existing {catalog_name} catalog found (type={catalog_type}). "
            "Skipping update — create the catalog via the API or management command first."
        )
        return None

    try:
        # Update version if it changed
        if catalog.version != loader.catalog_version:
            catalog.version = loader.catalog_version
            catalog.save(update_fields=["version"])

        # Update attempt timestamp
        catalog.last_update_attempt = timezone.now()
        catalog.save(update_fields=["last_update_attempt"])

        # Perform the actual update
        update_existing = config.SOFTWARE_CATALOG_UPDATE_EXISTING_PACKAGES
        stats = loader.load_catalog(
            update_existing=update_existing, dry_run=False, catalog=catalog
        )

        # Update success timestamp and clear errors
        catalog.last_successful_update = timezone.now()
        catalog.update_errors = ""
        catalog.save(update_fields=["last_successful_update", "update_errors"])

        logger.info(f"Successfully updated {catalog_name} catalog: {stats}")
        return catalog

    except Exception as e:
        error_msg = f"Catalog update failed: {e}"
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


def _get_catalog_configs():
    """Return the list of known catalog configurations."""
    return [
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


NAME_TO_CATALOG_TYPE = {
    "EESSI": "binary_runtime",
    "Spack": "source_package",
}


@shared_task(name="marketplace.import_software_catalog")
def import_software_catalog(name, catalog_type):
    """Import a new software catalog by creating the record and loading data.

    Args:
        name: Catalog name (e.g. "EESSI", "Spack")
        catalog_type: Catalog type (e.g. "binary_runtime", "source_package")
    """
    logger.info(f"Importing software catalog: {name} ({catalog_type})")

    catalog_configs = _get_catalog_configs()
    catalog_config = next(
        (
            c
            for c in catalog_configs
            if c["name"] == name and c["catalog_type"] == catalog_type
        ),
        None,
    )
    if catalog_config is None:
        raise ValueError(f"No loader configuration found for {name} ({catalog_type})")

    loader_class = catalog_config["loader_class"]
    loader_kwargs = catalog_config["loader_kwargs"]
    loader = loader_class(**loader_kwargs)

    catalog = models.SoftwareCatalog.objects.create(
        name=name,
        catalog_type=catalog_type,
        version=loader.catalog_version,
    )
    catalog.last_update_attempt = timezone.now()
    catalog.save(update_fields=["last_update_attempt"])

    try:
        update_existing = config.SOFTWARE_CATALOG_UPDATE_EXISTING_PACKAGES
        loader.load_catalog(
            update_existing=update_existing, dry_run=False, catalog=catalog
        )

        catalog.last_successful_update = timezone.now()
        catalog.update_errors = ""
        catalog.save(update_fields=["last_successful_update", "update_errors"])
        logger.info(f"Successfully imported {name} catalog (uuid={catalog.uuid})")
    except Exception as e:
        error_msg = f"Catalog import failed: {e}"
        catalog.update_errors = error_msg
        catalog.save(update_fields=["update_errors"])
        logger.error(error_msg, exc_info=True)
        raise


@shared_task(name="marketplace.update_single_software_catalog")
def update_single_software_catalog(catalog_uuid_hex):
    """Trigger an async update for a single existing software catalog.

    Args:
        catalog_uuid_hex: Hex string of the catalog UUID
    """
    catalog = models.SoftwareCatalog.objects.get(uuid=uuid_mod.UUID(catalog_uuid_hex))
    logger.info(
        f"Updating single software catalog: {catalog.name} ({catalog.catalog_type})"
    )

    catalog_configs = _get_catalog_configs()
    catalog_config = next(
        (
            c
            for c in catalog_configs
            if c["name"] == catalog.name and c["catalog_type"] == catalog.catalog_type
        ),
        None,
    )
    if catalog_config is None:
        raise ValueError(
            f"No loader configuration found for {catalog.name} ({catalog.catalog_type})"
        )

    loader_class = catalog_config["loader_class"]
    loader_kwargs = dict(catalog_config["loader_kwargs"])
    # Use the catalog's stored version instead of auto-detecting.
    # Without this, auto-detect picks the latest version (e.g. 2025.06)
    # and older catalogs (e.g. 2023.06) never get their data loaded.
    loader_kwargs["catalog_version"] = catalog.version
    loader = loader_class(**loader_kwargs)

    try:
        update_existing = config.SOFTWARE_CATALOG_UPDATE_EXISTING_PACKAGES
        catalog.last_update_attempt = timezone.now()
        catalog.save(update_fields=["last_update_attempt"])

        loader.load_catalog(
            update_existing=update_existing,
            dry_run=False,
            catalog=catalog,
            sync=True,
        )

        catalog.last_successful_update = timezone.now()
        catalog.update_errors = ""
        catalog.save(update_fields=["last_successful_update", "update_errors"])
    except Exception as e:
        error_msg = f"Catalog update failed: {e}"
        logger.error(error_msg, exc_info=True)
        catalog.update_errors = error_msg
        catalog.save(update_fields=["update_errors"])
        raise

    logger.info(f"Successfully updated catalog {catalog.name} (uuid={catalog.uuid})")


@shared_task(name="marketplace.cleanup_old_software_catalogs")
def cleanup_old_software_catalogs():
    """
    Periodic task to clean up old and duplicate software catalog data.

    This task performs two cleanup operations:
    1. Removes duplicate catalogs, keeping only the newest one per (name, catalog_type)
    2. Removes catalogs that haven't been updated within the retention period

    This task respects the SOFTWARE_CATALOG_CLEANUP_ENABLED setting.
    """
    if not config.SOFTWARE_CATALOG_CLEANUP_ENABLED:
        logger.info("Software catalog cleanup is disabled")
        return {"status": "disabled", "deleted_count": 0, "duplicates_deleted": 0}

    retention_days = config.SOFTWARE_CATALOG_RETENTION_DAYS
    cutoff_date = timezone.now() - timedelta(days=retention_days)

    logger.info(
        f"Starting software catalog cleanup (retention: {retention_days} days, "
        f"cutoff: {cutoff_date.date()})"
    )

    deleted_count = 0
    deleted_catalogs = []

    # Step 1: Remove duplicate catalogs (keep only newest per name/catalog_type)
    duplicates_deleted = _cleanup_duplicate_catalogs()
    deleted_count += duplicates_deleted

    # Step 2: Find catalogs that haven't been updated within the retention period
    old_catalogs = models.SoftwareCatalog.objects.filter(
        last_successful_update__lt=cutoff_date
    )

    for catalog in old_catalogs:
        catalog_info = {
            "name": catalog.name,
            "version": catalog.version,
            "reason": "retention_expired",
            "last_update": catalog.last_successful_update.isoformat()
            if catalog.last_successful_update
            else None,
        }

        logger.info(
            f"Deleting old catalog {catalog.name} v{catalog.version} "
            f"(exceeded {retention_days} day retention)"
        )

        catalog.delete()
        deleted_count += 1
        deleted_catalogs.append(catalog_info)

    result = {
        "status": "success",
        "deleted_count": deleted_count,
        "duplicates_deleted": duplicates_deleted,
        "retention_days": retention_days,
        "cutoff_date": cutoff_date.isoformat(),
        "deleted_catalogs": deleted_catalogs,
    }

    if deleted_count > 0:
        logger.info(
            f"Software catalog cleanup completed: {deleted_count} catalogs deleted "
            f"({duplicates_deleted} duplicates)"
        )
    else:
        logger.info("Software catalog cleanup completed: no catalogs to delete")

    return result


def _cleanup_duplicate_catalogs():
    """
    Remove duplicate catalogs, keeping only the newest one per (name, catalog_type).

    Updates OfferingSoftwareCatalog references before deletion to preserve relationships.
    Returns the count of deleted duplicate catalogs.
    """
    deleted_count = 0

    # Find unique (name, catalog_type) combinations with more than one catalog
    from django.db.models import Count

    duplicated_groups = (
        models.SoftwareCatalog.objects.values("name", "catalog_type")
        .annotate(count=Count("id"))
        .filter(count__gt=1)
    )

    for group in duplicated_groups:
        # Get all catalogs for this group, newest first
        catalogs = list(
            models.SoftwareCatalog.objects.filter(
                name=group["name"], catalog_type=group["catalog_type"]
            ).order_by("-last_successful_update", "-created")
        )

        if len(catalogs) <= 1:
            continue

        # Keep the newest catalog
        newest_catalog = catalogs[0]
        catalogs_to_delete = catalogs[1:]

        logger.info(
            f"Found {len(catalogs_to_delete)} duplicate catalogs for "
            f"{group['name']}/{group['catalog_type']}, keeping v{newest_catalog.version}"
        )

        # Update OfferingSoftwareCatalog references to point to newest catalog
        # Skip offerings that already link to the newest catalog
        existing_offering_ids = models.OfferingSoftwareCatalog.objects.filter(
            catalog=newest_catalog
        ).values_list("offering_id", flat=True)

        for old_catalog in catalogs_to_delete:
            updated = (
                models.OfferingSoftwareCatalog.objects.filter(catalog=old_catalog)
                .exclude(offering_id__in=existing_offering_ids)
                .update(catalog=newest_catalog)
            )
            if updated:
                logger.info(
                    f"Migrated {updated} offering references from "
                    f"v{old_catalog.version} to v{newest_catalog.version}"
                )

            logger.info(
                f"Deleting duplicate catalog {old_catalog.name} v{old_catalog.version}"
            )
            old_catalog.delete()
            deleted_count += 1

    return deleted_count


@shared_task(name="waldur_mastermind.marketplace.update_resource_scope_availability")
def update_resource_scope_availability(offering_uuid, can_be_managed):
    with transaction.atomic():
        for resource in models.Resource.objects.filter(offering__uuid=offering_uuid):
            if not resource.scope:
                continue

            if not hasattr(resource.scope, "can_be_managed"):
                continue

            resource.scope.can_be_managed = can_be_managed
            resource.scope.save(update_fields=["can_be_managed"])

    logger.info(
        f"Updated availability for scope objects of offering {offering_uuid} "
        f"(can_be_managed={can_be_managed})."
    )


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


@shared_task(
    name="waldur_mastermind.marketplace.cleanup_usage_poll_records",
)
def cleanup_usage_poll_records():
    """Delete ComponentUsagePollRecord entries older than the retention period."""
    retention_months = getattr(config, "USAGE_POLL_RECORD_RETENTION_MONTHS", 3)
    cutoff = timezone.now() - timedelta(days=retention_months * 30)
    deleted, _ = models.ComponentUsagePollRecord.objects.filter(
        last_poll_time__lt=cutoff
    ).delete()
    if deleted:
        logger.info(
            "Deleted %d stale usage poll records older than %s", deleted, cutoff
        )


def _revoke_user_roles_for_role_on_offering(role_id, offering):
    """Revoke active UserRoles for the given role whose scope is a
    Resource or ResourceProject under this offering. Used by the profile
    reconcile tasks when a role leaves the catalog."""
    resource_ct = ContentType.objects.get_for_model(models.Resource)
    rp_ct = ContentType.objects.get_for_model(models.ResourceProject)

    resource_ids = list(
        models.Resource.objects.filter(offering=offering).values_list("id", flat=True)
    )
    if not resource_ids:
        return

    rp_ids = list(
        models.ResourceProject.objects.filter(resource_id__in=resource_ids).values_list(
            "id", flat=True
        )
    )

    qs = UserRole.objects.filter(role_id=role_id, is_active=True).filter(
        Q(content_type=resource_ct, object_id__in=resource_ids)
        | Q(content_type=rp_ct, object_id__in=rp_ids)
    )
    for ur in qs:
        ur.revoke(reason="Role removed from offering profile")


@shared_task(
    name="waldur_mastermind.marketplace.reconcile_offering_profile_availabilities",
)
def reconcile_offering_profile_availabilities(profile_id):
    """Bring RoleAvailability rows in line with profile.roles for every
    offering bound to this profile.

    For each (offering, role-in-profile) pair, ensure a RoleAvailability
    row exists. For (offering, removed-role) pairs, delete the
    availability AND explicitly revoke active UserRoles on that offering's
    Resources / ResourceProjects — bypassing the "last availability =
    globally available" cascade rule that would otherwise leave them
    intact.
    """
    try:
        profile = models.OfferingProfile.objects.get(id=profile_id)
    except models.OfferingProfile.DoesNotExist:
        logger.info(
            "OfferingProfile id=%s no longer exists; skip reconcile.", profile_id
        )
        return

    offering_ct = ContentType.objects.get_for_model(models.Offering)
    target_role_ids = set(profile.roles.values_list("id", flat=True))
    offerings = list(profile.offerings.all())

    for offering in offerings:
        existing = RoleAvailability.objects.filter(
            content_type=offering_ct, object_id=offering.id
        )
        existing_role_ids = set(existing.values_list("role_id", flat=True))

        for role_id in target_role_ids - existing_role_ids:
            RoleAvailability.objects.get_or_create(
                role_id=role_id,
                content_type=offering_ct,
                object_id=offering.id,
            )

        # Drop availability rows for roles no longer in the profile catalog.
        # Per design, profile-bound offerings own their role catalog
        # exclusively (no mixing with custom roles), so any availability
        # outside target_role_ids is stale.
        stale_role_ids = list(
            existing.exclude(role_id__in=target_role_ids).values_list(
                "role_id", flat=True
            )
        )
        for role_id in stale_role_ids:
            _revoke_user_roles_for_role_on_offering(role_id, offering)
        if stale_role_ids:
            existing.filter(role_id__in=stale_role_ids).delete()


@shared_task(
    name="waldur_mastermind.marketplace.reconcile_offering_availabilities",
)
def reconcile_offering_availabilities(offering_id):
    """Reconcile RoleAvailability rows for a single offering against its
    current profile (if any). Used when offering.profile changes.

    When an offering leaves a profile, all profile-derived availability
    rows on that offering are deleted and matching UserRoles are revoked.
    """
    try:
        offering = models.Offering.objects.get(id=offering_id)
    except models.Offering.DoesNotExist:
        return

    offering_ct = ContentType.objects.get_for_model(models.Offering)
    existing = RoleAvailability.objects.filter(
        content_type=offering_ct, object_id=offering.id
    )

    if offering.profile is None:
        # Offering left the profile — drop ALL availability rows for it.
        # Per design, profile-bound offerings own their catalog
        # exclusively, so on unbinding the offering returns to "no
        # roles" until staff/owner re-creates them.
        stale_role_ids = list(existing.values_list("role_id", flat=True))
        for role_id in stale_role_ids:
            _revoke_user_roles_for_role_on_offering(role_id, offering)
        existing.delete()
        return

    profile_role_ids = set(offering.profile.roles.values_list("id", flat=True))
    existing_role_ids = set(existing.values_list("role_id", flat=True))

    for role_id in profile_role_ids - existing_role_ids:
        RoleAvailability.objects.get_or_create(
            role_id=role_id,
            content_type=offering_ct,
            object_id=offering.id,
        )

    # Drop rows not in the profile catalog (no mixing).
    stale_role_ids = list(
        existing.exclude(role_id__in=profile_role_ids).values_list("role_id", flat=True)
    )
    for role_id in stale_role_ids:
        _revoke_user_roles_for_role_on_offering(role_id, offering)
    if stale_role_ids:
        existing.filter(role_id__in=stale_role_ids).delete()


class _MarketplaceAwareServiceListPullTask(structure_tasks.ServiceListPullTask):
    """ServiceListPullTask narrowed to ServiceSettings that are the scope of
    at least one non-archived marketplace Offering."""

    def get_pulled_objects(self):
        qs = super().get_pulled_objects()
        settings_ct = ContentType.objects.get_for_model(self.model)
        referenced_ids = (
            models.Offering.objects.filter(content_type=settings_ct)
            .exclude(state=OfferingStates.ARCHIVED)
            .values_list("object_id", flat=True)
        )
        return qs.filter(id__in=referenced_ids)


class ServicePropertiesListPullTask(_MarketplaceAwareServiceListPullTask):
    """Pull service properties from settings tied to live marketplace offerings."""

    name = "waldur_mastermind.marketplace.ServicePropertiesListPullTask"
    pull_task = structure_tasks.ServicePropertiesPullTask


class ServiceResourcesListPullTask(_MarketplaceAwareServiceListPullTask):
    """Pull resources from settings tied to live marketplace offerings."""

    name = "waldur_mastermind.marketplace.ServiceResourcesListPullTask"
    pull_task = structure_tasks.ServiceResourcesPullTask


@shared_task(
    name="waldur_mastermind.marketplace.send_resource_limit_change_request_notification"
)
def send_resource_limit_change_request_notification(request_uuid):
    """Notify organization owners when a resource limit change request is created."""
    try:
        request = models.ResourceLimitChangeRequest.objects.get(uuid=request_uuid)
    except models.ResourceLimitChangeRequest.DoesNotExist:
        logger.warning(
            "Resource limit change request %s not found, skipping notification",
            request_uuid,
        )
        return

    mails = request.resource.project.customer.get_owner_mails()
    if not mails:
        logger.info(
            "No owner emails for customer %s, skipping resource limit change request notification",
            request.resource.project.customer.uuid,
        )
        return

    resource_url = core_utils.format_homeport_link(
        "resource-details/{resource_uuid}/?tab=limit-change-requests",
        resource_uuid=request.resource.uuid.hex,
    )
    context = {
        "resource_limit_change_request": request,
        "resource_url": resource_url,
    }
    core_utils.broadcast_mail(
        "marketplace",
        "notification_resource_limit_change_request_created",
        context,
        mails,
    )


@shared_task(
    name="waldur_mastermind.marketplace.send_resource_limit_change_request_approved_notification"
)
def send_resource_limit_change_request_approved_notification(request_uuid):
    """Notify the requester when their resource limit change request is approved."""
    try:
        request = models.ResourceLimitChangeRequest.objects.get(uuid=request_uuid)
    except models.ResourceLimitChangeRequest.DoesNotExist:
        logger.warning(
            "Resource limit change request %s not found, skipping approved notification",
            request_uuid,
        )
        return

    if not request.created_by or not request.created_by.email:
        return

    if not request.created_by.notifications_enabled:
        return

    resource_url = core_utils.format_homeport_link(
        "resource-details/{resource_uuid}/",
        resource_uuid=request.resource.uuid.hex,
    )
    context = {
        "resource_limit_change_request": request,
        "resource_url": resource_url,
    }
    core_utils.broadcast_mail(
        "marketplace",
        "notification_resource_limit_change_request_approved",
        context,
        [request.created_by.email],
    )


@shared_task(
    name="waldur_mastermind.marketplace.send_resource_limit_change_request_rejected_notification"
)
def send_resource_limit_change_request_rejected_notification(request_uuid):
    """Notify the requester when their resource limit change request is rejected."""
    try:
        request = models.ResourceLimitChangeRequest.objects.get(uuid=request_uuid)
    except models.ResourceLimitChangeRequest.DoesNotExist:
        logger.warning(
            "Resource limit change request %s not found, skipping rejected notification",
            request_uuid,
        )
        return

    if not request.created_by or not request.created_by.email:
        return

    if not request.created_by.notifications_enabled:
        return

    resource_url = core_utils.format_homeport_link(
        "resource-details/{resource_uuid}/",
        resource_uuid=request.resource.uuid.hex,
    )
    context = {
        "resource_limit_change_request": request,
        "resource_url": resource_url,
    }
    core_utils.broadcast_mail(
        "marketplace",
        "notification_resource_limit_change_request_rejected",
        context,
        [request.created_by.email],
    )
