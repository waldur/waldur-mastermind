import calendar
import datetime
import functools
import importlib
import ipaddress
import logging
import os
import re
import socket
import time
import unicodedata
import uuid
import warnings
from itertools import chain, groupby
from secrets import choice
from string import ascii_letters, digits
from urllib.parse import urlsplit

import jwt
import requests
import textile
from constance import config
from dateutil.relativedelta import relativedelta
from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import F, Subquery
from django.db.models.fields import PositiveIntegerField
from django.db.models.sql.query import get_order_dir
from django.http import HttpRequest, QueryDict
from django.template import Context
from django.template.loader import get_template, render_to_string
from django.urls import resolve
from django.utils import timezone
from django.utils.crypto import get_random_string
from requests.packages.urllib3 import exceptions
from rest_framework.serializers import ValidationError
from rest_framework.settings import api_settings
from ua_parser import user_agent_parser

from waldur_core.structure.notifications import NOTIFICATIONS

logger = logging.getLogger(__name__)


def flatten(*xs):
    return tuple(chain.from_iterable(xs))


def datetime_to_timestamp(datetime):
    return int(time.mktime(datetime.timetuple()))


def timestamp_to_datetime(timestamp, replace_tz=True):
    dt = datetime.datetime.fromtimestamp(int(timestamp))
    if replace_tz:
        dt = dt.replace(tzinfo=timezone.get_current_timezone())
    return dt


def timeshift(**kwargs):
    return timezone.now().replace(microsecond=0) + datetime.timedelta(**kwargs)


def calculate_duration_months(start_date, end_date):
    """Calculate duration in whole months, rounding up partial months.

    Used by prepaid billing, cost estimation, and the site agent.
    A partial month at the end counts as a full month.
    """
    delta = relativedelta(end_date, start_date)
    months = delta.years * 12 + delta.months
    if delta.days > 0:
        months += 1
    return max(1, months)


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
        pw_len, "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
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


def get_fake_context(user=None):
    from waldur_core.core.models import User

    if not user:
        user = User()
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
    subject: str,
    body: str,
    to: list[str] | tuple[str, ...],
    from_email: str | None = None,
    html_message: str | None = None,
    filename: str | None = None,
    attachment: str | None = None,
    content_type: str = "text/plain",
    bcc: list[str] | None = None,
    reply_to: str | None = None,
    fail_silently: bool = False,
) -> int:
    from waldur_core.logging.models import EmailLog

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

    result = email.send(fail_silently=fail_silently)

    # Extend emails with BCC recipients if provided
    logged_emails = list(to)
    if bcc is not None:
        logged_emails.extend(bcc)

    EmailLog.objects.create(
        subject=subject,
        body=email.body,
        emails=logged_emails,
    )
    return result


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

    It is assumed that there are 3 templates available for event type in application.
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
    :param bcc: list of emails for sending as bcc
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


def validate_uuid(value):
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ValidationError("Invalid UUID format")


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


def make_random_password(length=10):
    alphabet = ascii_letters + digits
    return "".join(choice(alphabet) for i in range(length))


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


def format_homeport_link(format_str="", **kwargs):
    link = config.HOMEPORT_URL + format_str
    return link.format(**kwargs)


# Usernames of special accounts acting on behalf of the system rather than
# a real person. Their email is SITE_EMAIL, so user-facing notifications
# addressed to them must be skipped.
ROBOT_USERNAMES = ("system_robot", "openportal_robot")


def is_robot_user(user) -> bool:
    return user.username in ROBOT_USERNAMES


def get_system_robot():
    from waldur_core.core import models

    # make sure that system_robot is always active and staff
    robot_user, created = models.User.all_objects.get_or_create(
        username="system_robot", defaults={"is_staff": True, "is_active": True}
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


def get_ip_address(request: HttpRequest) -> str | None:
    """
    Correct IP address is expected as first element of HTTP_X_FORWARDED_FOR or REMOTE_ADDR
    """
    if "HTTP_X_FORWARDED_FOR" in request.META:
        return request.META["HTTP_X_FORWARDED_FOR"].split(",")[0].strip()
    elif "REMOTE_ADDR" in request.META:
        return request.META["REMOTE_ADDR"]
    return None


def _strip_port(value: str) -> str:
    """Drop a ``host:port`` suffix, leaving a bare address.

    Azure Application Gateway writes ``1.2.3.4:5678`` into X-Forwarded-For, and
    under fail-closed enforcement an unparseable hop denies a valid token. Only
    the two unambiguous forms are stripped: a bracketed IPv6 literal, and a
    single-colon IPv4 pair — a bare IPv6 address is itself full of colons, so
    the colon count is what stops us truncating one into garbage. A non-numeric
    port is left in place so the address fails to parse rather than being
    silently accepted.
    """
    if value.startswith("["):
        host, separator, port = value.partition("]:")
        if separator and port.isdigit():
            return host[1:]
        return value
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            return value
        if port.isdigit():
            return host
    return value


def _normalize_ip(
    value: str | None,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an address, unwrapping IPv4-mapped IPv6, or return None.

    A dual-stack listener reports IPv4 clients as ``::ffff:203.0.113.5``.
    Unwrapping means such a client matches an IPv4 ACL entry and gets logged
    under its real address, instead of being denied on a version mismatch.
    """
    if not value:
        return None
    try:
        addr = ipaddress.ip_address(_strip_port(value))
    except ValueError:
        return None
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr


def ip_in_networks(address: str | None, networks: list[str]) -> bool:
    """Return True when ``address`` falls inside any of ``networks``.

    Malformed input never matches — this backs an allowlist, so anything we
    cannot parse must not be treated as permitted.
    """
    if not address or not networks:
        return False
    addr = _normalize_ip(address)
    if addr is None:
        return False
    for entry in networks:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def normalize_ip_address(value: str | None) -> str | None:
    """Canonical string form of an IP address, or None if it does not parse.

    Unwraps IPv4-mapped IPv6 and strips a ``host:port`` suffix, so the
    ingress-provided address is stored and logged in one canonical form.
    Anything unparseable becomes None — callers treat that as "no address"
    and, for a security control, fail closed. Keeping the value clean also
    matters because it flows on into event-message ``.format()`` templates,
    cache keys and ``last_used_ip`` (an inet column) unescaped.
    """
    address = _normalize_ip(value)
    return str(address) if address is not None else None


def merge_access_subnets(inet_values):
    """Collapse CIDR strings into the minimal list of networks.

    Adjacent or overlapping networks are merged (per IP version) using
    ``ipaddress.collapse_addresses``. Invalid or null values are skipped.
    Returns a list of ``ip_network`` objects sorted by version and address.
    """
    networks = []
    for value in inet_values:
        if value is None:
            continue
        try:
            networks.append(ipaddress.ip_network(value))
        except ValueError:
            continue

    networks.sort(key=lambda n: (n.version, n.network_address))

    merged = []
    for _version, version_networks in groupby(networks, key=lambda n: n.version):
        merged.extend(ipaddress.collapse_addresses(list(version_networks)))
    return merged


def validate_access_subnet_for_user(value, user):
    """Normalise and validate an access-subnet CIDR for the acting user.

    Non-staff users may only enter a single host, so a bare address is widened
    to ``/32`` (``/128`` for IPv6) and anything wider is rejected: these lists
    are how a consumer grants itself access, and an unbounded mask would let it
    open far more than intended. Staff may enter any width except ``/0``, which
    matches every address and would silently neutralise every restriction built
    on these entries.

    Networks with host bits set are rejected rather than silently masked —
    quietly turning ``203.0.113.5/24`` into ``203.0.113.0/24`` would grant a
    whole range where a single host was written.

    Returns the normalised CIDR string.
    """
    try:
        network = ipaddress.ip_network(str(value), strict=True)
    except ValueError as e:
        raise ValidationError(str(e))

    if network.prefixlen == 0:
        raise ValidationError("A /0 mask is not allowed: it matches every address.")

    if not user.is_staff and network.prefixlen != network.max_prefixlen:
        raise ValidationError(
            "Only a single IP address (/%s) is allowed." % network.max_prefixlen
        )

    return str(network)


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


def remove_duplicate_hyphens(text):
    return re.sub("-+", "-", text)


def get_valid_template_paths():
    valid_template_paths = set()
    for section_key, notifications in NOTIFICATIONS.items():
        for notification in notifications:
            for template in notification.get("templates", []):
                valid_template_paths.add(f"{section_key}/{template['path']}")
    return valid_template_paths


def get_valid_notification_keys():
    valid_keys = set()
    for section_key, notifications in NOTIFICATIONS.items():
        for notification in notifications:
            valid_keys.add(f"{section_key}.{notification['path']}")
    return valid_keys


# Quarterly date utilities
def get_current_quarter():
    """Get current quarter (1-4) based on current month."""
    return (timezone.now().month - 1) // 3 + 1


def get_current_quarter_start():
    """Get start of current quarter."""
    now = timezone.now()
    quarter = get_current_quarter()
    quarter_start_month = (quarter - 1) * 3 + 1
    return now.replace(
        month=quarter_start_month, day=1, hour=0, minute=0, second=0, microsecond=0
    )


def get_current_quarter_end():
    """Get end of current quarter."""
    now = timezone.now()
    quarter = get_current_quarter()
    quarter_end_month = quarter * 3
    # Get last day of quarter end month
    last_day = calendar.monthrange(now.year, quarter_end_month)[1]
    return now.replace(
        month=quarter_end_month,
        day=last_day,
        hour=23,
        minute=59,
        second=59,
        microsecond=999999,
    )


def get_quarter_start(date):
    """Get start of quarter for given date."""
    quarter = (date.month - 1) // 3 + 1
    quarter_start_month = (quarter - 1) * 3 + 1
    return date.replace(
        month=quarter_start_month, day=1, hour=0, minute=0, second=0, microsecond=0
    )


def get_quarter_end(date):
    """Get end of quarter for given date."""
    quarter = (date.month - 1) // 3 + 1
    quarter_end_month = quarter * 3
    # Get last day of quarter end month
    last_day = calendar.monthrange(date.year, quarter_end_month)[1]
    return date.replace(
        month=quarter_end_month,
        day=last_day,
        hour=23,
        minute=59,
        second=59,
        microsecond=999999,
    )


def get_full_quarters(start, end):
    """Calculate number of full quarters between start and end dates."""
    start_quarter = get_quarter_start(start)
    end_quarter = get_quarter_end(end)

    # Calculate quarters between dates
    quarters = 0
    current = start_quarter
    while current <= end_quarter:
        if current >= start and current <= end:
            quarters += 1
        # Move to next quarter
        if current.month <= 9:
            current = current.replace(month=current.month + 3)
        else:
            current = current.replace(year=current.year + 1, month=current.month - 9)

    return quarters


# Topological sort (Django removed django.utils.topological_sort in 5.0)


class CyclicDependencyError(ValueError):
    pass


def topological_sort_as_sets(dependency_graph):
    """
    Variation of Kahn's algorithm (1962) that returns sets.

    Take a dependency graph as a dictionary of node => dependencies.

    Yield sets of items in topological order, where the first set contains
    all nodes without dependencies, and each following set contains all
    nodes that may depend on the nodes only in the previously yielded sets.
    """
    todo = dependency_graph.copy()
    while todo:
        current = {node for node, deps in todo.items() if not deps}

        if not current:
            raise CyclicDependencyError(
                "Cyclic dependency in graph: {}".format(
                    ", ".join(repr(x) for x in todo.items())
                )
            )

        yield current

        todo = {
            node: (dependencies - current)
            for node, dependencies in todo.items()
            if node not in current
        }


def stable_topological_sort(nodes, dependency_graph):
    result = []
    for layer in topological_sort_as_sets(dependency_graph):
        for node in nodes:
            if node in layer:
                result.append(node)
    return result


def chunked_queryset(queryset, chunk_size=100, max_records=None):
    """Iterate a queryset in client-side chunks using primary-key pagination.

    Avoids server-side cursors (which ``QuerySet.iterator(chunk_size=...)``
    uses on psycopg3) — those break with PgBouncer transaction pooling
    and load-balanced PostgreSQL connections, since a cursor opened on
    one backend connection may not exist when the next fetch lands on a
    different one. Each chunk here is a fresh ``LIMIT``-bounded query
    that any pooled connection can serve.

    Memory stays bounded by ``chunk_size``. When ``max_records`` is
    set, iteration stops after yielding that many rows and emits a
    warning — use this as a safety net against accidentally walking
    a table that has grown unexpectedly large.
    """
    queryset = queryset.order_by("pk")
    last_pk = None
    yielded = 0
    while True:
        chunk_qs = queryset
        if last_pk is not None:
            chunk_qs = chunk_qs.filter(pk__gt=last_pk)
        chunk = list(chunk_qs[:chunk_size])
        if not chunk:
            return
        for obj in chunk:
            if max_records is not None and yielded >= max_records:
                logger.warning(
                    "chunked_queryset reached max_records=%d on %s; "
                    "iteration truncated",
                    max_records,
                    queryset.model.__name__,
                )
                return
            yield obj
            yielded += 1
        if len(chunk) < chunk_size:
            return
        last_pk = chunk[-1].pk


def validate_outbound_url(url: str) -> None:
    """
    Reject URLs that resolve to private, loopback, link-local, multicast,
    reserved, or otherwise non-public addresses. Use as a model field
    validator on user-supplied destination URLs (webhooks, callbacks,
    image-import sources) to defeat SSRF.

    Raises django.core.exceptions.ValidationError on rejection. The check
    is best-effort against DNS rebinding — call this again immediately
    before connecting (or use IP-pinned outbound HTTP) to defeat
    time-of-check / time-of-use bypasses.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise DjangoValidationError(
            f"URL scheme must be http or https, got {parsed.scheme!r}."
        )
    if not parsed.hostname:
        raise DjangoValidationError("URL must include a hostname.")

    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise DjangoValidationError(
            f"Hostname {parsed.hostname!r} could not be resolved: {exc}."
        )

    for *_, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError):
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise DjangoValidationError(
                f"URL host {parsed.hostname!r} resolves to a non-routable "
                f"address ({ip}); outbound webhook destinations must be public."
            )
