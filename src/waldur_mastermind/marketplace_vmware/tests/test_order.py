from unittest import mock

from rest_framework import exceptions, test

from waldur_core.core import exceptions as core_exceptions
from waldur_core.core.enums import CoreStates
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import (
    VMWARE_VM_OFFERING,
    BillingTypes,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_vmware import executors as vmware_executors
from waldur_vmware import models as vmware_models
from waldur_vmware.tests.fixtures import VMwareFixture


class BaseVirtualMachineOrderTest(test.APITestCase):
    def setUp(self):
        self.fixture = VMwareFixture()
        self.vm: vmware_models.VirtualMachine = self.fixture.virtual_machine
        # Both update and destroy are only allowed for a stopped machine.
        self.vm.runtime_state = vmware_models.VirtualMachine.RuntimeStates.POWERED_OFF
        self.vm.save()

        self.offering = marketplace_factories.OfferingFactory(
            type=VMWARE_VM_OFFERING,
            scope=self.fixture.settings,
            customer=self.fixture.customer,
        )
        # The three limit components the plugin registers; without them every
        # limit key is rejected as an invalid type before a processor sees it.
        for component_type in ("cpu", "ram", "disk"):
            marketplace_factories.OfferingComponentFactory(
                offering=self.offering,
                type=component_type,
                name=component_type.upper(),
                billing_type=BillingTypes.LIMIT,
            )
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            scope=self.vm,
            project=self.fixture.project,
            state=ResourceStates.OK,
            limits={
                "cpu": self.vm.cores,
                "ram": self.vm.ram,
                "disk": self.vm.total_disk,
            },
        )


class VirtualMachineUpdateProcessorTest(BaseVirtualMachineOrderTest):
    def create_order(self, limits):
        return marketplace_factories.OrderFactory(
            type=OrderTypes.UPDATE,
            project=self.fixture.project,
            offering=self.offering,
            resource=self.resource,
            plan=self.resource.plan,
            limits=limits,
            attributes={"old_limits": self.resource.limits},
            state=OrderStates.EXECUTING,
        )

    def process(self, order):
        with mock.patch.object(
            vmware_executors.VirtualMachineUpdateExecutor, "execute"
        ) as executor:
            with self.captureOnCommitCallbacks(execute=True):
                marketplace_utils.process_order(order, self.fixture.staff)
        order.refresh_from_db()
        self.vm.refresh_from_db()
        return executor

    def test_new_limits_are_pushed_to_the_virtual_machine(self):
        order = self.create_order(
            {"cpu": 4, "ram": 2048, "disk": self.vm.total_disk},
        )

        executor = self.process(order)

        self.assertEqual(order.state, OrderStates.EXECUTING, order.error_message)
        self.assertEqual(self.vm.cores, 4)
        self.assertEqual(self.vm.ram, 2048)
        self.assertTrue(executor.called)
        self.assertEqual(
            {"cores", "ram"}, executor.call_args[1]["updated_fields"] & {"cores", "ram"}
        )

    def test_order_is_completed_when_virtual_machine_becomes_ok(self):
        order = self.create_order(
            {"cpu": 4, "ram": 2048, "disk": self.vm.total_disk},
        )
        self.process(order)

        # The executor moves the machine through the updating state; the
        # marketplace picks the transition back to OK up as the end of the order.
        self.vm.state = CoreStates.UPDATING
        self.vm.save()
        self.vm.state = CoreStates.OK
        self.vm.save()

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.DONE, order.error_message)

    def test_limits_update_is_rejected_for_a_running_virtual_machine(self):
        self.vm.runtime_state = vmware_models.VirtualMachine.RuntimeStates.POWERED_ON
        self.vm.save()
        order = self.create_order(
            {"cpu": 4, "ram": 2048, "disk": self.vm.total_disk},
        )

        self.process(order)

        self.assertEqual(order.state, OrderStates.ERRED)
        self.assertIn("POWERED_OFF", order.error_message)
        self.assertEqual(self.vm.cores, self.resource.limits["cpu"])

    def test_plan_switch_is_completed_without_touching_the_backend(self):
        new_plan = marketplace_factories.PlanFactory(offering=self.offering)
        order = marketplace_factories.OrderFactory(
            type=OrderTypes.UPDATE,
            project=self.fixture.project,
            offering=self.offering,
            resource=self.resource,
            old_plan=self.resource.plan,
            plan=new_plan,
            limits=self.resource.limits,
            state=OrderStates.EXECUTING,
        )

        executor = self.process(order)

        self.assertEqual(order.state, OrderStates.DONE, order.error_message)
        self.assertFalse(executor.called)

    def test_order_that_leaves_the_disk_alone_is_accepted(self):
        # The machine reports no disks of its own, so this is only valid
        # against the limits the resource carries.
        order = self.create_order(
            {"cpu": 4, "ram": 2048, "disk": self.resource.limits["disk"]}
        )
        processor = marketplace_utils.get_order_processor(order)

        processor(order).validate_order(mock.Mock())

    def test_disk_change_is_rejected(self):
        order = self.create_order({"cpu": 4, "ram": 2048, "disk": 10240})
        processor = marketplace_utils.get_order_processor(order)

        # Matched on the message: an unrelated rejection -- an unknown limit
        # type, say -- would otherwise pass for the disk check.
        with self.assertRaisesRegex(
            exceptions.ValidationError, "Disk size cannot be changed"
        ):
            processor(order).validate_order(mock.Mock())


class VirtualMachineDeleteProcessorTest(BaseVirtualMachineOrderTest):
    def setUp(self):
        super().setUp()
        self.order = marketplace_factories.OrderFactory(
            type=OrderTypes.TERMINATE,
            project=self.fixture.project,
            offering=self.offering,
            resource=self.resource,
            plan=self.resource.plan,
            state=OrderStates.EXECUTING,
        )

    def test_deletion_is_scheduled_in_the_backend(self):
        with mock.patch.object(
            vmware_executors.VirtualMachineDeleteExecutor, "execute"
        ) as executor:
            marketplace_utils.process_order(self.order, self.fixture.staff)

        self.order.refresh_from_db()
        self.assertNotEqual(
            self.order.state, OrderStates.ERRED, self.order.error_message
        )
        self.assertTrue(executor.called)
        self.assertEqual(executor.call_args[0][0], self.vm)

    def test_termination_of_a_running_virtual_machine_is_refused(self):
        self.vm.runtime_state = vmware_models.VirtualMachine.RuntimeStates.POWERED_ON
        self.vm.save()
        processor = marketplace_utils.get_order_processor(self.order)

        # The plugin refuses to delete a running machine, and the order is
        # refused at validation rather than erring halfway through.
        with self.assertRaises(core_exceptions.IncorrectStateException):
            processor(self.order).validate_order(mock.Mock())

    def test_resource_is_terminated_when_the_virtual_machine_is_gone(self):
        with mock.patch.object(
            vmware_executors.VirtualMachineDeleteExecutor, "execute"
        ):
            marketplace_utils.process_order(self.order, self.fixture.staff)

        self.vm.delete()

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.state, ResourceStates.TERMINATED)
        self.assertEqual(
            marketplace_models.Order.objects.get(pk=self.order.pk).state,
            OrderStates.DONE,
        )
