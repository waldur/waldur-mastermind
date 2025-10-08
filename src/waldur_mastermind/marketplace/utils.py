import datetime
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
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from io import BytesIO
from typing import cast

import httpx
from constance import config
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage as storage
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

from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.core.models import User
from waldur_core.logging import models as logging_models
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging import utils as logging_utils
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import (
    get_permissions,
    get_users_with_permission,
    has_permission,
)
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure.managers import (
    get_connected_projects,
    get_customer_users,
    get_organization_groups,
    get_project_users,
)
from waldur_freeipa import models as freeipa_models
from waldur_mastermind.common.utils import create_request, mb_to_gb
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import registrators
from waldur_mastermind.invoices.structures import InvoiceResourceLimitPeriodDict
from waldur_mastermind.invoices.utils import get_full_days
from waldur_mastermind.marketplace import attribute_types
from waldur_mastermind.marketplace.enums import REMOTE_OFFERING as REMOTE_PLUGIN_NAME
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING as SITE_AGENT_PLUGIN_NAME,
)
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    LimitPeriods,
    OfferingUserStates,
    OrderStates,
    ResourceStates,
    RobotAccountStates,
)

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


def validate_limits(limits, offering, resource=None):
    """
    @param limits Maximum/Minimum limit-based components values and maximum available limit
    @param offering The offering being created
    @param resource Passing the resource if the limits of the resource are being updated.
    """
    if not plugins.manager.can_update_limits(offering.type):
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
        invoice__state=invoice_models.Invoice.States.PENDING,
        project=old_project,
    ):
        start_invoice = invoice_item.invoice

        target_invoice, _ = registrators.RegistrationManager.get_or_create_invoice(
            project.customer,
            date=datetime.date(
                year=start_invoice.year, month=start_invoice.month, day=1
            ),
        )

        if target_invoice.state != invoice_models.Invoice.States.PENDING:
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

    return create_request(view, user, {}, uuid=resource.uuid.hex)


def schedule_resources_termination(resources, termination_comment=None, user=None):
    if not resources:
        return

    for resource in resources:
        user = (
            user
            or resource.end_date_requested_by
            or resource.project.end_date_requested_by
            or core_utils.get_system_robot()
        )

        if not user:
            logger.error(
                "User for terminating resources of project with due date does not exist."
            )
            return

        response = terminate_resource(
            resource, user, termination_comment, scheduled=True
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


def import_current_usages(resource):
    date = datetime.date.today()

    for component_type, component_usage in resource.current_usages.items():
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

        try:
            component_usage_object = models.ComponentUsage.objects.get(
                resource=resource,
                component=offering_component,
                billing_period=core_utils.month_start(date),
                plan_period=plan_period,
            )
            component_usage_object.usage = max(
                component_usage, component_usage_object.usage
            )
            component_usage_object.save()
        except models.ComponentUsage.DoesNotExist:
            models.ComponentUsage.objects.create(
                resource=resource,
                component=offering_component,
                usage=component_usage,
                date=date,
                billing_period=core_utils.month_start(date),
                plan_period=plan_period,
            )


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
        .filter(backend_metadata__has_key="uidnumber")
        .order_by("backend_metadata__uidnumber")
        .values_list("backend_metadata__uidnumber", flat=True)
        .last()
    ) or initial_uidnumber

    robot_account_last_uidnumber = (
        models.RobotAccount.objects.exclude(backend_metadata=None)
        .filter(backend_metadata__has_key="uidnumber")
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


def generate_glauth_records_for_offering_users(offering, offering_users):
    user_records = []

    for offering_user in offering_users:
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

        ssh_keys = [
            f'"{ssh_key.public_key}"' for ssh_key in user.sshpublickey_set.all()
        ]
        ssh_keys_line = ",\n    ".join(ssh_keys)

        password_sha256 = generate_offering_password_hash(offering)

        user_projects = get_connected_projects(user)

        group_ids = models.OfferingUserGroup.objects.filter(
            projects__in=user_projects
        ).values_list("backend_metadata__gid", flat=True)
        group_ids = [str(gid) for gid in group_ids]

        other_groups = ", ".join(group_ids)

        user_disabled_status = "false"
        # Check if user has access to non-terminated resources in offering
        has_access = is_user_related_to_offering(offering, user)
        if not has_access:
            user_disabled_status = "true"

        record = textwrap.dedent(
            f"""
        [[users]]
          name = "{user.get_username()}"
          givenname="{user.first_name}"
          sn="{user.last_name}"
          mail = "{user.email}"
          uidnumber = {uidnumber}
          primarygroup = {primarygroup}
          otherGroups = [{other_groups}]
          sshkeys = [{ssh_keys_line}]
          loginShell = "{login_shell}"
          homeDir = "{home_dir}"
          passsha256 = "{password_sha256}"
          disabled = {user_disabled_status}
            [[users.customattributes]]
            preferredUsername = ["{username}"]
        """
        )

        record += textwrap.dedent(
            f"""
        [[groups]]
          name = "{username}"
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
        ssh_keys = robot_account.keys
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
          name = "{username}"
          uidnumber = {uidnumber}
          primarygroup = {primarygroup}
          sshkeys = ["{ssh_keys_line}"]
          loginShell = "{login_shell}"
          homeDir = "{home_dir}"
          passsha256 = "{password_sha256}"
            [[users.customattributes]]
            preferredUsername = ["{username}"]
        """
        )

        record += textwrap.dedent(
            f"""
        [[groups]]
          name = "{username}"
          gidnumber = {primarygroup}
        """
        )

        robot_account_records.append(record)

    return robot_account_records


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
        if not models.OfferingUser.objects.filter(
            user=user, offering=offering
        ).exists():
            username = generate_username(user, offering)
            # Set state to OK when username is known at creation time
            state = (
                OfferingUserStates.OK
                if username
                else OfferingUserStates.CREATION_REQUESTED
            )
            models.OfferingUser.objects.create(
                user=user, offering=offering, username=username, state=state
            )
            logger.info("Offering user %s has been created.")


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


def generate_resource_name(
    project: structure_models.Project, offering: models.Offering
):
    resource_count = models.Resource.objects.filter(
        project=project, offering=offering
    ).count()
    parts = [
        project.customer.slug,
        project.slug,
        offering.slug,
    ]
    result = "-".join(parts)

    if resource_count:
        result += "-" + str(resource_count + 1)

    return core_utils.remove_duplicate_hyphens(result)


def notification_about_project_ending(end_date):
    projects_by_recipient = defaultdict(list)
    expired_projects = structure_models.Project.available_objects.exclude(
        end_date__isnull=True
    ).filter(end_date=end_date)

    # If there are no expired projects, we don't need to send notifications
    if not expired_projects.exists():
        logger.info("No projects found with end_date=%s", end_date)
        return

    for project in expired_projects:
        logger.info(
            "Project %s (uuid=%s) has end_date=%s",
            project.name,
            project.uuid,
            project.end_date,
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
    url: str, service_account: dict, username: str = "", scope_type: str = ""
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
            "ownerUsername": username,
            "preferredIdentifier": service_account["preferred_identifier"],
            "email": customer.email,
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


def create_service_account(service_account: dict, username: str, scope_type: str):
    """
    Makes a synchronous call to the webhook URL to create a service account.
    Raises exceptions on failure which should be handled by the viewset.
    """
    if config.ENABLE_MOCK_SERVICE_ACCOUNT_BACKEND:
        logger.info("Mock mode enabled for create_service_account")
        return generate_mock_service_account_creation_response(
            service_account, username, scope_type
        )

    if not settings.WALDUR_CORE.get("SERVICE_ACCOUNT_USE_API"):
        return

    service_account_url = settings.WALDUR_CORE["SERVICE_ACCOUNT_URL"]
    if not service_account_url:
        raise ValueError("URL for service accounts is not configured")

    service_account_url = service_account_url.rstrip("/")

    try:
        response = post_service_account_to_url(
            service_account_url, service_account, username, scope_type
        )
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        error_details = (
            exc.response.json()
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.text
            else exc
        )
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
        error_details = (
            exc.response.json()
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.text
            else exc
        )
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
        error_details = (
            exc.response.json()
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.text
            else exc
        )
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


def prepare_messages(
    offering: models.Offering,
    message_payload: dict,
    affected_object: logging_utils.ObservableObjectType,
) -> list[dict[str, str]]:
    """Helper function to prepare event messages for marketplace events.

    Generates event messages for users who have subscribed to events related to marketplace
    offerings they have access to. Each message includes a vhost, topic and payload.

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

    messages_to_send = []
    for event_subscription in event_subscriptions:
        user = event_subscription.user
        logger.info("Processing subscription for user %s", user)

        # Check if user has access to offering
        linked_offerings = models.Offering.objects.all().filter_for_user(user)
        if not linked_offerings.filter(id=offering.id).exists():
            logger.debug(
                "The user %s does not have access to the offering %s", user, offering
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
        logging_utils.ObservableObjectType.IMPORTABLE_RESOURCES,
    )
    if messages:
        logging_tasks.publish_messages.delay(messages)


def convert_slurm_usage(usage: int | float | Decimal, component_type: str) -> int:
    # This is temporarily uplifted to marketplace in order to avoid circular dependency
    minutes_in_hour = 60
    usage_float = float(usage)
    if component_type in ["ram", "mem"]:
        mb_in_gb = 1024
        quantity = int(math.ceil(usage_float / mb_in_gb / minutes_in_hour))
    else:
        quantity = int(math.ceil(usage_float / minutes_in_hour))
    return quantity


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
        error_details = (
            exc.response.json()
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.text
            else exc
        )
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
                user.save(update_fields=["is_active"])
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
                user.save(update_fields=["is_active"])
    except (httpx.HTTPError, ValueError) as exc:
        error_details = (
            exc.response.json()
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.text
            else exc
        )
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
