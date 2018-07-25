import datetime
from dateutil.relativedelta import relativedelta
import mock

from rest_framework import status, test

from waldur_core.core.utils import datetime_to_timestamp
from waldur_core.monitoring.utils import format_period
from waldur_core.structure.tests import factories as structure_factories
from waldur_zabbix.tasks import pull_sla

from . import factories
from .. import models


class SlaViewTest(test.APITransactionTestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)
        self.itservice = factories.ITServiceFactory()

        today = datetime.date.today()
        period = format_period(today)
        self.timestamp = datetime_to_timestamp(today)

        next_month = datetime.date.today() + relativedelta(months=1)
        self.next_month = format_period(next_month)

        self.history = models.SlaHistory.objects.create(
            itservice=self.itservice, period=period, value=100.0)
        self.events = models.SlaHistoryEvent.objects.create(
            history=self.history, timestamp=self.timestamp, state='U')

    def test_render_actual_sla(self):
        url = factories.ITServiceFactory.get_url(self.itservice)
        response = self.client.get(url)
        self.assertEqual(100.0, response.data['actual_sla'])

    def test_render_sla_events(self):
        url = factories.ITServiceFactory.get_events_url(self.itservice)
        response = self.client.get(url)
        self.assertEqual([{'timestamp': self.timestamp, 'state': 'U'}], response.data)

    def test_sla_is_not_available(self):
        url = factories.ITServiceFactory.get_url(self.itservice)
        response = self.client.get(url, data={'period': self.next_month})
        self.assertEqual(None, response.data['actual_sla'])

    def test_future_sla_events(self):
        url = factories.ITServiceFactory.get_events_url(self.itservice)
        response = self.client.get(url, data={'period': self.next_month})
        self.assertEqual(status.HTTP_404_NOT_FOUND, response.status_code)


class SlaPullTest(test.APITransactionTestCase):

    @mock.patch('waldur_core.structure.models.ServiceProjectLink.get_backend')
    @mock.patch('waldur_zabbix.tasks.update_itservice_sla')
    def test_task_calls_backend(self, mock_task, mock_backend):
        # Given
        itservice = factories.ITServiceFactory(is_main=True, backend_id='VALID')

        min_dt = datetime.date.today().replace(day=10) - relativedelta(months=2)
        max_dt = datetime.date.today().replace(day=10) - relativedelta(months=1)
        mock_backend().get_sla_range.return_value = min_dt, max_dt

        # When
        pull_sla(itservice.host.uuid)

        # Then
        mock_backend().get_sla_range.assert_called_once_with(itservice.backend_id)
        month1_beginning = min_dt.replace(day=1)
        month2_beginning = min_dt.replace(day=1) + relativedelta(months=+1)
        mock_task.delay.assert_has_calls([
            mock.call(itservice.pk,
                      format_period(min_dt),
                      datetime_to_timestamp(month1_beginning),
                      datetime_to_timestamp(month2_beginning)),
            mock.call(itservice.pk,
                      format_period(max_dt),
                      datetime_to_timestamp(month2_beginning),
                      datetime_to_timestamp(max_dt))
        ])
