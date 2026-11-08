"""Filter facets: the values each list filter can still take, with counts.

A list endpoint opts in by mixing in :class:`FacetsMixin` and declaring
``facet_fields``. The mixin serves ``GET <list-url>/facets/``, which accepts
every query parameter the list accepts and answers, per facet, which values
still have rows and how many::

    class OfferingViewSet(FacetsMixin, viewsets.ReadOnlyModelViewSet):
        filterset_class = OfferingFilter
        facet_fields = (
            Facet("customer", field="customer__uuid", label="customer__name",
                  params=("customer_uuid", "customer")),
            Facet("state", field="state", params=("state",),
                  choices=OfferingStates.CHOICES),
        )

Faceting is disjunctive. A facet is counted under every parameter of the
request except its own (``params``), so the values offered for a filter do not
collapse to the one already selected, and changing a selection never leads to
an empty list. Search and every other filter apply, because they run through
the view's own filterset: the facet counts and the list cannot disagree on
what a parameter means.

Visibility is the list's. The queryset comes from ``get_facet_queryset``
(``get_queryset`` unless a view overrides it), and every filter backend other
than ``DjangoFilterBackend`` runs once on it with the full request, as for
``list``. Such backends therefore cannot be facets; ``check_facet_fields``
fails a facet whose params the filterset does not define.

``value`` in the response is always what the client sends back as the query
parameter: a hex UUID, a choice's API label, ``"true"``/``"false"``, or the
normalized value. Each facet costs one ``GROUP BY`` query.
"""

import contextlib
import dataclasses
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from django.db import connection
from django.db.models import Count
from django_filters.rest_framework import DjangoFilterBackend
from django_filters.utils import translate_validation
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_core.core.views import no_count_action

FACET_PARAM = "facet"
FACET_LIMIT_PARAM = "facet_limit"
FACET_SEARCH_PARAM = "facet_q"
DEFAULT_FACET_LIMIT = 100
MAX_FACET_LIMIT = 1000
MAX_FACET_SEARCH_LENGTH = 100
# Values no filter can be sent: a missing JSON key reads as None, and a
# filter given an empty string is not applied.
BLANK_VALUES = (None, "")

# Parameters that shape a page rather than select rows. They never reach the
# filterset: `o` in particular may trigger expensive annotations.
NON_FILTER_PARAMS = (
    FACET_PARAM,
    FACET_LIMIT_PARAM,
    FACET_SEARCH_PARAM,
    "page",
    "page_size",
    "field",
    "o",
)


@dataclasses.dataclass(frozen=True)
class Facet:
    """One filter whose values are counted.

    :param name: key of the facet in the response and in ``?facet=``.
    :param field: lookup path grouped on, e.g. ``customer__uuid`` or
        ``backend_metadata__runtime_state``.
    :param params: the filterset parameters this facet stands for; all of them
        are dropped when the facet is counted.
    :param label: lookup path of a human-readable label, e.g.
        ``customer__name``. Without it, the label is the choice label or the
        value itself. A facet with a label is searched and limited in SQL.
    :param choices: ``(stored, api_label)`` pairs for a mapped choice field;
        the API label becomes both ``value`` and ``label``.
    :param normalize: maps a stored value to the value the API exposes; values
        that normalize to the same one are merged into one bucket.
    :param labels: ``(value, label)`` pairs naming the values of a plain choice
        field, whose parameter takes the stored value itself.
    :param array: ``field`` reaches a JSON array, on the model itself (e.g.
        ``categories``) or through relations (e.g.
        ``versions__targets__gpu_architectures``); each element is counted as a
        value, searched and cut in SQL.
    :param describe: maps the raw values found to their labels in Python, for a
        label that is no column (a property, a user's full name). Search and
        the limit then apply in Python too.
    :param visible: ``(view, rows) -> rows``, narrows the rows the facet counts
        to those whose value the viewer may see. The list may hide an
        attribute from some viewers (who applied, who reviewed); counting it
        unrestricted would name what the list conceals.
    """

    name: str
    field: str
    params: tuple[str, ...]
    label: str | None = None
    choices: Iterable[tuple[Any, str]] | None = None
    normalize: Callable[[Any], Any] | None = None
    labels: Iterable[tuple[Any, str]] | None = None
    array: bool = False
    describe: Callable[[list], dict] | None = None
    visible: Callable[[Any, Any], Any] | None = None

    def __post_init__(self):
        # Tuples, so that a generator is not consumed by the first read.
        for name in ("choices", "labels"):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, tuple(getattr(self, name)))

    def label_for(self, value: str) -> str:
        if self.labels is not None:
            return str(dict(self.labels).get(value, value))
        return value

    def represent(self, raw) -> str:
        if self.normalize:
            raw = self.normalize(raw)
        if self.choices is not None:
            return dict(self.choices).get(raw, str(raw))
        if isinstance(raw, bool):
            return "true" if raw else "false"
        if isinstance(raw, uuid.UUID):
            return raw.hex
        return str(raw)


class FacetValueSerializer(serializers.Serializer):
    value = serializers.CharField(
        help_text="Value to send back as the facet's query parameter."
    )
    label = serializers.CharField()
    count = serializers.IntegerField(
        help_text="Rows matching this value under all other filters."
    )


class FacetSerializer(serializers.Serializer):
    name = serializers.CharField()
    values = FacetValueSerializer(many=True)
    truncated = serializers.BooleanField(
        help_text="More values exist than facet_limit; search with facet_q."
    )


FACETS_PARAMETERS = [
    OpenApiParameter(
        FACET_PARAM,
        OpenApiTypes.STR,
        description="Comma-separated facet names to compute; all by default.",
    ),
    OpenApiParameter(
        FACET_LIMIT_PARAM,
        OpenApiTypes.INT,
        description=f"Most values returned per facet, by count "
        f"(default {DEFAULT_FACET_LIMIT}, at most {MAX_FACET_LIMIT}).",
    ),
    OpenApiParameter(
        FACET_SEARCH_PARAM,
        OpenApiTypes.STR,
        description="Keep only values whose label contains this text "
        "(at most 100 characters).",
    ),
]


class FacetsMixin:
    """Adds a ``facets`` collection action; see the module docstring."""

    facet_fields: tuple[Facet, ...] = ()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # The action shows the rows that list shows; core.access documents its
        # visibility only when it is listed here.
        filtered = tuple(getattr(cls, "access_filtered_actions", ()))
        if "facets" not in filtered:
            cls.access_filtered_actions = filtered + ("facets",)

    @classmethod
    def check_facet_fields(cls) -> list[str]:
        """Problems with the declaration; empty when it is sound."""
        filters = getattr(cls.filterset_class, "base_filters", {})
        problems = []
        names = [facet.name for facet in cls.facet_fields]
        if len(names) != len(set(names)):
            problems.append(f"{cls.__name__}: duplicate facet names {names}")
        for facet in cls.facet_fields:
            if not facet.params:
                problems.append(f"{cls.__name__}.{facet.name}: no params")
            if facet.array and (facet.label or facet.describe):
                problems.append(
                    f"{cls.__name__}.{facet.name}: an array facet's elements "
                    "are their own labels"
                )
            if facet.label and facet.describe:
                problems.append(
                    f"{cls.__name__}.{facet.name}: label and describe both "
                    "name the values"
                )
            for param in facet.params:
                if param not in filters:
                    problems.append(
                        f"{cls.__name__}.{facet.name}: {param!r} is not a "
                        f"filter of {getattr(cls.filterset_class, '__name__', None)}"
                    )
        return problems

    def get_facet_queryset(self):
        """Rows before any filter. Override to skip annotations facets never read."""
        return self.get_queryset()

    @extend_schema(
        summary="Count the values each filter can still take",
        description=(
            "Accepts the same filters as the list. Each facet is counted under "
            "every filter except its own, so its values are those that still "
            "have rows given the other selections and search."
        ),
        parameters=FACETS_PARAMETERS,
        responses=FacetSerializer(many=True),
        # The list's filters are documented, so the SDK accepts them here.
        filters=True,
    )
    @no_count_action
    @action(detail=False, methods=["get"], pagination_class=None)
    def facets(self, request, *args, **kwargs):
        requested = self._requested_facets()
        limit = self._facet_limit()
        search = request.query_params.get(FACET_SEARCH_PARAM, "").strip()
        if len(search) > MAX_FACET_SEARCH_LENGTH:
            raise serializers.ValidationError(
                {
                    FACET_SEARCH_PARAM: "Must be at most "
                    f"{MAX_FACET_SEARCH_LENGTH} characters."
                }
            )
        with self._as_list():
            result = self._facets(requested, limit, search)
        return Response(FacetSerializer(result, many=True).data)

    @contextlib.contextmanager
    def _as_list(self):
        """Read the rows as the list action would: a view whose queryset or
        filters depend on ``self.action`` (read-only grants, consent rules)
        must count exactly the rows its list shows. Permissions were checked
        for the facets action before this runs."""
        actual = self.action
        self.action = "list"
        try:
            yield
        finally:
            self.action = actual

    def _facets(self, requested, limit, search) -> list[dict]:
        request = self.request
        queryset = self.get_facet_queryset()
        for backend in self.filter_backends:
            if not issubclass(backend, DjangoFilterBackend):
                queryset = backend().filter_queryset(request, queryset, self)
        filterset_class = DjangoFilterBackend().get_filterset_class(self, queryset)

        result = []
        for facet in requested:
            params = request.query_params.copy()
            for param in facet.params + NON_FILTER_PARAMS:
                params.pop(param, None)
            filterset = filterset_class(data=params, queryset=queryset, request=request)
            if not filterset.is_valid():
                raise translate_validation(filterset.errors)
            # Re-read the matches as a plain queryset: the view's annotations
            # and DISTINCT must not reach the GROUP BY, and joins made by
            # filters must not multiply rows.
            rows = queryset.model._base_manager.filter(
                pk__in=filterset.qs.order_by().values("pk")
            )
            if facet.visible:
                rows = facet.visible(self, rows)
            result.append(self._count_facet(facet, rows, limit, search))
        return result

    def _requested_facets(self) -> list[Facet]:
        by_name = {facet.name: facet for facet in self.facet_fields}
        names = [
            name.strip()
            for value in self.request.query_params.getlist(FACET_PARAM)
            for name in value.split(",")
            if name.strip()
        ]
        unknown = [name for name in names if name not in by_name]
        if unknown:
            raise serializers.ValidationError(
                {
                    FACET_PARAM: f"Unknown facets {unknown}; "
                    f"available are {sorted(by_name)}."
                }
            )
        if not names:
            return list(self.facet_fields)
        return [by_name[name] for name in dict.fromkeys(names)]

    def _facet_limit(self) -> int:
        raw = self.request.query_params.get(FACET_LIMIT_PARAM)
        if raw in (None, ""):
            return DEFAULT_FACET_LIMIT
        try:
            limit = int(raw)
        except ValueError:
            limit = 0
        if not 1 <= limit <= MAX_FACET_LIMIT:
            raise serializers.ValidationError(
                {FACET_LIMIT_PARAM: f"Must be an integer from 1 to {MAX_FACET_LIMIT}."}
            )
        return limit

    def _count_facet(self, facet: Facet, rows, limit: int, search: str) -> dict:
        if facet.array:
            return self._count_array_facet(facet, rows, limit, search)
        group = [facet.field] + ([facet.label] if facet.label else [])
        # filter(), not exclude(): across a multi-valued relation exclude()
        # drops a row when any related row lacks the value, and filter()'s
        # join is the one the grouping below reuses.
        rows = rows.filter(**{f"{facet.field}__isnull": False})
        # A labeled facet may be large (organizations, tags), so it is searched
        # and cut in SQL. Others are choices, booleans or normalized values:
        # few, and only comparable once represented.
        in_sql = (
            facet.label is not None and facet.normalize is None and not facet.describe
        )
        if in_sql and search:
            rows = rows.filter(**{f"{facet.label}__icontains": search})
        grouped = (
            rows.values(*group)
            .annotate(count=Count("pk", distinct=True))
            .order_by("-count", *group)
        )
        if in_sql:
            # Room for a JSON null and an empty bucket, skipped below.
            grouped = grouped[: limit + 1 + len(BLANK_VALUES)]

        grouped = list(grouped)
        described = (
            facet.describe([row[facet.field] for row in grouped])
            if facet.describe
            else {}
        )
        buckets: dict[str, dict] = {}
        for row in grouped:
            if row[facet.field] in BLANK_VALUES:
                continue
            value = facet.represent(row[facet.field])
            if facet.label:
                label = str(row[facet.label])
            elif facet.describe:
                label = str(described.get(row[facet.field]) or value)
            else:
                label = facet.label_for(value)
            bucket = buckets.setdefault(
                value, {"value": value, "label": label, "count": 0}
            )
            bucket["count"] += row["count"]

        values = list(buckets.values())
        if search and not in_sql:
            values = [v for v in values if search.lower() in v["label"].lower()]
        values.sort(key=lambda v: (-v["count"], v["label"].lower()))
        return {
            "name": facet.name,
            "values": values[:limit],
            "truncated": len(values) > limit,
        }

    def _count_array_facet(self, facet: Facet, rows, limit: int, search: str):
        """Counts the elements of a JSON array, which GROUP BY cannot reach
        through the ORM. The ORM builds the (row, array) pairs, following any
        relations in the path; Postgres unnests them in a lateral join."""
        pairs, params = (
            rows.filter(**{f"{facet.field}__isnull": False})
            .order_by()
            .values_list("pk", facet.field)
            .query.sql_with_params()
        )
        sql = (
            "SELECT element, COUNT(DISTINCT pairs.row_pk) "
            f"FROM ({pairs}) AS pairs(row_pk, elements) "
            "CROSS JOIN LATERAL jsonb_array_elements_text(pairs.elements) element "
            "WHERE jsonb_typeof(pairs.elements) = 'array' AND element <> '' "
            + ("AND element ILIKE %s " if search else "")
            + "GROUP BY element ORDER BY 2 DESC, 1 LIMIT %s"
        )
        params = (
            *params,
            *([f"%{connection.ops.prep_for_like_query(search)}%"] if search else []),
            limit + 1,
        )
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            found = cursor.fetchall()
        values = [
            {
                "value": facet.represent(value),
                "label": facet.label_for(str(value)),
                "count": count,
            }
            for value, count in found
        ]
        return {
            "name": facet.name,
            "values": values[:limit],
            "truncated": len(values) > limit,
        }
