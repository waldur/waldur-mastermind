import base64
from calendar import timegm
from datetime import datetime
import os
import tempfile

from django.apps import apps
from django.conf import settings
from django.http import HttpResponse
import jwt
from rest_framework.exceptions import ValidationError
from rest_framework.generics import get_object_or_404
from rest_framework.reverse import reverse

from waldur_core.core import utils
from waldur_core.core.models import User
from waldur_core.structure.managers import filter_queryset_for_user


def encode_attachment_token(user_uuid, obj, field):
    max_age = settings.WALDUR_CORE['ATTACHMENT_LINK_MAX_AGE']
    dt = datetime.utcnow() + max_age
    expires_at = timegm(dt.utctimetuple())
    payload = {
        'usr': user_uuid,
        'ct': str(obj._meta),
        'id': obj.uuid.hex,
        'field': field,
        'exp': expires_at,
    }
    return str(utils.encode_jwt_token(payload), 'utf-8')


def decode_attachment_token(token):
    try:
        data = utils.decode_jwt_token(token)
    except jwt.exceptions.InvalidTokenError:
        raise ValidationError('Bad signature.')

    if not isinstance(data, dict):
        raise ValidationError('Bad token data.')

    user_uuid = data.get('usr')
    content_type = data.get('ct')
    object_uuid = data.get('id')
    field = data.get('field')

    if not user_uuid:
        raise ValidationError('User UUID is not provided.')

    if not content_type:
        raise ValidationError('Content type is not provided.')

    if not object_uuid:
        raise ValidationError('Object UUID is not provided.')

    if not field:
        raise ValidationError('Field is not provided.')

    return user_uuid, content_type, object_uuid, field


def encode_protected_url(obj, field, request=None, user_uuid=None):
    """
    Returns URL with secure token for media download.
    :param obj: database model object which has file field, for example, customer or invoice.
    :param field: field name inside of database model object, for example, image or file
    :param request: authenticated HTTP request
    :param user_uuid: user UUID, it should be specified if request is not specified
    :return: URL with token.
    """
    if not user_uuid:
        user_uuid = request.user.uuid.hex
    token = encode_attachment_token(user_uuid, obj, field)
    return reverse('media-download', request=request, kwargs={'token': token})


def get_file_from_token(token):
    user_uuid, content_type, object_uuid, field = decode_attachment_token(token)
    user = get_object_or_404(User, uuid=user_uuid)
    if not user.is_active:
        raise ValidationError('User is not active.')
    queryset = apps.get_model(content_type).objects.all()
    if hasattr(queryset, 'filter_for_user'):
        queryset = queryset.filter_for_user(user)
    else:
        queryset = filter_queryset_for_user(queryset, user)
    obj = get_object_or_404(queryset, uuid=object_uuid)
    return getattr(obj, field, None)


def send_file(file):
    _, file_name = os.path.split(file.path)
    response = HttpResponse()
    response['Content-Disposition'] = 'attachment; filename=' + file_name
    response['X-Accel-Redirect'] = file.url
    return response


def dummy_image(filetype='gif'):
    """ Generate empty image in temporary file for testing """
    # 1x1px Transparent GIF
    GIF = 'R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'
    tmp_file = tempfile.NamedTemporaryFile(suffix='.%s' % filetype)
    tmp_file.write(base64.b64decode(GIF))
    return open(tmp_file.name, 'rb')
