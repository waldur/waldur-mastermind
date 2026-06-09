import calendar
import datetime
import decimal
import hashlib
import json
import logging
import math
import os
import random
import re
import textwrap
import traceback
import unicodedata
import uuid
from collections import defaultdict
from enum import Enum
from functools import lru_cache
from io import BytesIO
from string import Template
from typing import cast

import httpx
from constance import config
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage as storage
from django.db import models as models_module
from django.db import transaction
from django.db.models import F, Q, Sum
from django.db.models.fields import FloatField
from django.db.models.functions.math import Ceil
from django.urls import get_resolver
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from PIL import Image
from rest_framework import exceptions as rf_exceptions
from rest_framework import serializers, status
from rest_framework.response import Response

from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.core.models import User
from waldur_core.core.validators import is_potentially_dangerous_regex
from waldur_core.logging import models as logging_models
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging.enums import ObservableObjectType
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import (
    get_permissions,
    get_users_with_permission,
    has_permission,
    has_user,
)
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
    get_customer_users,
    get_organization_groups,
    get_project_users,
)
from waldur_freeipa import models as freeipa_models
from waldur_mastermind.common.utils import create_request, mb_to_gb
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.structures import InvoiceResourceLimitPeriodDict
from waldur_mastermind.invoices.utils import get_full_days
from waldur_mastermind.marketplace import attribute_types
from waldur_mastermind.marketplace.billing import MarketplaceBillingService
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_TENANT_OFFERING,
    BillingTypes,
    CourseAccountState,
    LimitPeriods,
    OfferingUserStates,
    OrderStates,
    ResourceStates,
    RobotAccountStates,
)
from waldur_mastermind.marketplace.enums import REMOTE_OFFERING as REMOTE_PLUGIN_NAME
from waldur_mastermind.marketplace.enums import SCRIPT_OFFERING as SCRIPT_PLUGIN_NAME
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING as SITE_AGENT_PLUGIN_NAME,
)
from waldur_mastermind.marketplace_openstack import get_mb_component_types

from . import models, plugins
from .enums import BASIC_OFFERING as BASIC_PLUGIN_NAME
from .enums import OrderTypes

logger = logging.getLogger(__name__)
USERNAME_ANONYMIZED_POSTFIX_LENGTH = 5


USERNAME_POSTFIX_LENGTH = 2


class UsernameGenerationPolicy(Enum):
    SERVICE_PROVIDER = (
        "service_provider"  # SP should manually submit username for the offering users
    )
    ANONYMIZED = "anonymized"  # Usernames are generated with <prefix>_<number>, e.g. "anonym_00001".
    # The prefix must be specified in offering.plugin_options as "username_anonymized_prefix"
    FULL_NAME = "full_name"  # Usernames are constructed using first and last name of users with numerical suffix, e.g. "john_doe_01"
    WALDUR_USERNAME = "waldur_username"  # Using username field of User model
    FREEIPA = "freeipa"  # Using username field of waldur_freeipa.Profile model
    IDENTITY_CLAIM = "identity_claim"  # Using username from external IDP system


def get_order_processor(order: models.Order):
    offering = order.resource.offering

    if order.type == OrderTypes.CREATE:
        return plugins.manager.get_processor(offering.type, "create_resource_processor")

    elif order.type == OrderTypes.UPDATE:
        return plugins.manager.get_processor(offering.type, "update_resource_processor")

    elif order.type == OrderTypes.TERMINATE:
        return plugins.manager.get_processor(offering.type, "delete_resource_processor")

    elif order.type == OrderTypes.RESTORE:
        return plugins.manager.get_processor(offering.type, "create_resource_processor")


def process_order(order: models.Order, user):
    processor = get_order_processor(order)
    if not processor:
        order.error_message = (
            "Skipping order processing because processor is not found."
        )
        order.set_state_erred()
        order.resource.set_state_erred()
        order.resource.save(update_fields=["state"])
        order.save(update_fields=["state", "error_message"])
        return

    try:
        processor(order).process_order(user)
    except Exception as e:
        # Here it is necessary to catch all exceptions.
        # If this is not done, then the order will remain in the executed status.
        order.refresh_from_db()
        order.error_message = str(e)
        order.error_traceback = traceback.format_exc()
        order.set_state_erred()

        if (
            order.attributes.get("action") == "force_destroy"
            and order.type == OrderTypes.TERMINATE
            and user.is_staff
        ):
            order.resource.set_state_terminated()
        else:
            order.resource.set_state_erred()

        logger.error(
            f"Error processing order {order}. "
            f"Order ID: {order.id}. "
            f"Exception: {order.error_message}."
        )
        order.resource.save(update_fields=["state"])

        order.save(
            update_fields=[
                "state",
                "error_message",
                "error_traceback",
            ]
        )


def validate_order(order, request):
    processor = get_order_processor(order)
    if processor:
        try:
            processor(order).validate_order(request)
        except NotImplementedError:
            # It is okay if validation is not implemented yet
            pass


def create_screenshot_thumbnail(screenshot):
    """Create a thumbnail for a screenshot."""
    pic = screenshot.image
    fh = storage.open(pic.name, "rb")
    image = Image.open(fh)
    width, height = map(int, config.THUMBNAIL_SIZE.split("x"))
    image.thumbnail((width, height))
    fh.close()

    thumb_extension = os.path.splitext(pic.name)[1]
    thumb_extension = thumb_extension.lower()
    thumb_name = os.path.basename(pic.name)

    if thumb_extension in [".jpg", ".jpeg"]:
        FTYPE = "JPEG"
    elif thumb_extension == ".gif":
        FTYPE = "GIF"
    elif thumb_extension == ".png":
        FTYPE = "PNG"
    else:
        return

    temp_thumb = BytesIO()
    image.save(temp_thumb, FTYPE)
    temp_thumb.seek(0)
    screenshot.thumbnail.save(thumb_name, ContentFile(temp_thumb.read()), save=True)
    temp_thumb.close()


def import_resource_metadata(resource: models.Resource):
    instance = resource.scope
    fields = {"action", "action_details", "state", "runtime_state"}

    for field in fields:
        if field == "state":
            value = instance.get_state_display()
        else:
            value = getattr(instance, field, None)
        if field in fields:
            resource.backend_metadata[field] = value

    if instance.backend_id:
        resource.backend_id = instance.backend_id
    resource.name = instance.name
    resource.save(
        update_fields=["backend_metadata", "attributes", "name", "backend_id"]
    )


def get_service_provider_info(source):
    try:
        resource = models.Resource.objects.get(scope=source)
        customer = resource.offering.customer
        service_provider = getattr(customer, "serviceprovider", None)

        return {
            "service_provider_name": customer.name,
            "service_provider_uuid": (
                "" if not service_provider else service_provider.uuid.hex
            ),
        }
    except models.Resource.DoesNotExist:
        return {}


def get_order_url(order):
    return core_utils.format_homeport_link(
        "marketplace-order-details/{order_uuid}/",
        order_uuid=order.uuid.hex,
        project_uuid=order.project.uuid,
    )


def get_info_about_missing_usage_reports():
    now = timezone.now()
    billing_period = core_utils.month_start(now)

    whitelist_types = [
        offering_type
        for offering_type in plugins.manager.get_offering_types()
        if plugins.manager.enable_usage_notifications(offering_type)
    ]

    offering_ids = models.OfferingComponent.objects.filter(
        billing_type=BillingTypes.USAGE,
        offering__type__in=whitelist_types,
    ).values_list("offering_id", flat=True)
    resource_with_usages = models.ComponentUsage.objects.filter(
        billing_period=billing_period
    ).values_list("resource", flat=True)
    resources_without_usages = models.Resource.objects.filter(
        state=ResourceStates.OK, offering_id__in=offering_ids
    ).exclude(id__in=resource_with_usages)
    result = []

    for resource in resources_without_usages:
        rows = list(
            filter(lambda x: x["customer"] == resource.offering.customer, result)
        )
        if rows:
            rows[0]["resources"].append(resource)
        else:
            result.append(
                {
                    "customer": resource.offering.customer,
                    "resources": [resource],
                }
            )

    return result


def validate_limit_amount(value, component):
    if not component.limit_amount:
        return

    if component.limit_period == LimitPeriods.MONTH:
        current = (
            (
                models.ComponentQuota.objects.filter(
                    component=component,
                    modified__year=timezone.now().year,
                    modified__month=timezone.now().month,
                )
                .exclude(limit=-1)
                .aggregate(sum=Sum("limit"))["sum"]
            )
            or 0
        )
        if current + value > component.limit_amount:
            raise serializers.ValidationError(
                _("Monthly limit exceeds threshold %s.") % component.limit_amount
            )

    elif component.limit_period == LimitPeriods.QUARTERLY:
        quarter_start = core_utils.get_current_quarter_start()
        quarter_end = core_utils.get_current_quarter_end()
        current = (
            (
                models.ComponentQuota.objects.filter(
                    component=component,
                    modified__gte=quarter_start,
                    modified__lte=quarter_end,
                )
                .exclude(limit=-1)
                .aggregate(sum=Sum("limit"))["sum"]
            )
            or 0
        )
        if current + value > component.limit_amount:
            raise serializers.ValidationError(
                _("Quarterly limit exceeds threshold %s.") % component.limit_amount
            )

    elif component.limit_period == LimitPeriods.ANNUAL:
        current = (
            (
                models.ComponentQuota.objects.filter(
                    component=component,
                    modified__year=timezone.now().year,
                )
                .exclude(limit=-1)
                .aggregate(sum=Sum("limit"))["sum"]
            )
            or 0
        )
        if current + value > component.limit_amount:
            raise serializers.ValidationError(
                _("Annual limit exceeds threshold %s.") % component.limit_amount
            )

    elif component.limit_period == LimitPeriods.TOTAL:
        current = (
            (
                models.ComponentQuota.objects.filter(
                    component=component,
                )
                .exclude(limit=-1)
                .aggregate(sum=Sum("limit"))["sum"]
            )
            or 0
        )
        if current + value > component.limit_amount:
            raise serializers.ValidationError(
                _("Total limit exceeds threshold %s.") % component.limit_amount
            )


def validate_maximum_available_limit(value, component, resource=None):
    if not component.max_available_limit:
        return

    all_offering_resources = models.Resource.objects.filter(
        offering=component.offering
    ).exclude(limits={})

    if resource:
        all_offering_resources = all_offering_resources.exclude(id=resource.id)

    current_total_limits = sum(
        resource["limits"].get(component.type, 0)
        for resource in all_offering_resources.values("limits")
    )

    if current_total_limits + value >= component.max_available_limit:
        error_message = "Requested %s cannot be provisioned due to offering safety limit. You can allocate up to %s of %s."
        if component.type == "cores":
            value = component.max_available_limit - current_total_limits - 1
        else:
            value = math.floor(
                mb_to_gb(component.max_available_limit - current_total_limits)
            )

        raise serializers.ValidationError(
            _(error_message)
            % (
                component.type,
                value,
                component.type,
            )
        )


def validate_min_max_limit(value, component):
    if component.max_value and value > component.max_value:
        raise serializers.ValidationError(
            _("The limit %s value cannot be more than %s.")
            % (value, component.max_value)
        )
    if component.min_value and value < component.min_value:
        raise serializers.ValidationError(
            _("The limit %s value cannot be less than %s.")
            % (value, component.min_value)
        )


def get_components_map(limits, offering: models.Offering):
    valid_component_types = set(
        offering.components.filter(
            Q(billing_type=BillingTypes.LIMIT)
            | Q(billing_type=BillingTypes.ONE_TIME, is_prepaid=True)
        ).values_list("type", flat=True)
    )

    invalid_types = set(limits.keys()) - valid_component_types
    if invalid_types:
        raise serializers.ValidationError(
            {"limits": _("Invalid types: %s") % ", ".join(invalid_types)}
        )

    components_map = {
        component.type: component
        for component in offering.components.filter(type__in=valid_component_types)
    }

    result = []
    for key, value in limits.items():
        component = components_map.get(key)
        if component:
            result.append((component, value))
    return result


def validate_limits(limits, offering, resource=None, is_creation=False):
    """
    @param limits Maximum/Minimum limit-based components values and maximum available limit
    @param offering The offering being created
    @param resource Passing the resource if the limits of the resource are being updated.
    @param is_creation If True, skip the can_update_limits check (it only applies to updates).
    """
    if not is_creation and not plugins.manager.can_update_limits(offering.type):
        raise serializers.ValidationError(
            {"limits": _("Limits update is not supported for this resource.")}
        )

    limits_validator = plugins.manager.get_limits_validator(offering.type)
    if limits_validator:
        limits_validator(limits)

    for component, value in get_components_map(limits, offering):
        validate_min_max_limit(value, component)

        validate_limit_amount(value, component)

        validate_maximum_available_limit(value, component, resource)


def validate_attributes(attributes, category):
    category_attributes = models.Attribute.objects.filter(section__category=category)

    required_attributes = category_attributes.filter(required=True).values_list(
        "key", flat=True
    )

    missing_attributes = set(required_attributes) - set(attributes.keys())
    if missing_attributes:
        raise rf_exceptions.ValidationError(
            {
                "attributes": _(
                    "These attributes are required: %s"
                    % ", ".join(sorted(missing_attributes))
                )
            }
        )

    for attribute in category_attributes:
        value = attributes.get(attribute.key)
        if value is None:
            # Use default attribute value if it is defined
            if attribute.default is not None:
                attributes[attribute.key] = attribute.default
            continue

        validator = attribute_types.get_attribute_type(attribute.type)
        if not validator:
            continue

        try:
            validator.validate(
                value, list(attribute.options.values_list("key", flat=True))
            )
        except ValidationError as e:
            raise rf_exceptions.ValidationError({attribute.key: e.message})


def create_offering_components(offering, custom_components=None):
    fixed_components = plugins.manager.get_components(offering.type)
    category_components = {
        component.type: component
        for component in models.CategoryComponent.objects.filter(
            category=offering.category
        )
    }

    for component_data in fixed_components:
        models.OfferingComponent.objects.create(
            offering=offering,
            parent=category_components.get(component_data.type, None),
            **component_data._asdict(),
        )

    if custom_components:
        for component_data in custom_components:
            models.OfferingComponent.objects.create(offering=offering, **component_data)


def get_resource_state(state):
    mapping = {
        CoreStates.CREATION_SCHEDULED: ResourceStates.CREATING,
        CoreStates.CREATING: ResourceStates.CREATING,
        CoreStates.UPDATE_SCHEDULED: ResourceStates.UPDATING,
        CoreStates.UPDATING: ResourceStates.UPDATING,
        CoreStates.DELETION_SCHEDULED: ResourceStates.TERMINATING,
        CoreStates.DELETING: ResourceStates.TERMINATING,
        CoreStates.OK: ResourceStates.OK,
        CoreStates.ERRED: ResourceStates.ERRED,
    }
    return mapping.get(state, ResourceStates.ERRED)


def get_marketplace_offering_uuid(serializer, scope) -> str | None:
    try:
        return models.Resource.objects.get(scope=scope).offering.uuid.hex
    except ObjectDoesNotExist:
        return


def get_marketplace_offering_plugin_options(serializer, scope) -> dict | None:
    try:
        return models.Resource.objects.get(scope=scope).offering.plugin_options
    except ObjectDoesNotExist:
        return


def get_marketplace_offering_name(serializer, scope) -> str | None:
    try:
        return models.Resource.objects.get(scope=scope).offering.name
    except ObjectDoesNotExist:
        return


def get_marketplace_offering_type(serializer, scope) -> str | None:
    try:
        return models.Resource.objects.get(scope=scope).offering.type
    except ObjectDoesNotExist:
        return


def get_marketplace_category_uuid(serializer, scope) -> str | None:
    try:
        return models.Resource.objects.get(scope=scope).offering.category.uuid.hex
    except ObjectDoesNotExist:
        return


def get_marketplace_category_name(serializer, scope) -> str | None:
    try:
        return models.Resource.objects.get(scope=scope).offering.category.title
    except ObjectDoesNotExist:
        return


def get_marketplace_resource_uuid(serializer, scope) -> str | None:
    try:
        return models.Resource.objects.get(scope=scope).uuid.hex
    except ObjectDoesNotExist:
        return


def get_marketplace_plan_uuid(serializer, scope) -> str | None:
    try:
        resource = models.Resource.objects.get(scope=scope)
        if resource.plan:
            return resource.plan.uuid.hex
    except ObjectDoesNotExist:
        return


def get_marketplace_resource_state(serializer, scope) -> str | None:
    try:
        return models.Resource.objects.get(scope=scope).get_state_display()
    except ObjectDoesNotExist:
        return


def get_is_usage_based(serializer, scope) -> bool | None:
    try:
        return models.Resource.objects.get(scope=scope).offering.is_usage_based
    except ObjectDoesNotExist:
        return


def get_is_limit_based(serializer, scope) -> bool | None:
    try:
        return models.Resource.objects.get(scope=scope).offering.is_limit_based
    except ObjectDoesNotExist:
        return


def add_marketplace_offering(sender, fields, **kwargs):
    """Add marketplace offering related fields to the serializer."""
    fields["marketplace_offering_uuid"] = serializers.SerializerMethodField()
    setattr(sender, "get_marketplace_offering_uuid", get_marketplace_offering_uuid)

    fields["marketplace_offering_name"] = serializers.SerializerMethodField()
    setattr(sender, "get_marketplace_offering_name", get_marketplace_offering_name)

    fields["marketplace_offering_type"] = serializers.SerializerMethodField()
    setattr(sender, "get_marketplace_offering_type", get_marketplace_offering_type)

    fields["marketplace_offering_plugin_options"] = serializers.SerializerMethodField()
    setattr(
        sender,
        "get_marketplace_offering_plugin_options",
        get_marketplace_offering_plugin_options,
    )

    fields["marketplace_category_uuid"] = serializers.SerializerMethodField()
    setattr(sender, "get_marketplace_category_uuid", get_marketplace_category_uuid)

    fields["marketplace_category_name"] = serializers.SerializerMethodField()
    setattr(sender, "get_marketplace_category_name", get_marketplace_category_name)

    fields["marketplace_resource_uuid"] = serializers.SerializerMethodField()
    setattr(sender, "get_marketplace_resource_uuid", get_marketplace_resource_uuid)

    fields["marketplace_plan_uuid"] = serializers.SerializerMethodField()
    setattr(sender, "get_marketplace_plan_uuid", get_marketplace_plan_uuid)

    fields["marketplace_resource_state"] = serializers.SerializerMethodField()
    setattr(sender, "get_marketplace_resource_state", get_marketplace_resource_state)

    fields["is_usage_based"] = serializers.SerializerMethodField()
    setattr(sender, "get_is_usage_based", get_is_usage_based)

    fields["is_limit_based"] = serializers.SerializerMethodField()
    setattr(sender, "get_is_limit_based", get_is_limit_based)


def get_offering_costs(invoice_items):
    price = Ceil(F("quantity") * F("unit_price") * 100) / 100
    tax_rate = F("invoice__tax_percent") / 100
    return invoice_items.values("invoice__year", "invoice__month").annotate(
        computed_price=Sum(price, output_field=FloatField()),
        computed_tax=Sum(price * tax_rate, output_field=FloatField()),
    )


def get_offering_customers(offering, active_customers):
    resources = models.Resource.objects.filter(
        offering=offering,
        project__customer__in=active_customers,
    )
    customers_ids = resources.values_list("project__customer_id", flat=True)
    return structure_models.Customer.objects.filter(id__in=customers_ids)


def get_offering_projects(offering):
    related_project_ids = (
        models.Resource.objects.filter(offering=offering)
        .exclude(state=ResourceStates.TERMINATED)
        .values_list("project", flat=True)
        .distinct()
        .order_by()
    )
    related_projects = structure_models.Project.objects.filter(
        id__in=related_project_ids
    )
    return related_projects


def is_user_related_to_offering(offering, user):
    connected_projects = get_connected_projects(user)
    return (
        models.Resource.objects.filter(
            offering=offering, project__in=connected_projects
        )
        .exclude(state=ResourceStates.TERMINATED)
        .exists()
    )


def get_start_and_end_dates_from_request(request):
    serializer = core_serializers.DateRangeFilterSerializer(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    today = datetime.date.today()
    default_start = datetime.date(year=today.year - 1, month=today.month, day=1)
    start_year, start_month = serializer.validated_data.get(
        "start", (default_start.year, default_start.month)
    )
    end_year, end_month = serializer.validated_data.get(
        "end", (today.year, today.month)
    )
    end = datetime.date(year=end_year, month=end_month, day=1)
    start = datetime.date(year=start_year, month=start_month, day=1)
    return start, end


def get_active_customers(request, view):
    customers = structure_models.Customer.objects.all()
    return structure_filters.AccountingStartDateFilter().filter_queryset(
        request, customers, view
    )


class MoveResourceException(Exception):
    pass


@transaction.atomic
def move_resource(resource: models.Resource, project):
    if project.customer.blocked:
        raise rf_exceptions.ValidationError("New customer must be not blocked")

    old_project = resource.project

    resource.project = project
    resource.save(update_fields=["project"])

    if resource.scope:
        resource.scope.project = project
        resource.scope.save(update_fields=["project"])

        for service_settings in structure_models.ServiceSettings.objects.filter(
            scope=resource.scope
        ):
            models.Offering.objects.filter(scope=service_settings).update(
                project=project
            )

    for order in resource.order_set.exclude(project=project):
        order.project = project
        order.save(update_fields=["project"])

    for invoice_item in invoice_models.InvoiceItem.objects.filter(
        resource=resource,
        invoice__state__in=invoice_models.Invoice.States.MUTABLE_STATES,
        project=old_project,
    ):
        start_invoice = invoice_item.invoice

        target_invoice, _ = MarketplaceBillingService.get_or_create_invoice(
            project.customer,
            date=datetime.date(
                year=start_invoice.year, month=start_invoice.month, day=1
            ),
        )

        if target_invoice.state not in invoice_models.Invoice.States.MUTABLE_STATES:
            raise MoveResourceException(
                "Resource moving is not possible, "
                "because invoice items moving is not possible."
            )

        invoice_item.project = project
        invoice_item.project_uuid = project.uuid.hex
        invoice_item.project_name = project.name
        invoice_item.invoice = target_invoice
        invoice_item.save(
            update_fields=["project", "project_uuid", "project_name", "invoice"]
        )

        start_invoice.update_cache()
        target_invoice.update_cache()


def get_invoice_item_for_component_usage(component_usage: models.ComponentUsage):
    if not component_usage.plan_period:
        # Field plan_period is optional if component_usage is not connected with billing
        return
    else:
        if component_usage.plan_period.end:
            plan_period_end = component_usage.plan_period.end
        else:
            plan_period_end = core_utils.month_end(component_usage.billing_period)

        if component_usage.plan_period.start:
            plan_period_start = component_usage.plan_period.start
        else:
            plan_period_start = component_usage.billing_period

    try:
        item = invoice_models.InvoiceItem.objects.get(
            invoice__year=component_usage.billing_period.year,
            invoice__month=component_usage.billing_period.month,
            resource=component_usage.resource,
            start__gte=plan_period_start,
            end__lte=plan_period_end,
            details__offering_component_type=component_usage.component.type,
        )
        return item
    except invoice_models.InvoiceItem.DoesNotExist:
        pass


def serialize_resource_limit_period(
    start: datetime.datetime, end: datetime.datetime, quantity: int
) -> InvoiceResourceLimitPeriodDict:
    billing_periods = get_full_days(start, end)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "quantity": quantity,
        "billing_periods": billing_periods,
        "total": str(quantity * billing_periods),
    }


def terminate_resource(resource, user, termination_comment=None, scheduled=False):
    from waldur_mastermind.marketplace import views

    view = views.ConsumerResourceViewSet.as_view({"post": "terminate"})

    # Terminate pending orders if they exist
    for order in models.Order.objects.filter(
        resource=resource,
        state__in=(
            [OrderStates.PENDING_CONSUMER]
            if scheduled
            else [
                OrderStates.PENDING_CONSUMER,
                OrderStates.PENDING_PROVIDER,
            ]
        ),
    ):
        order.cancel(termination_comment)
        order.save()

    if models.Order.objects.filter(resource=resource, state=OrderStates.EXECUTING):
        logger.info(
            "Terminate order has not been created because other executing orders exist."
        )
        return

    if models.Order.objects.filter(
        resource=resource,
        state=OrderStates.ERRED,
        type=OrderTypes.TERMINATE,
        modified__gte=timezone.now() - datetime.timedelta(days=1),
    ).exists():
        logger.info(
            "Skipping terminate for resource %s — recent ERRED terminate order exists.",
            resource,
        )
        return

    return create_request(view, user, {}, uuid=resource.uuid.hex)


def schedule_resources_termination(resources, termination_comment=None, user=None):
    if not resources:
        return

    system_robot = core_utils.get_system_robot()

    for resource in resources:
        # A separate `actor` variable per iteration is required: rebinding
        # `user` would short-circuit the fallback chain on the next iteration
        # and attribute every later resource to the first resource's actor.
        #
        # Inactive candidates are skipped: termination runs as an internal API
        # request authenticated as the actor, and an inactive user is rejected
        # with HTTP 401 "User inactive or deleted.", so the resource would never
        # be terminated. The system robot is always active and is the fallback.
        actor = next(
            (
                candidate
                for candidate in (
                    user,
                    resource.end_date_requested_by,
                    resource.project.end_date_requested_by,
                )
                if candidate is not None and candidate.is_active
            ),
            system_robot,
        )

        if not actor:
            logger.error(
                "User for terminating resources of project with due date does not exist."
            )
            return

        response = terminate_resource(
            resource, actor, termination_comment, scheduled=True
        )

        if response and response.status_code != status.HTTP_200_OK:
            logger.error(
                "Terminating resource %s has failed. %s",
                resource.uuid.hex,
                response.rendered_content,
            )


def get_service_provider_resources(service_provider):
    return models.Resource.objects.filter(
        offering__customer=service_provider.customer, offering__shared=True
    ).exclude(state=ResourceStates.TERMINATED)


def get_service_provider_customer_ids(service_provider):
    return (
        get_service_provider_resources(service_provider)
        .values_list("project__customer_id", flat=True)
        .distinct()
    )


def get_service_provider_project_ids(service_provider):
    return (
        get_service_provider_resources(service_provider)
        .values_list("project_id", flat=True)
        .distinct()
    )


def get_service_provider_user_ids(user, service_provider, customer=None):
    project_ids = get_service_provider_project_ids(service_provider)
    if customer:
        customer_projects = structure_models.Project.available_objects.filter(
            customer=customer
        ).values_list("id", flat=True)
        project_ids = set(project_ids) & set(customer_projects)
    content_type = ContentType.objects.get_for_model(structure_models.Project)
    qs = UserRole.objects.filter(
        content_type=content_type, object_id__in=project_ids, is_active=True
    )
    if user.is_authenticated and not user.is_staff and not user.is_support:
        qs = qs.filter(user__is_active=True)
    return qs.values_list("user_id", flat=True).distinct()


def get_plan_period(resource, date):
    return (
        models.ResourcePlanPeriod.objects.filter(
            Q(start__lte=date) | Q(start__isnull=True)
        )
        .filter(Q(end__gt=date) | Q(end__isnull=True))
        .filter(resource=resource)
        .order_by("start")
        .last()
    )


def get_or_create_plan_period(resource: models.Resource, date):
    plan_period = get_plan_period(resource, date)

    if (
        plan_period is None
        and resource.plan
        and resource.state in [ResourceStates.OK, ResourceStates.UPDATING]
    ):
        logger.info(
            "Creating missing Resource Plan Period for resource %s (UUID: %s)",
            resource.name,
            resource.uuid.hex,
        )
        plan_period = models.ResourcePlanPeriod.objects.create(
            resource=resource,
            plan=resource.plan,
            start=resource.created,
            end=None,
        )

    return plan_period


def import_current_usages(resource, usages=None, hourly_accumulation=False):
    now = timezone.now()
    date = now.date()
    if usages is None:
        usages = resource.current_usages

    for component_type, component_usage in usages.items():
        try:
            offering_component = models.OfferingComponent.objects.get(
                offering=resource.offering, type=component_type
            )
        except models.OfferingComponent.DoesNotExist:
            logger.warning(
                "Skipping current usage synchronization because related "
                "OfferingComponent does not exist."
                "Resource ID: %s",
                resource.id,
            )
            continue

        plan_period = get_plan_period(resource, date)
        billing_period = core_utils.month_start(date)

        use_accumulation = (
            hourly_accumulation
            and offering_component.billing_type == BillingTypes.USAGE
        )

        if use_accumulation:
            _accumulate_hourly_usage(
                resource,
                offering_component,
                component_usage,
                plan_period,
                billing_period,
                date,
                now,
            )
        else:
            _update_high_watermark_usage(
                resource,
                offering_component,
                component_usage,
                plan_period,
                billing_period,
                date,
            )


def _update_high_watermark_usage(
    resource, offering_component, component_usage, plan_period, billing_period, date
):
    """Original high-watermark logic: usage = max(new, existing)."""
    # Look up by (resource, component, billing_period) only —
    # plan_period is mutable and should not be part of the identity.
    existing_qs = models.ComponentUsage.objects.filter(
        resource=resource,
        component=offering_component,
        billing_period=billing_period,
    )
    existing = existing_qs.first()
    if existing:
        existing_qs.exclude(pk=existing.pk).delete()
        existing.plan_period = plan_period
        existing.usage = max(component_usage, existing.usage)
        existing.save()
    else:
        models.ComponentUsage.objects.create(
            resource=resource,
            component=offering_component,
            billing_period=billing_period,
            plan_period=plan_period,
            usage=component_usage,
            date=date,
        )


def _accumulate_hourly_usage(
    resource,
    offering_component,
    component_usage,
    plan_period,
    billing_period,
    date,
    now,
):
    """Hourly accumulation: usage += current_value × hours_since_last_poll."""
    # Read last poll time from the persistent poll record
    poll_record = models.ComponentUsagePollRecord.objects.filter(
        resource=resource,
        component=offering_component,
    ).first()

    if poll_record and poll_record.last_poll_time:
        elapsed_seconds = (now - poll_record.last_poll_time).total_seconds()
        elapsed_hours = min(elapsed_seconds / 3600.0, 24.0)
    else:
        # First poll for this resource+component — default to 1 hour
        elapsed_hours = 1.0

    increment = decimal.Decimal(str(component_usage)) * decimal.Decimal(
        str(elapsed_hours)
    )

    existing_qs = models.ComponentUsage.objects.filter(
        resource=resource,
        component=offering_component,
        billing_period=billing_period,
    )
    existing = existing_qs.first()
    if existing:
        existing_qs.exclude(pk=existing.pk).delete()
        existing.plan_period = plan_period
        new_total = existing.usage + increment
        existing.usage = new_total
        existing.save()
    else:
        new_total = increment
        models.ComponentUsage.objects.create(
            resource=resource,
            component=offering_component,
            billing_period=billing_period,
            plan_period=plan_period,
            usage=new_total,
            date=date,
        )

    # Upsert the poll record for staff observability and next-poll timestamp
    models.ComponentUsagePollRecord.objects.update_or_create(
        resource=resource,
        component=offering_component,
        defaults={
            "last_poll_time": now,
            "raw_usage": component_usage,
            "elapsed_hours": decimal.Decimal(str(round(elapsed_hours, 2))),
            "increment": increment,
            "accumulated_total": new_total,
            "billing_period": billing_period,
        },
    )


def get_current_period_usage(resource, limit_period=None):
    """Get per-component usage for a resource in the current period.

    Shared between the resource serializer (panel display) and the
    SLURM policy (QOS enforcement), ensuring both show the same numbers.

    Always queries ComponentUsage records as the source of truth.
    For each component, the effective period is determined by:
    1. The ``limit_period`` argument (if provided)
    2. The component's own ``limit_period`` field

    Args:
        resource: Marketplace Resource instance
        limit_period: Override period for all components.
            If None, each component's own limit_period is used.

    Returns:
        dict[str, float]: Component type → aggregated usage amount
    """
    result = {}

    for component in resource.offering.components.all():
        effective_period = limit_period or component.limit_period

        usages = models.ComponentUsage.objects.filter(
            resource=resource, component=component
        )

        if effective_period in (None, LimitPeriods.MONTH):
            month_start = core_utils.month_start(datetime.date.today())
            usages = usages.filter(billing_period=month_start)
        elif effective_period == LimitPeriods.QUARTERLY:
            quarter_start = core_utils.get_current_quarter_start()
            quarter_end = core_utils.get_current_quarter_end()
            usages = usages.filter(
                billing_period__gte=quarter_start, billing_period__lte=quarter_end
            )
        elif effective_period == LimitPeriods.ANNUAL:
            usages = usages.filter(billing_period__year__gte=datetime.date.today().year)
        elif effective_period == LimitPeriods.TOTAL:
            pass  # Sum all usages

        result[component.type] = float(
            usages.aggregate(total=Sum("usage"))["total"] or 0
        )

    return result


def get_components_usage_data(resources, for_current_month=False):
    """Aggregate per-component usage and limit stats across resources.

    Used by the Customer/Project stats endpoints. All usage data is
    sourced from ComponentUsage records (not current_usages snapshot).

    Child offerings (OpenStack VM/Volume sub-offerings) are excluded so that
    VPC-level allocation and individual-VM usage are not double-counted.
    OpenStack RAM and storage values are stored in MB; they are divided by
    1024 before returning so the caller always receives GB.
    """
    MB_COMPONENT_TYPES = get_mb_component_types()

    # Exclude child offerings (OpenStack VM/Volume) — their component usages
    # overlap with the parent tenant offering and must not be summed together.
    resources = resources.filter(offering__parent__isnull=True)

    offerings = models.Offering.objects.filter(
        id__in=resources.values_list("offering_id", flat=True)
    ).distinct()

    components = (
        models.OfferingComponent.objects.filter(offering__in=offerings)
        .select_related("offering")
        .distinct()
    )

    # Key by (component_type, billing_type) to avoid mixing usage and limit
    # components that share the same type name across different offerings.
    component_usage = defaultdict(float)
    component_limit = defaultdict(float)
    component_limit_usage = defaultdict(float)

    current_date = datetime.date.today()

    for resource in resources:
        usage_components = resource.offering.components.filter(
            billing_type=BillingTypes.USAGE
        )
        limit_components = resource.offering.components.filter(
            billing_type=BillingTypes.LIMIT
        )

        for component_type, limit in resource.limits.items():
            if limit is None:
                continue
            # Assign limit to the correct billing_type bucket
            if limit_components.filter(type=component_type).exists():
                key = (component_type, BillingTypes.LIMIT)
            elif usage_components.filter(type=component_type).exists():
                key = (component_type, BillingTypes.USAGE)
            else:
                key = (component_type, BillingTypes.LIMIT)
            component_limit[key] += float(limit)

        for component in usage_components:
            key = (component.type, BillingTypes.USAGE)
            if for_current_month:
                usages = models.ComponentUsage.objects.filter(
                    resource=resource,
                    component=component,
                    billing_period__year=current_date.year,
                    billing_period__month=current_date.month,
                )
                total_usage = usages.aggregate(total=Sum("usage"))["total"] or 0
                component_usage[key] += float(total_usage)
            else:
                latest = (
                    models.ComponentUsage.objects.filter(
                        resource=resource, component=component
                    )
                    .order_by("-billing_period")
                    .first()
                )
                if latest:
                    component_usage[key] += float(latest.usage)

        limit_override = LimitPeriods.MONTH if for_current_month else None
        limit_period_usage = get_current_period_usage(
            resource, limit_period=limit_override
        )
        for component in limit_components:
            key = (component.type, BillingTypes.LIMIT)
            component_limit_usage[key] += limit_period_usage.get(component.type, 0)

    components_data = {}
    # Track keys whose values need MB→GB conversion (OpenStack offerings only).
    # Only marked when the first-writing component belongs to an OpenStack tenant
    # offering, so a non-OpenStack offering that happens to share the same
    # (type, billing_type) key is never incorrectly divided by 1024.
    openstack_mb_keys: set[tuple[str, str]] = set()
    for component in components:
        key = (component.type, component.billing_type)
        if key not in components_data:
            components_data[key] = {
                "type": component.type,
                "name": component.name,
                "description": component.description,
                "measured_unit": component.measured_unit,
                "billing_type": component.billing_type,
                "usage": component_usage.get(key, 0),
                "limit_usage": component_limit_usage.get(key, 0),
                "limit": component_limit.get(key, None),
                "offering_name": component.offering.name,
                "offering_uuid": component.offering.uuid.hex,
            }
            if (
                component.offering.type == OPENSTACK_TENANT_OFFERING
                and component.type in MB_COMPONENT_TYPES
            ):
                openstack_mb_keys.add(key)

    # OpenStack persists RAM and storage in MB; convert to GB for the caller.
    for key in openstack_mb_keys:
        data = components_data[key]
        data["usage"] = round(data["usage"] / 1024, 2)
        data["limit_usage"] = round(data["limit_usage"] / 1024, 2)
        if data["limit"] is not None:
            data["limit"] = round(data["limit"] / 1024, 2)
        if data["measured_unit"] == "MB":
            data["measured_unit"] = "GB"

    return list(components_data.values())


def _resolve_period_bounds(limit_period, today=None, period_offset=0):
    """Return (start_date, end_date, label) for the given limit period.

    `start`/`end` are inclusive `date` objects suitable for filtering
    `ComponentUsage.billing_period` (which is always the first day of the
    month). `TOTAL` returns (None, None, "Total") to mean "no time bound".

    `period_offset` shifts the window backward by N periods — 0 is the
    current period, -1 the previous, etc. TOTAL ignores the offset since
    there is no concept of a "previous lifetime".
    """
    today = today or datetime.date.today()

    if limit_period == LimitPeriods.QUARTERLY:
        # Build the current quarter from today, then rewind by `offset` quarters.
        current_q = (today.month - 1) // 3 + 1
        target_q = current_q + period_offset
        target_year = today.year
        # Normalize quarter into [1, 4], rolling year as needed.
        while target_q < 1:
            target_q += 4
            target_year -= 1
        while target_q > 4:
            target_q -= 4
            target_year += 1
        start_month = (target_q - 1) * 3 + 1
        start = datetime.date(target_year, start_month, 1)
        end_month = start_month + 2
        last_day = calendar.monthrange(target_year, end_month)[1]
        end = datetime.date(target_year, end_month, last_day)
        return start, end, f"Q{target_q} {target_year}"

    if limit_period == LimitPeriods.ANNUAL:
        target_year = today.year + period_offset
        return (
            datetime.date(target_year, 1, 1),
            datetime.date(target_year, 12, 31),
            str(target_year),
        )

    if limit_period == LimitPeriods.TOTAL:
        return None, None, "Total"

    # MONTH or unspecified — shift the month back by `offset` months.
    target_year = today.year
    target_month = today.month + period_offset
    while target_month < 1:
        target_month += 12
        target_year -= 1
    while target_month > 12:
        target_month -= 12
        target_year += 1
    start = datetime.date(target_year, target_month, 1)
    label = start.strftime("%b %Y")
    return start, start, label


def get_components_usage_data_per_offering(resources):
    """Aggregate per-component usage and limit stats per offering.

    One row per (offering, component_type, billing_type). Each row's
    `usage` / `limit_usage` is computed using the offering's own
    `limit_period` — quarterly offerings report quarter-to-date,
    yearly report year-to-date, total report lifetime, monthly report
    current month. The current period bounds are returned alongside
    each row so callers can render period-correct labels.

    Child offerings (OpenStack VM/Volume sub-offerings) are excluded to
    prevent double-counting with the parent tenant offering.
    OpenStack RAM and storage values are stored in MB; they are divided
    by 1024 before returning so the caller always receives GB.
    """
    today = datetime.date.today()
    MB_COMPONENT_TYPES = get_mb_component_types()

    # Exclude child offerings (OpenStack VM/Volume) — same rationale as
    # get_components_usage_data: VPC-level and VM-level usages must not be summed.
    resources = resources.filter(offering__parent__isnull=True)

    resources_by_offering = defaultdict(list)
    for resource in resources:
        resources_by_offering[resource.offering_id].append(resource)

    if not resources_by_offering:
        return []

    components = (
        models.OfferingComponent.objects.filter(
            offering_id__in=list(resources_by_offering.keys())
        )
        .select_related("offering")
        .distinct()
    )

    rows = []
    for component in components:
        offering = component.offering
        offering_resources = resources_by_offering.get(offering.id, [])
        if not offering_resources:
            continue

        period_start, period_end, period_label = _resolve_period_bounds(
            component.limit_period or LimitPeriods.MONTH, today
        )

        usage_qs = models.ComponentUsage.objects.filter(
            resource__in=offering_resources, component=component
        )
        if period_start is not None:
            usage_qs = usage_qs.filter(billing_period__gte=period_start)
        if period_end is not None:
            usage_qs = usage_qs.filter(billing_period__lte=period_end)
        period_usage = float(usage_qs.aggregate(total=Sum("usage"))["total"] or 0)

        # Sum resource.limits[component.type] across the offering's resources.
        # Track presence so we emit None when no limit is set anywhere.
        total_limit = 0.0
        any_limit = False
        for resource in offering_resources:
            limit_val = (resource.limits or {}).get(component.type)
            if limit_val is None:
                continue
            try:
                total_limit += float(limit_val)
                any_limit = True
            except (TypeError, ValueError):
                continue

        if component.billing_type == BillingTypes.USAGE:
            usage = period_usage
            limit_usage = 0.0
        elif component.billing_type == BillingTypes.LIMIT:
            usage = 0.0
            limit_usage = period_usage
        else:
            usage = 0.0
            limit_usage = 0.0

        # OpenStack persists RAM and storage in MB; convert to GB for the caller.
        measured_unit = component.measured_unit
        limit_value = total_limit if any_limit else None
        if (
            offering.type == OPENSTACK_TENANT_OFFERING
            and component.type in MB_COMPONENT_TYPES
        ):
            usage = round(usage / 1024, 2)
            limit_usage = round(limit_usage / 1024, 2)
            if limit_value is not None:
                limit_value = round(limit_value / 1024, 2)
            if measured_unit == "MB":
                measured_unit = "GB"

        rows.append(
            {
                "type": component.type,
                "name": component.name,
                "description": component.description,
                "measured_unit": measured_unit,
                "billing_type": component.billing_type,
                "usage": usage,
                "limit_usage": limit_usage,
                "limit": limit_value,
                "offering_name": offering.name,
                "offering_uuid": offering.uuid.hex,
                "limit_period": component.limit_period,
                "current_period_label": period_label,
                "current_period_start": period_start,
                "current_period_end": period_end,
            }
        )

    return rows


def _select_offering_component(offering, component_type=None):
    """Pick the component the chart should track.

    LIMIT-billed components are preferred since they're the ones a cap is
    meaningful against. If `component_type` is given, narrow to that type
    first. Returns None if the offering has no matching component.
    """
    components = list(offering.components.all())
    if component_type:
        components = [c for c in components if c.type == component_type]
    if not components:
        return None
    return next(
        (c for c in components if c.billing_type == BillingTypes.LIMIT),
        components[0],
    )


def _sum_offering_limits(resources, component_type):
    """Sum per-resource limits for one component type across the queryset.

    Returns the float total, or None if no resource declared a limit for
    that component type (so the caller can render "no cap" rather than 0).
    """
    total = 0.0
    any_limit = False
    for resource in resources:
        limit_val = (resource.limits or {}).get(component_type)
        if limit_val is None:
            continue
        try:
            total += float(limit_val)
            any_limit = True
        except (TypeError, ValueError):
            continue
    return total if any_limit else None


def _offering_usage_envelope(
    offering, component, period_start, period_end, period_label, today, limit
):
    """Common metadata block shared by the timeseries and by-project payloads."""
    return {
        "offering_uuid": offering.uuid.hex,
        "offering_name": offering.name,
        "type": component.type,
        "name": component.name,
        "measured_unit": component.measured_unit,
        "billing_type": component.billing_type,
        "limit_period": component.limit_period,
        "limit": limit,
        "current_period_label": period_label,
        "current_period_start": period_start,
        "current_period_end": period_end,
        "today": today,
    }


def get_offering_usage_timeseries(
    resources, offering, component_type=None, period_offset=0
):
    """Return monthly usage buckets for an offering's component.

    `resources` should already be scoped to a customer/project and to
    the given offering. Buckets cover the component's current
    `limit_period` (quarter / year / lifetime / current month) and are
    keyed by `ComponentUsage.billing_period` (always month-start).

    When `component_type` is given, that type is selected; otherwise
    the offering's first LIMIT-billed component is preferred (it is
    the one whose cap the chart is most useful against), falling back
    to the first defined component.

    `period_offset` shifts the window backward by N periods so callers
    can request a prior quarter / year / month.
    """
    today = datetime.date.today()

    component = _select_offering_component(offering, component_type)
    if component is None:
        return None

    effective_period = component.limit_period or LimitPeriods.MONTH

    if effective_period == LimitPeriods.MONTH:
        # A monthly cap resets every month, so cumulating across months
        # has no meaning. Widen the window to a rolling 6 months ending
        # at today's month so the chart shows a usable trend.
        end = datetime.date(
            today.year, today.month, calendar.monthrange(today.year, today.month)[1]
        )
        start_month = today.month - 5
        start_year = today.year
        while start_month < 1:
            start_month += 12
            start_year -= 1
        period_start = datetime.date(start_year, start_month, 1)
        period_end = end
        period_label = "Last 6 months"
    else:
        period_start, period_end, period_label = _resolve_period_bounds(
            effective_period, today, period_offset
        )

    usage_qs = models.ComponentUsage.objects.filter(
        resource__in=resources, component=component
    )
    if period_start is not None:
        usage_qs = usage_qs.filter(billing_period__gte=period_start)
    if period_end is not None:
        usage_qs = usage_qs.filter(billing_period__lte=period_end)

    bucketed = (
        usage_qs.values("billing_period")
        .annotate(total=Sum("usage"))
        .order_by("billing_period")
    )
    buckets = [
        {"billing_period": b["billing_period"], "usage": float(b["total"] or 0)}
        for b in bucketed
    ]

    return {
        **_offering_usage_envelope(
            offering,
            component,
            period_start,
            period_end,
            period_label,
            today,
            _sum_offering_limits(resources, component.type),
        ),
        "buckets": buckets,
    }


def get_offering_usage_by_project(
    resources, offering, component_type=None, period_offset=0
):
    """Return per-project usage breakdown for an offering's component.

    Same period semantics as `get_offering_usage_timeseries`. Each project
    that has at least one resource of the offering gets an entry with its
    in-period total `usage` and a `buckets` list of monthly contributions.
    Projects are ordered by `usage` descending so the caller can paginate
    or top-N from the front. `period_offset` shifts the window backward
    by N periods (same semantics as the timeseries endpoint).
    """
    today = datetime.date.today()

    component = _select_offering_component(offering, component_type)
    if component is None:
        return None

    period_start, period_end, period_label = _resolve_period_bounds(
        component.limit_period or LimitPeriods.MONTH, today, period_offset
    )

    usage_qs = models.ComponentUsage.objects.filter(
        resource__in=resources, component=component
    ).select_related("resource__project")
    if period_start is not None:
        usage_qs = usage_qs.filter(billing_period__gte=period_start)
    if period_end is not None:
        usage_qs = usage_qs.filter(billing_period__lte=period_end)

    by_project: dict[int, dict] = {}
    total_usage = 0.0
    for cu in usage_qs:
        project = cu.resource.project
        usage_val = float(cu.usage or 0)
        total_usage += usage_val
        entry = by_project.setdefault(
            project.id,
            {
                "project_uuid": project.uuid.hex,
                "project_name": project.name,
                "usage": 0.0,
                "_buckets": defaultdict(float),
            },
        )
        entry["usage"] += usage_val
        entry["_buckets"][cu.billing_period] += usage_val

    projects = []
    for entry in by_project.values():
        bucket_dict = entry.pop("_buckets")
        entry["buckets"] = [
            {"billing_period": k, "usage": v} for k, v in sorted(bucket_dict.items())
        ]
        projects.append(entry)
    projects.sort(key=lambda p: p["usage"], reverse=True)

    return {
        **_offering_usage_envelope(
            offering,
            component,
            period_start,
            period_end,
            period_label,
            today,
            _sum_offering_limits(resources, component.type),
        ),
        "total_usage": total_usage,
        "projects": projects,
    }


def format_limits_list(components_map, limits):
    return ", ".join(
        f"{components_map[key].name or components_map[key].type}: {value}"
        for key, value in limits.items()
    )


def get_resource_users(resource):
    project_user_ids = get_project_users(resource.project_id)
    customer_user_ids = get_customer_users(resource.project.customer_id)
    return core_models.User.objects.filter(
        id__in=project_user_ids.union(customer_user_ids)
    )


def generate_uidnumber_and_primary_group(offering):
    initial_uidnumber = int(offering.plugin_options.get("initial_uidnumber", 5000))
    initial_primarygroup_number = int(
        offering.plugin_options.get("initial_primarygroup_number", 5000)
    )

    offering_user_last_uidnumber = (
        models.OfferingUser.objects.exclude(backend_metadata=None)
        .filter(offering=offering, backend_metadata__has_key="uidnumber")
        .order_by("backend_metadata__uidnumber")
        .values_list("backend_metadata__uidnumber", flat=True)
        .last()
    ) or initial_uidnumber

    robot_account_last_uidnumber = (
        models.RobotAccount.objects.exclude(backend_metadata=None)
        .filter(resource__offering=offering, backend_metadata__has_key="uidnumber")
        .order_by("backend_metadata__uidnumber")
        .values_list("backend_metadata__uidnumber", flat=True)
        .last()
    ) or initial_uidnumber

    last_uidnumber = max([offering_user_last_uidnumber, robot_account_last_uidnumber])

    offset = last_uidnumber - initial_uidnumber + 1
    uidnumber = initial_uidnumber + offset
    primarygroup = initial_primarygroup_number + offset

    return uidnumber, primarygroup


def count_customers_number_change(service_provider):
    to_day = timezone.datetime.today().date()
    new_customers = []
    lost_customers = []

    for customer_id in (
        models.Order.objects.filter(
            offering__customer=service_provider.customer,
            type=OrderTypes.CREATE,
            state=OrderStates.DONE,
            created__gte=core_utils.month_start(to_day),
        )
        .order_by()
        .values_list("project__customer_id", flat=True)
        .distinct()
    ):
        if (
            not models.Resource.objects.filter(
                offering__customer=service_provider.customer,
                project__customer_id=customer_id,
                created__lt=core_utils.month_start(to_day),
            )
            .exclude(state=ResourceStates.TERMINATED)
            .exists()
        ):
            new_customers.append(customer_id)

    for customer_id in (
        models.Order.objects.filter(
            offering__customer=service_provider.customer,
            type=OrderTypes.TERMINATE,
            state=OrderStates.DONE,
            created__gte=core_utils.month_start(to_day),
        )
        .order_by()
        .values_list("project__customer_id", flat=True)
        .distinct()
    ):
        if (
            not models.Resource.objects.filter(
                offering__customer=service_provider.customer,
                project__customer=customer_id,
            )
            .exclude(state=ResourceStates.TERMINATED)
            .exists()
        ):
            lost_customers.append(customer_id)

    return len(new_customers) - len(lost_customers)


def count_resources_number_change(service_provider):
    to_day = timezone.datetime.today().date()

    created = (
        models.Order.objects.filter(
            offering__customer=service_provider.customer,
            type=OrderTypes.CREATE,
            state=OrderStates.DONE,
            created__gte=core_utils.month_start(to_day),
        )
        .order_by()
        .values_list("resource", flat=True)
        .distinct()
        .count()
    )

    terminated = (
        models.Order.objects.filter(
            offering__customer=service_provider.customer,
            type=OrderTypes.TERMINATE,
            state=OrderStates.DONE,
            created__gte=core_utils.month_start(to_day),
        )
        .order_by()
        .values_list("resource", flat=True)
        .distinct()
        .count()
    )

    return created - terminated


def generate_offering_password_hash(offering: models.Offering):
    password = offering.secret_options.get("shared_user_password")
    if password:
        password_hash = hashlib.sha256()
        password_hash.update(password.encode("utf-8"))
        return password_hash.hexdigest()
    else:
        return ""


def setup_linux_related_data(
    instance: models.OfferingUser | models.RobotAccount, offering
):
    uidnumber = instance.backend_metadata.get("uidnumber")
    primarygroup = instance.backend_metadata.get("primarygroup")

    if not uidnumber or not primarygroup:
        uidnumber, primarygroup = generate_uidnumber_and_primary_group(offering)

        instance.backend_metadata["uidnumber"] = uidnumber
        instance.backend_metadata["primarygroup"] = primarygroup

    login_shell = instance.backend_metadata.get("loginShell")
    if not login_shell:
        instance.backend_metadata["loginShell"] = "/bin/bash"

    homedir_prefix = offering.plugin_options.get("homedir_prefix", "/home/")
    instance.backend_metadata["homeDir"] = f"{homedir_prefix}{instance.username}"


def get_plans_available_for_user(
    user, offering, allowed_customer_uuid=None, without_parents_plan=False
):
    if without_parents_plan:
        qs = offering.plans.all()
    else:
        qs = (offering.parent or offering).plans.all()

    if user.is_anonymous:
        qs = qs.filter(organization_groups__isnull=True)
    elif user.is_staff or user.is_support:
        pass
    elif allowed_customer_uuid:
        qs = qs.filter(
            Q(organization_groups__isnull=True)
            | Q(organization_groups__in=get_organization_groups(user))
        ).filter_for_customer(allowed_customer_uuid)
    else:
        qs = qs.filter(
            Q(organization_groups__isnull=True)
            | Q(organization_groups__in=get_organization_groups(user))
        )

    return qs


def generate_glauth_records_for_offering_users(
    offering, offering_users, extra_user_gids=None
):
    """
    Generate GLauth config records for offering users.

    ``extra_user_gids`` (optional): mapping of ``user_id -> set[int gids]``
    of role-aware group gids to merge into each user's ``otherGroups``
    on top of the project-mapped ones derived here. See
    ``build_glauth_tree`` for the source.

    This function is optimized to minimize database queries by:
    - Expecting offering_users to have user and sshpublickey_set prefetched
    - Batch querying user-to-project mappings
    - Batch querying project-to-group-gid mappings
    - Batch querying users with active resources
    """
    extra_user_gids = extra_user_gids or {}
    # Convert to list to allow multiple iterations
    offering_users_list = list(offering_users)
    if not offering_users_list:
        return []

    # Collect all user IDs for batch queries
    user_ids = [ou.user_id for ou in offering_users_list]

    # Batch query: user_id -> list of project_ids
    project_content_type = ContentType.objects.get_for_model(structure_models.Project)
    user_project_mappings = defaultdict(set)
    for user_role in UserRole.objects.filter(
        is_active=True,
        user_id__in=user_ids,
        content_type=project_content_type,
    ).values("user_id", "object_id"):
        user_project_mappings[user_role["user_id"]].add(user_role["object_id"])

    # Collect all project IDs that any user has access to
    all_project_ids = set()
    for project_ids in user_project_mappings.values():
        all_project_ids.update(project_ids)

    # Batch query: project_id -> list of gids from OfferingUserGroup
    project_gid_mappings = defaultdict(set)
    if all_project_ids:
        for group in models.OfferingUserGroup.objects.filter(
            projects__id__in=all_project_ids
        ).prefetch_related("projects"):
            gid = group.backend_metadata.get("gid")
            if gid is not None:
                for project in group.projects.all():
                    if project.id in all_project_ids:
                        project_gid_mappings[project.id].add(str(gid))

    # Batch query: users with active (non-terminated) resources in this offering
    users_with_active_resources = set(
        models.Resource.objects.filter(
            offering=offering,
            project_id__in=all_project_ids,
        )
        .exclude(state=ResourceStates.TERMINATED)
        .values_list("project_id", flat=True)
    )
    # Map user_id -> has_active_resource
    users_with_access = set()
    for user_id, project_ids in user_project_mappings.items():
        if project_ids & users_with_active_resources:
            users_with_access.add(user_id)

    password_sha256 = generate_offering_password_hash(offering)
    user_records = []

    for offering_user in offering_users_list:
        user = offering_user.user
        username = offering_user.username
        if "uidnumber" not in offering_user.backend_metadata:
            logger.warning(
                "OfferingUser %s does not have uidnumber in backend_metadata, skipping generation of glauth record",
                offering_user,
            )
            continue

        uidnumber = offering_user.backend_metadata["uidnumber"]
        primarygroup = offering_user.backend_metadata["primarygroup"]
        login_shell = offering_user.backend_metadata["loginShell"]
        home_dir = offering_user.backend_metadata["homeDir"]

        # Use prefetched SSH keys (no additional query)
        ssh_keys = [
            f'"{escape_toml_string(ssh_key.public_key)}"'
            for ssh_key in user.sshpublickey_set.all()
        ]
        ssh_keys_line = ",\n    ".join(ssh_keys)

        # Use pre-computed user-to-project-to-gid mapping
        user_project_ids = user_project_mappings.get(user.id, set())
        group_ids = set()
        for project_id in user_project_ids:
            group_ids.update(project_gid_mappings.get(project_id, set()))
        # Merge in role-aware gids supplied by build_glauth_tree.
        for gid in extra_user_gids.get(user.id, ()):
            group_ids.add(str(gid))
        other_groups = ", ".join(sorted(group_ids, key=lambda s: (len(s), s)))

        # Use pre-computed access check
        user_disabled_status = "false" if user.id in users_with_access else "true"

        record = textwrap.dedent(
            f"""
        [[users]]
          name = "{escape_toml_string(user.get_username())}"
          givenname="{escape_toml_string(user.first_name)}"
          sn="{escape_toml_string(user.last_name)}"
          mail = "{escape_toml_string(user.email)}"
          uidnumber = {uidnumber}
          primarygroup = {primarygroup}
          otherGroups = [{other_groups}]
          sshkeys = [{ssh_keys_line}]
          loginShell = "{escape_toml_string(login_shell)}"
          homeDir = "{escape_toml_string(home_dir)}"
          passsha256 = "{password_sha256}"
          disabled = {user_disabled_status}
            [[users.customattributes]]
            preferredUsername = ["{escape_toml_string(username)}"]
        """
        )

        record += textwrap.dedent(
            f"""
        [[groups]]
          name = "{escape_toml_string(username)}"
          gidnumber = {primarygroup}
        """
        )
        user_records.append(record)

    return user_records


def generate_glauth_records_for_robot_accounts(offering, robot_accounts):
    # make sure that only accounts in OK and requested_deletion are exposed e.g. in glauth
    valid_states = [
        RobotAccountStates.OK,
        RobotAccountStates.REQUESTED_DELETION,
    ]
    robot_accounts = robot_accounts.filter(state__in=valid_states)

    robot_account_records = []
    for robot_account in robot_accounts:
        ssh_keys = [f'"{escape_toml_string(key)}"' for key in robot_account.keys]
        ssh_keys_line = ",\n    ".join(ssh_keys)

        username = robot_account.username
        uidnumber = robot_account.backend_metadata["uidnumber"]
        primarygroup = robot_account.backend_metadata["primarygroup"]
        login_shell = robot_account.backend_metadata["loginShell"]
        home_dir = robot_account.backend_metadata["homeDir"]
        password_sha256 = generate_offering_password_hash(offering)

        record = textwrap.dedent(
            f"""
        [[users]]
          name = "{escape_toml_string(username)}"
          uidnumber = {uidnumber}
          primarygroup = {primarygroup}
          sshkeys = [{ssh_keys_line}]
          loginShell = "{escape_toml_string(login_shell)}"
          homeDir = "{escape_toml_string(home_dir)}"
          passsha256 = "{password_sha256}"
            [[users.customattributes]]
            preferredUsername = ["{escape_toml_string(username)}"]
        """
        )

        record += textwrap.dedent(
            f"""
        [[groups]]
          name = "{escape_toml_string(username)}"
          gidnumber = {primarygroup}
        """
        )

        robot_account_records.append(record)

    return robot_account_records


def escape_toml_string(value):
    """Escape a string for safe inclusion in a TOML double-quoted string.

    Backslashes and double quotes must be escaped to produce valid TOML.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


# --------------------------------------------------------------------------
# Role-aware glauth groups + structured tree builder
# --------------------------------------------------------------------------


def _render_role_group_name(template_str, **variables):
    """Render an LDAP/glauth group name from a string.Template.

    Missing variables substitute to the empty string (``safe_substitute``)
    so an operator's template that mentions ``${project_name}`` still
    renders when the scope is a Resource and not a ResourceProject.
    """
    return Template(template_str).safe_substitute(**variables)


def _allocate_role_group_gid(offering):
    """Pick the next gid for a new OfferingRoleGroup on this offering.

    The caller MUST hold an outer ``transaction.atomic`` + a
    ``SELECT FOR UPDATE`` on the offering row, so concurrent role
    assignments don't collide on the same gid.
    """
    plugin_options = offering.plugin_options or {}
    base = plugin_options.get("initial_rolegroup_number", 60000)
    existing = [
        int(rg.backend_metadata["gid"])
        for rg in models.OfferingRoleGroup.objects.filter(offering=offering)
        if "gid" in (rg.backend_metadata or {})
    ]
    return max(existing + [base - 1]) + 1


def _ensure_role_group(offering, scope, role):
    """Get-or-create an OfferingRoleGroup row, allocating a gid if new.

    Returns the row. Safe under concurrent role assignments — the
    offering row is locked while we allocate.
    """
    with transaction.atomic():
        models.Offering.objects.select_for_update().get(pk=offering.pk)
        ct = ContentType.objects.get_for_model(scope.__class__)
        rg, created = models.OfferingRoleGroup.objects.get_or_create(
            offering=offering,
            content_type=ct,
            object_id=scope.pk,
            role=role,
        )
        if created or "gid" not in (rg.backend_metadata or {}):
            gid = _allocate_role_group_gid(offering)
            rg.backend_metadata = {**(rg.backend_metadata or {}), "gid": gid}
            rg.save(update_fields=["backend_metadata"])
        return rg


def _glauth_scope_dict(scope):
    """Serialise a Resource / ResourceProject scope for the JSON tree."""
    if isinstance(scope, models.Resource):
        return {
            "type": "resource",
            "uuid": scope.uuid.hex,
            "name": scope.name,
            "slug": scope.slug or "",
            "resource_uuid": None,
        }
    if isinstance(scope, models.ResourceProject):
        return {
            "type": "resource_project",
            "uuid": scope.uuid.hex,
            "name": scope.name,
            "slug": None,
            "resource_uuid": scope.resource.uuid.hex,
        }
    raise TypeError(f"Unsupported glauth scope: {type(scope)!r}")


def _compute_role_groups(offering, *, resource_filter=None):
    """Walk Resources + ResourceProjects + UserRoles and emit role groups.

    ``resource_filter``: ``None`` for offering-wide, or a ``Resource``
    instance to restrict the walk to one resource + its RPs.

    Returns a list of dicts: ``{gid, name, kind, scope, role,
    member_user_ids}`` where ``scope`` is the dict from
    ``_glauth_scope_dict``. The set of emitted groups depends on
    ``plugin_options['resource_role_map']`` and
    ``plugin_options['resource_project_role_map']`` — roles outside
    the maps are skipped (mirrors MR 433's translator).
    """
    plugin_options = offering.plugin_options or {}
    resource_role_map = plugin_options.get("resource_role_map") or {}
    rp_role_map = plugin_options.get("resource_project_role_map") or {}
    if not resource_role_map and not rp_role_map:
        return []

    resource_template = plugin_options.get(
        "resource_role_group_template", "${resource_slug}_${role_name}"
    )
    rp_template = plugin_options.get(
        "resource_project_role_group_template",
        "${resource_slug}_${rp_uuid_short}_${role_name}",
    )

    if resource_filter is not None:
        resources_qs = models.Resource.objects.filter(pk=resource_filter.pk)
    else:
        resources_qs = models.Resource.objects.filter(offering=offering).exclude(
            state=ResourceStates.TERMINATED
        )
    resources_qs = resources_qs.select_related("project", "project__customer")
    resources = list(resources_qs)
    if not resources:
        return []

    resource_ct = ContentType.objects.get_for_model(models.Resource)
    rp_ct = ContentType.objects.get_for_model(models.ResourceProject)

    rps_by_resource = defaultdict(list)
    if rp_role_map:
        rp_qs = models.ResourceProject.available_objects.filter(
            resource__in=resources
        ).select_related("resource")
        for rp in rp_qs:
            rps_by_resource[rp.resource_id].append(rp)

    # Batch-fetch UserRoles for all scopes in two queries (one per ct).
    resource_user_roles = defaultdict(list)
    if resource_role_map:
        resource_ids = [r.pk for r in resources]
        for ur in UserRole.objects.filter(
            is_active=True,
            content_type=resource_ct,
            object_id__in=resource_ids,
            role__is_system_role=False,
        ).select_related("user", "role"):
            resource_user_roles[ur.object_id].append(ur)

    rp_user_roles = defaultdict(list)
    if rp_role_map:
        all_rp_ids = [rp.pk for rps in rps_by_resource.values() for rp in rps]
        if all_rp_ids:
            for ur in UserRole.objects.filter(
                is_active=True,
                content_type=rp_ct,
                object_id__in=all_rp_ids,
                role__is_system_role=False,
            ).select_related("user", "role"):
                rp_user_roles[ur.object_id].append(ur)

    out = []
    for resource in resources:
        resource_slug = resource.slug or ""
        customer = resource.project.customer if resource.project else None
        customer_slug = customer.slug if customer else ""
        project_slug = resource.project.slug if resource.project else ""

        if resource_role_map:
            out.extend(
                _emit_groups_for_scope(
                    offering=offering,
                    scope=resource,
                    user_roles=resource_user_roles.get(resource.pk, []),
                    role_map=resource_role_map,
                    template_str=resource_template,
                    template_vars=dict(
                        resource_slug=resource_slug,
                        customer_slug=customer_slug,
                        project_slug=project_slug,
                    ),
                    kind="resource_role",
                )
            )

        if rp_role_map:
            for rp in rps_by_resource.get(resource.pk, []):
                rp_uuid = rp.uuid.hex
                out.extend(
                    _emit_groups_for_scope(
                        offering=offering,
                        scope=rp,
                        user_roles=rp_user_roles.get(rp.pk, []),
                        role_map=rp_role_map,
                        template_str=rp_template,
                        template_vars=dict(
                            resource_slug=resource_slug,
                            customer_slug=customer_slug,
                            project_slug=project_slug,
                            rp_uuid=rp_uuid,
                            rp_uuid_short=rp_uuid[:8],
                            project_name=rp.name or "",
                        ),
                        kind="resource_project_role",
                    )
                )

    return out


def _emit_groups_for_scope(
    *, offering, scope, user_roles, role_map, template_str, template_vars, kind
):
    """Group UserRoles by role, render group name, ensure persistence."""
    by_role = defaultdict(list)
    for ur in user_roles:
        if ur.role.name not in role_map:
            continue
        by_role[ur.role].append(ur)

    emitted = []
    for role, urs in by_role.items():
        member_user_ids = {ur.user_id for ur in urs}
        member_usernames = sorted({ur.user.username for ur in urs if ur.user.username})
        if not member_user_ids:
            continue
        rg = _ensure_role_group(offering, scope, role)
        gid = int(rg.backend_metadata["gid"])
        name = _render_role_group_name(
            template_str, role_name=role_map[role.name], **template_vars
        )
        emitted.append(
            {
                "gid": gid,
                "name": name,
                "kind": kind,
                "scope": _glauth_scope_dict(scope),
                "role": role.name,
                "member_user_ids": member_user_ids,
                "members": member_usernames,
            }
        )
    return emitted


def build_glauth_tree(offering, *, resource_filter=None):
    """Build the structured glauth view for an offering (or one resource).

    Single source of truth shared by the TOML emitters and the JSON
    ``glauth_tree`` endpoints.

    ``resource_filter``: ``None`` for offering-wide; or a ``Resource``
    instance to scope users to ``resource.project`` and groups to one
    resource and its ResourceProjects.
    """
    # Offering users: queryset same as the existing endpoints.
    offering_users_qs = (
        models.OfferingUser.objects.filter(offering=offering)
        .exclude(username="")
        .select_related("user")
        .prefetch_related("user__sshpublickey_set")
    )
    if resource_filter is not None:
        # Mirror Resource.glauth_users_config which scopes to project users.
        user_ids = get_project_users(resource_filter.project_id)
        offering_users_qs = offering_users_qs.filter(user__id__in=user_ids)

    offering_users = list(offering_users_qs)

    # Project-mapped (existing) groups.
    project_groups = []
    project_user_membership = defaultdict(set)
    user_project_mappings = _user_project_mappings(
        [ou.user_id for ou in offering_users]
    )
    project_groups_qs = models.OfferingUserGroup.objects.filter(
        offering=offering
    ).prefetch_related("projects")
    for oug in project_groups_qs:
        gid = (oug.backend_metadata or {}).get("gid")
        if gid is None:
            continue
        projects = list(oug.projects.all())
        if resource_filter is not None and not any(
            p.id == resource_filter.project_id for p in projects
        ):
            continue
        scope_project = projects[0] if projects else None
        member_user_ids = set()
        for uid, pids in user_project_mappings.items():
            if any(p.id in pids for p in projects):
                member_user_ids.add(uid)
                project_user_membership[uid].add(int(gid))
        member_usernames = sorted(
            {ou.user.username for ou in offering_users if ou.user_id in member_user_ids}
        )
        project_groups.append(
            {
                "gid": int(gid),
                "name": str(gid),
                "kind": "project",
                "scope": {
                    "type": "project",
                    "uuid": scope_project.uuid.hex if scope_project else "",
                    "name": scope_project.name if scope_project else "",
                    "slug": getattr(scope_project, "slug", "") or "",
                    "resource_uuid": None,
                },
                "role": None,
                "members": member_usernames,
            }
        )

    # Role-aware groups.
    role_groups_raw = _compute_role_groups(offering, resource_filter=resource_filter)

    # Stitch user -> gids and order groups deterministically.
    role_user_membership = defaultdict(set)
    role_groups = []
    for g in sorted(
        role_groups_raw,
        key=lambda g: (g["scope"]["type"], g["scope"]["uuid"], g["role"] or ""),
    ):
        for uid in g["member_user_ids"]:
            role_user_membership[uid].add(g["gid"])
        # The internal _emit_groups_for_scope dict carries member_user_ids;
        # strip it before exposing.
        role_groups.append({k: v for k, v in g.items() if k != "member_user_ids"})

    # Users with membership rollup.
    users_with_active_resources = _users_with_active_resources(offering, offering_users)
    users = []
    for ou in offering_users:
        if not ou.username:
            continue
        meta = ou.backend_metadata or {}
        memberships = []
        for g in project_groups:
            if g["gid"] in project_user_membership.get(ou.user_id, ()):
                memberships.append(
                    {
                        "gid": g["gid"],
                        "group_name": g["name"],
                        "kind": g["kind"],
                        "role": g["role"],
                    }
                )
        for g in role_groups:
            if g["gid"] in role_user_membership.get(ou.user_id, ()):
                memberships.append(
                    {
                        "gid": g["gid"],
                        "group_name": g["name"],
                        "kind": g["kind"],
                        "role": g["role"],
                    }
                )
        users.append(
            {
                "username": ou.username,
                "uidnumber": meta.get("uidnumber"),
                "disabled": ou.user_id not in users_with_active_resources,
                "personal_group": meta.get("primarygroup"),
                "mail": ou.user.email or "",
                "givenname": ou.user.first_name or "",
                "sn": ou.user.last_name or "",
                "login_shell": meta.get("loginShell") or "",
                "home_dir": meta.get("homeDir") or "",
                "ssh_keys": [k.public_key for k in ou.user.sshpublickey_set.all()],
                "memberships": memberships,
            }
        )

    # Robot accounts (no group memberships in current model — surfaced flat).
    robot_qs = models.RobotAccount.objects.filter(resource__offering=offering).filter(
        state__in=[RobotAccountStates.OK, RobotAccountStates.REQUESTED_DELETION]
    )
    if resource_filter is not None:
        robot_qs = robot_qs.filter(resource=resource_filter)
    robot_accounts = [
        {
            "username": ra.username,
            "uidnumber": (ra.backend_metadata or {}).get("uidnumber"),
            "personal_group": (ra.backend_metadata or {}).get("primarygroup"),
            "login_shell": (ra.backend_metadata or {}).get("loginShell") or "",
            "home_dir": (ra.backend_metadata or {}).get("homeDir") or "",
            "ssh_keys": list(ra.keys) if ra.keys else [],
        }
        for ra in robot_qs
    ]

    return {
        "offering": {
            "uuid": offering.uuid.hex,
            "name": offering.name,
            "slug": offering.slug or "",
        },
        "groups": project_groups + role_groups,
        "users": users,
        "robot_accounts": robot_accounts,
        # Internal: pre-computed user_id -> set[gid] for the TOML emitter.
        # Stripped before serialisation by the view layer.
        "_user_role_gids": {
            uid: gids for uid, gids in role_user_membership.items() if gids
        },
        # Materialised list (not queryset) — keeps prefetch cache hot and
        # avoids a second SQL round-trip when the TOML emitter iterates.
        "_offering_users": offering_users,
    }


def _user_project_mappings(user_ids):
    """user_id -> set(project_id) for active project-scope user roles."""
    project_ct = ContentType.objects.get_for_model(structure_models.Project)
    mapping = defaultdict(set)
    if not user_ids:
        return mapping
    for ur in UserRole.objects.filter(
        is_active=True, user_id__in=user_ids, content_type=project_ct
    ).values("user_id", "object_id"):
        mapping[ur["user_id"]].add(ur["object_id"])
    return mapping


def _users_with_active_resources(offering, offering_users):
    """Return the set of user_ids that have access to a non-terminated resource."""
    user_ids = [ou.user_id for ou in offering_users]
    user_project_mappings = _user_project_mappings(user_ids)
    all_project_ids = {pid for pids in user_project_mappings.values() for pid in pids}
    if not all_project_ids:
        return set()
    active_project_ids = set(
        models.Resource.objects.filter(
            offering=offering, project_id__in=all_project_ids
        )
        .exclude(state=ResourceStates.TERMINATED)
        .values_list("project_id", flat=True)
    )
    return {
        uid for uid, pids in user_project_mappings.items() if pids & active_project_ids
    }


def sanitize_name(name):
    name = name.strip().lower()
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"\W+", "", name)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return name


def create_anonymized_username(offering):
    prefix = offering.plugin_options.get("username_anonymized_prefix", "walduruser_")
    previous_users = models.OfferingUser.objects.filter(
        offering=offering, username__istartswith=prefix
    ).order_by("username")

    if previous_users.exists():
        last_username = previous_users.last().username
        last_number = int(last_username[-USERNAME_ANONYMIZED_POSTFIX_LENGTH:])
        number = str(last_number + 1).zfill(USERNAME_ANONYMIZED_POSTFIX_LENGTH)
    else:
        number = "0".zfill(USERNAME_ANONYMIZED_POSTFIX_LENGTH)

    return f"{prefix}{number}"


def create_username_from_full_name(user, offering):
    first_name = sanitize_name(user.first_name)
    last_name = sanitize_name(user.last_name)

    username_raw = f"{first_name}_{last_name}"
    previous_users = models.OfferingUser.objects.filter(
        offering=offering, username__istartswith=username_raw
    ).order_by("username")

    if previous_users.exists():
        last_username = previous_users.last().username
        last_number = int(last_username[-USERNAME_POSTFIX_LENGTH:])
        number = str(last_number + 1).zfill(USERNAME_POSTFIX_LENGTH)
    else:
        number = "0".zfill(USERNAME_POSTFIX_LENGTH)

    return f"{username_raw}_{number}"


def create_username_from_freeipa_profile(user):
    profiles = freeipa_models.Profile.objects.filter(user=user)
    if profiles.count() == 0:
        logger.warning("There is no FreeIPA profile for user %s", user)
        return ""
    else:
        return profiles.first().username


def generate_username(user, offering):
    username_generation_policy = offering.plugin_options.get(
        "username_generation_policy", UsernameGenerationPolicy.SERVICE_PROVIDER.value
    )

    if username_generation_policy == UsernameGenerationPolicy.SERVICE_PROVIDER.value:
        return ""

    if username_generation_policy == UsernameGenerationPolicy.ANONYMIZED.value:
        return create_anonymized_username(offering)

    if username_generation_policy == UsernameGenerationPolicy.FULL_NAME.value:
        return create_username_from_full_name(user, offering)

    if username_generation_policy == UsernameGenerationPolicy.WALDUR_USERNAME.value:
        return user.username

    if username_generation_policy == UsernameGenerationPolicy.FREEIPA.value:
        return create_username_from_freeipa_profile(user)

    if username_generation_policy == UsernameGenerationPolicy.IDENTITY_CLAIM.value:
        return user.details.get("site_username", "")

    return ""


def user_offerings_mapping(offerings):
    resources = models.Resource.objects.filter(
        state=ResourceStates.OK, offering__in=offerings
    )
    resource_ids = resources.values_list("id", flat=True)

    project_ids = resources.values_list("project_id", flat=True)
    projects = structure_models.Project.objects.filter(id__in=project_ids)

    user_offerings_set = set()

    for project in projects:
        users = project.get_users()

        project_resources = project.resource_set.filter(id__in=resource_ids)
        project_offering_ids = project_resources.values_list("offering_id", flat=True)
        project_offerings = models.Offering.objects.filter(id__in=project_offering_ids)

        for user in users:
            for offering in project_offerings:
                if (
                    config.ENFORCE_USER_CONSENT_FOR_OFFERINGS
                    and offering.has_terms_of_service()
                ):
                    if models.UserOfferingConsent.objects.filter(
                        user=user,
                        offering=offering,
                        revocation_date__isnull=True,
                    ).exists():
                        user_offerings_set.add((user, offering))
                else:
                    user_offerings_set.add((user, offering))

    for user, offering in user_offerings_set:
        offering_user = models.OfferingUser.objects.filter(
            user=user, offering=offering
        ).first()
        if offering_user is None:
            username = generate_username(user, offering)
            # Set state to OK when username is known at creation time
            state = (
                OfferingUserStates.OK
                if username
                else OfferingUserStates.CREATION_REQUESTED
            )
            offering_user = models.OfferingUser.objects.create(
                user=user, offering=offering, username=username, state=state
            )
            logger.info("Offering user %s has been created.", offering_user)
        elif offering_user.state == OfferingUserStates.DELETION_REQUESTED:
            # Only restore from DELETION_REQUESTED — no backend action taken yet.
            # DELETING/ERROR_DELETING/DELETED are left untouched since
            # the service provider/site agent may have already started backend actions.
            if offering_user.username:
                offering_user.set_ok()
            else:
                offering_user.state = OfferingUserStates.CREATION_REQUESTED
            offering_user.save(update_fields=["state"])
            logger.info(
                "Offering user %s has been restored from deletion request.",
                offering_user,
            )


def order_should_not_be_reviewed_by_provider(order: models.Order):
    offering = order.offering
    user = order.consumer_reviewed_by or order.created_by

    if offering.type == SITE_AGENT_PLUGIN_NAME:
        return False

    if offering.type == BASIC_PLUGIN_NAME:
        return False

    if offering.type == REMOTE_PLUGIN_NAME:
        # If an offering has auto_approve_remote_orders flag set to True, an order can be processed without approval
        auto_approve_remote_orders = offering.plugin_options.get(
            "auto_approve_remote_orders", False
        )
        # A service provider owner or a service manager is not required to approve an order manually
        user_is_service_provider_owner = (
            offering.customer
            and structure_permissions._has_owner_access(user, offering.customer)
        )
        user_is_service_provider_offering_manager = (
            offering.customer
            and structure_permissions._has_service_manager_access(
                user, offering.customer
            )
            and offering.has_user(user)
        )
        # If any condition is not met, the order is requested for manual approval
        return (
            auto_approve_remote_orders
            or user_is_service_provider_owner
            or user_is_service_provider_offering_manager
        )

    if offering.type == SCRIPT_PLUGIN_NAME:
        # If auto_approve_marketplace_script is False, always require manual provider approval
        # This applies to all users including service provider owners and staff
        return offering.plugin_options.get("auto_approve_marketplace_script", True)

    return True


def get_consumer_approvers(order):
    users = User.objects.none()

    if config.NOTIFY_STAFF_ABOUT_APPROVALS:
        users |= User.objects.filter(is_staff=True, is_active=True)

    users |= get_users_with_permission(
        order.project.customer, PermissionEnum.APPROVE_ORDER
    )

    users |= get_users_with_permission(order.project, PermissionEnum.APPROVE_ORDER)

    approvers = (
        users.distinct()
        .exclude(email="")
        .exclude(notifications_enabled=False)
        .values_list("email", flat=True)
    )

    return approvers


def get_provider_approvers(order):
    users = User.objects.none()

    if config.NOTIFY_STAFF_ABOUT_APPROVALS:
        users |= User.objects.filter(is_staff=True, is_active=True)

    users |= get_users_with_permission(
        order.offering.customer, PermissionEnum.APPROVE_ORDER
    )

    users |= get_users_with_permission(order.offering, PermissionEnum.APPROVE_ORDER)

    approvers = (
        users.distinct()
        .exclude(email="")
        .exclude(notifications_enabled=False)
        .values_list("email", flat=True)
    )

    return approvers


def check_pending_order_exists(resource):
    return models.Order.objects.filter(
        resource=resource,
        state__in=(
            OrderStates.PENDING_CONSUMER,
            OrderStates.PENDING_PROVIDER,
            OrderStates.EXECUTING,
        ),
    ).exists()


def get_pending_consumer_terminate_order(resource):
    return (
        models.Order.objects.filter(
            resource=resource,
            type=OrderTypes.TERMINATE,
            state=OrderStates.PENDING_CONSUMER,
        )
        .select_related("offering", "project__customer")
        .order_by("-created")
        .first()
    )


def refresh_integration_agent_status(request, agent_type):
    user_agent = core_utils.get_user_agent(request)
    if "waldur-site-agent" not in user_agent:
        return

    offering_uuid = request.query_params.get("offering_uuid")
    if offering_uuid is None:
        logger.warning("Offering UUID is missing, skipping integration status update")
        return

    offering = models.Offering.objects.filter(uuid=offering_uuid).first()

    if offering is None:
        logger.warning(
            "Offering with UUID %s doesn't exist, skipping integration status update"
        )
        return

    if not has_permission(request, PermissionEnum.UPDATE_OFFERING, offering.customer):
        logger.error("User doesn't have permission for offering management")
        return

    integration_status, _ = models.IntegrationStatus.objects.get_or_create(
        offering=offering,
        agent_type=agent_type,
    )
    integration_status.set_last_request_timestamp()
    integration_status.set_backend_active()
    integration_status.service_name = request.headers.get("User-Agent", "")
    integration_status.save()


def parse_date(date_str: str | int | None) -> datetime.date | None:
    if not isinstance(date_str, str):
        return None
    try:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise serializers.ValidationError(
            {"end_date": _("Invalid date format. Use YYYY-MM-DD.")}
        )


def validate_end_date(
    offering: models.Offering,
    created_date: datetime.date,
    end_date: datetime.date | None = None,
) -> None | datetime.date:
    """
    Validate or compute the resource end date based on plugin options.
    Raises ValidationError if constraints are violated or configuration is invalid.
    """

    options = cast(dict[str, int | str | None], offering.plugin_options)

    is_required = options.get("is_resource_termination_date_required")
    max_offset = options.get("max_resource_termination_offset_in_days")
    default_offset = options.get("default_resource_termination_offset_in_days")

    latest_date = parse_date(options.get("latest_date_for_resource_termination"))

    if end_date:
        if end_date and end_date < timezone.datetime.today().date():
            raise serializers.ValidationError(
                {"end_date": _("Cannot be earlier than the current date.")}
            )

        if latest_date and end_date > latest_date:
            raise serializers.ValidationError(
                {"end_date": _("End date exceeds global termination limit.")}
            )
        if isinstance(max_offset, int):
            if end_date > created_date + datetime.timedelta(days=max_offset):
                raise serializers.ValidationError(
                    {"end_date": _("End date exceeds maximum allowed offset.")}
                )
        return end_date

    if not is_required:
        return

    if not isinstance(default_offset, int):
        raise serializers.ValidationError(
            {"end_date": _("Missing default termination offset configuration.")}
        )

    termination_date = created_date + datetime.timedelta(days=default_offset)
    if latest_date:
        return min(termination_date, latest_date)
    else:
        return termination_date


def sync_component_user_usage(allocation_user_usage, plugin_name):
    allocation = allocation_user_usage.allocation
    resource = models.Resource.objects.filter(scope=allocation).first()
    if resource is None:
        logger.error(
            "The allocation %s does not have a linked resource, skipping processing",
            allocation,
        )
        return

    if resource.offering.type != plugin_name:
        return

    offering_user = None
    if allocation_user_usage.user is not None:
        offering_user = models.OfferingUser.objects.filter(
            offering=resource.offering, user=allocation_user_usage.user
        ).first()

    for offering_component in models.OfferingComponent.objects.filter(
        offering=resource.offering
    ):
        if not hasattr(allocation_user_usage, offering_component.type + "_usage"):
            continue

        usage = getattr(allocation_user_usage, offering_component.type + "_usage")

        component_usage = models.ComponentUsage.objects.filter(
            resource=resource,
            billing_period__month=allocation_user_usage.month,
            billing_period__year=allocation_user_usage.year,
            component=offering_component,
        ).first()

        if component_usage is None:
            logger.warning(
                "The component usage for %s component of %s does not exist, skipping component user usage sync",
                offering_component,
                allocation,
            )
            continue

        component_user_usage, created = (
            models.ComponentUserUsage.objects.update_or_create(
                username=allocation_user_usage.username,
                component_usage=component_usage,
                defaults={"usage": usage, "user": offering_user},
            )
        )

        if created:
            logger.info("%s has been created", component_user_usage)
        else:
            logger.info("%s has been updated, new usage: %s", component_usage, usage)


def update_component_quota(allocation, plugin_name):
    """Shared handler for syncing allocation component quotas to marketplace resources.

    Used by marketplace_slurm, marketplace_openportal, and marketplace_openportal_remote
    plugins to update ComponentQuota, ComponentUsage, and Resource.limits when an
    allocation's usage or limit fields change.
    """
    from waldur_mastermind.marketplace.plugins import manager

    try:
        resource = models.Resource.objects.get(scope=allocation)
    except models.Resource.DoesNotExist:
        return

    new_limits = {}
    new_usages = {}
    for component in manager.get_components(plugin_name):
        usage = float(getattr(allocation, component.type + "_usage"))
        limit = float(getattr(allocation, component.type + "_limit"))

        try:
            offering_component = models.OfferingComponent.objects.get(
                offering=resource.offering, type=component.type
            )
        except models.OfferingComponent.DoesNotExist:
            logger.warning(
                "Skipping Allocation synchronization because "
                "marketplace.OfferingComponent does not exist. "
                "Allocation ID: %s",
                allocation.id,
            )
            continue

        new_limits[component.type] = limit
        new_usages[component.type] = usage
        models.ComponentQuota.objects.update_or_create(
            resource=resource,
            component=offering_component,
            defaults={"limit": limit, "usage": usage},
        )

        plan_periods = models.ResourcePlanPeriod.objects.filter(
            resource=resource, end=None
        )

        if not plan_periods.exists():
            logger.warning(
                "Skipping component usage synchronization because valid "
                "ResourcePlanPeriod is not found. "
                "Allocation: %s, Resource: %s",
                allocation,
                resource,
            )
            continue

        if plan_periods.count() > 1:
            logger.warning(
                "More than one active ResourcePlanPeriod found for "
                "Allocation: %s, Resource: %s. Using the first plan only.",
                allocation,
                resource,
            )

        plan_period = plan_periods.first()

        date = timezone.now()
        models.ComponentUsage.objects.update_or_create(
            resource=resource,
            component=offering_component,
            billing_period=core_utils.month_start(date),
            plan_period=plan_period,
            defaults={"usage": usage, "date": date},
        )

    if resource.limits != new_limits:
        logger.debug(
            "Syncing limits for %s. Allocation ID: %s. Old limits: %s. New limits: %s",
            plugin_name,
            allocation.id,
            resource.limits,
            new_limits,
        )
        resource.limits = new_limits
        resource.save(update_fields=["limits"])


class SafeFormatDict(dict):
    """A dict subclass that returns empty string for missing keys during str.format_map()."""

    def __missing__(self, key):
        return ""


def render_resource_name_pattern(
    pattern, project, offering, plan=None, attributes=None
):
    resource_count = models.Resource.objects.filter(
        project=project, offering=offering
    ).count()
    context = SafeFormatDict(
        customer_name=project.customer.name,
        customer_slug=project.customer.slug,
        project_name=project.name,
        project_slug=project.slug,
        offering_name=offering.name,
        offering_slug=offering.slug,
        plan_name=plan.name if plan else "",
        counter=str(resource_count + 1) if resource_count else "",
        attributes=SafeFormatDict(attributes or {}),
    )
    result = pattern.format_map(context)
    result = result.lower()
    result = re.sub(r"[^A-Za-z0-9.-]", "-", result)
    return core_utils.remove_duplicate_hyphens(result).strip("-")


def generate_resource_name(
    project: structure_models.Project,
    offering: models.Offering,
    plan=None,
    attributes=None,
):
    pattern = (offering.plugin_options or {}).get("resource_name_pattern")
    if pattern:
        try:
            return render_resource_name_pattern(
                pattern, project, offering, plan=plan, attributes=attributes
            )
        except (KeyError, ValueError, IndexError):
            logger.warning(
                "Failed to render resource_name_pattern %r for offering %s, falling back to default.",
                pattern,
                offering.uuid,
            )

    resource_count = models.Resource.objects.filter(
        project=project, offering=offering
    ).count()
    parts = [
        project.customer.slug,
        project.slug,
        offering.slug,
    ]
    result = "-".join(parts)
    result = result.lower()
    result = result.replace("_", "-")

    if resource_count:
        result += "-" + str(resource_count + 1)

    return core_utils.remove_duplicate_hyphens(result)


def notification_about_project_ending(end_date):
    projects_by_recipient = defaultdict(list)

    # Find projects where the effective end date (including grace period) matches the target date
    candidate_projects = structure_models.Project.available_objects.exclude(
        end_date__isnull=True
    )
    expired_projects = []

    for project in candidate_projects:
        effective_end_date = project.get_effective_end_date()
        if effective_end_date == end_date:
            expired_projects.append(project)

    # If there are no expired projects, we don't need to send notifications
    if not expired_projects:
        logger.info("No projects found with effective_end_date=%s", end_date)
        return

    for project in expired_projects:
        logger.info(
            "Project %s (uuid=%s) has end_date=%s, grace_period=%d days, effective_end_date=%s",
            project.name,
            project.uuid,
            project.end_date,
            project.get_grace_period_days(),
            project.get_effective_end_date(),
        )
        project_users = (
            project.get_users().exclude(email="").exclude(notifications_enabled=False)
        )
        owners = (
            project.customer.get_users(RoleEnum.CUSTOMER_OWNER)
            .exclude(email="")
            .exclude(notifications_enabled=False)
        )
        users = set(project_users) | set(owners)

        for user in users:
            projects_by_recipient[user].append(project)

    for user, projects in projects_by_recipient.items():
        for project in projects:
            project.url = core_utils.format_homeport_link(
                "projects/{project_uuid}/", project_uuid=project.uuid.hex
            )

        for project in projects:
            project.grace_period_days = project.get_grace_period_days()
            project.effective_end_date = project.get_effective_end_date()

        context = {
            "projects": projects,
            "user": user,
            "end_date": end_date,
            "count_projects": len(projects),
            "delta": (end_date - timezone.datetime.today().date()).days,
        }
        logger.info(
            "Sending notification to user %s about %d projects",
            user.email,
            len(projects),
        )
        core_utils.broadcast_mail(
            "marketplace",
            "notification_about_project_ending",
            context,
            [user.email],
        )


# Mock data generators for service accounts
def generate_mock_service_account_response(username: str) -> dict:
    """Generate a mock service account response that matches the GetServiceAccountResponse schema."""
    now = datetime.datetime.now()
    return {
        "serviceAccount": {
            "createdAt": now.isoformat(),
            "updatedAt": now.isoformat(),
            "type": "service_account",
            "status": "active",
            "disabledDate": None,
            "username": username,
            "email": "mock@example.com",
            "description": "Mock service account for testing",
            "unixUid": 5000 + hash(username) % 1000,  # Generate consistent UID
            "homeDir": f"/home/{username}",
            "shell": "/bin/bash",
            "targetType": "project",
            "targetIdentifier": f"mock-project-{username[:8]}",
            "apiKeyExpiresAt": (now + datetime.timedelta(days=90)).isoformat(),
            "apiKeyTtl": 7776000,  # 90 days in seconds
            "owner": None,
            "project": None,
        }
    }


def generate_mock_api_key_rotation_response(username: str) -> dict:
    """Generate a mock API key rotation response that matches GetServiceAccountWithApiKeyResponse schema."""
    now = datetime.datetime.now()
    expires_at = now + datetime.timedelta(days=90)
    ttl = 7776000  # 90 days in seconds

    return {
        "serviceAccount": {
            "createdAt": (now - datetime.timedelta(days=30)).isoformat(),
            "updatedAt": now.isoformat(),
            "type": "service_account",
            "status": "active",
            "disabledDate": None,
            "username": username,
            "email": "mock@example.com",
            "description": "Mock service account for testing",
            "unixUid": 5000 + hash(username) % 1000,
            "homeDir": f"/home/{username}",
            "shell": "/bin/bash",
            "targetType": "project",
            "targetIdentifier": f"mock-project-{username[:8]}",
            "apiKeyExpiresAt": expires_at.isoformat(),
            "apiKeyTtl": ttl,
            "owner": None,
            "project": None,
        },
        "apiKey": {
            "apiKey": f"rotated-mock-api-key-{username}-{uuid.uuid4().hex[:8]}",
            "createdAt": now.isoformat(),
            "expiresAt": expires_at.isoformat(),
            "ttl": ttl,
        },
    }


def generate_mock_service_account_creation_response(
    service_account: dict, username: str, scope_type: str
) -> dict:
    """Generate a mock service account creation response that matches GetServiceAccountWithApiKeyResponse schema."""
    now = datetime.datetime.now()
    expires_at = now + datetime.timedelta(days=90)
    ttl = 7776000  # 90 days in seconds
    mock_username = service_account.get(
        "preferred_identifier", f"mock-{uuid.uuid4().hex[:8]}"
    )

    return {
        "serviceAccount": {
            "createdAt": now.isoformat(),
            "updatedAt": now.isoformat(),
            "type": "service_account",
            "status": "active",
            "disabledDate": None,
            "username": mock_username,
            "email": service_account.get("email", "mock@example.com"),
            "description": service_account.get("description", "Mock service account"),
            "unixUid": 5000 + hash(mock_username) % 1000,
            "homeDir": f"/home/{mock_username}",
            "shell": "/bin/bash",
            "targetType": scope_type,
            "targetIdentifier": service_account.get("scope_slug", "mock-scope"),
            "apiKeyExpiresAt": expires_at.isoformat(),
            "apiKeyTtl": ttl,
            "owner": None,
            "project": None,
        },
        "apiKey": {
            "apiKey": f"mock-api-key-{uuid.uuid4().hex[:16]}",
            "createdAt": now.isoformat(),
            "expiresAt": expires_at.isoformat(),
            "ttl": ttl,
        },
    }


def generate_mock_service_account_update_response(service_account) -> dict:
    """Generate a mock service account update response that matches GetServiceAccountResponse schema."""
    now = datetime.datetime.now()
    return {
        "serviceAccount": {
            "createdAt": (now - datetime.timedelta(days=30)).isoformat(),
            "updatedAt": now.isoformat(),
            "type": "service_account",
            "status": "active",
            "disabledDate": None,
            "username": service_account.username,
            "email": service_account.email,
            "description": service_account.description,
            "unixUid": 5000 + hash(service_account.username) % 1000,
            "homeDir": f"/home/{service_account.username}",
            "shell": "/bin/bash",
            "targetType": "project",
            "targetIdentifier": f"mock-project-{service_account.username[:8]}",
            "apiKeyExpiresAt": (now + datetime.timedelta(days=90)).isoformat(),
            "apiKeyTtl": 7776000,
            "owner": None,
            "project": None,
        }
    }


# Mock data generators for course accounts
def generate_mock_course_account_response(username: str) -> dict:
    """Generate a mock course account response that matches the course account request schema."""
    now = datetime.datetime.now()
    return {
        "tempAccount": {
            "createdAt": now.isoformat(),
            "updatedAt": now.isoformat(),
            "type": "user",
            "status": "active",
            "lifecyclePhase": "available",
            "purpose": "course",
            "disabledDate": (now + datetime.timedelta(weeks=1)).isoformat(),
            "username": username,
            "email": "mock@example.com",
            "description": "Mock course account for testing",
            "unixUid": 5000 + hash(username) % 1000,  # Generate consistent UID
            "homeDir": f"/home/{username}",
            "shell": "/bin/bash",
            "targetType": "project",
            "targetIdentifier": f"mock-course-{username[:8]}",
            "owner": None,
            "project": None,
        }
    }


def generate_mock_course_account_creation_response(
    course_account: dict, username: str
) -> dict:
    """Generate a mock course account creation response that matches course account schema."""
    del course_account, username
    mock_username = f"course_{random.randint(1000, 9999)}"

    return generate_mock_course_account_response(mock_username)


def get_account_api_token(token_url, client_id, client_secret):
    token_url = token_url.rstrip("/")

    token_request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    token_params = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    try:
        token_response = httpx.post(
            token_url,
            data=token_params,
            headers=token_request_headers,
            follow_redirects=True,
        )
        token_response.raise_for_status()
        # Extract the token
        token_data = token_response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError("Access token not found in token response.")
        return access_token
    except httpx.HTTPError as e:
        logger.error("Error obtaining token: %s", e)
        raise


def get_service_account_api_token():
    token_url = settings.WALDUR_CORE["SERVICE_ACCOUNT_TOKEN_URL"]
    client_id = settings.WALDUR_CORE["SERVICE_ACCOUNT_TOKEN_CLIENT_ID"]
    client_secret = settings.WALDUR_CORE["SERVICE_ACCOUNT_TOKEN_SECRET"]
    return get_account_api_token(token_url, client_id, client_secret)


def get_course_account_api_token():
    token_url = settings.WALDUR_CORE["COURSE_ACCOUNT_TOKEN_URL"]
    client_id = settings.WALDUR_CORE["COURSE_ACCOUNT_TOKEN_CLIENT_ID"]
    client_secret = settings.WALDUR_CORE["COURSE_ACCOUNT_TOKEN_SECRET"]
    return get_account_api_token(token_url, client_id, client_secret)


def rotate_service_account_api_key(service_account: models.ScopedServiceAccount):
    if config.ENABLE_MOCK_SERVICE_ACCOUNT_BACKEND:
        logger.info(
            f"Mock mode enabled for rotate_service_account_api_key: {service_account.username}"
        )
        return generate_mock_api_key_rotation_response(service_account.username)

    service_account_url = settings.WALDUR_CORE["SERVICE_ACCOUNT_URL"]
    if not service_account_url:
        raise ValueError("URL for service accounts is not configured")

    service_account_url = service_account_url.rstrip("/")
    try:
        api_access_token = get_service_account_api_token()

        url = f"{service_account_url}/{service_account.username}/rotate-api-key"
        response = httpx.put(
            url,
            headers={"Authorization": f"Bearer {api_access_token}"},
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.error("Error obtaining token: %s", e)
        raise


def post_service_account_to_url(
    url: str, service_account: dict, owner_username: str = "", scope_type: str = ""
):
    try:
        api_access_token = get_service_account_api_token()
        if scope_type == "project":
            project: structure_models.Project = service_account["project"]
            customer = project.customer
            scope_name = project.name
            scope_slug = project.slug
            scope_offering_slugs = []
            offering_slugs = set(
                project.resource_set.exclude(
                    state=ResourceStates.TERMINATED
                ).values_list("offering__slug", flat=True)
            )
        elif scope_type == "customer":
            customer: structure_models.Customer = service_account["customer"]
            scope_name = customer.name
            scope_slug = customer.slug
            offering_slugs = set(
                models.Resource.objects.exclude(state=ResourceStates.TERMINATED)
                .filter(project__customer=customer)
                .values_list("offering__slug", flat=True)
            )
        else:
            raise ValueError(f"Unsupported service account type: {scope_type}")

        scope_offering_slugs = list(offering_slugs)

        payload = {
            "ownerUsername": owner_username,
            "preferredIdentifier": service_account["preferred_identifier"],
            "email": service_account.get("email", customer.email),
            "description": service_account.get("description", ""),
            "scopeType": scope_type,
            "scopeName": scope_name,
            "scopeSlug": scope_slug,
            "scopeOfferingSlugs": scope_offering_slugs,
        }

        headers = {"Authorization": f"Bearer {api_access_token}"}
        response = httpx.post(url, json=payload, headers=headers, follow_redirects=True)
        response.raise_for_status()
        logger.info("Service account has been successfully updated at %s", url)
        return response
    except (httpx.HTTPError, ValueError, KeyError) as e:
        logger.error("Request to %s failed: %s", url, e)
        raise


def extract_error_details_from_httpx_error(exc: httpx.HTTPError):
    """Extract error details from an HTTPx error depending on the error type."""
    return (
        exc.response.json()
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.text
        else f"Status code: {exc.response.status_code}, empty body"
    )


def create_service_account(service_account: dict, owner_username: str, scope_type: str):
    """
    Makes a synchronous call to the webhook URL to create a service account.
    Raises exceptions on failure which should be handled by the viewset.
    """
    if config.ENABLE_MOCK_SERVICE_ACCOUNT_BACKEND:
        logger.info("Mock mode enabled for create_service_account")
        return generate_mock_service_account_creation_response(
            service_account, owner_username, scope_type
        )

    if not settings.WALDUR_CORE.get("SERVICE_ACCOUNT_USE_API"):
        return

    service_account_url = settings.WALDUR_CORE["SERVICE_ACCOUNT_URL"]
    if not service_account_url:
        raise ValueError("URL for service accounts is not configured")

    service_account_url = service_account_url.rstrip("/")

    try:
        response = post_service_account_to_url(
            service_account_url, service_account, owner_username, scope_type
        )
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        error_details = extract_error_details_from_httpx_error(exc)
        logger.error(
            "Unable to create a service account for %s",
            error_details,
        )
        raise


def close_service_account(service_account: models.ScopedServiceAccount):
    """
    Makes a synchronous call to the webhook URL to remove a service account.
    Raises exceptions on failure which should be handled by the viewset.
    """
    if config.ENABLE_MOCK_SERVICE_ACCOUNT_BACKEND:
        logger.info(
            f"Mock mode enabled for delete_service_account: {service_account.username}"
        )
        # Generate a response showing the account as closed before deleting
        response = generate_mock_service_account_response(service_account.username)
        response["serviceAccount"]["status"] = "closed"
        response["serviceAccount"]["disabledDate"] = datetime.datetime.now().isoformat()
        service_account.set_state_closed()
        service_account.save(update_fields=["state"])
        return response

    if not settings.WALDUR_CORE.get("SERVICE_ACCOUNT_USE_API"):
        return

    service_account_url = settings.WALDUR_CORE["SERVICE_ACCOUNT_URL"]
    if not service_account_url:
        raise ValidationError("URL for service accounts is not configured")

    service_account_url = service_account_url.rstrip("/")

    try:
        api_access_token = get_service_account_api_token()
        existing_service_account = get_service_account(service_account)
        if existing_service_account is None:
            logger.warning(
                "Service account %s not found at backend, deleting locally",
                service_account.username,
            )
            service_account.set_state_closed()
            service_account.save(update_fields=["state"])
            return

        url = f"{service_account_url}/{service_account.username}/close"
        response = httpx.put(
            url,
            headers={"Authorization": f"Bearer {api_access_token}"},
            follow_redirects=True,
        )
        response.raise_for_status()
        if response.status_code == 200:
            service_account.set_state_closed()
            service_account.save(update_fields=["state"])
    except (httpx.HTTPError, ValueError) as exc:
        error_details = extract_error_details_from_httpx_error(exc)
        logger.error(
            "Unable to close the service account %s: %s",
            service_account.username,
            error_details,
        )
        service_account.set_state_erred()
        service_account.error_message = str(error_details)
        service_account.error_traceback = traceback.format_exc()
        service_account.save(update_fields=["error_message", "error_traceback"])
        raise


def get_service_account(service_account: models.ScopedServiceAccount):
    """
    Makes a synchronous call to the webhook URL to get a service account.
    Raises exceptions on failure which should be handled by the viewset.
    """
    if config.ENABLE_MOCK_SERVICE_ACCOUNT_BACKEND:
        logger.info(
            f"Mock mode enabled for get_service_account: {service_account.username}"
        )
        return generate_mock_service_account_response(service_account.username)

    if not settings.WALDUR_CORE.get("SERVICE_ACCOUNT_USE_API"):
        return

    service_account_url = settings.WALDUR_CORE["SERVICE_ACCOUNT_URL"]
    if not service_account_url:
        raise ValidationError("URL for service accounts is not configured")

    service_account_url = service_account_url.rstrip("/")

    try:
        api_access_token = get_service_account_api_token()
        url = f"{service_account_url}/{service_account.username}"
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {api_access_token}"},
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            logger.warning("Service account %s not found", service_account.username)
            return None
        raise
    except (httpx.HTTPError, ValueError) as exc:
        logger.error(exc)
        raise


def get_course_account(
    course_account: models.CourseAccount, api_access_token: str | None = None
):
    if not settings.WALDUR_CORE.get("COURSE_ACCOUNT_USE_API"):
        return

    course_account_url = settings.WALDUR_CORE["COURSE_ACCOUNT_URL"]
    if not course_account_url:
        raise ValidationError("URL for course accounts is not configured")

    course_account_url = course_account_url.rstrip("/")
    username = course_account.user.username
    try:
        if api_access_token is None:
            api_access_token = get_course_account_api_token()
        url = f"{course_account_url}/{username}"
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {api_access_token}"},
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            logger.warning("Course account %s not found", username)
            return None
        raise
    except (httpx.HTTPError, ValueError) as exc:
        logger.error(exc)
        raise


def update_service_account(service_account: models.ScopedServiceAccount):
    """
    Makes a synchronous call to the webhook URL to update a service account email or/and description fields.
    Raises exceptions on failure which should be handled by the viewset.
    """
    if config.ENABLE_MOCK_SERVICE_ACCOUNT_BACKEND:
        logger.info(
            f"Mock mode enabled for update_service_account: {service_account.username}"
        )
        return generate_mock_service_account_update_response(service_account)

    if not settings.WALDUR_CORE.get("SERVICE_ACCOUNT_USE_API"):
        return

    service_account_url = settings.WALDUR_CORE["SERVICE_ACCOUNT_URL"]
    if not service_account_url:
        raise ValidationError("URL for service accounts is not configured")

    service_account_url = service_account_url.rstrip("/")

    try:
        api_access_token = get_service_account_api_token()
        url = f"{service_account_url}/{service_account.username}"

        response = httpx.put(
            url,
            headers={"Authorization": f"Bearer {api_access_token}"},
            follow_redirects=True,
            json={
                "email": service_account.email,
                "description": service_account.description,
            },
        )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        error_details = extract_error_details_from_httpx_error(exc)
        logger.error(
            "Unable to update service account %s: %s",
            service_account.username,
            error_details,
        )
        service_account.set_state_erred()
        service_account.error_message = str(error_details)
        service_account.error_traceback = traceback.format_exc()
        service_account.save(
            update_fields=["state", "error_message", "error_traceback"]
        )
        raise


@transaction.atomic
def move_offering(
    offering: models.Offering,
    target_customer: structure_models.Customer,
    current_user=None,
    preserve_permissions=False,
):
    if target_customer.blocked:
        raise rf_exceptions.ValidationError(
            _("Target provider's customer can not be blocked.")
        )

    if offering.customer == target_customer:
        raise rf_exceptions.ValidationError(
            _("Offering is already assigned to the target provider.")
        )

    offering.customer = target_customer
    offering.save(update_fields=["customer"])

    if not preserve_permissions:
        for permission in get_permissions(offering):
            permission.revoke(
                current_user, reason="Offering moved to different provider"
            )
            logger.info(f"Permission {permission} has been revoked")

    logger.info("Offering %s has been moved to provider %s", offering, target_customer)


def _identity_manager_matches_offering_user(
    subscriber: User,
    message_payload: dict,
) -> bool:
    """Check if an identity manager should receive this OFFERING_USER event.

    Returns True when the subscriber is an identity manager whose managed_isds
    overlap with the offering user's linked user's active_isds.
    """
    if not subscriber.is_identity_manager:
        return False

    managed_isds = subscriber.managed_isds or []
    if not managed_isds:
        return False

    target_user_uuid = message_payload.get("user_uuid")
    if not target_user_uuid:
        return False

    target_user = User.objects.filter(uuid=target_user_uuid).first()
    if not target_user:
        return False

    active_isds = target_user.active_isds or []
    return bool(set(managed_isds) & set(active_isds))


# Event types where consumer access is determined by payload content.
# Scoped via order_uuid → order.project.customer
_CONSUMER_ORDER_EVENTS = {ObservableObjectType.ORDER}
# Scoped via resource_uuid → resource.project.customer
_CONSUMER_RESOURCE_EVENTS = {
    ObservableObjectType.RESOURCE,
    ObservableObjectType.RESOURCE_PERIODIC_LIMITS,
}
# Scoped via project_uuid in payload
_CONSUMER_PROJECT_SCOPED_EVENTS = {
    ObservableObjectType.USER_ROLE,
    ObservableObjectType.SERVICE_ACCOUNT,
    ObservableObjectType.COURSE_ACCOUNT,
}


def _resolve_event_consumer_customer(
    offering: models.Offering,
    message_payload: dict,
    affected_object: ObservableObjectType,
) -> structure_models.Customer | None:
    """Resolve which consumer customer an event belongs to.

    Returns the customer that owns the project associated with the event,
    or None if the event type is not consumer-visible or cannot be resolved.
    Only resources with non-terminated state on the given offering are considered.
    """
    project = None

    # Projects with active (non-terminated) resources on this offering
    active_project_ids = (
        models.Resource.objects.filter(offering=offering)
        .exclude(state=ResourceStates.TERMINATED)
        .values_list("project_id", flat=True)
    )

    if affected_object in _CONSUMER_ORDER_EVENTS:
        order_uuid = message_payload.get("order_uuid")
        if order_uuid:
            order = (
                models.Order.objects.filter(uuid=order_uuid)
                .select_related("project__customer")
                .first()
            )
            if order and order.project_id in set(active_project_ids):
                project = order.project
    elif affected_object in _CONSUMER_RESOURCE_EVENTS:
        resource_uuid = message_payload.get("resource_uuid")
        if resource_uuid:
            resource = (
                models.Resource.objects.filter(
                    uuid=resource_uuid,
                    offering=offering,
                    project_id__in=active_project_ids,
                )
                .select_related("project__customer")
                .first()
            )
            if resource:
                project = resource.project
    elif affected_object in _CONSUMER_PROJECT_SCOPED_EVENTS:
        event_project_uuid = message_payload.get("project_uuid")
        if event_project_uuid:
            project = (
                structure_models.Project.objects.filter(
                    uuid=event_project_uuid,
                    id__in=active_project_ids,
                )
                .select_related("customer")
                .first()
            )
    elif affected_object == ObservableObjectType.OFFERING_USER:
        offering_user_uuid = message_payload.get("user_uuid")
        if offering_user_uuid:
            project_ct = ContentType.objects.get_for_model(structure_models.Project)
            role = UserRole.objects.filter(
                content_type=project_ct,
                object_id__in=active_project_ids,
                user__uuid=offering_user_uuid,
                is_active=True,
            ).first()
            if role:
                project = (
                    structure_models.Project.objects.filter(id=role.object_id)
                    .select_related("customer")
                    .first()
                )

    if project:
        return project.customer
    return None


def prepare_messages(
    offering: models.Offering,
    message_payload: dict,
    affected_object: ObservableObjectType,
) -> list[dict[str, str]]:
    """Helper function to prepare event messages for marketplace events.

    Generates event messages for users who have subscribed to events related to marketplace
    offerings they have access to. Each message includes a vhost, topic and payload.

    For OFFERING_USER events, identity managers whose managed_isds overlap with
    the linked user's active_isds also receive the event, even without direct
    offering access.

    Args:
        offering: Marketplace offering instance to generate messages for
        message_payload: Dictionary containing event-specific data to be included in the message
        affected_object: Type of event for the topic name (e.g. "order" or "user_role")

    Returns:
        List of dictionaries, each containing:
            - vhost: User UUID hex string
            - topic: Topic string in format "subscription/{sub_uuid}/offering/{offering_uuid}/{affected_object}"
            - payload: JSON string containing the input payload plus offering_uuid

    Example:
        >>> messages = prepare_messages(
        ...     offering=some_offering,
        ...     payload={"order_uuid": "123"},
        ...     affected_object=ObservableObjectType.ORDER
        ... )
        >>> messages[0]
        {
            'vhost': 'user-uuid-hex',
            'topic': 'subscription/sub-uuid/offering/off-uuid/order',
            'payload': '{"order_uuid": "123", "offering_uuid": "off-uuid"}'
        }
    """

    logger.debug(
        "Preparing messages for event %s, offering %s",
        affected_object.value,
        offering,
    )
    event_subscriptions = logging_models.EventSubscription.objects.filter(
        observable_objects__contains=[{"object_type": affected_object.value}]
    )

    if not event_subscriptions.exists():
        logger.debug(
            "No event subscriptions exist for %s, skipping message sending",
            affected_object.value,
        )
        return []

    # Resolve the event's target customer for consumer access checks.
    # This is computed once (outside the loop) since it depends only on the
    # payload, not the subscribing user.
    event_consumer_customer = _resolve_event_consumer_customer(
        offering, message_payload, affected_object
    )

    messages_to_send = []
    for event_subscription in event_subscriptions:
        user = event_subscription.user
        logger.info("Processing subscription for user %s", user)

        # Check if user has access to offering
        linked_offerings = models.Offering.objects.all().filter_for_user(user)
        if not linked_offerings.filter(id=offering.id).exists():
            # Consumer access: the event's target customer must be one of
            # the user's connected customers.
            # get_connected_customers returns a flat QuerySet of customer IDs.
            has_consumer_access = (
                event_consumer_customer is not None
                and event_consumer_customer.id in set(get_connected_customers(user))
            )
            if not has_consumer_access:
                # Identity managers can receive OFFERING_USER events for users
                # whose active_isds overlap with the manager's managed_isds.
                if not (
                    affected_object == ObservableObjectType.OFFERING_USER
                    and _identity_manager_matches_offering_user(user, message_payload)
                ):
                    logger.debug(
                        "The user %s does not have access to the offering %s",
                        user,
                        offering,
                    )
                    continue

        # Check if queue is registered (receiver must request queue creation first)
        queue_exists = logging_models.EventSubscriptionQueue.objects.filter(
            event_subscription=event_subscription,
            offering_uuid=offering.uuid,
            object_type=affected_object.value,
        ).exists()

        if not queue_exists:
            logger.debug(
                "Queue not registered for subscription %s, offering %s, type %s. Skipping.",
                event_subscription.uuid.hex,
                offering.uuid.hex,
                affected_object.value,
            )
            continue

        topic_name = f"subscription/{event_subscription.uuid.hex}/offering/{offering.uuid.hex}/{affected_object.value}"
        message_payload["offering_uuid"] = offering.uuid.hex
        message_payload_str = json.dumps(message_payload)
        vhost_name = user.uuid.hex
        messages_to_send.append(
            {"vhost": vhost_name, "topic": topic_name, "payload": message_payload_str}
        )

    return messages_to_send


def publish_backend_resource_request(request: models.BackendResourceRequest):
    """
    Send a message to RabbitMQ requesting a list of resources for the offering.
    """
    logger.info("Requesting a list of backend resources for offering %s via RabbitMQ")

    payload = {
        "backend_resource_request_uuid": request.uuid.hex,
    }
    messages = prepare_messages(
        request.offering,
        payload,
        ObservableObjectType.IMPORTABLE_RESOURCES,
    )
    if messages:
        logging_tasks.publish_messages.delay(messages)


def post_course_account_to_url(
    url: str,
    course_account: dict,
    owner_username: str = "",
    api_access_token: str | None = None,
):
    try:
        if api_access_token is None:
            api_access_token = get_course_account_api_token()
        project: structure_models.Project = course_account["project"]
        offering_slugs = list(
            set(
                project.resource_set.exclude(
                    state=ResourceStates.TERMINATED
                ).values_list("offering__slug", flat=True)
            )
        )

        payload = {
            "ownerUsername": owner_username,
            "email": course_account["email"],
            "description": course_account.get("description", ""),
            "scopeType": "project",
            "scopeName": project.name,
            "scopeSlug": project.slug,
            "scopeOfferingSlugs": offering_slugs,
            "scopeValidTo": project.end_date.isoformat(),
            "scopeValidFrom": project.start_date.isoformat()
            if project.start_date
            else project.created.date().isoformat(),
        }

        headers = {"Authorization": f"Bearer {api_access_token}"}
        response = httpx.post(url, json=payload, headers=headers, follow_redirects=True)
        response.raise_for_status()
        logger.info("Course account has been successfully updated at %s", url)
        return response
    except (httpx.HTTPError, ValueError, KeyError) as e:
        logger.error("Request to %s failed: %s", url, e)
        raise


def create_course_account(
    course_account: dict, owner_username: str, api_access_token: str | None = None
):
    if config.ENABLE_MOCK_COURSE_ACCOUNT_BACKEND:
        logger.info("Mock mode enabled for create_course_account")
        return generate_mock_course_account_creation_response(
            course_account, owner_username
        )

    if not settings.WALDUR_CORE.get("COURSE_ACCOUNT_USE_API"):
        return

    course_account_url = settings.WALDUR_CORE["COURSE_ACCOUNT_URL"]
    if not course_account_url:
        raise ValidationError("URL for course accounts is not configured")

    course_account_url = course_account_url.rstrip("/")

    try:
        response = post_course_account_to_url(
            course_account_url, course_account, owner_username, api_access_token
        )
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        error_details = extract_error_details_from_httpx_error(exc)
        logger.error(
            "Unable to create a course account for %s",
            error_details,
        )
        raise


def create_multiple_course_accounts(
    course_accounts_data: list[dict], owner_username: str
):
    if config.ENABLE_MOCK_COURSE_ACCOUNT_BACKEND:
        logger.info("Mock mode enabled for create_multiple_course_accounts")
        course_accounts_created = []
        for course_account_data in course_accounts_data:
            response_data = generate_mock_course_account_creation_response(
                course_account_data, owner_username
            )
            # Add email from request to the mock response for consistency
            response_data["email"] = course_account_data["email"]
            response_data["project_uuid"] = str(course_account_data["project"].uuid)
            response_data["project_name"] = course_account_data["project"].name
            course_accounts_created.append(response_data)
        return course_accounts_created

    if not settings.WALDUR_CORE.get("COURSE_ACCOUNT_USE_API"):
        return

    course_account_url = settings.WALDUR_CORE["COURSE_ACCOUNT_URL"]
    if not course_account_url:
        raise ValidationError("URL for course accounts is not configured")

    course_account_url = course_account_url.rstrip("/")
    course_accounts_created = []

    try:
        api_access_token = get_course_account_api_token()
    except (httpx.HTTPError, ValueError, KeyError) as e:
        logger.error("Request to %s failed: %s", course_account_url, e)
        return course_accounts_created

    for course_account_data in course_accounts_data:
        try:
            response = post_course_account_to_url(
                course_account_url,
                course_account_data,
                owner_username,
                api_access_token,
            )
            response_data = response.json()
            user = core_models.User.objects.create(
                username=response_data["tempAccount"]["username"],
                email=response_data["tempAccount"]["email"],
                description="Course Account",
            )
            course_account = models.CourseAccount.objects.create(
                user=user,
                email=course_account_data["email"],
                project=course_account_data["project"],
            )
            course_accounts_created.append(course_account)
        except (httpx.HTTPError, ValueError) as exc:
            logger.error(exc)

    return course_accounts_created


def close_course_account(
    course_account: models.CourseAccount, api_access_token: str | None = None
):
    # Already closed — nothing to do. CLOSED is terminal and the FSM forbids
    # CLOSED→CLOSED, so re-entry (e.g. via project pre_delete signal after a
    # prior manual close) would raise TransitionNotAllowed and bubble up as 500.
    if course_account.state == CourseAccountState.CLOSED:
        return

    # No backend account was ever created — nothing to close remotely.
    if course_account.user is None:
        course_account.set_state_closed()
        course_account.save(update_fields=["state"])
        return

    if config.ENABLE_MOCK_COURSE_ACCOUNT_BACKEND:
        logger.info(
            f"Mock mode enabled for delete_course_account: {course_account.user.username}"
        )
        # Generate a response showing the account as closed before deleting
        response = generate_mock_course_account_response(course_account.user.username)
        response["tempAccount"]["status"] = "closed"
        response["tempAccount"]["disabledDate"] = datetime.datetime.now().isoformat()
        course_account.set_state_closed()
        course_account.save(update_fields=["state"])
        return response

    if not settings.WALDUR_CORE.get("COURSE_ACCOUNT_USE_API"):
        return

    course_account_url = settings.WALDUR_CORE["COURSE_ACCOUNT_URL"]
    if not course_account_url:
        raise ValidationError("URL for course accounts is not configured")

    course_account_url = course_account_url.rstrip("/")
    username = course_account.user.username
    user = course_account.user

    try:
        if api_access_token is None:
            api_access_token = get_course_account_api_token()
        existing_course_account = get_course_account(course_account, api_access_token)
        if existing_course_account is None:
            logger.warning(
                "Service account %s not found at backend, deleting locally",
                username,
            )
            course_account.set_state_closed()
            course_account.save(update_fields=["state"])
            if user:
                user.is_active = False
                user.deactivation_reason = (
                    f"Course account {course_account.uuid} not found at backend"
                )
                user.save(update_fields=["is_active", "deactivation_reason"])
            return

        url = f"{course_account_url}/{username}/close"
        response = httpx.put(
            url,
            headers={"Authorization": f"Bearer {api_access_token}"},
            follow_redirects=True,
        )
        response.raise_for_status()
        if response.status_code == 200:
            course_account.set_state_closed()
            course_account.save(update_fields=["state"])
            if user:
                user.is_active = False
                user.deactivation_reason = (
                    f"Course account {course_account.uuid} closed"
                )
                user.save(update_fields=["is_active", "deactivation_reason"])
    except (httpx.HTTPError, ValueError) as exc:
        error_details = extract_error_details_from_httpx_error(exc)
        logger.error(
            "Unable to close the course account %s: %s",
            course_account.email,
            error_details,
        )
        course_account.set_state_erred()
        course_account.error_message = str(error_details)
        course_account.error_traceback = traceback.format_exc()
        course_account.save(update_fields=["error_message", "error_traceback"])
        raise


def get_viewset_from_basename(basename):
    """
    Resolve a router basename to its viewset class
    """
    resolver = get_resolver()

    # DRF router creates URLs with basename + action suffix
    # We'll look for the list action
    url_name = f"{basename}-list"

    # Get the URL pattern
    for pattern in resolver.url_patterns:
        if hasattr(pattern, "name") and pattern.name == url_name:
            return pattern.callback.cls

    # If not found directly, search in included patterns
    for pattern in resolver.url_patterns:
        if hasattr(pattern, "url_patterns"):
            for sub_pattern in pattern.url_patterns:
                if hasattr(sub_pattern, "name") and sub_pattern.name == url_name:
                    return sub_pattern.callback.cls

    return None


@lru_cache(maxsize=1)
def get_model_serializer(model: type):
    """
    Retrieve the serializer class associated with a model's viewset.

    This function resolves a model's URL basename to its corresponding DRF viewset
    and returns the serializer class used by that viewset.

    Args:
        model (Type): A model class that implements `get_url_name()` method,
                     which returns the DRF router basename for the model.

    Returns:
        Type[serializers.Serializer] | None: The serializer class associated with
                                             the model's viewset, or None if:
                                             - The model doesn't have get_url_name()
                                             - No viewset is registered for the basename
                                             - The viewset doesn't have serializer_class

    Example:
        >>> from waldur_openstack.models import Instance
        >>> serializer = get_model_serializer(Instance)
        >>> print(serializer)
        <class 'waldur_openstack.serializers.OpenStackInstanceSerializer'>

    Note:
        This function only retrieves the static `serializer_class` attribute.
        If the viewset uses dynamic serializer selection via `get_serializer_class()`,
        this function will return the default serializer class or None.
    """

    try:
        base_url = model.get_url_name()
        view = get_viewset_from_basename(base_url)
        return getattr(view, "serializer_class", None)
    except (AttributeError, KeyError, TypeError):
        logger.debug("Unable to resolve model serializer %s", model)
        return None


def validate_reallocation(source_resource, limits_to_reallocate, targets, user):
    """
    Validate reallocation of resource limits from source to target resources.
    """

    if not limits_to_reallocate or not targets:
        error_validation("Limits to reallocate and targets cannot be empty.")

    source_limits = validate_source_resource(source_resource)

    for component, value in limits_to_reallocate.items():
        validate_source_component(component, value, source_limits)

    target_resource_uuids = [target["resource_uuid"] for target in targets]

    if source_resource.uuid in target_resource_uuids:
        error_validation("Source resource cannot be a target resource.")

    target_resources = models.Resource.objects.filter(uuid__in=target_resource_uuids)
    for target_resource in target_resources:
        if target_resource.offering != source_resource.offering:
            error_validation(
                "Target resource %(name)s must be from the same offering as the source resource.",
                name=target_resource.name,
            )
        if target_resource.state != ResourceStates.OK:
            error_validation(
                "Target resource %(name)s must be in OK state.",
                name=target_resource.name,
            )

        if check_pending_order_exists(target_resource):
            error_validation(
                "Target resource %(name)s has pending orders.",
                name=target_resource.name,
            )
    found_uuids = {resource.uuid for resource in target_resources}
    missing_uuids = set(target_resource_uuids) - found_uuids
    if missing_uuids:
        error_validation(
            "Target resources with UUIDs %(uuids)s do not exist.",
            uuids=", ".join(str(uuid) for uuid in missing_uuids),
        )

    total_allocated = defaultdict(int)

    for target_data in targets:
        target_uuid = target_data["resource_uuid"]
        target_resource = target_resources.get(uuid=target_uuid)
        if not target_resource:
            error_validation(
                "Target resource with UUID %(uuid)s not found.",
                uuid=target_uuid,
            )
        validate_target_allocation(
            target_data, target_resource, source_limits, user, total_allocated
        )

    # Validate total allocated matches what's being reallocated
    for component, total_to_reallocate in limits_to_reallocate.items():
        total_allocated_for_component = total_allocated.get(component, 0)
        if total_allocated_for_component > total_to_reallocate:
            error_validation(
                "Total allocated %(total)s for component %(component)s "
                "exceeds reallocated amount %(reallocated)s.",
                total=total_allocated_for_component,
                component=component,
                reallocated=total_to_reallocate,
            )
        if total_allocated_for_component < total_to_reallocate:
            error_validation(
                "Total allocated %(total)s for component %(component)s "
                "is less than reallocated amount %(reallocated)s. "
                "All allocated limits must sum to the reallocated amount.",
                total=total_allocated_for_component,
                component=component,
                reallocated=total_to_reallocate,
            )


def validate_source_resource(source_resource):
    if source_resource.state != ResourceStates.OK:
        error_validation("Source resource must be in OK state to reallocate limits.")

    if check_pending_order_exists(source_resource):
        error_validation(
            "Source resource has pending orders. Cannot reallocate limits."
        )

    if not source_resource.limits:
        error_validation("Source resource has no limits to reallocate.")

    source_limits = source_resource.limits
    validate_limits(source_limits, source_resource.offering, source_resource)
    return source_limits


def error_validation(message, **params):
    raise serializers.ValidationError(_(message) % params)


def validate_source_component(component, value, source_limits):
    if component not in source_limits:
        error_validation(
            "Component %(component)s is not present in source resource limits.",
            component=component,
        )

    if value <= 0:
        error_validation(
            "Reallocated limit for %(component)s must be positive.",
            component=component,
        )

    available = source_limits.get(component, 0)

    if value > available:
        error_validation(
            "Cannot reallocate %(value)s of %(component)s. "
            "Source resource only has %(available)s available.",
            value=value,
            component=component,
            available=available,
        )


def validate_target_allocation(
    target_data, target_resource, source_limits, user, total_allocated
):
    target_uuid = target_data["resource_uuid"]
    allocated_limits = target_data.get("allocated_limits", {})

    if not allocated_limits:
        error_validation(
            "Target resource %(uuid)s has no allocated limits specified.",
            uuid=target_uuid,
        )

    if not has_permission(
        user, PermissionEnum.UPDATE_RESOURCE_LIMITS, target_resource.project
    ) and not has_permission(
        user, PermissionEnum.UPDATE_RESOURCE_LIMITS, target_resource.project.customer
    ):
        error_validation(
            "User does not have permission to update target resource %(name)s limits.",
            name=target_resource.name,
        )

    target_limits = target_resource.limits or {}
    validate_limits(target_limits, target_resource.offering, target_resource)

    if set(target_limits.keys()) != set(source_limits.keys()):
        error_validation(
            "Target resource %(name)s must have the same components as the source resource.",
            name=target_resource.name,
        )

    for component, allocated_value in allocated_limits.items():
        if allocated_value <= 0:
            error_validation(
                "Allocated limit for %(component)s in target %(name)s must be positive.",
                component=component,
                name=target_resource.name,
            )

        new_target_limits = target_limits.copy()
        new_target_limits[component] = target_limits.get(component, 0) + allocated_value

        try:
            validate_limits(
                new_target_limits, target_resource.offering, target_resource
            )
        except serializers.ValidationError as e:
            error_validation(
                "Target resource %(name)s cannot accept allocated limits for %(component)s: %(error)s",
                name=target_resource.name,
                component=component,
                error=str(e),
            )

        total_allocated[component] += allocated_value


def calculate_new_limits(current_limits, allocated_limits, subtract=False):
    """
    Calculate new limits by adding or subtracting delta from current limits.
    """
    new_limits = current_limits.copy() if current_limits else {}

    for component, allocated_value in allocated_limits.items():
        current_value = new_limits.get(component, 0)
        if subtract:
            new_limits[component] = max(0, current_value - allocated_value)
        else:
            new_limits[component] = current_value + allocated_value

    return new_limits


VALID_UNIQUENESS_SCOPES = {
    "offering",
    "offering_group",
    "service_provider",
    "service_provider_category",
}


def validate_backend_id_rules(rules):
    """Validate the JSON structure of backend_id_rules. Raises rest_framework ValidationError."""
    if not isinstance(rules, dict):
        raise rf_exceptions.ValidationError(
            {"backend_id_rules": "Must be a JSON object."}
        )

    allowed_keys = {"format", "uniqueness"}
    unknown_keys = set(rules.keys()) - allowed_keys
    if unknown_keys:
        raise rf_exceptions.ValidationError(
            {"backend_id_rules": f"Unknown keys: {', '.join(sorted(unknown_keys))}"}
        )

    if "format" in rules:
        fmt = rules["format"]
        if not isinstance(fmt, dict):
            raise rf_exceptions.ValidationError(
                {"backend_id_rules": "format must be a JSON object."}
            )
        fmt_allowed = {"regex", "description"}
        fmt_unknown = set(fmt.keys()) - fmt_allowed
        if fmt_unknown:
            raise rf_exceptions.ValidationError(
                {
                    "backend_id_rules": f"Unknown keys in format: {', '.join(sorted(fmt_unknown))}"
                }
            )
        regex = fmt.get("regex")
        if regex is not None:
            if not isinstance(regex, str):
                raise rf_exceptions.ValidationError(
                    {"backend_id_rules": "format.regex must be a string."}
                )
            if is_potentially_dangerous_regex(regex):
                raise rf_exceptions.ValidationError(
                    {
                        "backend_id_rules": "format.regex is potentially dangerous (too long or contains nested/adjacent quantifiers)."
                    }
                )
            try:
                re.compile(regex)
            except re.error as e:
                raise rf_exceptions.ValidationError(
                    {
                        "backend_id_rules": f"format.regex is not a valid regular expression: {e}"
                    }
                )

    if "uniqueness" in rules:
        uniq = rules["uniqueness"]
        if not isinstance(uniq, dict):
            raise rf_exceptions.ValidationError(
                {"backend_id_rules": "uniqueness must be a JSON object."}
            )
        uniq_allowed = {"scope", "include_terminated"}
        uniq_unknown = set(uniq.keys()) - uniq_allowed
        if uniq_unknown:
            raise rf_exceptions.ValidationError(
                {
                    "backend_id_rules": f"Unknown keys in uniqueness: {', '.join(sorted(uniq_unknown))}"
                }
            )
        scope = uniq.get("scope")
        if scope is not None and scope not in VALID_UNIQUENESS_SCOPES:
            raise rf_exceptions.ValidationError(
                {
                    "backend_id_rules": f"uniqueness.scope must be one of: {', '.join(sorted(VALID_UNIQUENESS_SCOPES))}"
                }
            )
        include_terminated = uniq.get("include_terminated")
        if include_terminated is not None and not isinstance(include_terminated, bool):
            raise rf_exceptions.ValidationError(
                {"backend_id_rules": "uniqueness.include_terminated must be a boolean."}
            )


def validate_backend_id_format(backend_id, offering):
    """Check backend_id against the offering's format regex. Raises ValidationError if invalid."""
    rules = offering.backend_id_rules or {}
    fmt = rules.get("format", {})
    regex = fmt.get("regex")
    if not regex:
        return

    if is_potentially_dangerous_regex(regex):
        # Skip validation for dangerous patterns
        return

    try:
        if not re.fullmatch(regex, backend_id):
            description = fmt.get("description", f"Must match pattern: {regex}")
            raise rf_exceptions.ValidationError(
                {"backend_id": f"Invalid format. {description}"}
            )
    except re.error:
        # Skip validation for invalid patterns
        return


def validate_backend_id_uniqueness(backend_id, offering, exclude_resource=None):
    """Check backend_id uniqueness per the offering's scope config. Raises ValidationError if not unique."""
    rules = offering.backend_id_rules or {}
    uniq = rules.get("uniqueness", {})
    scope = uniq.get("scope")
    if not scope:
        return

    include_terminated = uniq.get("include_terminated", True)

    queryset = models.Resource.objects.filter(backend_id=backend_id)

    if not include_terminated:
        queryset = queryset.exclude(state=ResourceStates.TERMINATED)

    if scope == "offering":
        queryset = queryset.filter(offering=offering)
    elif scope == "offering_group":
        if offering.backend_id:
            queryset = queryset.filter(offering__backend_id=offering.backend_id)
        else:
            queryset = queryset.filter(offering=offering)
    elif scope == "service_provider":
        queryset = queryset.filter(offering__customer=offering.customer)
    elif scope == "service_provider_category":
        queryset = queryset.filter(
            offering__customer=offering.customer,
            offering__category=offering.category,
        )
    else:
        return

    if exclude_resource is not None:
        queryset = queryset.exclude(pk=exclude_resource.pk)

    if queryset.exists():
        raise rf_exceptions.ValidationError(
            {"backend_id": "This backend_id is already in use."}
        )


def validate_backend_id(backend_id, offering, exclude_resource=None):
    """Run all backend_id validations. Skips if backend_id is empty."""
    if not backend_id:
        return
    validate_backend_id_format(backend_id, offering)
    validate_backend_id_uniqueness(
        backend_id, offering, exclude_resource=exclude_resource
    )


# Mapping from User model field names to attribute "gate" names
# used by OfferingUserAttributeConfig (expose_<attribute_name>).
# Shared between handlers.py (pub/sub) and profile completeness filtering.
USER_FIELD_TO_ATTRIBUTE = {
    "first_name": "full_name",
    "last_name": "full_name",
    "email": "email",
    "phone_number": "phone_number",
    "organization": "organization",
    "job_title": "job_title",
    "affiliations": "affiliations",
    "gender": "gender",
    "civil_number": "civil_number",
    "birth_date": "birth_date",
    "personal_title": "personal_title",
    "place_of_birth": "place_of_birth",
    "address": "address",
    "country_of_residence": "country_of_residence",
    "nationality": "nationality",
    "nationalities": "nationalities",
    "organization_country": "organization_country",
    "organization_type": "organization_type",
    "eduperson_assurance": "eduperson_assurance",
    "identity_source": "identity_source",
    "registration_method": "registration_method",
}


def _is_user_field_empty(user, field_name):
    """Check if a User model field value is considered empty.

    - CharField/EmailField: "" or None
    - JSONField: [] or None
    - PositiveSmallIntegerField/DateField: None
    """
    value = getattr(user, field_name, None)
    if value is None:
        return True
    if isinstance(value, str) and value == "":
        return True
    if isinstance(value, list) and value == []:
        return True
    return False


def get_missing_profile_attributes(user, exposed_attributes):
    """Return list of attribute names the user hasn't filled in.

    Args:
        user: User model instance
        exposed_attributes: list of attribute names (e.g. ["email", "full_name"])

    Returns:
        List of attribute names that are empty on the user's profile.
    """
    attr_to_fields = _build_attribute_to_user_fields()
    missing = []
    for attr_name in exposed_attributes:
        user_fields = attr_to_fields.get(attr_name)
        if not user_fields:
            continue
        if len(user_fields) > 1:
            # full_name: incomplete only when ALL sub-fields are empty
            if all(_is_user_field_empty(user, uf) for uf in user_fields):
                missing.append(attr_name)
        else:
            if _is_user_field_empty(user, user_fields[0]):
                missing.append(attr_name)
    return missing


def _build_attribute_to_user_fields():
    """Invert USER_FIELD_TO_ATTRIBUTE: attribute_name -> [user_field_names]."""
    result = defaultdict(list)
    for user_field, attr_name in USER_FIELD_TO_ATTRIBUTE.items():
        result[attr_name].append(user_field)
    # username is not in USER_FIELD_TO_ATTRIBUTE (not change-tracked)
    if "username" not in result:
        result["username"] = ["username"]
    return dict(result)


def _is_field_empty_q(user_field_name):
    """Return a Q object that matches when the given User field is empty.

    The "empty" definition depends on the field type:
    - CharField/EmailField: "" or NULL
    - JSONField: [] or NULL
    - PositiveSmallIntegerField/DateField: NULL
    """
    from waldur_core.core.models import User

    field = User._meta.get_field(user_field_name)
    prefix = f"user__{user_field_name}"

    if isinstance(field, models_module.CharField | models_module.EmailField):
        return Q(**{prefix: ""}) | Q(**{f"{prefix}__isnull": True})
    elif isinstance(field, models_module.JSONField):
        return Q(**{prefix: []}) | Q(**{f"{prefix}__isnull": True})
    else:
        # DateField, PositiveSmallIntegerField, etc.
        return Q(**{f"{prefix}__isnull": True})


def is_resource_project_only_viewer(user, resource) -> bool:
    """True when ``user`` can only see ``resource`` via a ResourceProject role.

    Returns False for staff/support, for users with project/customer roles on
    the consuming side, and for users with a direct UserRole on the resource.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_staff or user.is_support:
        return False
    project = resource.project
    if get_connected_projects(user).filter(id=project.id).exists():
        return False
    if get_connected_customers(user).filter(id=project.customer_id).exists():
        return False
    if has_user(resource, user):
        return False
    rp_ids = models.ResourceProject.available_objects.filter(
        resource=resource
    ).values_list("id", flat=True)
    if not rp_ids:
        return False
    rp_ct = ContentType.objects.get_for_model(models.ResourceProject)
    return UserRole.objects.filter(
        user=user, is_active=True, content_type=rp_ct, object_id__in=list(rp_ids)
    ).exists()


def build_incomplete_profile_q():
    """Build a Q object matching OfferingUsers with incomplete profiles.

    An OfferingUser is "incomplete" when:
    1. The offering has an OfferingUserAttributeConfig
    2. At least one exposed attribute's corresponding User field(s) are empty

    For `full_name`, both first_name AND last_name must be empty for it
    to be considered incomplete (since full_name = first_name + last_name).
    """
    attr_to_fields = _build_attribute_to_user_fields()
    incomplete_q = Q()

    for attr_name, user_fields in attr_to_fields.items():
        config_flag = f"offering__user_attribute_config__expose_{attr_name}"

        if len(user_fields) > 1:
            # full_name case: both first_name AND last_name must be empty
            all_empty = Q()
            for uf in user_fields:
                all_empty &= _is_field_empty_q(uf)
            incomplete_q |= Q(**{config_flag: True}) & all_empty
        else:
            incomplete_q |= Q(**{config_flag: True}) & _is_field_empty_q(user_fields[0])

    return incomplete_q


def filter_users_with_active_offering_consent(users, offering):
    return users.filter(
        offering_consents__offering=offering,
        offering_consents__revocation_date__isnull=True,
    ).distinct()


def should_filter_provider_resource_team_by_consent(user, offering) -> bool:
    if user.is_staff or user.is_support:
        return False
    if not config.ENFORCE_USER_CONSENT_FOR_OFFERINGS:
        return False
    return offering.has_terms_of_service()


def build_resource_team_response(resource, request, users):
    from waldur_mastermind.marketplace import (
        models,
        serializers,
    )  # to avoid circular import

    project = resource.project
    offering = resource.offering

    permissions_qs = get_permissions(project).select_related("role")
    permissions_map = {}
    for perm in permissions_qs:
        if perm.user_id not in permissions_map:
            permissions_map[perm.user_id] = perm

    offering_users_qs = models.OfferingUser.objects.filter(
        offering=offering, user__in=users
    )
    offering_users_map = {ou.user_id: ou for ou in offering_users_qs}

    return Response(
        serializers.ProjectUserSerializer(
            instance=users,
            many=True,
            context={
                "project": project,
                "offering": offering,
                "request": request,
                "permissions_map": permissions_map,
                "offering_users_map": offering_users_map,
            },
        ).data,
        status=status.HTTP_200_OK,
    )
