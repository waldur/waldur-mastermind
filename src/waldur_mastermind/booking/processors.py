from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import datetime_re, parse_datetime
from django.utils.translation import gettext_lazy as _
from rest_framework.serializers import ValidationError

from waldur_mastermind.booking.models import BookingSlot
from waldur_mastermind.booking.utils import (
    get_offering_bookings_and_busy_slots,
    get_other_offering_booking_requests,
)
from waldur_mastermind.marketplace import processors

from .utils import TimePeriod, is_interval_in_schedules


class BookingCreateProcessor(processors.BaseOrderProcessor):
    def process_order(self, user):
        with transaction.atomic():
            for slot in self.order.attributes.get("schedules"):
                BookingSlot.objects.create(
                    resource=self.order.resource,
                    start=slot["start"],
                    end=slot["end"],
                )

    def validate_order(self, request):
        schedules = self.order.attributes.get("schedules")

        # We check that the schedule is set.
        if not schedules:
            raise ValidationError(_("Schedules are required."))

        if not len(schedules):
            raise ValidationError(_("Schedules are required."))

        for period in schedules:
            try:
                start = period["start"]
                end = period["end"]

                if not start or not end:
                    raise ValidationError(
                        _("Value 'start' or 'end' does not exist in schedules item.")
                    )

            except KeyError:
                raise ValidationError(
                    _("Key 'start' or 'end' does not exist in schedules item.")
                )

            for value in [start, end]:
                match = datetime_re.match(value)
                kw = match.groupdict()
                if list(
                    filter(
                        lambda x: not kw[x],
                        ["hour", "month", "second", "year", "tzinfo", "day", "minute"],
                    )
                ):
                    raise ValidationError(
                        _("The value %s does not match the format.") % value
                    )

            if parse_datetime(start) < timezone.now():
                raise ValidationError(_("Past slots are not available for selection."))

        # Check that the schedule is available for the offering.
        offering = self.order.offering
        offering_schedules = offering.attributes.get("schedules", [])

        for period in schedules:
            if not is_interval_in_schedules(
                TimePeriod(period["start"], period["end"]),
                [TimePeriod(i["start"], i["end"]) for i in offering_schedules],
            ):
                raise ValidationError(
                    _(
                        "Time period from %s to %s is not available for selected offering."
                    )
                    % (period["start"], period["end"])
                )

        # Check that there are no other bookings.
        bookings = get_offering_bookings_and_busy_slots(offering)
        for period in schedules:
            if is_interval_in_schedules(
                TimePeriod(period["start"], period["end"]), bookings
            ):
                raise ValidationError(
                    _("Time period from %s to %s is not available.")
                    % (period["start"], period["end"])
                )

        # Check that there are no other booking requests.
        booking_requests = get_other_offering_booking_requests(self.order)
        for period in schedules:
            if is_interval_in_schedules(
                TimePeriod(period["start"], period["end"]), booking_requests
            ):
                raise ValidationError(
                    _(
                        "Time period from %s to %s is not available. Other booking request exists."
                    )
                    % (period["start"], period["end"])
                )


class BookingDeleteProcessor(processors.BasicDeleteResourceProcessor):
    pass
