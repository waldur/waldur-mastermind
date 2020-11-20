from celery import shared_task

from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import models as marketplace_models

from . import calendar, utils


@shared_task(
    name='waldur_mastermind.booking.send_notifications_about_upcoming_bookings'
)
def send_notifications_about_upcoming_bookings():
    for info in utils.get_info_about_upcoming_bookings():
        emails = [info['user'].email]
        core_utils.broadcast_mail('booking', 'notification', info, emails)


@shared_task(name='waldur_mastermind.booking.sync_bookings_to_google_calendar')
def sync_bookings_to_google_calendar(offering_uuid):
    offering = marketplace_models.Offering.objects.get(uuid=offering_uuid)
    sync_bookings = calendar.SyncBookings(offering)
    sync_bookings.sync_events()
