from rest_framework import status, test

from . import factories, fixtures


class VirtualDiskCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.VMwareFixture()
        self.vm = self.fixture.virtual_machine
        self.url = factories.VirtualMachineFactory.get_url(self.vm, 'create_disk')

    def test_max_disk_is_not_exceeded(self):
        self.fixture.settings.options['max_disk'] = 100
        self.fixture.settings.save(update_fields=['options'])
        payload = self.get_valid_payload(10)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_max_disk_is_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options['max_disk'] = 100
        self.fixture.settings.save(update_fields=['options'])
        payload = self.get_valid_payload(200)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def get_valid_payload(self, size):
        return {
            'name': 'Virtual disk',
            'size': size,
        }


class VirtualDiskExtendTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.VMwareFixture()
        self.disk = self.fixture.disk
        self.url = factories.DiskFactory.get_url(self.disk, 'extend')

    def test_max_disk_is_not_exceeded(self):
        self.fixture.settings.options['max_disk'] = 100
        self.fixture.settings.save(update_fields=['options'])
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, {'size': 10})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_max_disk_is_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options['max_disk'] = 100
        self.fixture.settings.save(update_fields=['options'])
        response = self.client.post(self.url, {'size': 200})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
