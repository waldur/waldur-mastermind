import datetime

from calendar import monthrange

from django.conf import settings
from django.core.mail import EmailMessage
from django.utils import timezone

from waldur_core.core import utils as core_utils


def get_current_month():
    return timezone.now().month


def get_current_year():
    return timezone.now().year


def get_current_month_end():
    return core_utils.month_end(timezone.now())


def get_current_month_start():
    return core_utils.month_start(timezone.now())


def get_full_days(start, end):
    seconds_in_day = 24 * 60 * 60
    full_days, extra_seconds = divmod((end - start).total_seconds(), seconds_in_day)
    if extra_seconds > 0:
        full_days += 1

    return int(full_days)


def get_current_month_days():
    now = timezone.now()
    range = monthrange(now.year, now.month)
    return range[1]


def check_past_date(year, month, day=None):
    day = day or 1

    try:
        return datetime.date(year=int(year), month=int(month), day=int(day)) <= timezone.now().date()
    except ValueError:
        return False


def send_mail_attachment(subject, body, to, filename, attach_text, content_type='text/plain', from_email=None):
    from_email = from_email or settings.DEFAULT_FROM_EMAIL
    email = EmailMessage(
        subject=subject,
        body=body,
        to=to,
        from_email=from_email
    )
    email.attach(filename, attach_text, content_type)
    return email.send()
