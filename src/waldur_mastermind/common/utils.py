import json

from decimal import Decimal, ROUND_UP

from dateutil import parser
from django.utils.timezone import get_current_timezone

from rest_framework.test import APIRequestFactory


def quantize_price(value):
    """
    Returns value rounded up to 2 places after the decimal point.
    :rtype: Decimal
    """
    return value.quantize(Decimal('0.01'), rounding=ROUND_UP)


def internal_api_request(view, user, post_data):
    """
    It is assumed that localhost is specified in ALLOWED_HOSTS Django setting
    so that internal API requests are allowed.
    """
    factory = APIRequestFactory()
    request = factory.post('/', data=json.dumps(post_data),
                           content_type='application/json',
                           HTTP_AUTHORIZATION='Token %s' % user.auth_token.key,
                           SERVER_NAME='localhost')
    return view(request)


def parse_datetime(timestr):
    return parser.parse(timestr).replace(tzinfo=get_current_timezone())


def parse_date(timestr):
    return parse_datetime(timestr).date()
