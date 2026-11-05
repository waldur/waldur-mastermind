from django.test import SimpleTestCase

from waldur_core.core.tests.test_access_coverage import assert_app_routes_declared


class MarketplaceOpenStackRoutesSayWhoMayCallThemTest(SimpleTestCase):
    def test_every_route_says_who_may_call_it(self):
        assert_app_routes_declared(self, "marketplace_openstack")
