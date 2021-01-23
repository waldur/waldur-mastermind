from unittest import mock

from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_zabbix import models

from . import factories


class TriggerQueryTest(test.APITransactionTestCase):
    @mock.patch('pyzabbix.ZabbixAPI')
    def execute_call(self, params, mock_zabbix):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)

        url = factories.ZabbixServiceFactory.get_url(action='trigger_status')
        self.client.get(url, params)

        kwargs = mock_zabbix().trigger.get.call_args[1]
        return kwargs

    def test_filter_problem_status(self):
        kwargs = self.execute_call({'value': 1,})

        self.assertEqual(kwargs['filter'], dict(value=1))

    def test_filter_priority(self):
        kwargs = self.execute_call({'priority': [1, 2, 3],})

        self.assertEqual(kwargs['filter']['priority'], [1, 2, 3])

    def test_filter_acknowledge_status(self):
        kwargs = self.execute_call(
            {
                'acknowledge_status': models.Trigger.AcknowledgeStatus.SOME_EVENTS_UNACKNOWLEDGED,
            }
        )

        self.assertEqual(kwargs['withUnacknowledgedEvents'], 1)
