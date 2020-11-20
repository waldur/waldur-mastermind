import re

import pytz
from django.utils import timezone

from waldur_mastermind.booking.utils import TimePeriod, get_offering_bookings
from waldur_mastermind.google.backend import GoogleCalendar


class SyncBookings:
    def __init__(self, offering):
        self.offering = offering
        self.credentials = offering.customer.serviceprovider.googlecredentials
        self.calendar_id = self.offering.secret_options.get(
            'google_calendar_id', 'primary'
        )
        self.backend = GoogleCalendar(
            tokens=self.credentials, calendar_id=self.calendar_id,
        )

    def get_bookings(self):
        def get_date(event_date):
            date = event_date.get('date', None)
            if not date:
                return event_date.get('dateTime', None)

            return date

        bookings = get_offering_bookings(self.offering)
        utc = pytz.UTC
        now = utc.localize(timezone.datetime.now())
        reg_exp = re.compile(r'[^a-z0-9]')
        waldur_bookings = [
            TimePeriod(b.start, b.end, re.sub(reg_exp, '', b.id))
            for b in bookings
            if b.start > now
        ]
        google_bookings = []

        for event in self.backend.get_events(calendar_id=self.calendar_id):
            start = event.get('start')
            if start:
                start = get_date(start)
            else:
                continue

            end = event.get('end')
            if end:
                end = get_date(end)
            else:
                continue

            google_bookings.append(TimePeriod(start, end, event['id']))

        need_to_delete = {b.id for b in google_bookings} - {
            b.id for b in waldur_bookings
        }
        need_to_update = []
        need_to_add = []

        for booking in waldur_bookings:
            google_booking = list(filter(lambda x: x.id == booking.id, google_bookings))
            if len(google_booking):
                google_booking = google_booking[0]
                if (
                    booking.start != google_booking.start
                    or booking.end != google_booking.end
                ):
                    need_to_update.append(booking)
            else:
                need_to_add.append(booking)

        return need_to_add, need_to_delete, need_to_update

    def sync_events(self):
        need_to_add, need_to_delete, need_to_update = self.get_bookings()

        for booking in need_to_add:
            self.backend.create_event(
                summary=self.offering.name,
                event_id=booking.id,
                start=booking.start,
                end=booking.end,
                calendar_id=self.calendar_id,
            )

        for booking_id in need_to_delete:
            self.backend.delete_event(
                calendar_id=self.calendar_id, event_id=booking_id,
            )

        for booking in need_to_update:
            self.backend.update_event(
                summary=self.offering.name,
                event_id=booking.id,
                start=booking.start,
                end=booking.end,
                calendar_id=self.calendar_id,
            )
