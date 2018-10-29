import calendar
from collections import OrderedDict
import datetime
import importlib
from itertools import chain
from operator import itemgetter
import os
import re
import time

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.core.management import call_command
from django.db.models import F
from django.db.models.sql.query import get_order_dir
from django.http import QueryDict
from django.template.loader import render_to_string
from django.urls import resolve
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.utils.encoding import force_text
from rest_framework.settings import api_settings


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


def format_time_and_value_to_segment_list(time_and_value_list, segments_count, start_timestamp,
                                          end_timestamp, average=False):
    """
    Format time_and_value_list to time segments

    Parameters
    ^^^^^^^^^^
    time_and_value_list: list of tuples
        Have to be sorted by time
        Example: [(time, value), (time, value) ...]
    segments_count: integer
        How many segments will be in result
    Returns
    ^^^^^^^
    List of dictionaries
        Example:
        [{'from': time1, 'to': time2, 'value': sum_of_values_from_time1_to_time2}, ...]
    """
    segment_list = []
    time_step = (end_timestamp - start_timestamp) / segments_count
    for i in range(segments_count):
        segment_start_timestamp = start_timestamp + time_step * i
        segment_end_timestamp = segment_start_timestamp + time_step
        value_list = [
            value for time, value in time_and_value_list
            if time >= segment_start_timestamp and time < segment_end_timestamp]
        segment_value = sum(value_list)
        if average and len(value_list) != 0:
            segment_value /= len(value_list)

        segment_list.append({
            'from': segment_start_timestamp,
            'to': segment_end_timestamp,
            'value': segment_value,
        })
    return segment_list


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
    return timezone.make_aware(datetime.datetime(day=1, month=date.month, year=date.year))


def month_end(date):
    days_in_month = calendar.monthrange(date.year, date.month)[1]
    last_day_of_month = datetime.date(month=date.month, year=date.year, day=days_in_month)
    last_second_of_month = datetime.datetime.combine(last_day_of_month, datetime.time.max)
    return timezone.make_aware(last_second_of_month, timezone.get_current_timezone())


def pwgen(pw_len=16):
    """ Generate a random password with the given length.
        Allowed chars does not have "I" or "O" or letters and
        digits that look similar -- just to avoid confusion.
    """
    return get_random_string(pw_len, 'abcdefghjkmnpqrstuvwxyz'
                                     'ABCDEFGHJKLMNPQRSTUVWXYZ'
                                     '23456789')


def serialize_instance(instance):
    """ Serialize Django model instance """
    model_name = force_text(instance._meta)
    return '{}:{}'.format(model_name, instance.pk)


def deserialize_instance(serialized_instance):
    """ Deserialize Django model instance """
    model_name, pk = serialized_instance.split(':')
    model = apps.get_model(model_name)
    return model._default_manager.get(pk=pk)


def serialize_class(cls):
    """ Serialize Python class """
    return '{}:{}'.format(cls.__module__, cls.__name__)


def deserialize_class(serilalized_cls):
    """ Deserialize Python class """
    module_name, cls_name = serilalized_cls.split(':')
    module = importlib.import_module(module_name)
    return getattr(module, cls_name)


def clear_url(url):
    """ Remove domain and protocol from url """
    if url.startswith('http'):
        return '/' + url.split('/', 3)[-1]
    return url


def get_model_from_resolve_match(match):
    queryset = match.func.cls.queryset
    if queryset is not None:
        return queryset.model
    else:
        return match.func.cls.model


def instance_from_url(url, user=None):
    """ Restore instance from URL """
    # XXX: This circular dependency will be removed then filter_queryset_for_user
    # will be moved to model manager method
    from waldur_core.structure.managers import filter_queryset_for_user

    url = clear_url(url)
    match = resolve(url)
    model = get_model_from_resolve_match(match)
    queryset = model.objects.all()
    if user is not None:
        queryset = filter_queryset_for_user(model.objects.all(), user)
    return queryset.get(**match.kwargs)


def get_detail_view_name(model):
    if model is NotImplemented:
        raise AttributeError('Cannot get detail view name for not implemented model')

    if hasattr(model, 'get_url_name') and callable(model.get_url_name):
        return '%s-detail' % model.get_url_name()

    return '%s-detail' % model.__name__.lower()


def get_list_view_name(model):
    if model is NotImplemented:
        raise AttributeError('Cannot get list view name for not implemented model')

    if hasattr(model, 'get_url_name') and callable(model.get_url_name):
        return '%s-list' % model.get_url_name()

    return '%s-list' % model.__name__.lower()


def get_fake_context():
    user = get_user_model()()
    request = type('R', (object,), {'method': 'GET', 'user': user, 'query_params': QueryDict()})
    return {'request': request, 'user': user}


def camel_case_to_underscore(name):
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()


def silent_call(name, *args, **options):
    call_command(name, stdout=open(os.devnull, 'w'), *args, **options)


def broadcast_mail(app, event_type, context, recipient_list):
    """
    Shorthand to format email message from template file and sent it to all recipients.

    It is assumed that there are there are 3 templates available for event type in application.
    For example, if app is 'experts' and event_type is 'new_request', then there should be 3 files:

    1) experts/new_request_subject.txt is template for email subject
    2) experts/new_request_message.txt is template for email body as text
    3) experts/new_request_message.html is template for email body as HTML

    By default, built-in Django send_mail is used, all members
    of the recipient list will see the other recipients in the 'To' field.
    Contrary to this, we're using explicit loop in order to ensure that
    recipients would NOT see the other recipients.

    :param app: prefix for template filename.
    :param event_type: postfix for template filename.
    :param context: dictionary passed to the template for rendering.
    :param recipient_list: list of strings, each an email address.
    """

    subject_template = '%s/%s_subject.txt' % (app, event_type)
    text_template = '%s/%s_message.txt' % (app, event_type)
    html_template = '%s/%s_message.html' % (app, event_type)

    subject = render_to_string(subject_template, context).strip()
    text_message = render_to_string(text_template, context)
    html_message = render_to_string(html_template, context)

    for recipient in recipient_list:
        send_mail(subject, text_message, settings.DEFAULT_FROM_EMAIL, [recipient], html_message=html_message)


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
    descending = True if order == 'DESC' else False

    if descending:
        return queryset.order_by(F(col).desc(nulls_last=True))
    else:
        return queryset.order_by(F(col).asc(nulls_first=True))
