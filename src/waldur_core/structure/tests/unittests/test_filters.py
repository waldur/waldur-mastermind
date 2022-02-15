from datetime import datetime, timedelta

from rest_framework.test import APITransactionTestCase

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.structure.tests import factories
from waldur_core.structure.tests.factories import ProjectFactory, UserFactory


class CustomerAccountingStartDateFilterTest(APITransactionTestCase):
    def setUp(self):
        running_customer = factories.CustomerFactory(
            accounting_start_date=datetime.now() - timedelta(days=7)
        )
        not_running_customer = factories.CustomerFactory(
            accounting_start_date=datetime.now() + timedelta(days=7)
        )
        self.running_project = factories.ProjectFactory(customer=running_customer)
        self.not_running_project = factories.ProjectFactory(
            customer=not_running_customer
        )

    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=True)
    def test_accounting_is_running_filter_behaves_properly(self):
        staff = UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        response = self.client.get(ProjectFactory.get_list_url())
        self.assertEqual(len(response.data), 2)

        response = self.client.get(
            ProjectFactory.get_list_url(),
            {
                "accounting_is_running": "true",
            },
        )
        self.assertEqual(len(response.data), 1)

        response = self.client.get(
            ProjectFactory.get_list_url(),
            {
                "accounting_is_running": "false",
            },
        )
        self.assertEqual(len(response.data), 1)
