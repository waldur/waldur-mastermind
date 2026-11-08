import uuid

from django.test import SimpleTestCase
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from rest_framework import status, test

from waldur_core.core.facets import Facet, FacetsMixin
from waldur_core.core.tests.test_actions_permission import iter_routes
from waldur_core.structure.tests import factories as structure_factories


def facet_views():
    seen = set()
    for callback, _ in iter_routes(get_resolver().url_patterns):
        cls = getattr(callback, "cls", None)
        if isinstance(cls, type) and issubclass(cls, FacetsMixin) and cls not in seen:
            seen.add(cls)
            yield cls


class FacetDeclarationsTest(SimpleTestCase):
    def test_some_views_declare_facets(self):
        self.assertTrue(list(facet_views()))

    def test_every_facet_param_is_a_filter_of_the_view(self):
        # A param the filterset does not define is never dropped when its
        # facet is counted, so the facet would narrow to its own selection.
        problems = [p for cls in facet_views() for p in cls.check_facet_fields()]
        self.assertEqual(problems, [])

    def test_facets_action_is_access_filtered(self):
        for cls in facet_views():
            self.assertIn("facets", cls.access_filtered_actions, cls)

    def test_check_reports_unknown_param(self):
        class View(FacetsMixin):
            filterset_class = type("F", (), {"base_filters": {"state": None}})
            facet_fields = (Facet("state", field="state", params=("state", "nope")),)

        [problem] = View.check_facet_fields()
        self.assertIn("'nope'", problem)

    def test_check_reports_two_namings(self):
        class View(FacetsMixin):
            filterset_class = type("F", (), {"base_filters": {"round": None}})
            facet_fields = (
                Facet(
                    "round",
                    field="round__uuid",
                    label="round__name",
                    describe=dict,
                    params=("round",),
                ),
            )

        [problem] = View.check_facet_fields()
        self.assertIn("label and describe", problem)


class FacetRepresentationTest(SimpleTestCase):
    def test_choice_label(self):
        facet = Facet("state", field="state", params=("state",), choices=[(1, "On")])
        self.assertEqual(facet.represent(1), "On")

    def test_boolean(self):
        facet = Facet("shared", field="shared", params=("shared",))
        self.assertEqual(facet.represent(False), "false")

    def test_normalized_then_mapped(self):
        facet = Facet(
            "type", field="type", params=("type",), normalize=lambda t: t.lower()
        )
        self.assertEqual(facet.represent("A"), "a")


def facet_routes(patterns=None, groups=frozenset()):
    """(route name, URL kwargs, view) of every facets route."""
    for entry in get_resolver().url_patterns if patterns is None else patterns:
        names = groups | set(entry.pattern.regex.groupindex)
        if isinstance(entry, URLResolver):
            yield from facet_routes(entry.url_patterns, names)
        elif isinstance(entry, URLPattern) and (entry.name or "").endswith("-facets"):
            yield entry.name, names - {"format"}, entry.callback.cls


def facet_urls():
    """A URL per facets route, any parent in its path a random UUID."""
    seen = set()
    for name, kwargs, cls in facet_routes():
        if name not in seen:
            seen.add(name)
            yield cls, reverse(name, kwargs={k: uuid.uuid4().hex for k in kwargs})


class FacetsEndpointsTest(test.APITestCase):
    """Every facets route runs its declared lookups against the database.

    With empty tables each facet is empty, but its query is still built and
    executed, which catches a lookup path or a JSON column that does not
    exist, or a view that cannot serve the action at all.
    """

    def test_every_facets_route_answers_staff(self):
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        urls = list(facet_urls())
        self.assertGreater(len(urls), 5)
        for cls, url in urls:
            with self.subTest(view=cls.__name__, url=url):
                response = self.client.get(url)
                # A nested list whose parent does not exist answers 404.
                self.assertIn(
                    response.status_code,
                    (status.HTTP_200_OK, status.HTTP_404_NOT_FOUND),
                    getattr(response, "data", response.content),
                )
                if response.status_code == status.HTTP_200_OK:
                    self.assertEqual(
                        [facet["name"] for facet in response.data],
                        [facet.name for facet in cls.facet_fields],
                    )
