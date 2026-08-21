from django.apps import apps
from django.db.models import QuerySet
from django.test import TestCase
from django.urls import get_resolver
from rest_framework.mixins import ListModelMixin

from waldur_core.core.pagination import ordering_ends_with_unique_column
from waldur_core.structure.models import BaseResource


def collect_view_classes():
    """Every view class reachable from the root URLconf."""
    views = set()

    def walk(patterns):
        for pattern in patterns:
            if hasattr(pattern, "url_patterns"):
                walk(pattern.url_patterns)
            else:
                view_class = getattr(pattern.callback, "cls", None)
                if view_class is not None:
                    views.add(view_class)

    walk(get_resolver().url_patterns)
    return views


def get_serializer_model(view):
    """The model a viewset serializes, for viewsets that build the queryset lazily."""
    meta = getattr(getattr(view, "serializer_class", None), "Meta", None)
    return getattr(meta, "model", None)


def ends_with_unique_column(model):
    """Whether the model's ordering ends with a column that cannot repeat."""
    return ordering_ends_with_unique_column(model, model._meta.ordering[-1])


class PaginationOrderingTest(TestCase):
    """
    Paginated list endpoints slice querysets with LIMIT/OFFSET. Without a
    total order the database is free to return rows in any order, so paging
    over an unordered queryset can repeat one row and skip another.
    """

    def test_every_exposed_queryset_is_ordered(self):
        """Anything reachable through a viewset must come back in a defined order."""
        unordered = sorted(
            {
                view.queryset.model._meta.label
                for view in collect_view_classes()
                if isinstance(getattr(view, "queryset", None), QuerySet)
                and not view.queryset.ordered
            }
        )
        self.assertEqual(
            unordered,
            [],
            "Models served by a viewset must be ordered, either via Meta.ordering "
            "or an explicit order_by() on the viewset queryset.",
        )

    def test_lazily_built_querysets_rely_on_model_ordering(self):
        """
        A viewset that only defines get_queryset() hides its ordering from the
        check above, so the model itself has to carry one. Third-party models
        are exempt: we cannot give them a Meta, so their viewsets order the
        queryset instead.
        """
        unordered = sorted(
            {
                model._meta.label
                for view in collect_view_classes()
                if not isinstance(getattr(view, "queryset", None), QuerySet)
                and issubclass(view, ListModelMixin)
                and (model := get_serializer_model(view)) is not None
                and model.__module__.startswith("waldur")
                and not model._meta.ordering
            }
        )
        self.assertEqual(
            unordered,
            [],
            "These models are listed by a viewset that builds its queryset in "
            "get_queryset(), where ordering cannot be verified statically. Give "
            "the model a Meta.ordering.",
        )

    def test_every_resource_model_is_ordered(self):
        """
        Meta.ordering is the only safeguard that survives a get_queryset()
        override rebuilding the queryset and dropping the viewset's order_by().
        """
        unordered = sorted(
            model._meta.label
            for model in apps.get_models()
            if issubclass(model, BaseResource) and not model._meta.ordering
        )
        self.assertEqual(
            unordered,
            [],
            "Resource models need Meta.ordering of their own. Declare "
            "class Meta(BaseResource.Meta) instead of a bare class Meta, or set "
            "ordering explicitly when a mixin Meta shadows it.",
        )

    def test_ordering_is_a_total_order(self):
        """
        Ordering must end with a unique column, otherwise ties still float.

        Every model we own is checked, not only the ones a viewset exposes
        today: an ordering is what .first() and unpaginated exports follow too,
        and a model tends to reach a list endpoint long after it is written.
        Models we do not own are exempt, since their ordering can only live on
        the queryset.
        """
        without_tiebreaker = sorted(
            model._meta.label
            for model in apps.get_models()
            if model.__module__.startswith("waldur")
            and model._meta.ordering
            and not ends_with_unique_column(model)
        )
        self.assertEqual(
            without_tiebreaker,
            [],
            "Meta.ordering must end with a unique column so that rows with "
            "equal sort keys keep a stable position across pages.",
        )
