from __future__ import division

import decimal
import math

from django.utils.functional import cached_property

from .base import BaseReportLine


class MoabReportLine(BaseReportLine):
    def __init__(self, line):
        self._parts = line.split('|')

    @cached_property
    def account(self):
        return self._parts[0].strip()

    @cached_property
    def user(self):
        return self._parts[5]

    @cached_property
    def cpu(self):
        return self.get_int(1)

    @cached_property
    def gpu(self):
        return self.get_int(2)

    @cached_property
    def ram(self):
        return self.get_int(3)

    @cached_property
    def duration(self):
        # convert seconds to minutes
        return int(math.ceil(self.get_int(4) / 60))

    @cached_property
    def charge(self):
        return decimal.Decimal(self._parts[6])

    @cached_property
    def node(self):
        return int(self._parts[7])

    def get_int(self, index):
        value = self._parts[index] or 0
        return int(value)
