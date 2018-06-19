from __future__ import unicode_literals

import os
import re

import six
from PIL import Image
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage as storage


def hstore_to_dict(hstore):
    attributes = {}
    for attr in hstore:
        attr_list = attr.split('__')
        key = attr_list[0]
        if len(attr_list) > 1:
            if key in attributes:
                value = attributes[key]
                if isinstance(value, list):
                    attributes[key].append(attr_list[1])
                else:
                    attributes[key] = [value, attr_list[1]]
            else:
                attributes[key] = attr_list[1]
        else:
            attributes[attr] = hstore[attr]
    return attributes


def dict_to_hstore(dictionary):
    result = {}
    for key, value in dictionary.items():
        if isinstance(value, int):
            result[key] = value

        if isinstance(value, six.text_type) and re.match('^[A-Za-z0-9_-]+$', value):
            result[key + '__' + value] = True

        if isinstance(value, list) and value:
            for v in value:
                if isinstance(v, six.text_type) and re.match('^[A-Za-z0-9_-]+$', v):
                    result[key + '__' + v] = True
    return result


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
