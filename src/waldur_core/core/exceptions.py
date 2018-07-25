from __future__ import unicode_literals

from django.utils.translation import ugettext_lazy as _
from rest_framework import status
from rest_framework.exceptions import APIException


class IncorrectStateException(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = _('Cannot modify an object in its current state.')


class RuntimeStateException(Exception):
    pass
