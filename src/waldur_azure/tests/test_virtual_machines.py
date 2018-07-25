from ddt import data, ddt
from rest_framework import test, status
from libcloud.compute.types import NodeState
import mock

from . import fixtures, factories
from .. import models


@ddt
class VirtualMachineRDPTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.AzureFixture()

    def test_rdp_returns_xrdp_file_as_attachment(self):
        self.client.force_authenticate(self.fixture.owner)
        vm = self.fixture.virtual_machine
        factories.InstanceEndpoint(instance=vm, name=models.InstanceEndpoint.Name.RDP)
        url = factories.VirtualMachineFactory.get_url(vm, 'rdp')

        response = self.client.get(url)

        self.assertEquals(response.status_code, status.HTTP_200_OK)
        self.assertIn('%s.rdp' % vm.name, response._headers['content-disposition'][1])

    def test_rdp_is_not_available_if_backend_vm_does_not_have_a_port(self):
        self.client.force_authenticate(self.fixture.owner)
        vm = self.fixture.virtual_machine
        url = factories.VirtualMachineFactory.get_url(vm, 'rdp')

        response = self.client.get(url)

        self.assertEquals(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_rdp_action_is_not_available_only_to_erred_or_instances(self):
        self.client.force_authenticate(self.fixture.owner)
        vm = self.fixture.virtual_machine
        vm.state = vm.States.ERRED
        vm.save()
        url = factories.VirtualMachineFactory.get_url(vm, 'rdp')

        response = self.client.get(url)

        self.assertEquals(response.status_code, status.HTTP_409_CONFLICT)

    @data(NodeState.STOPPED, NodeState.UNKNOWN, NodeState.ERROR, NodeState.STARTING)
    def test_rdp_action_is_allowed_only_for_running_instances(self, runtime_state):
        self.client.force_authenticate(self.fixture.owner)
        vm = self.fixture.virtual_machine
        vm.runtime_state = runtime_state
        vm.save()
        url = factories.VirtualMachineFactory.get_url(vm, 'rdp')

        response = self.client.get(url)

        self.assertEquals(response.status_code, status.HTTP_409_CONFLICT)


@ddt
class VirtualMachineStartTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.AzureFixture()
        self.client.force_authenticate(self.fixture.owner)

    @mock.patch('waldur_azure.executors.VirtualMachineStartExecutor.execute')
    def test_stopped_machine_can_be_started(self, start_executor_mock):
        vm = self.fixture.virtual_machine
        vm.runtime_state = NodeState.STOPPED
        vm.save()
        url = factories.VirtualMachineFactory.get_url(vm, 'start')

        response = self.client.post(url)

        self.assertEquals(response.status_code, status.HTTP_202_ACCEPTED)

    @mock.patch('waldur_azure.executors.VirtualMachineStartExecutor.execute')
    def test_machine_in_ok_state_can_be_started(self, start_executor_mock):
        vm = self.fixture.virtual_machine
        vm.state = vm.States.OK
        vm.runtime_state = NodeState.STOPPED
        vm.save()
        url = factories.VirtualMachineFactory.get_url(vm, 'start')

        response = self.client.post(url)

        self.assertEquals(response.status_code, status.HTTP_202_ACCEPTED)

    @data(NodeState.RUNNING, NodeState.UNKNOWN, NodeState.ERROR, NodeState.STARTING)
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
class VirtualMachineStopTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.AzureFixture()
        self.client.force_authenticate(self.fixture.owner)

    @mock.patch('waldur_azure.executors.VirtualMachineStopExecutor.execute')
    def test_running_machine_can_be_stopped(self, stop_executor_mock):
        vm = self.fixture.virtual_machine
        vm.runtime_state = NodeState.RUNNING
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

    @data(NodeState.STOPPED, NodeState.UNKNOWN, NodeState.ERROR, NodeState.STARTING)
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
