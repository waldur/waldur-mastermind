import datetime

from django.utils.dateparse import parse_datetime


class TimePeriod(object):
    def __init__(self, start, end):
        if not isinstance(start, datetime.datetime):
            start = parse_datetime(start)

        if not isinstance(end, datetime.datetime):
            end = parse_datetime(end)

        self.start = start
        self.end = end


def is_interval_in_schedules(interval, schedules):
    for s in schedules:
        if interval.start >= s.start:
            if interval.end <= s.end:
                return True

    return False
