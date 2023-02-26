import datetime

from django.utils.functional import cached_property

from waldur_mastermind.booking import models
from waldur_mastermind.booking.utils import TimePeriod, get_offering_bookings
from waldur_mastermind.google.backend import GoogleCalendar


class SyncBookingsError(Exception):
    pass


class SyncBookings:
    def __init__(self, offering):
        self.offering = offering
        self.credentials = offering.customer.serviceprovider.googlecredentials
        self.backend = GoogleCalendar(tokens=self.credentials)

    @cached_property
    def calendar_id(self):
        calendar = self.offering.googlecalendar

        if not calendar.backend_id:
            self.create_calendar()
            calendar.refresh_from_db()
            # This code is usually called through the executor for automatically calendar creation,
            # and in case of an error,
            # it will register the error status for the calendar
        return calendar.backend_id

    def get_bookings(self):
        waldur_bookings = get_offering_bookings(self.offering)
        busy_slots = []
        google_bookings = []

        for event in self.backend.get_events(calendar_id=self.calendar_id):
            start = event.get('start')

            if start:
                start = start.get('dateTime', None) or start['date'] + 'T00:00:00'
            else:
                continue

            end = event.get('end')

            if end:
                end = end.get('dateTime', None) or datetime.datetime.fromisoformat(
                    end['date'] + 'T23:59:59'
                ) - datetime.timedelta(days=1)
            else:
                continue

            if 'booking' not in event['id']:
                busy_slots.append(
                    TimePeriod(
                        start,
                        end,
                        event['id'],
                        time_zone=self.backend.get_calendar_time_zone(self.calendar_id),
                    )
                )
                continue

            attendees = []
            if event.get('attendees'):
                for attendee in event.get('attendees'):
                    attendees.append(
                        {
                            'displayName': attendee['displayName'],
                            'email': attendee['email'],
                        }
                    )

            google_bookings.append(
                TimePeriod(
                    start,
                    end,
                    event['id'],
                    location=event.get('location'),
                    attendees=attendees,
                )
            )

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
                    or booking.location != google_booking.location
                    or booking.attendees != google_booking.attendees
                ):
                    need_to_update.append(booking)
            else:
                need_to_add.append(booking)

        return need_to_add, need_to_delete, need_to_update, busy_slots

    def sync_events(self):
        need_to_add, need_to_delete, need_to_update, busy_slots = self.get_bookings()

        for booking in need_to_add:
            self.backend.create_event(
                summary=booking.name or self.offering.name,
                event_id=booking.id,
                start=booking.start,
                end=booking.end,
                calendar_id=self.calendar_id,
                location=booking.location,
                attendees=booking.attendees,
            )

        for booking_id in need_to_delete:
            self.backend.delete_event(
                calendar_id=self.calendar_id,
                event_id=booking_id,
            )

        for booking in need_to_update:
            self.backend.update_event(
                summary=self.offering.name,
                event_id=booking.id,
                start=booking.start,
                end=booking.end,
                calendar_id=self.calendar_id,
                location=booking.location,
                attendees=booking.attendees,
            )

        models.BusySlot.objects.filter(offering=self.offering).delete()

        for slots in busy_slots:
            models.BusySlot.objects.create(
                offering=self.offering,
                start=slots.start,
                end=slots.end,
                backend_id=slots.id,
            )

    def update_calendar_name(self):
        self.backend.update_calendar(self.calendar_id, summary=self.offering.name)

    def share_calendar(self):
        self.backend.share_calendar(self.calendar_id)
        self.offering.googlecalendar.public = True
        self.offering.googlecalendar.save()

    def unshare_calendar(self):
        self.backend.unshare_calendar(self.calendar_id)
        self.offering.googlecalendar.public = False
        self.offering.googlecalendar.save()

    def create_calendar(self):
        calendar = self.offering.googlecalendar
        backend_id = self.backend.create_calendar(calendar_name=calendar.offering.name)
        calendar.backend_id = backend_id
        calendar.save()

    def clear_calendar(self):
        for event in self.backend.get_events(calendar_id=self.calendar_id):
            self.backend.delete_event(
                calendar_id=self.calendar_id,
                event_id=event['id'],
            )
