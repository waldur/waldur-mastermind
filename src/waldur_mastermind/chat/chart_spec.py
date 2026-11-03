"""Charts as data: what a chart shows, never how a renderer draws it.

A chart spec names a chart kind, its axes, series of points and a few
annotations. One frontend component turns it into a chart, both in the
assistant's thread and on pages that show the same figures. The spec never
carries renderer options, formatters or markup, so a tool result cannot
inject behaviour into the page; styling belongs to the frontend.

Time axes take epoch milliseconds.
"""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

CHART_TYPES = (
    "line",
    "area",
    "bar",
    "stacked_bar",
    "scatter",
    "heatmap",
    "timeline",
)
AXIS_TYPES = ("time", "category", "value", "log")
SERIES_STYLES = ("solid", "dashed", "dotted")
TONES = ("neutral", "info", "success", "warning", "danger")
ANNOTATION_TYPES = ("line_x", "line_y", "area_x", "area_y")

MAX_SERIES = 24
MAX_POINTS = 5000
MAX_POINT_VALUES = 4


@extend_schema_field(
    {"oneOf": [{"type": "number"}, {"type": "string"}], "nullable": True}
)
class ChartValueField(serializers.JSONField):
    """One coordinate: a number, a category label, or null for no value."""

    def __init__(self, **kwargs):
        kwargs.setdefault("allow_null", True)
        super().__init__(**kwargs)


class ChartAxisSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=AXIS_TYPES, default="value")
    label = serializers.CharField(required=False, allow_blank=True, max_length=100)
    unit = serializers.CharField(required=False, allow_blank=True, max_length=30)
    min = serializers.FloatField(required=False, allow_null=True)
    max = serializers.FloatField(required=False, allow_null=True)
    categories = serializers.ListField(
        child=serializers.CharField(allow_blank=True, max_length=200),
        required=False,
        max_length=MAX_POINTS,
        help_text="Category labels, in order, for a category axis.",
    )


class ChartSeriesSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    style = serializers.ChoiceField(choices=SERIES_STYLES, default="solid")
    tone = serializers.ChoiceField(choices=TONES, required=False)
    y_axis = serializers.ChoiceField(
        choices=["primary", "secondary"],
        default="primary",
        help_text="Secondary plots against `y2`, for a series in other units.",
    )
    points = serializers.ListField(
        child=serializers.ListField(
            child=ChartValueField(),
            min_length=2,
            max_length=MAX_POINT_VALUES,
        ),
        max_length=MAX_POINTS,
        help_text="[x, y] pairs; heatmap [x, y, value]; timeline "
        "[row, start, end, limit_end].",
    )
    labels = serializers.ListField(
        child=serializers.CharField(allow_blank=True, max_length=300),
        required=False,
        max_length=MAX_POINTS,
        help_text="Tooltip text per point.",
    )
    ids = serializers.ListField(
        child=serializers.CharField(allow_blank=True, max_length=100),
        required=False,
        max_length=MAX_POINTS,
        help_text="Identifier per point (e.g. a job ID) the page may link to.",
    )

    def validate_points(self, points):
        for point in points:
            for value in point:
                if value is not None and not isinstance(value, (int, float, str)):
                    raise serializers.ValidationError(
                        "Point values must be numbers, strings or null."
                    )
        return points

    def validate(self, attrs):
        count = len(attrs["points"])
        for field in ("labels", "ids"):
            if field in attrs and len(attrs[field]) != count:
                raise serializers.ValidationError(
                    {field: "Must have one entry per point."}
                )
        return attrs


class ChartAnnotationSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=ANNOTATION_TYPES)
    value = serializers.FloatField()
    value_end = serializers.FloatField(required=False, allow_null=True)
    label = serializers.CharField(required=False, allow_blank=True, max_length=100)
    tone = serializers.ChoiceField(choices=TONES, default="neutral")


class ChartLinkSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=100)
    url = serializers.CharField(max_length=500)

    def validate_url(self, url):
        # Same-site paths only: a chart links into the portal, not out of it.
        if not url.startswith("/") or url.startswith("//"):
            raise serializers.ValidationError("Must be a path within the portal.")
        return url


class ChartSpecSerializer(serializers.Serializer):
    chart_type = serializers.ChoiceField(choices=CHART_TYPES)
    title = serializers.CharField(max_length=200)
    subtitle = serializers.CharField(required=False, allow_blank=True, max_length=300)
    x = ChartAxisSerializer()
    y = ChartAxisSerializer()
    y2 = ChartAxisSerializer(required=False, help_text="Right-hand value axis.")
    series = ChartSeriesSerializer(many=True, max_length=MAX_SERIES)
    annotations = ChartAnnotationSerializer(many=True, required=False)
    link = ChartLinkSerializer(required=False)
    empty_message = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=300,
        help_text="Shown instead of the chart when no series has points.",
    )


def chart(chart_type: str, title: str, x: dict, y: dict, series: list, **extra):
    """Build a chart spec dict; validated where it is emitted."""
    return {
        "chart_type": chart_type,
        "title": title,
        "x": x,
        "y": y,
        "series": series,
        **{k: v for k, v in extra.items() if v not in (None, "", [])},
    }


def series(name: str, points: list, **extra) -> dict:
    return {
        "name": name,
        "points": points,
        **{k: v for k, v in extra.items() if v is not None},
    }
