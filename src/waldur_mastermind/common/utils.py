import json
from collections.abc import Callable
from datetime import date, datetime
from decimal import ROUND_UP, Decimal
from urllib.parse import urlencode

from dateutil import parser
from django.utils.timezone import get_current_timezone
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from waldur_core.core import models as core_models
from waldur_core.core.authentication import refresh_token
from waldur_mastermind.common import mixins as common_mixins


def quantize_price(value: Decimal) -> Decimal:
    """
    Returns value rounded up to 2 places after the decimal point.
    :rtype: Decimal
    """
    return value.quantize(Decimal("0.01"), rounding=ROUND_UP)


def get_headers(user: core_models.User) -> dict[str, str]:
    """
    It is assumed that localhost is specified in ALLOWED_HOSTS Django setting
    so that internal API requests are allowed.
    """
    token = refresh_token(user)
    return dict(
        content_type="application/json",
        HTTP_AUTHORIZATION="Token %s" % token.key,
        SERVER_NAME="localhost",
    )


def get_request(view: Callable, user: core_models.User, **extra) -> Response:
    factory = APIRequestFactory()
    request = factory.get("/", **get_headers(user))
    return view(request, **extra)


def create_request(
    view: Callable,
    user: core_models.User,
    post_data: dict,
    query_params: dict | None = None,
    **kwargs,
) -> Response:
    factory = APIRequestFactory()
    path = "/" if not query_params else "/" + "?" + urlencode(query_params)
    request = factory.post(path, data=json.dumps(post_data), **get_headers(user))
    return view(request, **kwargs)


def delete_request(
    view: Callable, user: core_models.User, query_params: dict | None = None, **extra
) -> Response:
    factory = APIRequestFactory()
    path = ""
    if query_params:
        path = "?" + urlencode(query_params)
    request = factory.delete(path, **get_headers(user))
    return view(request, **extra)


def parse_datetime(timestr: str) -> datetime:
    return parser.parse(timestr).replace(tzinfo=get_current_timezone())


def parse_date(timestr: str) -> date:
    return parse_datetime(timestr).date()


def mb_to_gb(value: int | float | Decimal) -> Decimal:
    # In marketplace RAM and storage is stored in GB, but in plugin it is stored in MB.
    return quantize_price(Decimal(value) / 1024)


def prices_are_equal(
    x: Decimal | float | int | str, y: Decimal | float | int | str
) -> bool:
    exp = Decimal(".1") ** common_mixins.PRICE_DECIMAL_PLACES
    return Decimal(x).quantize(exp) == Decimal(y).quantize(exp)
