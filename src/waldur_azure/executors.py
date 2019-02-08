from __future__ import unicode_literals

from celery import chain

from waldur_core.core import executors as core_executors, \
    tasks as core_tasks, utils as core_utils
from waldur_core.structure import executors as structure_executors

from . import models


class VirtualMachineCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        serialized_resource_group = core_utils.serialize_instance(instance.resource_group)
        serialized_storage_account = core_utils.serialize_instance(
            instance.resource_group.storageaccount_set.get())
        serialized_network = core_utils.serialize_instance(
            instance.network_interface.subnet.network)
        serialized_subnet = core_utils.serialize_instance(
            instance.network_interface.subnet)
        serialized_public_ip = core_utils.serialize_instance(
            instance.network_interface.public_ip)
        serialized_nic = core_utils.serialize_instance(
            instance.network_interface)

        return chain(
            core_tasks.StateTransitionTask().si(
                serialized_instance,
                state_transition='begin_creating'
            ),
            core_tasks.BackendMethodTask().si(
                serialized_resource_group,
                backend_method='create_resource_group',
                state_transition='begin_creating',
            ),
            core_tasks.StateTransitionTask().si(
                serialized_resource_group,
                state_transition='set_ok',
            ),
            core_tasks.BackendMethodTask().si(
                serialized_storage_account,
                backend_method='create_storage_account',
                state_transition='begin_creating',
            ),
            core_tasks.StateTransitionTask().si(
                serialized_storage_account,
                state_transition='set_ok',
            ),
            core_tasks.BackendMethodTask().si(
                serialized_network,
                backend_method='create_network',
                state_transition='begin_creating',
            ),
            core_tasks.StateTransitionTask().si(
                serialized_network,
                state_transition='set_ok',
            ),
            core_tasks.BackendMethodTask().si(
                serialized_subnet,
                backend_method='create_subnet',
                state_transition='begin_creating',
            ),
            core_tasks.StateTransitionTask().si(
                serialized_subnet,
                state_transition='set_ok',
            ),
            core_tasks.BackendMethodTask().si(
                serialized_public_ip,
                backend_method='create_public_ip',
                state_transition='begin_creating',
            ),
            core_tasks.StateTransitionTask().si(
                serialized_public_ip,
                state_transition='set_ok',
            ),
            core_tasks.BackendMethodTask().si(
                serialized_nic,
                backend_method='create_network_interface',
                state_transition='begin_creating',
            ),
            core_tasks.StateTransitionTask().si(
                serialized_nic,
                state_transition='set_ok',
            ),
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                backend_method='create_virtual_machine',
            ),
            core_tasks.BackendMethodTask().si(
                serialized_nic,
                backend_method='pull_network_interface',
            ),
            core_tasks.BackendMethodTask().si(
                serialized_public_ip,
                backend_method='pull_public_ip_address',
            ),
        )


class VirtualMachineDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id:
            return core_tasks.BackendMethodTask().si(
                serialized_instance,
                backend_method='delete_virtual_machine',
                state_transition='begin_deleting',
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance, state_transition='begin_deleting')


class VirtualMachineStartExecutor(core_executors.ActionExecutor):
    action = 'Start'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_instance,
            backend_method='start_virtual_machine',
            state_transition='begin_updating',
        )


class VirtualMachineStopExecutor(core_executors.ActionExecutor):
    action = 'Stop'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_instance,
            backend_method='stop_virtual_machine',
            state_transition='begin_updating',
        )


class VirtualMachineRestartExecutor(core_executors.ActionExecutor):
    action = 'Restart'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_instance,
            backend_method='restart_virtual_machine',
            state_transition='begin_updating',
        )


class PublicIPCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_instance,
            backend_method='create_public_ip',
            state_transition='begin_creating',
        )


class PublicIPDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id:
            return core_tasks.BackendMethodTask().si(
                serialized_instance,
                backend_method='delete_public_ip',
                state_transition='begin_deleting',
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance, state_transition='begin_deleting')


class SQLServerCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        serialized_resource_group = core_utils.serialize_instance(instance.resource_group)
        return chain(
            core_tasks.StateTransitionTask().si(
                serialized_instance,
                state_transition='begin_creating'
            ),
            core_tasks.BackendMethodTask().si(
                serialized_resource_group,
                backend_method='create_resource_group',
                state_transition='begin_creating',
            ),
            core_tasks.StateTransitionTask().si(
                serialized_resource_group,
                state_transition='set_ok',
            ),
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                backend_method='create_pgsql_server',
            ),
        )


class SQLServerDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id:
            return core_tasks.BackendMethodTask().si(
                serialized_instance,
                backend_method='delete_pgsql_server',
                state_transition='begin_deleting',
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance, state_transition='begin_deleting')


class SQLDatabaseCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_instance,
            backend_method='create_pgsql_database',
            state_transition='begin_creating',
        )


class SQLDatabaseDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        if instance.backend_id:
            return core_tasks.BackendMethodTask().si(
                serialized_instance,
                backend_method='delete_pgsql_database',
                state_transition='begin_deleting',
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance, state_transition='begin_deleting')


class AzureCleanupExecutor(structure_executors.BaseCleanupExecutor):
    executors = (
        (models.VirtualMachine, VirtualMachineDeleteExecutor),
        (models.SQLDatabase, SQLDatabaseDeleteExecutor),
        (models.SQLServer, SQLServerDeleteExecutor),
    )
