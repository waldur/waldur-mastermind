import datetime
import re

from django.utils.functional import cached_property

from .base import BaseReportLine

SLURM_UNIT_PATTERN = re.compile('(\d+)([KMGTP]?)')

SLURM_UNITS = {
    'K': 2**10,
    'M': 2**20,
    'G': 2**30,
    'T': 2**40,
}


def parse_int(value):
    """
    Convert 5K to 5000.
    """
    match = re.match(SLURM_UNIT_PATTERN, value)
    if not match:
        return 0
    value = int(match.group(1))
    unit = match.group(2)
    if unit:
        factor = SLURM_UNITS[unit]
    else:
        factor = 1
    return factor * value


def parse_duration(value):
    """
    Returns duration in minutes as an integer number.
    For example 00:01:00 is equal to 1
    """
    dt = datetime.datetime.strptime(value, '%H:%M:%S')
    delta = datetime.timedelta(hours=dt.hour, minutes=dt.minute, seconds=dt.second)
    return int(delta.total_seconds()) // 60


class SlurmReportLine(BaseReportLine):
    def __init__(self, line):
        self._parts = line.split('|')

    @cached_property
    def account(self):
        return self._parts[0].strip()

    @cached_property
    def user(self):
        return self._parts[3]

    @cached_property
    def cpu(self):
        return self.parse_field('cpu')

    @cached_property
    def gpu(self):
        return self.parse_field('gres/gpu')

    @cached_property
    def ram(self):
        return self.parse_field('mem')

    @cached_property
    def node(self):
        return self.parse_field('node')

    @cached_property
    def duration(self):
        return parse_duration(self._parts[2])

    @cached_property
    def _resources(self):
        pairs = self._parts[1].split(',')
        return dict(pair.split('=') for pair in pairs)

    def parse_field(self, field):
        if field not in self._resources:
            return 0
        return parse_int(self._resources[field])
