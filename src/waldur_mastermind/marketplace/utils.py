from __future__ import unicode_literals

import base64
import os
import hashlib

from django.db import transaction
import pdfkit
import six
from PIL import Image
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage as storage
from django.template.loader import render_to_string
from rest_framework import serializers, status

from waldur_mastermind.common import utils as common_utils

from . import models


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


def check_api_signature(data, api_secret_code, signature):
    return signature == get_api_signature(data, api_secret_code)


def get_api_signature(data, api_secret_code):
    concatenate_string = api_secret_code

    for usage in data['usages']:
        for key in sorted(usage.keys()):
            concatenate_string += key + six.text_type(usage[key])

    return hashlib.sha512(concatenate_string).hexdigest()


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


class BaseOrderItemProcessor(object):
    def __init__(self, order_item):
        self.order_item = order_item

    def process_order_item(self, user):
        """
        This method receives user object and creates plugin's resource corresponding
        to provided order item. It is called after order has been approved.
        """
        raise NotImplementedError()

    def validate_order_item(self, request):
        """
        This method receives request object, and raises
        validation error if provided order item is invalid.
        It is called after order has been created but before it is submitted.
        """
        raise NotImplementedError()


class CreateResourceProcessor(BaseOrderItemProcessor):
    """
    This class implements order processing using internal API requests.

    Order item validation flow looks as following:
    1) Convert order item to HTTP POST request data expected by DRF serializer.
    2) Pass request data to serializer and check if data is valid.

    Order item processing flow looks as following:
    1) Convert order item to HTTP POST request data expected by DRF serializer.
    2) Issue internal API request to DRF viewset.
    3) Extract Django model for created resource from HTTP response.
    4) Create marketplace resource object from order item and plugin resource.
    5) Store link from order item to the resource.

    Therefore this class implements template method design pattern.
    """

    def validate_order_item(self, request):
        post_data = self.get_post_data()
        serializer_class = self.get_serializer_class()
        serializer = serializer_class(data=post_data, context={'request': request})
        serializer.is_valid(raise_exception=True)

    def process_order_item(self, user):
        post_data = self.get_post_data()
        view = self.get_viewset().as_view({'post': 'create'})
        response = common_utils.create_request(view, user, post_data)
        if response.status_code != status.HTTP_201_CREATED:
            raise serializers.ValidationError(response.data)

        with transaction.atomic():
            scope = self.get_scope_from_response(response)
            resource = models.Resource.objects.create(
                project=self.order_item.order.project,
                offering=self.order_item.offering,
                plan=self.order_item.plan,
                limits=self.order_item.limits,
                attributes=self.order_item.attributes,
                scope=scope,
            )
            resource.init_quotas()
            self.order_item.resource = resource
            self.order_item.save(update_fields=['resource'])

    def get_serializer_class(self):
        """
        This method should return DRF serializer class which
        validates request data to provision new resources.
        """
        raise NotImplementedError

    def get_viewset(self):
        """
        This method should return DRF viewset class which
        processes request to provision new resources.
        """
        raise NotImplementedError

    def get_post_data(self):
        """
        This method converts order item to request data expected by DRF serializer.
        """
        raise NotImplementedError

    def get_scope_from_response(self, response):
        """
        This method extracts Django model from response returned by DRF viewset.
        """
        raise NotImplementedError


class UpdateResourceProcessor(BaseOrderItemProcessor):
    def validate_order_item(self, request):
        post_data = self.get_post_data()
        serializer_class = self.get_serializer_class()
        serializer = serializer_class(data=post_data, context={'request': request})
        serializer.is_valid(raise_exception=True)

    def process_order_item(self, user):
        resource = self.get_resource()
        if not resource:
            raise serializers.ValidationError('Resource is not found.')

        view = self.get_view()
        payload = self.get_post_data()
        response = common_utils.create_request(view, user, payload)

        if response.status_code == status.HTTP_202_ACCEPTED:
            self.order_item.resource.set_state_updating()
            self.order_item.resource.save(update_fields=['state'])
        else:
            raise serializers.ValidationError(response.data)

    def get_serializer_class(self):
        """
        This method should return DRF serializer class which
        validates request data to update existing resource.
        """
        raise NotImplementedError

    def get_resource(self):
        """
        This method should return related resource of order item.
        """
        return self.order_item.resource.scope

    def get_view(self):
        """
        This method should return DRF viewset class which
        processes request to change existing resource.
        """
        raise NotImplementedError

    def get_post_data(self):
        """
        This method converts order item to request data expected by DRF serializer.
        """
        raise NotImplementedError


class DeleteResourceProcessor(BaseOrderItemProcessor):
    def validate_order_item(self, request):
        pass

    def process_order_item(self, user):
        resource = self.get_resource()
        if not resource:
            raise serializers.ValidationError('Resource is not found.')

        view = self.get_viewset().as_view({'delete': 'destroy'})
        response = common_utils.delete_request(view, user, uuid=resource.uuid)

        if response.status_code == status.HTTP_204_NO_CONTENT:
            with transaction.atomic():
                self.order_item.resource.set_state_terminated()
                self.order_item.resource.save(update_fields=['state'])

                self.order_item.state = models.OrderItem.States.DONE
                self.order_item.save(update_fields=['state'])

        elif response.status_code == status.HTTP_202_ACCEPTED:
            with transaction.atomic():
                self.order_item.resource.set_state_terminating()
                self.order_item.resource.save(update_fields=['state'])
        else:
            raise serializers.ValidationError(response.data)

    def get_resource(self):
        """
        This method should return related resource of order item.
        """
        return self.order_item.resource.scope

    def get_viewset(self):
        """
        This method should return DRF viewset class which
        processes request to delete existing resource.
        """
        raise NotImplementedError()
