import datetime
import logging
import math
import os
import traceback
from io import BytesIO

from django.conf import settings
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
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_mastermind.common.utils import create_request, mb_to_gb
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import registrators
from waldur_mastermind.invoices.utils import get_full_days
from waldur_mastermind.marketplace import attribute_types

from . import models, plugins

logger = logging.getLogger(__name__)


def get_order_item_processor(order_item):
    if order_item.resource:
        offering = order_item.resource.offering
    else:
        offering = order_item.offering

    if order_item.type == models.RequestTypeMixin.Types.CREATE:
        return plugins.manager.get_processor(offering.type, 'create_resource_processor')

    elif order_item.type == models.RequestTypeMixin.Types.UPDATE:
        return plugins.manager.get_processor(offering.type, 'update_resource_processor')

    elif order_item.type == models.RequestTypeMixin.Types.TERMINATE:
        return plugins.manager.get_processor(offering.type, 'delete_resource_processor')


def process_order_item(order_item: models.OrderItem, user):
    processor = get_order_item_processor(order_item)
    if not processor:
        order_item.error_message = (
            'Skipping order item processing because processor is not found.'
        )
        order_item.set_state_erred()
        order_item.save(update_fields=['state', 'error_message'])
        return

    try:
        processor(order_item).process_order_item(user)
    except Exception as e:
        # Here it is necessary to catch all exceptions.
        # If this is not done, then the order will remain in the executed status.
        order_item.error_message = str(e)
        order_item.error_traceback = traceback.format_exc()
        order_item.set_state_erred()
        logger.error(
            f'Error processing order item { order_item }. '
            f'Order item ID: { order_item.id }. '
            f'Exception: { order_item.error_message }.'
        )
        order_item.save(update_fields=['state', 'error_message', 'error_traceback'])


def validate_order_item(order_item, request):
    processor = get_order_item_processor(order_item)
    if processor:
        try:
            processor(order_item).validate_order_item(request)
        except NotImplementedError:
            # It is okay if validation is not implemented yet
            pass


def create_screenshot_thumbnail(screenshot):
    pic = screenshot.image
    fh = storage.open(pic.name, 'rb')
    image = Image.open(fh)
    image.thumbnail(settings.WALDUR_MARKETPLACE['THUMBNAIL_SIZE'], Image.ANTIALIAS)
    fh.close()

    thumb_extension = os.path.splitext(pic.name)[1]
    thumb_extension = thumb_extension.lower()
    thumb_name = os.path.basename(pic.name)

    if thumb_extension in ['.jpg', '.jpeg']:
        FTYPE = 'JPEG'
    elif thumb_extension == '.gif':
        FTYPE = 'GIF'
    elif thumb_extension == '.png':
        FTYPE = 'PNG'
    else:
        return

    temp_thumb = BytesIO()
    image.save(temp_thumb, FTYPE)
    temp_thumb.seek(0)
    screenshot.thumbnail.save(thumb_name, ContentFile(temp_thumb.read()), save=True)
    temp_thumb.close()


def import_resource_metadata(resource):
    instance = resource.scope
    fields = {'action', 'action_details', 'state', 'runtime_state'}

    for field in fields:
        if field == 'state':
            value = instance.get_state_display()
        else:
            value = getattr(instance, field, None)
        if field in fields:
            resource.backend_metadata[field] = value

    if instance.backend_id:
        resource.backend_id = instance.backend_id
    resource.name = instance.name
    resource.save(
        update_fields=['backend_metadata', 'attributes', 'name', 'backend_id']
    )


def get_service_provider_info(source):
    try:
        resource = models.Resource.objects.get(scope=source)
        customer = resource.offering.customer
        service_provider = getattr(customer, 'serviceprovider', None)

        return {
            'service_provider_name': customer.name,
            'service_provider_uuid': ''
            if not service_provider
            else service_provider.uuid.hex,
        }
    except models.Resource.DoesNotExist:
        return {}


def get_offering_details(offering):
    if not isinstance(offering, models.Offering):
        return {}

    return {
        'offering_type': offering.type,
        'offering_name': offering.name,
        'offering_uuid': offering.uuid.hex,
        'service_provider_name': offering.customer.name,
        'service_provider_uuid': offering.customer.uuid.hex,
    }


def format_list(resources):
    """
    Format comma-separated list of IDs from Django queryset.
    """
    return ', '.join(map(str, sorted(resources.values_list('id', flat=True))))


def get_order_item_url(order_item):
    return core_utils.format_homeport_link(
        'projects/{project_uuid}/marketplace-order-item-details/{order_item_uuid}/',
        order_item_uuid=order_item.uuid.hex,
        project_uuid=order_item.order.project.uuid,
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
    ).values_list('offering_id', flat=True)
    resource_with_usages = models.ComponentUsage.objects.filter(
        billing_period=billing_period
    ).values_list('resource', flat=True)
    resources_without_usages = models.Resource.objects.filter(
        state=models.Resource.States.OK, offering_id__in=offering_ids
    ).exclude(id__in=resource_with_usages)
    result = []

    for resource in resources_without_usages:
        rows = list(
            filter(lambda x: x['customer'] == resource.offering.customer, result)
        )
        if rows:
            rows[0]['resources'].append(resource)
        else:
            result.append(
                {
                    'customer': resource.offering.customer,
                    'resources': [resource],
                }
            )

    return result


def get_public_resources_url(customer):
    return core_utils.format_homeport_link(
        'organizations/{organization_uuid}/marketplace-public-resources/',
        organization_uuid=customer.uuid,
    )


def validate_limit_amount(value, component):
    if not component.limit_amount:
        return

    if component.limit_period == models.OfferingComponent.LimitPeriods.MONTH:
        current = (
            models.ComponentQuota.objects.filter(
                component=component,
                modified__year=timezone.now().year,
                modified__month=timezone.now().month,
            )
            .exclude(limit=-1)
            .aggregate(sum=Sum('limit'))['sum']
        ) or 0
        if current + value > component.limit_amount:
            raise serializers.ValidationError(
                _('Monthly limit exceeds threshold %s.') % component.limit_amount
            )

    elif component.limit_period == models.OfferingComponent.LimitPeriods.ANNUAL:
        current = (
            models.ComponentQuota.objects.filter(
                component=component,
                modified__year=timezone.now().year,
            )
            .exclude(limit=-1)
            .aggregate(sum=Sum('limit'))['sum']
        ) or 0
        if current + value > component.limit_amount:
            raise serializers.ValidationError(
                _('Annual limit exceeds threshold %s.') % component.limit_amount
            )

    elif component.limit_period == models.OfferingComponent.LimitPeriods.TOTAL:
        current = (
            models.ComponentQuota.objects.filter(
                component=component,
            )
            .exclude(limit=-1)
            .aggregate(sum=Sum('limit'))['sum']
        ) or 0
        if current + value > component.limit_amount:
            raise serializers.ValidationError(
                _('Total limit exceeds threshold %s.') % component.limit_amount
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
        resource['limits'].get(component.type, 0)
        for resource in all_offering_resources.values('limits')
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
            _('The limit %s value cannot be more than %s.')
            % (value, component.max_value)
        )
    if component.min_value and value < component.min_value:
        raise serializers.ValidationError(
            _('The limit %s value cannot be less than %s.')
            % (value, component.min_value)
        )


def get_components_map(limits, offering):
    valid_component_types = set(
        offering.components.filter(
            billing_type=models.OfferingComponent.BillingTypes.LIMIT
        ).values_list('type', flat=True)
    )

    invalid_types = set(limits.keys()) - valid_component_types
    if invalid_types:
        raise serializers.ValidationError(
            {'limits': _('Invalid types: %s') % ', '.join(invalid_types)}
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
            {'limits': _('Limits update is not supported for this resource.')}
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
        'key', flat=True
    )

    missing_attributes = set(required_attributes) - set(attributes.keys())
    if missing_attributes:
        raise rf_exceptions.ValidationError(
            {
                'attributes': _(
                    'These attributes are required: %s'
                    % ', '.join(sorted(missing_attributes))
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
                value, list(attribute.options.values_list('key', flat=True))
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
    fields['marketplace_offering_uuid'] = serializers.SerializerMethodField()
    setattr(sender, 'get_marketplace_offering_uuid', get_marketplace_offering_uuid)

    fields['marketplace_offering_name'] = serializers.SerializerMethodField()
    setattr(sender, 'get_marketplace_offering_name', get_marketplace_offering_name)

    fields['marketplace_category_uuid'] = serializers.SerializerMethodField()
    setattr(sender, 'get_marketplace_category_uuid', get_marketplace_category_uuid)

    fields['marketplace_category_name'] = serializers.SerializerMethodField()
    setattr(sender, 'get_marketplace_category_name', get_marketplace_category_name)

    fields['marketplace_resource_uuid'] = serializers.SerializerMethodField()
    setattr(sender, 'get_marketplace_resource_uuid', get_marketplace_resource_uuid)

    fields['marketplace_plan_uuid'] = serializers.SerializerMethodField()
    setattr(sender, 'get_marketplace_plan_uuid', get_marketplace_plan_uuid)

    fields['marketplace_resource_state'] = serializers.SerializerMethodField()
    setattr(sender, 'get_marketplace_resource_state', get_marketplace_resource_state)

    fields['is_usage_based'] = serializers.SerializerMethodField()
    setattr(sender, 'get_is_usage_based', get_is_usage_based)

    fields['is_limit_based'] = serializers.SerializerMethodField()
    setattr(sender, 'get_is_limit_based', get_is_limit_based)


def get_offering_costs(invoice_items):
    price = Ceil(F('quantity') * F('unit_price') * 100) / 100
    tax_rate = F('invoice__tax_percent') / 100
    return invoice_items.values('invoice__year', 'invoice__month').annotate(
        computed_price=Sum(price, output_field=FloatField()),
        computed_tax=Sum(price * tax_rate, output_field=FloatField()),
    )


def get_offering_customers(offering, active_customers):
    resources = models.Resource.objects.filter(
        offering=offering,
        project__customer__in=active_customers,
    )
    customers_ids = resources.values_list('project__customer_id', flat=True)
    return structure_models.Customer.objects.filter(id__in=customers_ids)


def get_start_and_end_dates_from_request(request):
    serializer = core_serializers.DateRangeFilterSerializer(data=request.query_params)
    serializer.is_valid(raise_exception=True)
    today = datetime.date.today()
    default_start = datetime.date(year=today.year - 1, month=today.month, day=1)
    start_year, start_month = serializer.validated_data.get(
        'start', (default_start.year, default_start.month)
    )
    end_year, end_month = serializer.validated_data.get(
        'end', (today.year, today.month)
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
        raise rf_exceptions.ValidationError('New customer must be not blocked')

    old_project = resource.project

    resource.project = project
    resource.save(update_fields=['project'])

    if resource.scope:
        resource.scope.project = project
        resource.scope.save(update_fields=['project'])

        for service_settings in structure_models.ServiceSettings.objects.filter(
            scope=resource.scope
        ):
            models.Offering.objects.filter(scope=service_settings).update(
                project=project
            )

    order_ids = resource.orderitem_set.values_list('order_id', flat=True)
    for order in models.Order.objects.filter(pk__in=order_ids):
        if order.items.exclude(resource=resource).exists():
            raise MoveResourceException(
                'Resource moving is not possible, '
                'because related orders are related to other resources.'
            )

        order.project = project
        order.save(update_fields=['project'])

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
                'Resource moving is not possible, '
                'because invoice items moving is not possible.'
            )

        invoice_item.project = project
        invoice_item.project_uuid = project.uuid.hex
        invoice_item.project_name = project.name
        invoice_item.invoice = target_invoice
        invoice_item.save(
            update_fields=['project', 'project_uuid', 'project_name', 'invoice']
        )

        start_invoice.update_total_cost()
        target_invoice.update_total_cost()


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
    billing_periods = get_full_days(period['start'], period['end'])
    return {
        'start': period['start'].isoformat(),
        'end': period['end'].isoformat(),
        'quantity': period['quantity'],
        'billing_periods': billing_periods,
        'total': str(period['quantity'] * billing_periods),
    }


def check_customer_blocked_for_terminating(resource):
    if resource.project.customer.blocked:
        raise rf_exceptions.ValidationError(_('Blocked organization is not available.'))


def check_pending_order_item_exists(resource):
    if models.OrderItem.objects.filter(
        resource=resource,
        state__in=(
            models.OrderItem.States.PENDING,
            models.OrderItem.States.EXECUTING,
        ),
    ).exists():
        raise rf_exceptions.ValidationError(
            _('Pending order item for resource already exists.')
        )


def schedule_resources_termination(resources):
    from waldur_mastermind.marketplace import views

    if not resources:
        return

    view = views.ResourceViewSet.as_view({'post': 'terminate'})

    for resource in resources:
        user = (
            resource.end_date_requested_by
            or resource.project.end_date_requested_by
            or core_utils.get_system_robot()
        )

        if not user:
            logger.error(
                'User for terminating resources '
                'of project with due date does not exist.'
            )
            return

        # Terminate pending order items if they exist
        for order_item in models.OrderItem.objects.filter(
            resource=resource, state=models.OrderItem.States.PENDING
        ):
            order_item.set_state_terminated()
            order_item.save()
            if order_item.order.items.count() == 1:
                order_item.order.terminate()
                order_item.order.save()

        response = create_request(view, user, {}, uuid=resource.uuid.hex)

        if response.status_code != status.HTTP_200_OK:
            logger.error(
                'Terminating resource %s has failed. %s'
                % (resource.uuid.hex, response.rendered_content)
            )


def create_local_resource(order_item, scope, effective_id=''):
    resource = models.Resource(
        project=order_item.order.project,
        offering=order_item.offering,
        plan=order_item.plan,
        limits=order_item.limits,
        attributes=order_item.attributes,
        name=order_item.attributes.get('name') or '',
        scope=scope if scope and type(scope) != str else None,
        backend_id=scope if scope and type(scope) == str else '',
        effective_id=effective_id,
    )
    resource.init_cost()
    resource.save()
    order_item.resource = resource
    order_item.save(update_fields=['resource'])
    return resource


def get_service_provider_resources(service_provider):
    return models.Resource.objects.filter(
        offering__customer=service_provider.customer, offering__shared=True
    ).exclude(state=models.Resource.States.TERMINATED)


def get_service_provider_customer_ids(service_provider):
    return (
        get_service_provider_resources(service_provider)
        .values_list('project__customer_id', flat=True)
        .distinct()
    )


def get_service_provider_project_ids(service_provider):
    return (
        get_service_provider_resources(service_provider)
        .values_list('project_id', flat=True)
        .distinct()
    )


def get_service_provider_user_ids(user, service_provider, customer=None):
    project_ids = get_service_provider_project_ids(service_provider)
    qs = structure_models.ProjectPermission.objects.filter(
        project_id__in=project_ids, is_active=True
    )
    if customer:
        qs = qs.filter(project__customer=customer)
    if not user.is_staff and not user.is_support:
        qs = qs.filter(user__is_active=True)
    return qs.values_list('user_id', flat=True).distinct()


def import_current_usages(resource):
    date = datetime.date.today()

    for component_type, component_usage in resource.current_usages.items():
        try:
            offering_component = models.OfferingComponent.objects.get(
                offering=resource.offering, type=component_type
            )
        except models.OfferingComponent.DoesNotExist:
            logger.warning(
                'Skipping current usage synchronization because related '
                'OfferingComponent does not exist.'
                'Resource ID: %s',
                resource.id,
            )
            continue

        try:
            plan_period = (
                models.ResourcePlanPeriod.objects.filter(
                    Q(start__lte=date) | Q(start__isnull=True)
                )
                .filter(Q(end__gt=date) | Q(end__isnull=True))
                .get(resource=resource)
            )
        except models.ResourcePlanPeriod.DoesNotExist:
            logger.warning(
                'Skipping current usage synchronization because related '
                'ResourcePlanPeriod does not exist.'
                'Resource ID: %s',
                resource.id,
            )
            continue

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
    return ', '.join(
        f'{components_map[key].name or components_map[key].type}: {value}'
        for key, value in limits.items()
    )


def link_parent_resource(resource):
    offering_scope = resource.offering.scope
    if isinstance(offering_scope, structure_models.ServiceSettings) and isinstance(
        offering_scope.scope, structure_models.BaseResource
    ):
        try:
            parent_resource = models.Resource.objects.get(scope=offering_scope.scope)
        except models.Resource.DoesNotExist:
            pass
        else:
            resource.parent = parent_resource
            resource.save()


def get_resource_users(resource):
    project_user_ids = structure_models.ProjectPermission.objects.filter(
        project=resource.project, is_active=True
    ).values_list('user_id')
    customer_user_ids = structure_models.CustomerPermission.objects.filter(
        customer=resource.project.customer, is_active=True
    ).values_list('user_id')
    return core_models.User.objects.filter(
        Q(id__in=project_user_ids) | Q(id__in=customer_user_ids)
    )


def generate_uidnumber_and_primary_group(offering):
    initial_uidnumber = offering.plugin_options.get('initial_uidnumber', 100000)
    initial_primarygroup_number = offering.plugin_options.get(
        'initial_primarygroup_number', 100000
    )

    offering_user_with_last_uidnumber = (
        models.OfferingUser.objects.exclude(backend_metadata=None)
        .filter(backend_metadata__has_key='uidnumber')
        .order_by('backend_metadata__uidnumber')
        .last()
    )

    if offering_user_with_last_uidnumber:
        last_uidnumber = offering_user_with_last_uidnumber.backend_metadata['uidnumber']
        offset = last_uidnumber - initial_uidnumber + 1
    else:
        offset = 1

    uidnumber = initial_uidnumber + offset
    primarygroup = initial_primarygroup_number + offset

    return uidnumber, primarygroup
