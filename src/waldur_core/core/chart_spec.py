"""Charts as data: what a chart shows, never how a renderer draws it.

A chart spec names a chart kind, its axes, series of points and a few
annotations. One frontend component turns it into a chart, both in the
assistant's thread and on pages that show the same figures. The spec never
carries renderer options, formatters or markup, so a tool result cannot
inject behavior into the page; styling belongs to the frontend.

Time axes take epoch milliseconds.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Literal, NotRequired, TypedDict, cast, get_args
from urllib.parse import urlsplit

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

ChartType = Literal[
    "line",
    "area",
    "bar",
    "stacked_bar",
    "scatter",
    "heatmap",
    "timeline",
]
AxisType = Literal["time", "category", "value", "log"]
SeriesStyle = Literal["solid", "dashed", "dotted"]
Tone = Literal["neutral", "info", "success", "warning", "danger"]
AnnotationType = Literal["line_x", "line_y", "area_x", "area_y"]
YAxis = Literal["primary", "secondary"]
ChartValue = int | float | str | None


class ChartAxis(TypedDict, total=False):
    type: AxisType
    label: str
    unit: str
    min: float | None
    max: float | None
    categories: list[str]


class ChartSeries(TypedDict):
    name: str
    points: list[list[ChartValue]]
    style: NotRequired[SeriesStyle]
    tone: NotRequired[Tone]
    y_axis: NotRequired[YAxis]
    labels: NotRequired[list[str]]
    ids: NotRequired[list[str]]


class ChartAnnotation(TypedDict):
    type: AnnotationType
    value: float
    value_end: NotRequired[float | None]
    label: NotRequired[str]
    tone: NotRequired[Tone]


class ChartLink(TypedDict):
    label: str
    url: str


class ChartSpec(TypedDict):
    chart_type: ChartType
    title: str
    x: ChartAxis
    y: ChartAxis
    series: list[ChartSeries]
    subtitle: NotRequired[str]
    y2: NotRequired[ChartAxis]
    annotations: NotRequired[list[ChartAnnotation]]
    link: NotRequired[ChartLink]
    empty_message: NotRequired[str]


CHART_TYPES = get_args(ChartType)
AXIS_TYPES = get_args(AxisType)
SERIES_STYLES = get_args(SeriesStyle)
TONES = get_args(Tone)
ANNOTATION_TYPES = get_args(AnnotationType)
Y_AXES = get_args(YAxis)

MAX_SERIES = 24
MAX_POINTS = 5000
MAX_POINT_VALUES = 4
MAX_ANNOTATIONS = 64

MAX_TITLE_LENGTH = 200
MAX_SUBTITLE_LENGTH = 300
MAX_EMPTY_MESSAGE_LENGTH = 300
MAX_LABEL_LENGTH = 100
MAX_UNIT_LENGTH = 30
MAX_CATEGORY_LENGTH = 200
MAX_POINT_STRING_LENGTH = 200
MAX_POINT_LABEL_LENGTH = 300
MAX_ID_LENGTH = 100
MAX_URL_LENGTH = 500


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
    label = serializers.CharField(
        required=False, allow_blank=True, max_length=MAX_LABEL_LENGTH
    )
    unit = serializers.CharField(
        required=False, allow_blank=True, max_length=MAX_UNIT_LENGTH
    )
    min = serializers.FloatField(required=False, allow_null=True)
    max = serializers.FloatField(required=False, allow_null=True)
    categories = serializers.ListField(
        child=serializers.CharField(allow_blank=True, max_length=MAX_CATEGORY_LENGTH),
        required=False,
        max_length=MAX_POINTS,
        help_text="Category labels, in order, for a category axis.",
    )

    def validate(self, attrs):
        low, high = attrs.get("min"), attrs.get("max")
        if low is not None and high is not None and low > high:
            raise serializers.ValidationError({"min": "Must not exceed max."})
        return attrs


class ChartSeriesSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=MAX_LABEL_LENGTH)
    style = serializers.ChoiceField(choices=SERIES_STYLES, default="solid")
    tone = serializers.ChoiceField(choices=TONES, required=False)
    y_axis = serializers.ChoiceField(
        choices=Y_AXES,
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
        child=serializers.CharField(
            allow_blank=True, max_length=MAX_POINT_LABEL_LENGTH
        ),
        required=False,
        max_length=MAX_POINTS,
        help_text="Tooltip text per point.",
    )
    ids = serializers.ListField(
        child=serializers.CharField(allow_blank=True, max_length=MAX_ID_LENGTH),
        required=False,
        max_length=MAX_POINTS,
        help_text="Identifier per point (e.g. a job ID) the page may link to.",
    )

    def validate_points(self, points):
        for point in points:
            for value in point:
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                    raise serializers.ValidationError(
                        "Point values must be numbers, strings or null."
                    )
                if isinstance(value, str) and len(value) > MAX_POINT_STRING_LENGTH:
                    raise serializers.ValidationError(
                        f"String point values must be at most "
                        f"{MAX_POINT_STRING_LENGTH} characters."
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
    label = serializers.CharField(
        required=False, allow_blank=True, max_length=MAX_LABEL_LENGTH
    )
    tone = serializers.ChoiceField(choices=TONES, default="neutral")

    def validate(self, attrs):
        if attrs["type"] in ("area_x", "area_y") and attrs.get("value_end") is None:
            raise serializers.ValidationError(
                {"value_end": "Required for area annotations."}
            )
        return attrs


class ChartLinkSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=MAX_LABEL_LENGTH)
    url = serializers.CharField(max_length=MAX_URL_LENGTH)

    def validate_url(self, url):
        # Same-site paths only: a chart links into the portal, not out of it.
        # Backslashes and ASCII control/whitespace characters are rejected
        # because WHATWG URL parsers normalize them into protocol-relative URLs.
        if (
            not url.startswith("/")
            or url.startswith("//")
            or "\\" in url
            or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url)
        ):
            raise serializers.ValidationError("Must be a path within the portal.")
        parsed = urlsplit(url)
        if parsed.scheme or parsed.netloc:
            raise serializers.ValidationError("Must be a path within the portal.")
        return url


class ChartSpecSerializer(serializers.Serializer):
    chart_type = serializers.ChoiceField(choices=CHART_TYPES)
    title = serializers.CharField(max_length=MAX_TITLE_LENGTH)
    subtitle = serializers.CharField(
        required=False, allow_blank=True, max_length=MAX_SUBTITLE_LENGTH
    )
    x = ChartAxisSerializer()
    y = ChartAxisSerializer()
    y2 = ChartAxisSerializer(required=False, help_text="Right-hand value axis.")
    series = ChartSeriesSerializer(many=True, max_length=MAX_SERIES)
    annotations = ChartAnnotationSerializer(
        many=True, required=False, max_length=MAX_ANNOTATIONS
    )
    link = ChartLinkSerializer(required=False)
    empty_message = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=MAX_EMPTY_MESSAGE_LENGTH,
        help_text="Shown instead of the chart when no series has points.",
    )

    def validate(self, attrs):
        if (
            any(item.get("y_axis") == "secondary" for item in attrs.get("series", []))
            and "y2" not in attrs
        ):
            raise serializers.ValidationError(
                {"y2": "Required when a series plots against the secondary y-axis."}
            )
        return attrs


def _cut(text, limit: int):
    if not isinstance(text, str) or len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def fit(spec: ChartSpec | Mapping[str, Any]) -> ChartSpec:
    """``spec`` with its text cut to the serializer's limits and blank series
    names filled in, so that text taken from user data (a CSV column header,
    a job or project name) shortens the chart's labels instead of dropping
    the chart. Structure is left as it is, for the serializer to judge."""
    if not isinstance(spec, Mapping):
        return spec  # type: ignore[return-value]
    result: dict[str, Any] = dict(spec)
    if isinstance(result.get("title"), str):
        stripped = result["title"].strip()
        # Required and never blank; an untitled plot keeps its chart.
        result["title"] = _cut(stripped, MAX_TITLE_LENGTH) if stripped else "Chart"
    for key, limit in (
        ("subtitle", MAX_SUBTITLE_LENGTH),
        ("empty_message", MAX_EMPTY_MESSAGE_LENGTH),
    ):
        if key in result:
            cut = _cut(result[key], limit)
            if cut is None:
                result.pop(key)
            else:
                result[key] = cut
    for key in ("x", "y", "y2"):
        axis = result.get(key)
        if isinstance(axis, dict):
            axis = dict(axis)
            for field, limit in (
                ("label", MAX_LABEL_LENGTH),
                ("unit", MAX_UNIT_LENGTH),
            ):
                if field in axis:
                    if axis[field] is None:
                        axis.pop(field)
                    else:
                        axis[field] = _cut(axis[field], limit)
            if isinstance(axis.get("categories"), list):
                axis["categories"] = [
                    _cut(c, MAX_CATEGORY_LENGTH) for c in axis["categories"]
                ]
            result[key] = axis
    if isinstance(result.get("series"), list):
        fitted = []
        for index, item in enumerate(result["series"]):
            if isinstance(item, dict):
                item = dict(item)
                if "name" in item:
                    name = item["name"]
                    if isinstance(name, str):
                        stripped = name.strip()
                        item["name"] = (
                            _cut(stripped, MAX_LABEL_LENGTH)
                            if stripped
                            else f"Series {index + 1}"
                        )
                if isinstance(item.get("points"), list):
                    item["points"] = [
                        [
                            _cut(v, MAX_POINT_STRING_LENGTH)
                            if isinstance(v, str)
                            else v
                            for v in point
                        ]
                        if isinstance(point, list)
                        else point
                        for point in item["points"]
                    ]
                for key, limit in (
                    ("labels", MAX_POINT_LABEL_LENGTH),
                    ("ids", MAX_ID_LENGTH),
                ):
                    if isinstance(item.get(key), list):
                        item[key] = [_cut(v, limit) for v in item[key]]
            fitted.append(item)
        result["series"] = fitted
    if isinstance(result.get("annotations"), list):
        result["annotations"] = [
            {**a, "label": _cut(a["label"], MAX_LABEL_LENGTH)}
            if isinstance(a, dict) and "label" in a
            else a
            for a in result["annotations"]
        ]
    if isinstance(result.get("link"), dict) and "label" in result["link"]:
        label = result["link"]["label"]
        if isinstance(label, str):
            stripped = label.strip()
            label = _cut(stripped, MAX_LABEL_LENGTH) if stripped else "Open"
        result["link"] = {**result["link"], "label": label}
    return cast(ChartSpec, result)


def chart(
    chart_type: ChartType,
    title: str,
    x: ChartAxis,
    y: ChartAxis,
    series: list[ChartSeries],
    *,
    subtitle: str | None = None,
    y2: ChartAxis | None = None,
    annotations: Sequence[ChartAnnotation] | None = None,
    link: ChartLink | None = None,
    empty_message: str | None = None,
) -> ChartSpec:
    """Build a chart spec; validated where it is emitted."""
    spec: ChartSpec = {
        "chart_type": chart_type,
        "title": title,
        "x": x,
        "y": y,
        "series": series,
    }
    if subtitle:
        spec["subtitle"] = subtitle
    if y2 is not None:
        spec["y2"] = y2
    if annotations:
        spec["annotations"] = list(annotations)
    if link is not None:
        spec["link"] = link
    if empty_message:
        spec["empty_message"] = empty_message
    return spec


def series(
    name: str,
    points: list[list[ChartValue]],
    *,
    style: SeriesStyle | None = None,
    tone: Tone | None = None,
    y_axis: YAxis | None = None,
    labels: list[str] | None = None,
    ids: list[str] | None = None,
) -> ChartSeries:
    item: ChartSeries = {"name": name, "points": points}
    if style is not None:
        item["style"] = style
    if tone is not None:
        item["tone"] = tone
    if y_axis is not None:
        item["y_axis"] = y_axis
    if labels is not None:
        item["labels"] = labels
    if ids is not None:
        item["ids"] = ids
    return item
