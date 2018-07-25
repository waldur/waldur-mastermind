import unittest

import datetime
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.core.utils import datetime_to_timestamp
from waldur_core.structure.tests.factories import TestNewInstanceFactory, TestServiceProjectLinkFactory, UserFactory

from ..models import ResourceSla, ResourceItem, ResourceSlaStateTransition
from ..utils import format_period


class BaseMonitoringTest(test.APITransactionTestCase):
    def setUp(self):
        self.link = TestServiceProjectLinkFactory()
        self.vm1 = TestNewInstanceFactory(service_project_link=self.link)
        self.vm2 = TestNewInstanceFactory(service_project_link=self.link)
        self.vm3 = TestNewInstanceFactory(service_project_link=self.link)
        self.client.force_authenticate(UserFactory(is_staff=True))


@unittest.skip('Monitoring is not supported by structure yet.')
class SlaTest(BaseMonitoringTest):
    def setUp(self):
        super(SlaTest, self).setUp()

        today = datetime.date.today()
        period = format_period(today)

        invalid_date = today + datetime.timedelta(days=100)
        invalid_period = format_period(invalid_date)

        ResourceSla.objects.create(scope=self.vm1, period=period, value=90)
        ResourceSla.objects.create(scope=self.vm1, period=invalid_period, value=70)
        ResourceSla.objects.create(scope=self.vm2, period=period, value=80)

    def test_sorting(self):
        response = self.client.get(TestNewInstanceFactory.get_list_url(), data={'o': 'actual_sla'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(2, len(response.data))
        self.assertEqual([80, 90], [item['sla']['value'] for item in response.data])

    def test_filtering(self):
        response = self.client.get(TestNewInstanceFactory.get_list_url(), data={'actual_sla': 80})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(1, len(response.data))

    def test_actual_sla_serializer(self):
        response = self.client.get(TestNewInstanceFactory.get_url(self.vm1))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(90, response.data['sla']['value'])


@unittest.skip('Monitoring is not supported by structure yet.')
class EventsTest(BaseMonitoringTest):
    def setUp(self):
        super(EventsTest, self).setUp()

        today = datetime.date.today()
        timestamp = datetime_to_timestamp(today)
        period = format_period(today)

        ResourceSlaStateTransition.objects.create(scope=self.vm1, period=period, timestamp=timestamp, state=True)
        ResourceSlaStateTransition.objects.create(scope=self.vm2, period=period, timestamp=timestamp, state=False)

        self.url = reverse('resource-sla-state-transition-list')

    def test_scope_filter(self):
        vm1_url = TestNewInstanceFactory.get_url(self.vm1)
        response = self.client.get(self.url, data={'scope': vm1_url})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual('U', response.data[0]['state'])

        vm2_url = TestNewInstanceFactory.get_url(self.vm2)
        response = self.client.get(self.url, data={'scope': vm2_url})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual('D', response.data[0]['state'])

    def test_period_filter(self):
        url = reverse('resource-sla-state-transition-list')

        today = datetime.date.today()
        invalid_date = today + datetime.timedelta(days=100)
        invalid_period = format_period(invalid_date)

        response = self.client.get(url, data={'period': invalid_period})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(0, len(response.data))


@unittest.skip('Monitoring is not supported by structure yet.')
class ItemTest(BaseMonitoringTest):
    def setUp(self):
        super(ItemTest, self).setUp()

        ResourceItem.objects.create(scope=self.vm1, name='application_status', value=1)
        ResourceItem.objects.create(scope=self.vm2, name='application_status', value=0)

        ResourceItem.objects.create(scope=self.vm1, name='ram_usage', value=10)
        ResourceItem.objects.create(scope=self.vm2, name='ram_usage', value=20)

    def test_serializer(self):
        response = self.client.get(TestNewInstanceFactory.get_url(self.vm1))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual({'application_status': 1, 'ram_usage': 10},
                         response.data['monitoring_items'])

    def test_filter(self):
        response = self.client.get(TestNewInstanceFactory.get_list_url(),
                                   data={'monitoring__application_status': 1})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(1, len(response.data))

    def test_sorter(self):
        response = self.client.get(TestNewInstanceFactory.get_list_url(),
                                   data={'o': 'monitoring__application_status'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        values = []
        for item in response.data:
            if not item['monitoring_items']:
                values.append(None)
            else:
                values.append(item['monitoring_items']['application_status'])
        self.assertEqual([0, 1], values)
