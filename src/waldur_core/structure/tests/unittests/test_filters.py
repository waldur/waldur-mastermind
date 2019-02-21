from datetime import datetime, timedelta

from rest_framework.test import APITransactionTestCase
from six.moves import mock
from django.test import TestCase

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.logging import models as logging_models
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.structure.filters import AggregateFilter
from waldur_core.structure.tests import factories
from waldur_core.structure.tests.factories import UserFactory, ProjectFactory


class AggregateFilterTest(TestCase):

    def setUp(self):
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory()
        self.aggregate_filter = AggregateFilter()
        self.queryset = logging_models.Alert.objects

    def test_service_alert_is_returned_when_aggregate_customer_is_the_same_as_its_scope_customer(self):
        scope = factories.TestServiceFactory(customer=self.customer)
        alert = logging_factories.AlertFactory(scope=scope)

        result = self._make_aggregate_request('customer', self.customer.uuid.hex)

        self.assertEqual(len(result), 1)
        self.assertTrue(result.filter(uuid=alert.uuid).exists())

    def test_project_alert_is_not_returned_when_its_scope_belongs_to_another_customer(self):
        alert = logging_factories.AlertFactory(scope=factories.ProjectFactory())

        result = self._make_aggregate_request('customer', self.customer.uuid.hex)

        self.assertFalse(result.filter(uuid=alert.uuid).exists())

    def test_only_alerts_where_scopes_customer_is_the_aggregated_one_are_returned(self):
        customer_related_alerts = []
        logging_factories.AlertFactory(scope=factories.ProjectFactory())
        spl = factories.TestServiceProjectLinkFactory(service__customer=self.customer)
        customer_related_alerts.append(logging_factories.AlertFactory(scope=spl))
        service = factories.TestServiceFactory(customer=self.customer)
        customer_related_alerts.append(logging_factories.AlertFactory(scope=service))
        expected_alerts_ids = [alert.uuid for alert in customer_related_alerts]

        result = self._make_aggregate_request('customer', self.customer.uuid.hex)
        actual_alerts_ids = [alert.uuid for alert in result]

        self.assertEqual(expected_alerts_ids, actual_alerts_ids)

    def test_service_project_link_alert_is_not_returned_when_its_scope_is_related_to_another_project(self):
        not_owned_alert = logging_factories.AlertFactory(scope=factories.TestServiceProjectLinkFactory())
        spl = factories.TestServiceProjectLinkFactory(project=self.project)
        owned_alert = logging_factories.AlertFactory(scope=spl)

        result = self._make_aggregate_request('project', self.project.uuid.hex)

        self.assertTrue(result.filter(uuid=owned_alert.uuid).exists())
        self.assertFalse(result.filter(uuid=not_owned_alert.uuid).exists())

    def _make_aggregate_request(self, aggregate_by, uuid):
        request = mock.Mock()
        request.query_params = {
            'aggregate': aggregate_by,
            'uuid': uuid,
        }

        return self.aggregate_filter.filter(request, self.queryset, None)


class CustomerAccountingStartDateFilterTest(APITransactionTestCase):

    def setUp(self):
        running_customer = factories.CustomerFactory(accounting_start_date=datetime.now() - timedelta(days=7))
        not_running_customer = factories.CustomerFactory(accounting_start_date=datetime.now() + timedelta(days=7))
        self.running_project = factories.ProjectFactory(customer=running_customer)
        self.not_running_project = factories.ProjectFactory(customer=not_running_customer)

    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=True)
    def test_accounting_is_running_filter_behaves_properly(self):
        staff = UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        response = self.client.get(ProjectFactory.get_list_url())
        self.assertEqual(len(response.data), 2)

        response = self.client.get(ProjectFactory.get_list_url(), {
            "accounting_is_running": "true",
        })
        self.assertEqual(len(response.data), 1)

        response = self.client.get(ProjectFactory.get_list_url(), {
            "accounting_is_running": "false",
        })
        self.assertEqual(len(response.data), 1)
