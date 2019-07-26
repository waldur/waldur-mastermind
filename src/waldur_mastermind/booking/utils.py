from django.utils import timezone


class TimePeriod(object):
    def __init__(self, start, end):
        if not isinstance(start, timezone.datetime):
            start = timezone.datetime.strptime(start, '%Y-%d-%mT%H:%M:%S')

        if not isinstance(end, timezone.datetime):
            end = timezone.datetime.strptime(end, '%Y-%d-%mT%H:%M:%S')

        self.start = start
        self.end = end


def interval_in_schedules(interval, schedules):
    for s in schedules:
        if interval.start >= s.start:
            if interval.end <= s.end:
                return True

    return
