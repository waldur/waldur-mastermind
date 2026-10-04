from django.utils.translation import gettext_lazy as _


class MetricKinds:
    """What a reported number is, in OpenTelemetry terms.

    A gauge is a level read at a moment (active learners, median response
    time); a counter counts things that happened (courses completed) and is
    stored as the increment since the previous point, so a period's figure is
    the sum of its points.
    """

    GAUGE = "gauge"
    COUNTER = "counter"

    CHOICES = (
        (GAUGE, _("Gauge")),
        (COUNTER, _("Counter")),
    )


class GoodDirections:
    """Which way a metric has to move to count as an improvement."""

    UP = "up"
    DOWN = "down"
    NEUTRAL = "neutral"

    CHOICES = (
        (UP, _("Higher is better")),
        (DOWN, _("Lower is better")),
        (NEUTRAL, _("Neither")),
    )


class DefinitionStates:
    ACTIVE = "active"
    DEPRECATED = "deprecated"

    CHOICES = (
        (ACTIVE, _("Active")),
        (DEPRECATED, _("Deprecated")),
    )


class OfferingMetricStates:
    """Lifecycle of a metric an offering has adopted.

    Paused refuses new points but keeps showing what was reported; archived
    also hides the metric from consuming projects. Data stays until retention
    removes it.
    """

    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"

    CHOICES = (
        (ACTIVE, _("Active")),
        (PAUSED, _("Paused")),
        (ARCHIVED, _("Archived")),
    )


class ProjectAggregations:
    """How the figures of a project's resources combine into one."""

    SUM = "sum"
    MEAN = "mean"

    CHOICES = (
        (SUM, _("Add the resource figures up")),
        (MEAN, _("Average the resource figures")),
    )


class Granularities:
    RAW = "raw"
    HOUR = "hour"
    DAY = "day"

    ROLLUPS = (HOUR, DAY)
    CHOICES = (
        (RAW, _("Raw points")),
        (HOUR, _("Hourly")),
        (DAY, _("Daily")),
    )


class Comparators:
    AT_LEAST = "ge"
    AT_MOST = "le"

    CHOICES = (
        (AT_LEAST, _("At least")),
        (AT_MOST, _("At most")),
    )


class GoalPeriods:
    """The stretch of time a goal's figure is computed over."""

    MONTH = "month"
    QUARTER = "quarter"
    ROLLING_30_DAYS = "rolling_30d"

    CHOICES = (
        (MONTH, _("Calendar month")),
        (QUARTER, _("Calendar quarter")),
        (ROLLING_30_DAYS, _("Last 30 days")),
    )
