from celery import shared_task

from waldur_core.core import utils as core_utils

from . import calendar, utils


@shared_task(
    name='waldur_mastermind.booking.send_notifications_about_upcoming_bookings'
)
def send_notifications_about_upcoming_bookings():
    for info in utils.get_info_about_upcoming_bookings():
        emails = [info['user'].email]
        core_utils.broadcast_mail('booking', 'notification', info, emails)


@shared_task(name='waldur_mastermind.booking.sync_bookings_to_google_calendar')
def sync_bookings_to_google_calendar(serialized_google_calendar):
    google_calendar = core_utils.deserialize_instance(serialized_google_calendar)
    sync_bookings = calendar.SyncBookings(google_calendar.offering)
    sync_bookings.sync_events()


@shared_task(name='waldur_mastermind.booking.share_google_calendar')
def share_google_calendar(serialized_google_calendar):
    google_calendar = core_utils.deserialize_instance(serialized_google_calendar)
    sync_bookings = calendar.SyncBookings(google_calendar.offering)
    sync_bookings.share_calendar()


@shared_task(name='waldur_mastermind.booking.unshare_google_calendar')
def unshare_google_calendar(serialized_google_calendar):
    google_calendar = core_utils.deserialize_instance(serialized_google_calendar)
    sync_bookings = calendar.SyncBookings(google_calendar.offering)
    sync_bookings.unshare_calendar()


@shared_task(name='waldur_mastermind.booking.update_calendar_name')
def rename_google_calendar(serialized_google_calendar):
    google_calendar = core_utils.deserialize_instance(serialized_google_calendar)
    sync_bookings = calendar.SyncBookings(google_calendar.offering)
    sync_bookings.update_calendar_name()
