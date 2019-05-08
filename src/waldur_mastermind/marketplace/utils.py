from __future__ import unicode_literals

import base64
import os

import jwt
import pdfkit
from PIL import Image
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage as storage
from django.core.serializers.json import DjangoJSONEncoder
from django.template.loader import render_to_string
from django.utils import six
from rest_framework import exceptions

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


def decode_api_data(encoded_data, api_secret_code):
    return jwt.decode(encoded_data, api_secret_code, algorithms=['HS256'])


def encode_api_data(data, api_secret_code):
    return jwt.encode(data, api_secret_code, algorithm='HS256', json_encoder=DjangoJSONEncoder)


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
    fields_metadata = {'action', 'action_details', 'state', 'runtime_state'}
    fields = {'name'}

    for field in fields | fields_metadata:
        if field == 'state':
            value = instance.get_state_display()
        else:
            value = getattr(instance, field, None)
        if field in fields_metadata:
            resource.backend_metadata[field] = value
        else:
            resource.attributes[field] = value
    resource.save(update_fields=['backend_metadata', 'attributes'])


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
