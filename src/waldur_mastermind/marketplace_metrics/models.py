from django.conf import settings
from django.contrib.postgres.indexes import BrinIndex
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q, UniqueConstraint
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models

from . import enums

# Dotted lowercase names, as OpenTelemetry semantic conventions write them:
# "education.course.completions".
METRIC_KEY_VALIDATOR = RegexValidator(
    r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$",
    _("Use lowercase dotted names, for example education.course.completions."),
)
ATTRIBUTE_KEY_VALIDATOR = RegexValidator(
    r"^[a-z][a-z0-9_.]*$",
    _("Use lowercase names, for example course or support.queue."),
)


class RetentionPolicy(core_models.UuidMixin, core_models.NameMixin, TimeStampedModel):
    """How long a metric's data is kept, tier by tier.

    Raw points answer recent, detailed questions; hourly and daily roll-ups
    answer long-range ones at a fraction of the size. Empty daily_days keeps
    daily roll-ups forever.
    """

    raw_days = models.PositiveIntegerField(
        default=90, validators=[MinValueValidator(1)]
    )
    hourly_days = models.PositiveIntegerField(
        default=400, validators=[MinValueValidator(1)]
    )
    daily_days = models.PositiveIntegerField(
        null=True, blank=True, help_text=_("Empty keeps daily roll-ups forever.")
    )

    class Meta:
        ordering = ["name", "id"]
        verbose_name_plural = _("retention policies")
        constraints = [
            # The default policy is found by name; two of them would make
            # every lookup, and the seeding after migrate, ambiguous.
            UniqueConstraint(fields=["name"], name="unique_retention_policy_name"),
        ]

    def __str__(self):
        return self.name


class MetricDefinition(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    TimeStampedModel,
):
    """A metric in the catalogue: what it measures and how it may be reported.

    A definition is declared before anything reports it, so a typo in a
    service never creates a new metric and every series stays bounded.
    Global definitions (no owner) are curated by staff; a service provider
    may add private ones for its own offerings.
    """

    key = models.CharField(
        max_length=100,
        validators=[METRIC_KEY_VALIDATOR],
        help_text=_("Name services report against. Cannot be changed."),
    )
    unit = models.CharField(
        max_length=30,
        blank=True,
        help_text=_("UCUM unit, for example h, % or {learners}."),
    )
    kind = models.CharField(
        max_length=10,
        choices=enums.MetricKinds.CHOICES,
        default=enums.MetricKinds.GAUGE,
    )
    good_direction = models.CharField(
        max_length=10,
        choices=enums.GoodDirections.CHOICES,
        default=enums.GoodDirections.NEUTRAL,
    )
    attribute_keys = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Attribute names a point may carry, for example course."),
    )
    max_attribute_values = models.PositiveIntegerField(
        default=100,
        validators=[MinValueValidator(1)],
        help_text=_("Distinct values one attribute may take per resource."),
    )
    retention_policy = models.ForeignKey(
        RetentionPolicy,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="definitions",
        help_text=_("Empty uses the default retention policy."),
    )
    owner_customer = models.ForeignKey(
        structure_models.Customer,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="metric_definitions",
        help_text=_("Service provider the definition is private to; empty is global."),
    )
    state = models.CharField(
        max_length=20,
        choices=enums.DefinitionStates.CHOICES,
        default=enums.DefinitionStates.ACTIVE,
    )

    class Permissions:
        customer_path = "owner_customer"

    class Meta:
        ordering = ["key", "id"]
        constraints = [
            UniqueConstraint(
                fields=["key"],
                condition=Q(owner_customer__isnull=True),
                name="unique_global_metric_key",
            ),
            UniqueConstraint(
                fields=["key", "owner_customer"],
                condition=Q(owner_customer__isnull=False),
                name="unique_private_metric_key",
            ),
        ]

    def __str__(self):
        return self.key

    def has_data(self):
        return MetricSeries.objects.filter(offering_metric__definition=self).exists()


class OfferingMetric(core_models.UuidMixin, TimeStampedModel):
    """A catalogue metric an offering reports for the resources it provides."""

    offering = models.ForeignKey(
        marketplace_models.Offering, on_delete=models.CASCADE, related_name="metrics"
    )
    definition = models.ForeignKey(
        MetricDefinition, on_delete=models.PROTECT, related_name="offering_metrics"
    )
    display_name = models.CharField(
        max_length=150, blank=True, help_text=_("Empty shows the definition's name.")
    )
    project_aggregation = models.CharField(
        max_length=10,
        choices=enums.ProjectAggregations.CHOICES,
        default=enums.ProjectAggregations.SUM,
        help_text=_(
            "How the figures of a project's resources combine. An average is "
            "unweighted: every resource counts once."
        ),
    )
    state = models.CharField(
        max_length=20,
        choices=enums.OfferingMetricStates.CHOICES,
        default=enums.OfferingMetricStates.ACTIVE,
    )

    class Permissions:
        customer_path = "offering__customer"

    class Meta:
        ordering = ["offering", "definition__key", "id"]
        unique_together = ("offering", "definition")

    def __str__(self):
        return f"{self.offering.name} / {self.definition.key}"

    @property
    def name(self):
        return self.display_name or self.definition.name


class MetricSeries(TimeStampedModel):
    """One time series: a resource's points for one metric and attribute set.

    For a cumulative counter it also holds the last reported total, which is
    what turns the next total into an increment.
    """

    resource = models.ForeignKey(
        marketplace_models.Resource,
        on_delete=models.CASCADE,
        related_name="metric_series",
    )
    offering_metric = models.ForeignKey(
        OfferingMetric, on_delete=models.CASCADE, related_name="series"
    )
    attributes = models.JSONField(default=dict, blank=True)
    attributes_hash = models.CharField(max_length=64)
    start_time = models.DateTimeField(null=True, blank=True)
    last_value = models.DecimalField(
        max_digits=24, decimal_places=6, null=True, blank=True
    )
    last_timestamp = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = _("metric series")
        constraints = [
            UniqueConstraint(
                fields=["resource", "offering_metric", "attributes_hash"],
                name="unique_metric_series",
            ),
        ]

    def __str__(self):
        return (
            f"{self.resource} / {self.offering_metric.definition.key} {self.attributes}"
        )


class MetricPoint(models.Model):
    """A raw point. Nothing references this table, so it can be partitioned."""

    id = models.BigAutoField(primary_key=True)
    series = models.ForeignKey(
        MetricSeries, on_delete=models.CASCADE, related_name="points"
    )
    timestamp = models.DateTimeField()
    value = models.DecimalField(max_digits=24, decimal_places=6)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["series", "timestamp"], name="unique_metric_point"
            ),
        ]
        indexes = [BrinIndex(fields=["timestamp"], name="metric_point_ts_brin")]

    def __str__(self):
        return f"{self.series_id} @ {self.timestamp}: {self.value}"


class MetricRollup(models.Model):
    """A series' points summarised per hour or day.

    count, sum, min, max and the last value are enough to answer every
    question the dashboard asks of a period without the raw points: a
    counter's total is the sum, a gauge's level the last value or the mean.
    """

    id = models.BigAutoField(primary_key=True)
    series = models.ForeignKey(
        MetricSeries, on_delete=models.CASCADE, related_name="rollups"
    )
    granularity = models.CharField(
        max_length=10,
        choices=[
            c
            for c in enums.Granularities.CHOICES
            if c[0] in enums.Granularities.ROLLUPS
        ],
    )
    bucket_start = models.DateTimeField()
    count = models.PositiveIntegerField()
    sum = models.DecimalField(max_digits=30, decimal_places=6)
    min = models.DecimalField(max_digits=24, decimal_places=6)
    max = models.DecimalField(max_digits=24, decimal_places=6)
    last = models.DecimalField(max_digits=24, decimal_places=6)
    last_timestamp = models.DateTimeField()

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["series", "granularity", "bucket_start"],
                name="unique_metric_rollup",
            ),
        ]
        indexes = [
            models.Index(
                fields=["granularity", "bucket_start"], name="metric_rollup_age"
            )
        ]


class MetricGoal(core_models.UuidMixin, TimeStampedModel):
    """What a metric's figure should reach, kept apart from the metric itself.

    The offering's goal is the provider's default; a project may set its own,
    since a small project and a large one rarely aim at the same number.
    """

    offering_metric = models.ForeignKey(
        OfferingMetric, on_delete=models.CASCADE, related_name="goals"
    )
    project = models.ForeignKey(
        structure_models.Project,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="metric_goals",
        help_text=_("Empty is the offering's default goal."),
    )
    comparator = models.CharField(
        max_length=2,
        choices=enums.Comparators.CHOICES,
        default=enums.Comparators.AT_LEAST,
    )
    value = models.DecimalField(max_digits=24, decimal_places=6)
    period = models.CharField(
        max_length=20,
        choices=enums.GoalPeriods.CHOICES,
        default=enums.GoalPeriods.MONTH,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Permissions:
        customer_path = "offering_metric__offering__customer"

    class Meta:
        ordering = ["offering_metric", "project", "id"]
        constraints = [
            UniqueConstraint(
                fields=["offering_metric"],
                condition=Q(project__isnull=True),
                name="unique_offering_default_metric_goal",
            ),
            UniqueConstraint(
                fields=["offering_metric", "project"],
                condition=Q(project__isnull=False),
                name="unique_project_metric_goal",
            ),
        ]

    def __str__(self):
        scope = self.project.name if self.project_id else "default"
        return f"{self.offering_metric} ({scope}) {self.comparator} {self.value}"

    def is_met(self, figure):
        if figure is None:
            return None
        if self.comparator == enums.Comparators.AT_LEAST:
            return figure >= self.value
        return figure <= self.value
