import datetime
import hashlib
import logging
import math
import os
import re
import textwrap
import traceback
import unicodedata
from enum import Enum
from io import BytesIO

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage as storage
from django.db import transaction
from django.db.models import F, Q, Sum
from django.db.models.fields import FloatField
from django.db.models.functions.math import Ceil
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from PIL import Image
from rest_framework import exceptions as rf_exceptions
from rest_framework import serializers, status

from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core import utils as core_utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import get_users_with_permission, has_permission
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
from waldur_mastermind.invoices.utils import get_full_days
from waldur_mastermind.marketplace import attribute_types
from waldur_mastermind.marketplace_remote import PLUGIN_NAME as REMOTE_PLUGIN_NAME
from waldur_mastermind.marketplace_slurm_remote import (
    PLUGIN_NAME as SLURM_REMOTE_PLUGIN_NAME,
)

from . import PLUGIN_NAME as BASIC_PLUGIN_NAME
from . import models, plugins

User = get_user_model()
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


def get_order_processor(order):
    offering = order.resource.offering

    if order.type == models.RequestTypeMixin.Types.CREATE:
        return plugins.manager.get_processor(offering.type, "create_resource_processor")

    elif order.type == models.RequestTypeMixin.Types.UPDATE:
        return plugins.manager.get_processor(offering.type, "update_resource_processor")

    elif order.type == models.RequestTypeMixin.Types.TERMINATE:
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
            and order.type == models.RequestTypeMixin.Types.TERMINATE
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
    pic = screenshot.image
    fh = storage.open(pic.name, "rb")
    image = Image.open(fh)
    image.thumbnail(settings.WALDUR_MARKETPLACE["THUMBNAIL_SIZE"], Image.ANTIALIAS)
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


def import_resource_metadata(resource):
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
            "service_provider_uuid": ""
            if not service_provider
            else service_provider.uuid.hex,
        }
    except models.Resource.DoesNotExist:
        return {}


def get_offering_details(offering):
    if not isinstance(offering, models.Offering):
        return {}

    return {
        "offering_type": offering.type,
        "offering_name": offering.name,
        "offering_uuid": offering.uuid.hex,
        "service_provider_name": offering.customer.name,
        "service_provider_uuid": offering.customer.uuid.hex,
    }


def format_list(resources):
    """
    Format comma-separated list of IDs from Django queryset.
    """
    return ", ".join(map(str, sorted(resources.values_list("id", flat=True))))


def get_order_url(order):
    return core_utils.format_homeport_link(
        "projects/{project_uuid}/marketplace-order-details/{order_uuid}/",
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
        billing_type=models.OfferingComponent.BillingTypes.USAGE,
        offering__type__in=whitelist_types,
    ).values_list("offering_id", flat=True)
    resource_with_usages = models.ComponentUsage.objects.filter(
        billing_period=billing_period
    ).values_list("resource", flat=True)
    resources_without_usages = models.Resource.objects.filter(
        state=models.Resource.States.OK, offering_id__in=offering_ids
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


def get_public_resources_url(customer):
    return core_utils.format_homeport_link(
        "organizations/{organization_uuid}/marketplace-public-resources/",
        organization_uuid=customer.uuid,
    )


def validate_limit_amount(value, component):
    if not component.limit_amount:
        return

    if component.limit_period == models.OfferingComponent.LimitPeriods.MONTH:
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

    elif component.limit_period == models.OfferingComponent.LimitPeriods.ANNUAL:
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

    elif component.limit_period == models.OfferingComponent.LimitPeriods.TOTAL:
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


def get_components_map(limits, offering):
    valid_component_types = set(
        offering.components.filter(
            billing_type=models.OfferingComponent.BillingTypes.LIMIT
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
    SrcStates = core_models.StateMixin.States
    DstStates = models.Resource.States
    mapping = {
        SrcStates.CREATION_SCHEDULED: DstStates.CREATING,
        SrcStates.CREATING: DstStates.CREATING,
        SrcStates.UPDATE_SCHEDULED: DstStates.UPDATING,
        SrcStates.UPDATING: DstStates.UPDATING,
        SrcStates.DELETION_SCHEDULED: DstStates.TERMINATING,
        SrcStates.DELETING: DstStates.TERMINATING,
        SrcStates.OK: DstStates.OK,
        SrcStates.ERRED: DstStates.ERRED,
    }
    return mapping.get(state, DstStates.ERRED)


def get_marketplace_offering_uuid(serializer, scope):
    try:
        return models.Resource.objects.get(scope=scope).offering.uuid
    except ObjectDoesNotExist:
        return


def get_marketplace_offering_plugin_options(serializer, scope):
    try:
        return models.Resource.objects.get(scope=scope).offering.plugin_options
    except ObjectDoesNotExist:
        return


def get_marketplace_offering_name(serializer, scope):
    try:
        return models.Resource.objects.get(scope=scope).offering.name
    except ObjectDoesNotExist:
        return


def get_marketplace_category_uuid(serializer, scope):
    try:
        return models.Resource.objects.get(scope=scope).offering.category.uuid
    except ObjectDoesNotExist:
        return


def get_marketplace_category_name(serializer, scope):
    try:
        return models.Resource.objects.get(scope=scope).offering.category.title
    except ObjectDoesNotExist:
        return


def get_marketplace_resource_uuid(serializer, scope):
    try:
        return models.Resource.objects.get(scope=scope).uuid
    except ObjectDoesNotExist:
        return


def get_marketplace_plan_uuid(serializer, scope):
    try:
        resource = models.Resource.objects.get(scope=scope)
        if resource.plan:
            return resource.plan.uuid
    except ObjectDoesNotExist:
        return


def get_marketplace_resource_state(serializer, scope):
    try:
        return models.Resource.objects.get(scope=scope).get_state_display()
    except ObjectDoesNotExist:
        return


def get_is_usage_based(serializer, scope):
    try:
        return models.Resource.objects.get(scope=scope).offering.is_usage_based
    except ObjectDoesNotExist:
        return


def get_is_limit_based(serializer, scope):
    try:
        return models.Resource.objects.get(scope=scope).offering.is_limit_based
    except ObjectDoesNotExist:
        return


def add_marketplace_offering(sender, fields, **kwargs):
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
        .exclude(state=models.Resource.States.TERMINATED)
        .values_list("project", flat=True)
        .distinct()
        .order_by()
    )
    related_projects = structure_models.Project.objects.filter(
        id__in=related_project_ids
    )
    return related_projects


def is_user_related_to_offering(offering, user):
    if offering.type == BASIC_PLUGIN_NAME:
        connected_projects = get_connected_projects(user)
        return (
            models.Resource.objects.filter(
                offering=offering, project__in=connected_projects
            )
            .exclude(state=models.Resource.States.TERMINATED)
            .exists()
        )
    return False


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


def get_invoice_item_for_component_usage(component_usage):
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


def serialize_resource_limit_period(period):
    billing_periods = get_full_days(period["start"], period["end"])
    return {
        "start": period["start"].isoformat(),
        "end": period["end"].isoformat(),
        "quantity": period["quantity"],
        "billing_periods": billing_periods,
        "total": str(period["quantity"] * billing_periods),
    }


def terminate_resource(resource, user, termination_comment=None, scheduled=False):
    from waldur_mastermind.marketplace import views

    view = views.ResourceViewSet.as_view({"post": "terminate"})

    # Terminate pending orders if they exist
    for order in models.Order.objects.filter(
        resource=resource,
        state__in=[models.Order.States.PENDING_CONSUMER]
        if scheduled
        else [
            models.Order.States.PENDING_CONSUMER,
            models.Order.States.PENDING_PROVIDER,
        ],
    ):
        order.cancel(termination_comment)
        order.save()

    if models.Order.objects.filter(
        resource=resource, state=models.Order.States.EXECUTING
    ):
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
    ).exclude(state=models.Resource.States.TERMINATED)


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
        customer_projects = customer.projects.all().values_list("id", flat=True)
        project_ids = set(project_ids) & set(customer_projects)
    content_type = ContentType.objects.get_for_model(structure_models.Project)
    qs = UserRole.objects.filter(
        content_type=content_type, object_id__in=project_ids, is_active=True
    )
    if not user.is_staff and not user.is_support:
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
            type=models.Order.Types.CREATE,
            state=models.Order.States.DONE,
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
            .exclude(state=models.Resource.States.TERMINATED)
            .exists()
        ):
            new_customers.append(customer_id)

    for customer_id in (
        models.Order.objects.filter(
            offering__customer=service_provider.customer,
            type=models.Order.Types.TERMINATE,
            state=models.Order.States.DONE,
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
            .exclude(state=models.Resource.States.TERMINATED)
            .exists()
        ):
            lost_customers.append(customer_id)

    return len(new_customers) - len(lost_customers)


def count_resources_number_change(service_provider):
    to_day = timezone.datetime.today().date()

    created = (
        models.Order.objects.filter(
            offering__customer=service_provider.customer,
            type=models.Order.Types.CREATE,
            state=models.Order.States.DONE,
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
            type=models.Order.Types.TERMINATE,
            state=models.Order.States.DONE,
            created__gte=core_utils.month_start(to_day),
        )
        .order_by()
        .values_list("resource", flat=True)
        .distinct()
        .count()
    )

    return created - terminated


def generate_offering_password_hash(offering):
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

    return ""


def user_offerings_mapping(offerings):
    resources = models.Resource.objects.filter(
        state=models.Resource.States.OK, offering__in=offerings
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
                user_offerings_set.add((user, offering))

    for user, offering in user_offerings_set:
        if not models.OfferingUser.objects.filter(
            user=user, offering=offering
        ).exists():
            username = generate_username(user, offering)
            offering_user = models.OfferingUser.objects.create(
                user=user, offering=offering, username=username
            )
            offering_user.set_propagation_date()
            offering_user.save()
            logger.info("Offering user %s has been created.")


def order_should_not_be_reviewed_by_provider(order: models.Order):
    offering = order.offering
    user = order.consumer_reviewed_by or order.created_by

    if offering.type == SLURM_REMOTE_PLUGIN_NAME:
        return False

    if offering.type == BASIC_PLUGIN_NAME:
        return False

    if offering.type == REMOTE_PLUGIN_NAME:
        # If an offering has auto_approve_remote_orders flag set to True, an order can be processed without approval
        auto_approve_remote_orders = offering.plugin_options.get(
            "auto_approve_remote_orders", False
        )
        # A service provider owner or a service manager is not required to approve an order manually
        user_is_service_provider_owner = structure_permissions._has_owner_access(
            user, offering.customer
        )
        user_is_service_provider_offering_manger = (
            structure_permissions._has_service_manager_access(user, offering.customer)
            and offering.has_user(user)
        )
        # If any condition is not met, the order is requested for manual approval
        return (
            auto_approve_remote_orders
            or user_is_service_provider_owner
            or user_is_service_provider_offering_manger
        )

    return True


def get_consumer_approvers(order):
    users = User.objects.none()

    if settings.WALDUR_MARKETPLACE["NOTIFY_STAFF_ABOUT_APPROVALS"]:
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

    if settings.WALDUR_MARKETPLACE["NOTIFY_STAFF_ABOUT_APPROVALS"]:
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
    if "waldur-slurm-agent" not in user_agent:
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
    integration_status.save()


def validate_end_date(
    resource,
    user,
    end_date=None,
):
    is_resource_termination_date_required = resource.offering.plugin_options.get(
        "is_resource_termination_date_required"
    )
    max_resource_termination_offset_in_days = resource.offering.plugin_options.get(
        "max_resource_termination_offset_in_days"
    )
    default_resource_termination_offset_in_days = resource.offering.plugin_options.get(
        "default_resource_termination_offset_in_days"
    )
    latest_date_for_resource_termination = resource.offering.plugin_options.get(
        "latest_date_for_resource_termination"
    )

    if latest_date_for_resource_termination:
        latest_date_for_resource_termination = datetime.datetime.strptime(
            latest_date_for_resource_termination, "%Y-%m-%d"
        ).date()

    if is_resource_termination_date_required:
        if not end_date:
            resource_termination_date = resource.created + datetime.timedelta(
                days=int(default_resource_termination_offset_in_days)
            )
            resource_termination_date = resource_termination_date.date()
            if latest_date_for_resource_termination:
                resource_termination_date = min(
                    resource_termination_date, latest_date_for_resource_termination
                )
            resource.end_date = resource_termination_date
        elif max_resource_termination_offset_in_days:
            calculated_max_end_date = resource.created + datetime.timedelta(
                days=int(max_resource_termination_offset_in_days)
            )
            resource_termination_date = datetime.datetime.strptime(
                str(end_date), "%Y-%m-%d"
            ).date()
            calculated_max_end_date_date = calculated_max_end_date.date()
            if resource_termination_date > calculated_max_end_date_date:
                raise serializers.ValidationError(
                    {
                        "end_date": _(
                            "End date can not be later than the maximal date set for termination."
                        )
                    }
                )
            resource.end_date = resource_termination_date
        else:
            resource_termination_date = datetime.datetime.strptime(
                str(end_date), "%Y-%m-%d"
            ).date()
            if latest_date_for_resource_termination:
                if resource_termination_date > latest_date_for_resource_termination:
                    raise serializers.ValidationError(
                        {
                            "end_date": _(
                                "End date can not be later than the maximal date set for termination."
                            )
                        }
                    )
            resource.end_date = resource_termination_date

    if end_date:
        resource.end_date_requested_by = user
