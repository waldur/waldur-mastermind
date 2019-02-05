import unittest
from ddt import data, ddt
from rest_framework import test, status
import mock

from . import fixtures, factories
from .. import models


@ddt
@unittest.skip('Runtime state is not synchronized yet.')
class VirtualMachineStartTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.AzureFixture()
        self.client.force_authenticate(self.fixture.owner)

    @mock.patch('waldur_azure.executors.VirtualMachineStartExecutor.execute')
    def test_stopped_machine_can_be_started(self, start_executor_mock):
        vm = self.fixture.virtual_machine
        vm.runtime_state = 'STOPPED'
        vm.save()
        url = factories.VirtualMachineFactory.get_url(vm, 'start')

        response = self.client.post(url)

        self.assertEquals(response.status_code, status.HTTP_202_ACCEPTED)

    @mock.patch('waldur_azure.executors.VirtualMachineStartExecutor.execute')
    def test_machine_in_ok_state_can_be_started(self, start_executor_mock):
        vm = self.fixture.virtual_machine
        vm.state = vm.States.OK
        vm.runtime_state = 'STOPPED'
        vm.save()
        url = factories.VirtualMachineFactory.get_url(vm, 'start')

        response = self.client.post(url)

        self.assertEquals(response.status_code, status.HTTP_202_ACCEPTED)

    @mock.patch('waldur_azure.executors.VirtualMachineStartExecutor.execute')
    def test_only_stopped_machine_can_be_started(self, runtime_state, start_executor_mock):
        vm = self.fixture.virtual_machine
        vm.runtime_state = runtime_state
        vm.save()
        url = factories.VirtualMachineFactory.get_url(vm, 'start')

        response = self.client.post(url)

        self.assertEquals(response.status_code, status.HTTP_409_CONFLICT)

    @data(models.VirtualMachine.States.DELETING, models.VirtualMachine.States.UPDATING,
          models.VirtualMachine.States.DELETION_SCHEDULED, models.VirtualMachine.States.ERRED)
    @mock.patch('waldur_azure.executors.VirtualMachineStartExecutor.execute')
    def test_only_machine_in_ok_state_can_be_started(self, state, start_executor_mock):
        vm = self.fixture.virtual_machine
        vm.state = state
        vm.save()
        url = factories.VirtualMachineFactory.get_url(vm, 'start')

        response = self.client.post(url)

        self.assertEquals(response.status_code, status.HTTP_409_CONFLICT)


@ddt
@unittest.skip('Runtime state is not synchronized yet.')
class VirtualMachineStopTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.AzureFixture()
        self.client.force_authenticate(self.fixture.owner)

    @mock.patch('waldur_azure.executors.VirtualMachineStopExecutor.execute')
    def test_running_machine_can_be_stopped(self, stop_executor_mock):
        vm = self.fixture.virtual_machine
        vm.runtime_state = 'RUNNING'
        vm.save()
        url = factories.VirtualMachineFactory.get_url(vm, 'stop')

        response = self.client.post(url)

        self.assertEquals(response.status_code, status.HTTP_202_ACCEPTED)

    @mock.patch('waldur_azure.executors.VirtualMachineStopExecutor.execute')
    def test_machine_in_ok_state_can_be_stopped(self, stop_executor_mock):
        vm = self.fixture.virtual_machine
        vm.state = vm.States.OK
        vm.save()
        url = factories.VirtualMachineFactory.get_url(vm, 'stop')

        response = self.client.post(url)

        self.assertEquals(response.status_code, status.HTTP_202_ACCEPTED)

    @mock.patch('waldur_azure.executors.VirtualMachineStopExecutor.execute')
    def test_only_running_machine_can_be_stopped(self, runtime_state, stop_executor_mock):
        vm = self.fixture.virtual_machine
        vm.runtime_state = runtime_state
        vm.save()
        url = factories.VirtualMachineFactory.get_url(vm, 'stop')

        response = self.client.post(url)

        self.assertEquals(response.status_code, status.HTTP_409_CONFLICT)

    @data(models.VirtualMachine.States.DELETING, models.VirtualMachine.States.UPDATING,
          models.VirtualMachine.States.DELETION_SCHEDULED, models.VirtualMachine.States.ERRED)
    @mock.patch('waldur_azure.executors.VirtualMachineStopExecutor.execute')
    def test_only_machine_in_ok_state_can_be_stopped(self, state, stop_executor_mock):
        vm = self.fixture.virtual_machine
        vm.state = state
        vm.save()
        url = factories.VirtualMachineFactory.get_url(vm, 'stop')

        response = self.client.post(url)

        self.assertEquals(response.status_code, status.HTTP_409_CONFLICT)
