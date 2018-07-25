from django.test import TransactionTestCase

from waldur_core.structure.tests import fixtures


class TotalVolumeSizeTest(TransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()

    def test_total_volume_size_for_project(self):
        volume = self.fixture.volume
        scope = self.fixture.project
        quota = scope.quotas.get(name='nc_volume_size')
        self.assertEqual(quota.usage, volume.size)

    def test_total_volume_size_for_customer(self):
        volume = self.fixture.volume
        scope = self.fixture.customer
        quota = scope.quotas.get(name='nc_volume_size')
        self.assertEqual(quota.usage, volume.size)

    def test_total_snapshot_size_for_project(self):
        snapshot = self.fixture.snapshot
        scope = self.fixture.project
        quota = scope.quotas.get(name='nc_snapshot_size')
        self.assertEqual(quota.usage, snapshot.size)

    def test_total_snapshot_size_for_customer(self):
        snapshot = self.fixture.snapshot
        scope = self.fixture.customer
        quota = scope.quotas.get(name='nc_snapshot_size')
        self.assertEqual(quota.usage, snapshot.size)
