import decimal
from unittest import mock

from django.conf import settings as django_settings
from django.test import TestCase
from freezegun import freeze_time

from waldur_freeipa import models as freeipa_models
from waldur_slurm.client import SlurmClient
from waldur_slurm.parser import SlurmReportLine

from .. import models
from . import factories, fixtures

VALID_REPORT = """
allocation1|cpu=1,mem=51200M,node=1,gres/gpu=1,gres/gpu:tesla=1|00:01:00|user1|
allocation1|cpu=2,mem=51200M,node=2,gres/gpu=2,gres/gpu:tesla=1|00:02:00|user2|
"""


class BackendTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.SlurmFixture()
        self.allocation = self.fixture.allocation
        self.account = self.allocation.backend_id

    @mock.patch('subprocess.check_output')
    def test_usage_synchronization(self, check_output):
        check_output.return_value = VALID_REPORT.replace('allocation1', self.account)

        backend = self.allocation.get_backend()
        backend.sync_usage()
        self.allocation.refresh_from_db()

        self.assertEqual(self.allocation.cpu_usage, 1 + 2 * 2 * 2)
        self.assertEqual(self.allocation.gpu_usage, 1 + 2 * 2 * 2)
        self.assertEqual(self.allocation.ram_usage, (1 + 2 * 2) * 51200)

    @freeze_time('2017-10-16')
    @mock.patch('subprocess.check_output')
    def test_usage_per_user(self, check_output):
        check_output.return_value = VALID_REPORT.replace('allocation1', self.account)

        user1 = self.fixture.manager
        user2 = self.fixture.admin

        freeipa_models.Profile.objects.create(user=user1, username='user1')
        freeipa_models.Profile.objects.create(user=user2, username='user2')

        backend = self.allocation.get_backend()
        backend.sync_usage()

        allocation_usage = models.AllocationUsage.objects.get(
            allocation=self.allocation, year=2017, month=10,
        )

        user1_allocation_usage = models.AllocationUserUsage.objects.get(
            allocation_usage=allocation_usage, user=user1
        )

        self.assertEqual(user1_allocation_usage.cpu_usage, 1)
        self.assertEqual(user1_allocation_usage.gpu_usage, 1)
        self.assertEqual(user1_allocation_usage.ram_usage, 51200)

        user2_allocation_usage = models.AllocationUserUsage.objects.get(
            allocation_usage=allocation_usage, user=user2
        )
        self.assertEqual(user2_allocation_usage.cpu_usage, 2 * 2 * 2)
        self.assertEqual(user2_allocation_usage.gpu_usage, 2 * 2 * 2)
        self.assertEqual(user2_allocation_usage.ram_usage, 2 * 2 * 51200)

    @mock.patch('subprocess.check_output')
    def test_set_resource_limits(self, check_output):
        default_limits = django_settings.WALDUR_SLURM['DEFAULT_LIMITS']
        self.allocation.cpu_limit = default_limits['CPU']
        self.allocation.gpu_limit = default_limits['GPU']
        self.allocation.ram_limit = default_limits['RAM']
        self.allocation.save()

        template = (
            'sacctmgr --parsable2 --noheader --immediate'
            ' modify account %s set GrpTRES=cpu=%d,gres/gpu=%d,mem=%d'
        )
        context = (
            self.account,
            self.allocation.cpu_limit,
            self.allocation.gpu_limit,
            self.allocation.ram_limit,
        )
        command = [
            'ssh',
            '-o',
            'UserKnownHostsFile=/dev/null',
            '-o',
            'StrictHostKeyChecking=no',
            'root@localhost',
            '-p',
            '22',
            '-i',
            '/etc/waldur/id_rsa',
            template % context,
        ]

        backend = self.allocation.get_backend()
        backend.set_resource_limits(self.allocation)

        check_output.assert_called_once_with(command, encoding='utf-8', stderr=-2)

    @mock.patch('subprocess.check_output')
    def test_pull_allocation(self, check_output):
        association = f"{self.account}|cpu=400,mem=100M,gres/gpu=120"
        check_output.return_value = association

        with mock.patch.object(SlurmClient, 'get_usage_report') as usage_report:
            report = VALID_REPORT.replace('allocation1', self.account)
            usage_report.return_value = [
                SlurmReportLine(line) for line in report.splitlines() if '|' in line
            ]

            backend = self.allocation.get_backend()
            backend.pull_allocation(self.allocation)
            self.allocation.refresh_from_db()

            self.assertEqual(self.allocation.cpu_limit, 400)
            self.assertEqual(self.allocation.gpu_limit, 120)
            self.assertEqual(self.allocation.ram_limit, 100)

    def test_name_changing(self):
        sample_name = 'al*lo$ca#tio#n_12~!34-5'
        correct_name = 'allocation_1234-5'
        prefix = django_settings.WALDUR_SLURM['ALLOCATION_PREFIX']

        allocation = factories.AllocationFactory(name=sample_name)
        hexpart = allocation.uuid.hex[:5]

        final_correct_name = ("%s%s_%s" % (prefix, hexpart, correct_name))[
            : models.SLURM_ALLOCATION_NAME_MAX_LEN
        ]
        backend = allocation.get_backend()
        result_name = backend.get_allocation_name(allocation)

        self.assertEqual(result_name, final_correct_name)

    @freeze_time('2020-02-01')
    @mock.patch('subprocess.check_output')
    def test_allocation_zero_usage_created(self, check_output):
        association = f"{self.account}|cpu=400,mem=100M,gres/gpu=120"
        check_output.return_value = association

        with mock.patch.object(SlurmClient, 'get_usage_report') as usage_report:
            usage_report.return_value = []

            backend = self.allocation.get_backend()
            backend.pull_allocation(self.allocation)
            self.allocation.refresh_from_db()

            self.assertEqual(self.allocation.cpu_usage, 0)
            self.assertEqual(self.allocation.gpu_usage, 0)
            self.assertEqual(self.allocation.ram_usage, 0)

            year = 2020
            month = 2
            allocation_usage = models.AllocationUsage.objects.get(
                allocation=self.allocation, year=year, month=month
            )
            self.assertEqual(allocation_usage.cpu_usage, 0)
            self.assertEqual(allocation_usage.gpu_usage, 0)
            self.assertEqual(allocation_usage.ram_usage, 0)


class BackendMOABTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.SlurmFixture()
        self.fixture.service.settings.options = {'batch_service': 'MOAB'}
        self.fixture.allocation.deposit_usage = 0

        self.subprocess_patcher = mock.patch('subprocess.check_output')
        self.subprocess_mock = self.subprocess_patcher.start()
        self.subprocess_mock.return_value = """
            test_acc|4|||21|centos|0.00|1
            test_acc|4|6|12|20|centos|0.00|1
            test_acc|4|||100|centos|0.03|1
            test_acc|4|||100|centos|0.03|1
            test_acc|4|||500|centos|0.17|1
            test_acc|4|||2|centos|0.00|1
        """.replace(
            'test_acc', self.fixture.allocation.backend_id
        )

    def tearDown(self):
        mock.patch.stopall()

    def test_allocation_synchronization(self):
        backend = self.fixture.service.settings.get_backend()
        backend.sync()
        self.fixture.allocation.refresh_from_db()
        self.assertEqual(self.fixture.allocation.deposit_usage, decimal.Decimal('0.23'))

    def test_allocation_usage_synchronization(self):
        backend = self.fixture.service.settings.get_backend()
        backend.sync()
        usage = models.AllocationUsage.objects.get(allocation=self.fixture.allocation)
        self.assertEqual(usage.cpu_usage, 64)
        self.assertEqual(usage.gpu_usage, 6)
        self.assertEqual(usage.ram_usage, 12)
