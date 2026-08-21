from collections import OrderedDict

from django.db.models import QuerySet
from django.db.models.query import ModelIterable
from rest_framework import pagination
from rest_framework.response import Response
from rest_framework.utils.urls import remove_query_param, replace_query_param

RESULT_COUNT_HEADER = "X-Result-Count"


def ordering_ends_with_unique_column(model, term) -> bool:
    """Whether an ordering term is a column that cannot repeat.

    Expressions and related lookups are treated as non-unique: their column does
    not belong to this table, so it cannot be trusted to break ties.
    """
    if not isinstance(term, str):
        return False
    name = term.lstrip("-")
    if name in ("pk", model._meta.pk.name, model._meta.pk.attname):
        return True
    try:
        field = model._meta.get_field(name)
    except Exception:
        return False
    # A nullable unique column still allows several NULLs, hence several ties.
    return field.unique and not field.null


def get_effective_ordering(queryset) -> list:
    """The ordering the database will actually apply to a queryset."""
    query = queryset.query
    if query.order_by:
        return list(query.order_by)
    if query.default_ordering:
        return list(query.get_meta().ordering or [])
    return []


def ensure_total_ordering(queryset):
    """Append the primary key unless the ordering already ends on a unique column.

    Paginated endpoints slice with LIMIT/OFFSET, and a non-unique final sort key
    is not a total order: rows sharing it are free to swap places between pages,
    so one row comes back twice and another is never seen. Model orderings carry
    their own tie-breaker, but an ordering filter replaces the ordering rather
    than extending it - ``?o=state`` leaves ``ORDER BY state`` alone - so the
    guarantee has to be re-established where the slicing happens.

    Querysets whose SELECT list is not the model's own columns are left alone:
    appending a column to ``values()`` or ``DISTINCT ON`` would change what the
    query returns rather than just its order.
    """
    if not isinstance(queryset, QuerySet):
        return queryset
    if queryset._iterable_class is not ModelIterable:
        return queryset
    query = queryset.query
    if query.distinct_fields or query.combinator or query.is_sliced:
        return queryset
    ordering = get_effective_ordering(queryset)
    if ordering and ordering_ends_with_unique_column(queryset.model, ordering[-1]):
        return queryset
    return queryset.order_by(*ordering, "pk")


class LinkHeaderPagination(pagination.PageNumberPagination):
    page_size_query_param = "page_size"
    max_page_size = 300

    def paginate_queryset(self, queryset, request, view=None):
        return super().paginate_queryset(ensure_total_ordering(queryset), request, view)

    def get_paginated_response(self, data):
        link_candidates = OrderedDict(
            (
                ("first", self.get_first_link),
                ("prev", self.get_previous_link),
                ("next", self.get_next_link),
                ("last", self.get_last_link),
            )
        )

        link = ", ".join(
            f'<{get_link()}>; rel="{rel}"'
            for rel, get_link in link_candidates.items()
            if get_link()
        )

        headers = {
            RESULT_COUNT_HEADER: self.page.paginator.count,
            "Link": link,
        }

        return Response(data, headers=headers)

    def get_first_link(self):
        url = self.request.build_absolute_uri()
        return remove_query_param(url, self.page_query_param)

    def get_last_link(self):
        url = self.request.build_absolute_uri()
        page_number = self.page.paginator.page_range[-1]
        if page_number == 1:
            return remove_query_param(url, self.page_query_param)
        return replace_query_param(url, self.page_query_param, page_number)

    def get_paginated_response_schema(self, schema):
        return schema
