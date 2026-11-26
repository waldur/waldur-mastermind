from django.test import SimpleTestCase

from waldur_core.core.tests.test_access_coverage import assert_app_routes_declared


class PIDAccessDeclarationsTest(SimpleTestCase):
    def test_every_route_declares_access(self):
        assert_app_routes_declared(self, "pid")
