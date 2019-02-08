from __future__ import unicode_literals

import base64
import os

import jwt
import pdfkit
import six
from PIL import Image
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage as storage
from django.core.serializers.json import DjangoJSONEncoder
from django.template.loader import render_to_string


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
