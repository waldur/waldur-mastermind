from rest_framework import status, test

from waldur_vmware import models

from . import factories, fixtures


class VirtualDiskCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.VMwareFixture()
        self.vm = self.fixture.virtual_machine
        self.url = factories.VirtualMachineFactory.get_url(self.vm, "create_disk")

    def test_max_disk_is_not_exceeded(self):
        self.fixture.settings.options["max_disk"] = 100 * 1024
        self.fixture.settings.save(update_fields=["options"])
        payload = self.get_valid_payload(10 * 1024)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_max_disk_is_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options["max_disk"] = 100 * 1024
        self.fixture.settings.save(update_fields=["options"])
        payload = self.get_valid_payload(200 * 1024)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_max_disk_total_is_not_exceeded(self):
        self.fixture.settings.options["max_disk_total"] = 100 * 1024
        self.fixture.settings.save(update_fields=["options"])
        payload = self.get_valid_payload(10 * 1024)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_max_disk_total_is_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options["max_disk_total"] = 100 * 1024
        self.fixture.settings.save(update_fields=["options"])
        payload = self.get_valid_payload(200 * 1024)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_max_disk_total_is_exceeded_because_there_is_another_disk(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options["max_disk_total"] = 100 * 1024
        factories.DiskFactory(vm=self.vm, size=80 * 1024)
        self.fixture.settings.save(update_fields=["options"])
        payload = self.get_valid_payload(50 * 1024)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_when_disk_is_created_vm_summary_is_updated(self):
        # Act
        payload = self.get_valid_payload(10 * 1024)
        self.client.force_authenticate(self.fixture.owner)
        self.client.post(self.url, payload)

        # Assert
        self.vm.refresh_from_db()
        self.assertEqual(self.vm.disk, 10 * 1024)

    def get_valid_payload(self, size):
        return {"size": size}


class VirtualDiskExtendTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.VMwareFixture()
        self.disk = self.fixture.disk
        self.url = factories.DiskFactory.get_url(self.disk, "extend")
        self.disk.vm.runtime_state = models.VirtualMachine.RuntimeStates.POWERED_OFF
        self.disk.vm.save()

    def test_max_disk_is_not_exceeded(self):
        self.fixture.settings.options["max_disk"] = 100 * 1024
        self.fixture.settings.save(update_fields=["options"])
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, {"size": 10 * 1024})
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_max_disk_is_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options["max_disk"] = 100 * 1024
        self.fixture.settings.save(update_fields=["options"])
        response = self.client.post(self.url, {"size": 200 * 1024})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_max_disk_total_is_not_exceeded(self):
        self.fixture.settings.options["max_disk_total"] = 100 * 1024
        self.fixture.settings.save(update_fields=["options"])
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, {"size": 10 * 1024})
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_max_disk_total_is_exceeded(self):
        self.client.force_authenticate(self.fixture.owner)
        self.fixture.settings.options["max_disk_total"] = 100 * 1024
        self.fixture.settings.save(update_fields=["options"])
        response = self.client.post(self.url, {"size": 200 * 1024})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_max_disk_total_is_exceeded_because_there_is_another_disk(self):
        self.client.force_authenticate(self.fixture.owner)
        factories.DiskFactory(vm=self.disk.vm, size=80 * 1024)
        self.fixture.settings.options["max_disk_total"] = 100 * 1024
        self.fixture.settings.save(update_fields=["options"])
        response = self.client.post(self.url, {"size": 50 * 1024})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_current_disk_size_is_added_to_remaining_quota(self):
        # Arrange
        self.fixture.settings.options["max_disk_total"] = 25 * 1024
        self.fixture.settings.save(update_fields=["options"])

        self.disk.size = 24 * 1024
        self.disk.save()

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, {"size": 25 * 1024})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_extension_is_allowed_when_vm_is_running(self):
        self.disk.vm.runtime_state = models.VirtualMachine.RuntimeStates.POWERED_ON
        self.disk.vm.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, {"size": 10 * 1024})
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_when_disk_is_created_vm_summary_is_updated(self):
        # Act
        self.client.force_authenticate(self.fixture.owner)
        self.client.post(self.url, {"size": 20 * 1024})

        # Assert
        self.disk.vm.refresh_from_db()
        self.assertEqual(self.disk.vm.disk, 20 * 1024)


class VirtualDiskDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.VMwareFixture()
        self.disk = self.fixture.disk
        self.vm = self.disk.vm

    def test_when_disk_is_deleted_vm_summary_is_updated(self):
        self.disk.delete()
        self.vm.refresh_from_db()
        self.assertEqual(self.vm.disk, 0)
