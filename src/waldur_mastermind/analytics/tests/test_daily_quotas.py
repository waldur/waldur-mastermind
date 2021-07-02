from django.test import testcases
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.analytics import models, tasks, utils
from waldur_mastermind.common.utils import parse_date


class TestDailyQuotasEndpoint(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project

        models.DailyQuotaHistory.objects.create(
            scope=self.project,
            name='nc_user_count',
            date=parse_date('2018-10-01'),
            usage=10,
        )

        models.DailyQuotaHistory.objects.create(
            scope=self.project,
            name='nc_user_count',
            date=parse_date('2018-10-02'),
            usage=11,
        )

        models.DailyQuotaHistory.objects.create(
            scope=self.project,
            name='nc_user_count',
            date=parse_date('2018-10-03'),
            usage=12,
        )

    def test_daily_quotas_are_serialized(self):
        self.client.force_login(self.fixture.owner)
        url = reverse('daily-quotas-list')
        scope = structure_factories.ProjectFactory.get_url(self.project)
        request = {
            'start': '2018-09-30',
            'end': '2018-10-04',
            'scope': scope,
            'quota_names': ['nc_user_count'],
        }
        response = self.client.get(url, request)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        expected = {
            'nc_user_count': [0, 10, 11, 12, 12],
        }
        self.assertDictEqual(response.data, expected)


class TestDailyQuotasImport(test.APITransactionTestCase):
    def setUp(self):
        with freeze_time('2018-10-01'):
            self.fixture = structure_fixtures.ProjectFixture()
            self.project = self.fixture.project
            self.project.set_quota_usage('nc_user_count', 0)

        with freeze_time('2018-10-03'):
            self.project.set_quota_usage('nc_user_count', 10)

        with freeze_time('2018-10-05'):
            self.project.set_quota_usage('nc_user_count', 30)

    def test_quotas_usage_history_is_imported_correctly(self):
        with freeze_time('2018-10-06'):
            utils.import_daily_usage()

        actual = list(
            models.DailyQuotaHistory.objects.filter(
                scope=self.project, name='nc_user_count',
            )
            .order_by('date')
            .values('date', 'usage')
        )
        expected = [
            {'date': parse_date('2018-10-01'), 'usage': 0,},
            {'date': parse_date('2018-10-02'), 'usage': 0,},
            {'date': parse_date('2018-10-03'), 'usage': 10,},
            {'date': parse_date('2018-10-04'), 'usage': 10,},
            {'date': parse_date('2018-10-05'), 'usage': 30,},
            {'date': parse_date('2018-10-06'), 'usage': 30,},
        ]
        self.assertEqual(expected, actual)


class TestDailyQuotasTask(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project

    def test_quotas_are_synced(self):
        self.project.set_quota_usage('nc_user_count', 30)
        models.DailyQuotaHistory.objects.all().delete()
        tasks.sync_daily_quotas()
        actual = models.DailyQuotaHistory.objects.get(
            scope=self.project, name='nc_user_count', date=timezone.now().date()
        ).usage
        self.assertEqual(30, actual)


class TestDailyQuotasSignalHandler(testcases.TestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project

    def test_quotas_are_synced(self):
        self.project.set_quota_usage('nc_user_count', 30)
        actual = models.DailyQuotaHistory.objects.get(
            scope=self.project, name='nc_user_count', date=timezone.now().date()
        ).usage
        self.assertEqual(30, actual)
