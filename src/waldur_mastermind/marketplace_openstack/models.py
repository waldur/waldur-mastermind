from django.conf import settings
from django.db import models
from model_utils.models import TimeStampedModel

from waldur_openstack import models as openstack_models


class InstanceMetricSample(models.Model):
    """What the last metrics poll read from an instance's hypervisor counters.

    Nova gives running totals: the CPU time of the server's process and, per
    disk and port, the bytes and requests since it was attached or the process
    started. Each poll counts what they added since this one, device by
    device, so that a device coming or going doesn't count the others again.
    The CPU time and the poll time together also give the utilization over
    the interval.
    """

    instance = models.OneToOneField(
        openstack_models.Instance,
        on_delete=models.CASCADE,
        related_name="metric_sample",
    )
    sampled_at = models.DateTimeField()
    cpu_time = models.DecimalField(
        max_digits=20, decimal_places=6, help_text="Seconds."
    )
    counters = models.JSONField(
        default=dict,
        help_text="Each port's and disk's counters as last read, by device.",
    )

    def __str__(self):
        return f"{self.instance} @ {self.sampled_at}"


class InstanceMetricSchedule(TimeStampedModel):
    """Faster polling someone who manages an instance asked for, for a while.

    Polling every instance often costs Nova API calls and stored points, so the
    offering's interval is slow; this lets one instance be watched closely
    while it matters, and lapses on its own.
    """

    instance = models.OneToOneField(
        openstack_models.Instance,
        on_delete=models.CASCADE,
        related_name="metric_schedule",
    )
    interval = models.PositiveIntegerField(help_text="Minutes.")
    until = models.DateTimeField()
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    def __str__(self):
        return f"{self.instance} every {self.interval} min until {self.until}"
