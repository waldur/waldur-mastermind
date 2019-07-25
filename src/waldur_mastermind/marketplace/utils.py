from __future__ import unicode_literals

import base64
import os

import pdfkit
from PIL import Image
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage as storage
from django.template.loader import render_to_string
from django.utils import six
from django.utils import timezone
from rest_framework import exceptions

from waldur_core.core import utils as core_utils

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
        order_item.error_message = 'Skipping order item processing because processor is not found.'
        order_item.set_state_erred()
        order_item.save(update_fields=['state', 'error_message'])
        return

    try:
        processor(order_item).process_order_item(user)
    except exceptions.APIException as e:
        order_item.error_message = six.text_type(e)
        order_item.set_state_erred()
        order_item.save(update_fields=['state', 'error_message'])
    else:
        if order_item.state != models.OrderItem.States.DONE:
            order_item.set_state_executing()
            order_item.save(update_fields=['state'])


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
    fh = storage.open(pic.name, 'r')
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

    temp_thumb = six.StringIO()
    image.save(temp_thumb, FTYPE)
    temp_thumb.seek(0)
    screenshot.thumbnail.save(thumb_name, ContentFile(temp_thumb.read()), save=True)
    temp_thumb.close()


def create_order_pdf(order):
    logo_path = settings.WALDUR_CORE['SITE_LOGO']
    if logo_path:
        with open(logo_path, 'r') as image_file:
            deployment_logo = base64.b64encode(image_file.read())
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
    order.file = base64.b64encode(pdf)
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
            'service_provider_uuid': '' if not service_provider else service_provider.uuid.hex,
        }
    except models.Resource.DoesNotExist:
        return {}


def format_list(resources):
    """
    Format comma-separated list of IDs from Django queryset.
    """
    return ', '.join(map(str, sorted(resources.values_list('id', flat=True))))


def get_order_item_url(order_item):
    link_template = settings.WALDUR_MARKETPLACE['ORDER_ITEM_LINK_TEMPLATE']
    return link_template.format(order_item_uuid=order_item.uuid,
                                project_uuid=order_item.order.project.uuid)


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

    offering_ids = models.OfferingComponent.objects.filter(billing_type=models.OfferingComponent.BillingTypes.USAGE).\
        values_list('offering_id', flat=True)
    resource_with_usages = models.ComponentUsage.objects.filter(billing_period=billing_period).\
        values_list('resource', flat=True)
    resources_without_usages = models.Resource.objects.\
        filter(state=models.Resource.States.OK, offering_id__in=offering_ids).exclude(id__in=resource_with_usages)
    result = []

    for resource in resources_without_usages:
        if filter(lambda x: x['customer'] == resource.offering.customer, result):
            filter(lambda x: x['customer'] == resource.offering.customer, result)[0]['resources'].append(resource)
        else:
            result.append({
                'customer': resource.offering.customer,
                'resources': [resource],
            })

    return result


def get_public_resources_url(customer):
    link_template = settings.WALDUR_MARKETPLACE['PUBLIC_RESOURCES_LINK_TEMPLATE']
    return link_template.format(organization_uuid=customer.uuid)
