import base64
import datetime
import os
from io import BytesIO

import pdfkit
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage as storage
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from PIL import Image
from rest_framework import serializers

from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core import utils as core_utils
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_mastermind.invoices import models as invoice_models

from . import models, plugins


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


def process_order_item(order_item, user):
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
        order_item.set_state_erred()
        order_item.save(update_fields=['state', 'error_message'])


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
    order.file = str(base64.b64encode(pdf), 'utf-8')
    order.save()


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

    resource.name = instance.name
    resource.save(update_fields=['backend_metadata', 'attributes', 'name'])


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
    return {
        'offering_type': offering.type,
        'offering_name': offering.name,
        'offering_uuid': offering.uuid.hex,
    }


def format_list(resources):
    """
    Format comma-separated list of IDs from Django queryset.
    """
    return ', '.join(map(str, sorted(resources.values_list('id', flat=True))))


def get_order_item_url(order_item):
    link_template = settings.WALDUR_MARKETPLACE['ORDER_ITEM_LINK_TEMPLATE']
    return link_template.format(
        order_item_uuid=order_item.uuid.hex, project_uuid=order_item.order.project.uuid
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

    offering_ids = models.OfferingComponent.objects.filter(
        billing_type=models.OfferingComponent.BillingTypes.USAGE
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
    link_template = settings.WALDUR_MARKETPLACE['PUBLIC_RESOURCES_LINK_TEMPLATE']
    return link_template.format(organization_uuid=customer.uuid)


def validate_limits(limits, offering):
    usage_components = (
        offering.components.filter(
            billing_type=models.OfferingComponent.BillingTypes.USAGE
        )
        .exclude(disable_quotas=True)
        .values_list('type', flat=True)
    )
    valid_component_types = set(usage_components)
    valid_component_types.update(plugins.manager.get_available_limits(offering.type))
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
            **component_data._asdict()
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


def get_offering_costs(offering, active_customers, start, end):
    costs = []

    resources = models.Resource.objects.filter(
        offering=offering, project__customer__in=active_customers,
    )
    resources_ids = resources.values_list('id', flat=True)
    date = start

    while date <= end:
        year = date.year
        month = date.month

        invoice_items = invoice_models.InvoiceItem.objects.filter(
            content_type_id=ContentType.objects.get_for_model(models.Resource).id,
            object_id__in=resources_ids,
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

        invoice_items = invoice_models.InvoiceItem.objects.filter(
            content_type_id=ContentType.objects.get_for_model(models.Resource).id,
            object_id__in=resources_ids,
            invoice__year=year,
            invoice__month=month,
        )

        stats = {}
        for item in invoice_items:
            limits = item.details.get('limits', {})

            '''If a resource will be deleted then usages will be deleted too.
            Then statistics will be not available.
            Therefore we use invoice item details.'''
            usages = item.details.get('usages', {})
            limits.update(usages)

            for limit, usage in limits.items():
                if limit in stats.keys():
                    stats[limit] += usage
                else:
                    stats[limit] = usage
        component_stats.append(
            {'period': '%s-%02d' % (year, month), 'components': stats}
        )

        date += relativedelta(months=1)

    return component_stats
