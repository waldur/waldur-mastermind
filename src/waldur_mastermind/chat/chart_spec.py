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


def _cut(text, limit: int):
    if not isinstance(text, str) or len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def fit(spec: dict) -> dict:
    """``spec`` with its text cut to the serializer's limits and blank series
    names filled in, so that text taken from user data (a CSV column header,
    a job or project name) shortens the chart's labels instead of dropping
    the chart. Structure is left as it is, for the serializer to judge."""
    if not isinstance(spec, dict):
        return spec
    spec = dict(spec)
    for key, limit in (("title", 200), ("subtitle", 300), ("empty_message", 300)):
        spec[key] = _cut(spec.get(key), limit) if key in spec else None
        if spec[key] is None:
            spec.pop(key)
    if isinstance(spec.get("title"), str) and not spec["title"].strip():
        # Required and never blank; an untitled plot keeps its chart.
        spec["title"] = "Chart"
    for key in ("x", "y", "y2"):
        axis = spec.get(key)
        if isinstance(axis, dict):
            axis = dict(axis)
            axis["label"] = _cut(axis.get("label"), 100)
            axis["unit"] = _cut(axis.get("unit"), 30)
            if isinstance(axis.get("categories"), list):
                axis["categories"] = [_cut(c, 200) for c in axis["categories"]]
            spec[key] = {k: v for k, v in axis.items() if v is not None}
    if isinstance(spec.get("series"), list):
        fitted = []
        for index, item in enumerate(spec["series"]):
            if isinstance(item, dict):
                item = dict(item)
                name = item.get("name")
                if isinstance(name, str) and not name.strip():
                    item["name"] = f"Series {index + 1}"
                else:
                    item["name"] = _cut(name, 100)
                for key, limit in (("labels", 300), ("ids", 100)):
                    if isinstance(item.get(key), list):
                        item[key] = [_cut(v, limit) for v in item[key]]
            fitted.append(item)
        spec["series"] = fitted
    if isinstance(spec.get("annotations"), list):
        spec["annotations"] = [
            {**a, "label": _cut(a["label"], 100)}
            if isinstance(a, dict) and "label" in a
            else a
            for a in spec["annotations"]
        ]
    if isinstance(spec.get("link"), dict) and "label" in spec["link"]:
        label = spec["link"]["label"]
        if isinstance(label, str) and not label.strip():
            label = "Open"
        spec["link"] = {**spec["link"], "label": _cut(label, 100)}
    return spec


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
