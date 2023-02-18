from django.db.models.query import QuerySet
from django.test import TestCase

from waldur_core.core import WaldurExtension


class ViewsetsTest(TestCase):
    def test_default_ordering_must_be_defined_for_all_viewsets(self):
        for ext in WaldurExtension.get_extensions():
            try:
                views = __import__(
                    ext.django_app() + '.views',
                    fromlist=['views'],
                )
            except ImportError:
                continue

            for v in dir(views):
                if 'ViewSet' not in v:
                    continue

                view = getattr(views, v)
                try:
                    queryset = view().get_queryset()
                except (AttributeError, AssertionError):
                    continue

                if not isinstance(queryset, QuerySet):
                    continue

                self.assertTrue(
                    queryset.ordered,
                    msg='default ordering for %s has not been defined.' % v,
                )
