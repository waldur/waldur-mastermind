from celery import shared_task

from waldur_core.core import utils as core_utils

from . import utils


@shared_task(name='waldur_mastermind.booking.send_notifications_about_upcoming_bookings')
def send_notifications_about_upcoming_bookings():
    for info in utils.get_info_about_upcoming_bookings():
        emails = [info['user'].email]
        core_utils.broadcast_mail('booking', 'notification', info, emails)
