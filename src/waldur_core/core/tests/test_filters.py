from rest_framework import test
from six.moves import mock

from waldur_core.core import filters as core_filters


class TestUrlFilter(test.APITransactionTestCase):
    def setUp(self):
        from waldur_core.structure.tests.factories import CustomerFactory

        self.customer = CustomerFactory()
        self.url = CustomerFactory.get_url(self.customer)

        self.customer_filter = core_filters.URLFilter(
            view_name='customer-detail',
            name='customer__uuid'
        )

    def test_filter_checks_that_url_matches_view(self):
        qs = mock.Mock()
        self.customer_filter.filter(qs, self.url)
        qs.filter.assert_called_once_with(customer__uuid__exact=self.customer.uuid.hex)
