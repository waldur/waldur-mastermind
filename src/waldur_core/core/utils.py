import calendar
import datetime
import functools
import importlib
import logging
import os
import re
import time
import unicodedata
import uuid
import warnings
from collections import OrderedDict
from itertools import chain
from operator import itemgetter

import jwt
import requests
from constance import config
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import F, Subquery
from django.db.models.fields import PositiveIntegerField
from django.db.models.sql.query import get_order_dir
from django.http import QueryDict
from django.template import Context
from django.template.loader import get_template, render_to_string
from django.urls import resolve
from django.utils import timezone
from django.utils.crypto import get_random_string
from geopy.geocoders import Nominatim
from requests.packages.urllib3 import exceptions
from rest_framework.settings import api_settings

import textile
from ua_parser import user_agent_parser
from waldur_core.structure.notifications import NOTIFICATIONS

logger = logging.getLogger(__name__)


def flatten(*xs):
    return tuple(chain.from_iterable(xs))


def sort_dict(unsorted_dict):
    """
    Return a OrderedDict ordered by key names from the :unsorted_dict:
    """
    sorted_dict = OrderedDict()
    # sort items before inserting them into a dict
    for key, value in sorted(unsorted_dict.items(), key=itemgetter(0)):
        sorted_dict[key] = value
    return sorted_dict


def datetime_to_timestamp(datetime):
    return int(time.mktime(datetime.timetuple()))


def timestamp_to_datetime(timestamp, replace_tz=True):
    dt = datetime.datetime.fromtimestamp(int(timestamp))
    if replace_tz:
        dt = dt.replace(tzinfo=timezone.get_current_timezone())
    return dt


def timeshift(**kwargs):
    return timezone.now().replace(microsecond=0) + datetime.timedelta(**kwargs)


def hours_in_month(month=None, year=None):
    now = datetime.datetime.now()
    if not month:
        month = now.month
    if not year:
        year = now.year

    days_in_month = calendar.monthrange(year, month)[1]
    return 24 * days_in_month


def month_start(date):
    return timezone.make_aware(
        datetime.datetime(day=1, month=date.month, year=date.year)
    )


def month_end(date):
    days_in_month = calendar.monthrange(date.year, date.month)[1]
    last_day_of_month = datetime.date(
        month=date.month, year=date.year, day=days_in_month
    )
    last_second_of_month = datetime.datetime.combine(
        last_day_of_month, datetime.time.max
    )
    return timezone.make_aware(last_second_of_month, timezone.get_current_timezone())


def pwgen(pw_len=16):
    """Generate a random password with the given length.
    Allowed chars does not have "I" or "O" or letters and
    digits that look similar -- just to avoid confusion.
    """
    return get_random_string(
        pw_len, "abcdefghjkmnpqrstuvwxyz" "ABCDEFGHJKLMNPQRSTUVWXYZ" "23456789"
    )


def serialize_instance(instance):
    """Serialize Django model instance"""
    model_name = str(instance._meta)
    return f"{model_name}:{instance.pk}"


def deserialize_instance(serialized_instance):
    """Deserialize Django model instance"""
    model_name, pk = serialized_instance.split(":")
    model = apps.get_model(model_name)
    return model._default_manager.get(pk=pk)


def serialize_class(cls):
    """Serialize Python class"""
    return f"{cls.__module__}:{cls.__name__}"


def deserialize_class(serilalized_cls):
    """Deserialize Python class"""
    module_name, cls_name = serilalized_cls.split(":")
    module = importlib.import_module(module_name)
    return getattr(module, cls_name)


def clear_url(url):
    """Remove domain and protocol from url"""
    if url.startswith("http"):
        return "/" + url.split("/", 3)[-1]
    return url


def get_model_from_resolve_match(match):
    queryset = match.func.cls.queryset
    if queryset is not None:
        return queryset.model
    else:
        return match.func.cls.model


def instance_from_url(url, user=None):
    """Restore instance from URL"""
    # XXX: This circular dependency will be removed then filter_queryset_for_user
    # will be moved to model manager method
    from waldur_core.core.models import User
    from waldur_core.structure.managers import filter_queryset_for_user

    url = clear_url(url)
    match = resolve(url)
    model = get_model_from_resolve_match(match)
    queryset = model.objects.all()
    if user is not None:
        if user.is_staff and model == User:
            queryset = filter_queryset_for_user(User.all_objects.all(), user)
        else:
            queryset = filter_queryset_for_user(model.objects.all(), user)
    return queryset.get(**match.kwargs)


def get_detail_view_name(model):
    if model is NotImplemented:
        raise AttributeError("Cannot get detail view name for not implemented model")

    if hasattr(model, "get_url_name") and callable(model.get_url_name):
        return "%s-detail" % model.get_url_name()

    return "%s-detail" % model.__name__.lower()


def get_list_view_name(model):
    if model is NotImplemented:
        raise AttributeError("Cannot get list view name for not implemented model")

    if hasattr(model, "get_url_name") and callable(model.get_url_name):
        return "%s-list" % model.get_url_name()

    return "%s-list" % model.__name__.lower()


def get_fake_context(user=None):
    if not user:
        user = get_user_model()()
    request = type(
        "R", (object,), {"method": "GET", "user": user, "query_params": QueryDict()}
    )
    return {"request": request, "user": user}


def camel_case_to_underscore(name):
    s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def format_text(template_name, context):
    template = get_template(template_name).template
    return template.render(Context(context, autoescape=False)).strip()


def find_template_from_registry(app, event_type, template_suffix):
    app_dict = NOTIFICATIONS.get(app)
    for section in app_dict:
        if event_type == section.get("path"):
            return f"{app}/{event_type}_{template_suffix}"


def send_mail(
    subject,
    body,
    to,
    from_email=None,
    html_message=None,
    filename=None,
    attachment=None,
    content_type="text/plain",
    bcc=None,
    reply_to=None,
    fail_silently=False,
):
    from_email = from_email or settings.DEFAULT_FROM_EMAIL
    reply_to = reply_to or settings.DEFAULT_REPLY_TO_EMAIL
    email = EmailMultiAlternatives(
        subject=subject,
        body=body,
        to=to,
        from_email=from_email,
        bcc=bcc,
        reply_to=[reply_to],
    )

    footer_text = config.COMMON_FOOTER_TEXT
    footer_html = config.COMMON_FOOTER_HTML
    if footer_text != "" or footer_html != "":
        email.body += f"\n\n{footer_text}"

        if html_message:
            email.attach_alternative(f"{html_message}\n\n{footer_html}", "text/html")

    elif html_message:
        email.attach_alternative(html_message, "text/html")

    if filename:
        email.attach(filename, attachment, content_type)
    return email.send(fail_silently=fail_silently)


def broadcast_mail(
    app,
    event_type,
    context,
    recipient_list,
    filename=None,
    attachment=None,
    content_type="text/plain",
    bcc=None,
):
    """
    Shorthand to format email message from template file and sent it to all recipients.

    It is assumed that there are there are 3 templates available for event type in application.
    For example, if app is 'users' and event_type is 'invitation_rejected', then there should be 3 files:

    1) users/invitation_rejected_subject.txt is template for email subject
    2) users/invitation_rejected_message.txt is template for email body as text
    3) users/invitation_rejected_message.html is template for email body as HTML

    By default, built-in Django send_mail is used, all members
    of the recipient list will see the other recipients in the 'To' field.
    Contrary to this, we're using explicit loop in order to ensure that
    recipients would NOT see the other recipients.

    :param app: prefix for template filename.
    :param event_type: postfix for template filename.
    :param context: dictionary passed to the template for rendering.
    :param recipient_list: list of strings, each an email address.
    :param filename: name of the attached file
    :param attachment: content of attachment
    :param content_type: the content type of attachment
    """
    from .models import Notification

    notification_key = f"{app}.{event_type}"
    try:
        notification = Notification.objects.get(key=notification_key)
    except Notification.DoesNotExist:
        return

    if notification.enabled:
        subject_template_name = find_template_from_registry(
            app, event_type, "subject.txt"
        )
        text_template_name = find_template_from_registry(app, event_type, "message.txt")
        html_template_name = find_template_from_registry(
            app, event_type, "message.html"
        )

        subject = format_text(subject_template_name, context)
        text_message = format_text(text_template_name, context)
        html_message = render_to_string(html_template_name, context)

        for recipient in recipient_list:
            logger.info(f"About to send {event_type} notification to {recipient}")
            send_mail(
                subject,
                text_message,
                to=[recipient],
                html_message=html_message,
                filename=filename,
                attachment=attachment,
                content_type=content_type,
                bcc=bcc,
            )


def get_ordering(request):
    """
    Extract ordering from HTTP request.
    """
    return request.query_params.get(api_settings.ORDERING_PARAM)


def order_with_nulls(queryset, field):
    """
    If sorting order is ascending, then NULL values come first,
    if sorting order is descending, then NULL values come last.
    """
    col, order = get_order_dir(field)
    descending = True if order == "DESC" else False

    if descending:
        return queryset.order_by(F(col).desc(nulls_last=True))
    else:
        return queryset.order_by(F(col).asc(nulls_first=True))


def is_uuid_like(val):
    """
    Check if value looks like a valid UUID.
    """
    if isinstance(val, uuid.UUID):
        return True
    try:
        uuid.UUID(val)
    except (TypeError, ValueError, AttributeError):
        return False
    else:
        return True


def chunks(xs, n):
    """
    Split list to evenly sized chunks

    >> chunks(range(10), 4)
    [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]]

    :param xs: arbitrary list
    :param n: chunk size
    :return: list of lists
    """
    return [xs[i : i + n] for i in range(0, len(xs), n)]


def create_batch_fetcher(fetcher):
    """
    Decorator to simplify code for chunked fetching.
    It fetches resources from backend API in evenly sized chunks.
    It is needed in order to avoid too long HTTP request error.
    Essentially, it gives the same result as fetcher(items) but does not throw an error.

    :param fetcher: fetcher: function which accepts list of items and returns list of results,
    for example, list of UUIDs and returns list of projects with given UUIDs
    :return: function with the same signature as fetcher
    """

    @functools.wraps(fetcher)
    def wrapped(items):
        """
        :param items: list of items for request, for example, list of UUIDs
        :return: merged list of results
        """
        result = []
        for chunk in chunks(items, settings.WALDUR_CORE["HTTP_CHUNK_SIZE"]):
            result.extend(fetcher(chunk))
        return result

    return wrapped


class DryRunCommand(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Don't make any changes, instead show what objects would be created.",
        )


def encode_jwt_token(data, api_secret_code=None):
    """
    Encode Python dictionary as JWT token.
    :param data: Dictionary with payload.
    :param api_secret_code: optional string, application secret key is used by default.
    :return: JWT token string with encoded and signed data.
    """
    if api_secret_code is None:
        api_secret_code = settings.SECRET_KEY
    return jwt.encode(
        data, api_secret_code, algorithm="HS256", json_encoder=DjangoJSONEncoder
    )


def decode_jwt_token(encoded_data, api_secret_code=None):
    """
    Decode JWT token string to Python dictionary.
    :param encoded_data: JWT token string with encoded and signed data.
    :param api_secret_code: optional string, application secret key is used by default.
    :return: Dictionary with payload.
    """
    if api_secret_code is None:
        api_secret_code = settings.SECRET_KEY
    return jwt.decode(encoded_data, api_secret_code, algorithms=["HS256"])


def normalize_unicode(data):
    return unicodedata.normalize("NFKD", data).encode("ascii", "ignore").decode("utf8")


UNIT_PATTERN = re.compile(r"(\d+)([KMGTP]?)")

UNITS = {
    "K": 2**10,
    "M": 2**20,
    "G": 2**30,
    "T": 2**40,
}


def parse_int(value):
    """
    Convert 5K to 5000.
    """
    match = re.match(UNIT_PATTERN, value)
    if not match:
        return 0
    value = int(match.group(1))
    unit = match.group(2)
    if unit:
        factor = UNITS[unit]
    else:
        factor = 1
    return factor * value


class QuietSession(requests.Session):
    """Session class that suppresses warning about unsafe TLS sessions and clogging the logs.
    Inspired by: https://github.com/kennethreitz/requests/issues/2214#issuecomment-110366218
    """

    def request(self, *args, **kwargs):
        if not kwargs.get("verify", self.verify):
            with warnings.catch_warnings():
                if hasattr(
                    exceptions, "InsecurePlatformWarning"
                ):  # urllib3 1.10 and lower does not have this warning
                    warnings.simplefilter("ignore", exceptions.InsecurePlatformWarning)
                warnings.simplefilter("ignore", exceptions.InsecureRequestWarning)
                return super().request(*args, **kwargs)
        else:
            return super().request(*args, **kwargs)


def get_lat_lon_from_address(address):
    geo_locator = Nominatim(user_agent="waldur")
    location = geo_locator.geocode(address)

    if location:
        return location.latitude, location.longitude


def format_homeport_link(format_str="", **kwargs):
    link = settings.WALDUR_CORE["HOMEPORT_URL"] + format_str
    return link.format(**kwargs)


def get_system_robot():
    from waldur_core.core import models

    robot_user, created = models.User.objects.get_or_create(
        username="system_robot", is_staff=True, is_active=True
    )
    if created:
        robot_user.set_unusable_password()
        robot_user.description = (
            "Special user used for performing actions on behalf of a system."
        )
        robot_user.first_name = "System"
        robot_user.last_name = "Robot"
        robot_user.save()
    return robot_user


def get_ip_address(request):
    """
    Correct IP address is expected as first element of HTTP_X_FORWARDED_FOR or REMOTE_ADDR
    """
    if "HTTP_X_FORWARDED_FOR" in request.META:
        return request.META["HTTP_X_FORWARDED_FOR"].split(",")[0].strip()
    elif "REMOTE_ADDR" in request.META:
        return request.META["REMOTE_ADDR"]


def get_user_agent(request):
    return request.META.get("HTTP_USER_AGENT", "")


def get_device_info(user_agent):
    return user_agent_parser.Parse(user_agent)


def get_last_month():
    today = datetime.date.today()
    first = today.replace(day=1)
    return first - datetime.timedelta(days=1)


def get_deployment_type():
    """
    1. If environment variable KUBERNETES_SERVICE_HOST is set - Waldur is running in kubernetes

    2. If file /.dockerenv is set and /etc/resolv.conf has line
    "nameserver 127.0.0.11" - Waldur is running in docker compose

    3. If file /.dockerenv is set, but /etc/resolv.conf does not have line
    "nameserver 127.0.0.11" - Waldur is running in custom docker environment

    4. If file /.dockerenv does not exist - Waldur is running in "other" installation environment
    """
    docker_env_path = "/.dockerenv"
    resolv_path = "/etc/resolv.conf"

    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return "kubernetes"

    docker_env = os.path.exists(docker_env_path)
    has_line = False

    if os.path.exists(resolv_path):
        with open(resolv_path) as file:
            for line in file:
                if "nameserver 127.0.0.11" in line:
                    has_line = True
                    break

    if docker_env and has_line:
        return "docker compose"

    if docker_env and not has_line:
        return "custom docker environment"

    return "other"


def get_all_subclasses(cls):
    return set(cls.__subclasses__()).union(
        [s for c in cls.__subclasses__() for s in get_all_subclasses(c)]
    )


class SubqueryCount(Subquery):
    # Custom Count function to just perform simple count on any queryset without grouping.
    # Source: https://gist.github.com/bblanchon/9e158058fe360e93b1c5d5ce5310015e
    template = "(SELECT count(*) FROM (%(subquery)s) _count)"
    output_field = PositiveIntegerField()


class SubqueryAggregate(Subquery):
    template = '(SELECT %(function)s(_agg."%(column)s") FROM (%(subquery)s) _agg)'

    def __init__(self, queryset, column, output_field=None, **extra):
        if not output_field:
            # infer output_field from field type
            output_field = queryset.model._meta.get_field(column)
        super().__init__(
            queryset, output_field, column=column, function=self.function, **extra
        )


class SubquerySum(SubqueryAggregate):
    function = "SUM"


def text2html(value: str):
    return textile.textile(value.strip())
