import base64
import datetime
import logging
import os
import traceback
from io import BytesIO

import pdfkit
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage as storage
from django.db import transaction
from django.db.models import Sum
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from PIL import Image
from rest_framework import exceptions as rf_exceptions
from rest_framework import serializers, status

from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core import utils as core_utils
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_mastermind.common.utils import create_request
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
        order_item.set_state_executing()
        order_item.save(update_fields=['state'])
        processor(order_item).process_order_item(user)
    except Exception as e:
        # Here it is necessary to catch all exceptions.
        # If this is not done, then the order will remain in the executed status.
        order_item.error_message = str(e)
        order_item.error_traceback = traceback.format_exc()
        order_item.set_state_erred()
        logger.error(f'Error processing order item { order_item }.')
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


def create_order_pdf(order):
    logo_path = settings.WALDUR_CORE['SITE_LOGO']
    if logo_path:
        with open(logo_path, 'rb') as image_file:
            deployment_logo = base64.b64encode(image_file.read()).decode("utf-8")
    else:
        deployment_logo = None

    context = dict(
        order=order,
        currency=settings.WALDUR_CORE['CURRENCY_NAME'],
        deployment_name=settings.WALDUR_CORE['SITE_NAME'],
        deployment_address=settings.WALDUR_CORE['SITE_ADDRESS'],
        deployment_email=settings.WALDUR_CORE['SITE_EMAIL'],
        deployment_phone=settings.WALDUR_CORE['SITE_PHONE'],
        deployment_logo=deployment_logo,
    )
    html = render_to_string('marketplace/order.html', context)
    pdf = pdfkit.from_string(html, False)
    return pdf


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


def fill_activated_field(apps, schema_editor):
    # We cannot use RequestTypeMixin.Types.CREATE and OrderItem.States.Done because this function called in migrations
    state_done = 3
    type_create = 1

    OrderItem = apps.get_model('marketplace', 'OrderItem')

    for order_item in OrderItem.objects.filter(type=type_create, state=state_done):
        if not order_item.activated and order_item.resource:
            order_item.activated = order_item.resource.created
            order_item.save()


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
                {'customer': resource.offering.customer, 'resources': [resource],}
            )

    return result


def get_public_resources_url(customer):
    return core_utils.format_homeport_link(
        'organizations/{organization_uuid}/marketplace-public-resources/',
        organization_uuid=customer.uuid,
    )


def validate_limits(limits, offering):
    if not plugins.manager.can_update_limits(offering.type):
        raise serializers.ValidationError(
            {'limits': _('Limits update is not supported for this resource.')}
        )

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

    # Validate max and min limit value.
    components_map = {
        component.type: component
        for component in offering.components.filter(type__in=valid_component_types)
    }

    for key, value in limits.items():
        component = components_map.get(key)
        if not component:
            continue

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


def get_offering_costs(offering, active_customers, start, end):
    costs = []
    date = start

    while date <= end:
        year = date.year
        month = date.month

        invoice_items = invoice_models.InvoiceItem.objects.filter(
            details__offering_uuid=offering.uuid.hex,
            project__customer__in=active_customers,
            invoice__year=year,
            invoice__month=month,
        )

        stats = {
            'tax': 0,
            'total': 0,
            'price': 0,
            'price_current': 0,
            'period': '%s-%02d' % (year, month),
        }
        for item in invoice_items:
            stats['tax'] += item.tax
            stats['total'] += item.total
            stats['price'] += item.price
            stats['price_current'] += item.price_current

        costs.append(stats)

        date += relativedelta(months=1)

    return costs


def get_offering_customers(offering, active_customers):
    resources = models.Resource.objects.filter(
        offering=offering, project__customer__in=active_customers,
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


def get_offering_component_stats(offering, active_customers, start, end):
    component_stats = []

    resources = models.Resource.objects.filter(
        offering=offering, project__customer__in=active_customers,
    )
    resources_ids = resources.values_list('id', flat=True)
    date = start

    while date <= end:
        year = date.year
        month = date.month
        period = '%s-%02d' % (year, month)
        # for consistency with usage resource usage reporting, assume values at the beginning of the last day
        period_visible = (
            core_utils.month_end(date)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .isoformat()
        )
        invoice_items = invoice_models.InvoiceItem.objects.filter(
            resource_id__in=resources_ids, invoice__year=year, invoice__month=month,
        )

        for item in invoice_items:
            # Case when invoice item details includes plan component data.
            plan_component_id = item.details.get('plan_component_id')

            if not plan_component_id:
                continue

            try:
                plan_component = models.PlanComponent.objects.get(pk=plan_component_id)
                offering_component = plan_component.component

                if (
                    offering_component.billing_type
                    == models.OfferingComponent.BillingTypes.LIMIT
                ):
                    component_stats.append(
                        {
                            'usage': item.quantity,
                            'description': offering_component.description,
                            'measured_unit': offering_component.measured_unit,
                            'type': offering_component.type,
                            'name': offering_component.name,
                            'period': period,
                            'date': period_visible,
                            'offering_component_id': offering_component.id,
                        }
                    )

                if (
                    offering_component.billing_type
                    == models.OfferingComponent.BillingTypes.USAGE
                ):
                    if [
                        *filter(
                            lambda x: x['period'] == period
                            and x['offering_component_id'] == offering_component.id,
                            component_stats,
                        )
                    ]:
                        continue

                    usages = models.ComponentUsage.objects.filter(
                        component=offering_component, billing_period=date
                    ).aggregate(usage=Sum('usage'))['usage']

                    component_stats.append(
                        {
                            'usage': usages,
                            'description': offering_component.description,
                            'measured_unit': offering_component.measured_unit,
                            'type': offering_component.type,
                            'name': offering_component.name,
                            'period': period,
                            'date': period_visible,
                            'offering_component_id': offering_component.id,
                        }
                    )

                if (
                    offering_component.billing_type
                    == models.OfferingComponent.BillingTypes.FIXED
                ):
                    other = [
                        *filter(
                            lambda x: x['period'] == period
                            and x['offering_component_id'] == offering_component.id,
                            component_stats,
                        )
                    ]
                    if other:
                        other[0]['usage'] += item.get_factor()
                        continue

                    component_stats.append(
                        {
                            'usage': item.get_factor(),
                            'description': offering_component.description,
                            'measured_unit': offering_component.measured_unit,
                            'type': offering_component.type,
                            'name': offering_component.name,
                            'period': period,
                            'date': period_visible,
                            'offering_component_id': offering_component.id,
                        }
                    )

            except models.PlanComponent.DoesNotExist:
                logger.error(
                    'PlanComponent with id %s is not found.' % plan_component_id
                )

        date += relativedelta(months=1)

    # delete internal data
    [s.pop('offering_component_id', None) for s in component_stats]

    return component_stats


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

        start_invoice.update_current_cost()
        target_invoice.update_current_cost()


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
    try:
        project = resource.project
    except structure_models.Project.DoesNotExist:
        project = structure_models.Project.all_objects.get(pk=resource.project_id)

    if project.customer.blocked:
        raise rf_exceptions.ValidationError(_('Blocked organization is not available.'))


def schedule_resources_termination(resources):
    from waldur_mastermind.marketplace import views

    if not resources:
        return

    view = views.ResourceViewSet.as_view({'post': 'terminate'})
    user = core_utils.get_system_robot()

    if not user:
        logger.error(
            'Staff user with username system_robot for terminating resources '
            'of project with due date does not exist.'
        )
        return

    for resource in resources:
        response = create_request(view, user, {}, uuid=resource.uuid.hex)

        if response.status_code != status.HTTP_200_OK:
            logger.error(
                'Terminating resource %s has failed. %s'
                % (resource.uuid.hex, response.content)
            )


def create_local_resource(order_item, scope):
    resource = models.Resource(
        project=order_item.order.project,
        offering=order_item.offering,
        plan=order_item.plan,
        limits=order_item.limits,
        attributes=order_item.attributes,
        name=order_item.attributes.get('name') or '',
        scope=scope if scope and type(scope) != str else None,
        backend_id=scope if scope and type(scope) == str else '',
    )
    resource.init_cost()
    resource.save()
    resource.init_quotas()
    order_item.resource = resource
    order_item.save(update_fields=['resource'])
    return resource
