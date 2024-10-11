import logging

from celery import chain
from django.db import transaction

from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils
from waldur_core.structure import executors as structure_executors
from waldur_openstack import executors as openstack_executors

from . import models, tasks

logger = logging.getLogger(__name__)


class SecurityGroupCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, security_group, serialized_security_group, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_security_group,
            "create_security_group",
            state_transition="begin_creating",
        )


class ServerGroupCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, server_group, serialized_server_group, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_server_group,
            "create_server_group",
            state_transition="begin_creating",
        )


class ServerGroupDeleteExecutor(core_executors.DeleteExecutor):
    @classmethod
    def get_task_signature(cls, server_group, serialized_server_group, **kwargs):
        if server_group.backend_id:
            return core_tasks.BackendMethodTask().si(
                serialized_server_group,
                "delete_server_group",
                state_transition="begin_deleting",
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_server_group, state_transition="begin_deleting"
            )


class SecurityGroupUpdateExecutor(core_executors.UpdateExecutor):
    @classmethod
    def get_task_signature(cls, security_group, serialized_security_group, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_security_group,
            "update_security_group",
            state_transition="begin_updating",
        )


class SecurityGroupPullExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, security_group, serialized_security_group, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_security_group,
            "pull_security_group",
            state_transition="begin_updating",
        )


class ServerGroupPullExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, server_group, serialized_server_group, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_server_group,
            "pull_server_group",
            state_transition="begin_updating",
        )


class SecurityGroupDeleteExecutor(core_executors.BaseExecutor):
    """
    Security group is being deleted in the last task instead of
    using separate DeleteTask from DeleteExecutorMixin so that
    deletion is performed transactionally.
    """

    @classmethod
    def pre_apply(cls, instance, **kwargs):
        instance.schedule_deleting()
        instance.save(update_fields=["state"])

    @classmethod
    def get_failure_signature(
        cls, instance, serialized_instance, force=False, **kwargs
    ):
        return core_tasks.ErrorStateTransitionTask().s(serialized_instance)

    @classmethod
    def get_task_signature(cls, security_group, serialized_security_group, **kwargs):
        state_transition_task = core_tasks.StateTransitionTask().si(
            serialized_security_group, state_transition="begin_deleting"
        )
        detach_task = core_tasks.BackendMethodTask().si(
            serialized_security_group, "detach_security_group_from_all_instances"
        )
        detach_ports_task = core_tasks.BackendMethodTask().si(
            serialized_security_group, "detach_security_group_from_all_ports"
        )
        delete_task = core_tasks.BackendMethodTask().si(
            serialized_security_group, "delete_security_group"
        )
        _tasks = [state_transition_task]
        if security_group.backend_id:
            _tasks.append(detach_task)
            _tasks.append(detach_ports_task)
            _tasks.append(delete_task)
        return chain(*_tasks)


class PushSecurityGroupRulesExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, security_group, serialized_security_group, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_security_group,
            "push_security_group_rules",
            state_transition="begin_updating",
        )


class TenantCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(
        cls, tenant, serialized_tenant, pull_security_groups=True, **kwargs
    ):
        """Create tenant, add user to it, create internal network, pull quotas"""
        # we assume that tenant one network and subnet after creation
        network = tenant.networks.first()
        subnet = network.subnets.first()
        serialized_network = core_utils.serialize_instance(network)
        serialized_subnet = core_utils.serialize_instance(subnet)
        creation_tasks = [
            core_tasks.BackendMethodTask().si(
                serialized_tenant,
                "create_tenant_safe",
                state_transition="begin_creating",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "add_admin_user_to_tenant"
            ),
            core_tasks.BackendMethodTask().si(serialized_tenant, "create_tenant_user"),
            core_tasks.BackendMethodTask().si(
                serialized_network, "create_network", state_transition="begin_creating"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_subnet, "create_subnet", state_transition="begin_creating"
            ),
        ]
        creation_tasks.append(
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "push_tenant_quotas", tenant.quota_limits
            )
        )
        # handle security groups
        # XXX: Create default security groups
        for security_group in tenant.security_groups.all():
            creation_tasks.append(
                SecurityGroupCreateExecutor.as_signature(security_group)
            )

        if pull_security_groups:
            creation_tasks.append(
                core_tasks.BackendMethodTask().si(
                    serialized_tenant, "pull_tenant_security_groups"
                )
            )

        # initialize external network if it defined in service settings
        service_settings = tenant.service_settings
        customer = tenant.project.customer
        external_network_id = service_settings.get_option("external_network_id")

        try:
            customer_openstack = models.CustomerOpenStack.objects.get(
                settings=service_settings, customer=customer
            )
            external_network_id = customer_openstack.external_network_id
        except models.CustomerOpenStack.DoesNotExist:
            pass

        if external_network_id and not kwargs.get("skip_connection_extnet"):
            creation_tasks.append(
                core_tasks.BackendMethodTask().si(
                    serialized_tenant,
                    "connect_tenant_to_external_network",
                    external_network_id=external_network_id,
                )
            )
            creation_tasks.append(
                core_tasks.BackendMethodTask().si(
                    serialized_tenant,
                    backend_method="pull_tenant_routers",
                )
            )

        creation_tasks += [
            core_tasks.BackendMethodTask().si(serialized_tenant, "pull_tenant_quotas"),
            core_tasks.BackendMethodTask().si(serialized_tenant, "pull_tenant_images"),
            core_tasks.BackendMethodTask().si(serialized_tenant, "pull_tenant_flavors"),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_volume_types"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_instance_availability_zones"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_volume_availability_zones"
            ),
        ]
        return chain(*creation_tasks)

    @classmethod
    def get_success_signature(cls, tenant, serialized_tenant, **kwargs):
        return tasks.TenantCreateSuccessTask().si(serialized_tenant)

    @classmethod
    def get_failure_signature(cls, tenant, serialized_tenant, **kwargs):
        return tasks.TenantCreateErrorTask().s(serialized_tenant)


class TenantImportExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, tenant, serialized_tenant, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_tenant,
                "add_admin_user_to_tenant",
                state_transition="begin_updating",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "create_or_update_tenant_user"
            ),
            core_tasks.BackendMethodTask().si(serialized_tenant, "pull_tenant_quotas"),
            core_tasks.BackendMethodTask().si(serialized_tenant, "pull_tenant_images"),
            core_tasks.BackendMethodTask().si(serialized_tenant, "pull_tenant_flavors"),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_volume_types"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_floating_ips"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_security_groups"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_server_groups"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "import_tenant_networks"
            ),
            core_tasks.BackendMethodTask().si(serialized_tenant, "pull_tenant_subnets"),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "detect_external_network"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, backend_method="pull_tenant_routers"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, backend_method="pull_tenant_ports"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_instance_availability_zones"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_volume_availability_zones"
            ),
            core_tasks.BackendMethodTask().si(serialized_tenant, "pull_tenant_volumes"),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_snapshots"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_instances"
            ),
        )

    @classmethod
    def get_success_signature(cls, tenant, serialized_tenant, **kwargs):
        return core_tasks.StateTransitionTask().si(
            serialized_tenant, state_transition="set_ok"
        )


class TenantUpdateExecutor(core_executors.UpdateExecutor):
    @classmethod
    def get_task_signature(cls, tenant, serialized_tenant, **kwargs):
        updated_fields = kwargs["updated_fields"]
        if "name" in updated_fields or "description" in updated_fields:
            return core_tasks.BackendMethodTask().si(
                serialized_tenant, "update_tenant", state_transition="begin_updating"
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_tenant, state_transition="begin_updating"
            )


class TenantDeleteExecutor(core_executors.DeleteExecutor):
    @classmethod
    def get_task_signature(cls, tenant, serialized_tenant, **kwargs):
        state_transition = core_tasks.StateTransitionTask().si(
            serialized_tenant, state_transition="begin_deleting"
        )
        if not tenant.backend_id:
            return state_transition

        cleanup_networks = cls.get_networks_cleanup_tasks(serialized_tenant)
        cleanup_instances = cls.get_instances_cleanup_tasks(serialized_tenant)
        cleanup_identities = cls.get_identity_cleanup_tasks(serialized_tenant)

        return chain(
            [state_transition]
            + cleanup_networks
            + cleanup_instances
            + cleanup_identities
        )

    @classmethod
    def get_networks_cleanup_tasks(cls, serialized_tenant):
        return [
            core_tasks.BackendMethodTask().si(
                serialized_tenant,
                backend_method="delete_tenant_floating_ips",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant,
                backend_method="delete_tenant_routes",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant,
                backend_method="delete_tenant_ports",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant,
                backend_method="delete_tenant_routers",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant,
                backend_method="pull_tenant_routers",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant,
                backend_method="delete_tenant_networks",
            ),
        ]

    @classmethod
    def get_instances_cleanup_tasks(cls, serialized_tenant):
        return [
            core_tasks.BackendMethodTask().si(
                serialized_tenant,
                backend_method="delete_tenant_security_groups",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant,
                backend_method="delete_tenant_snapshots",
            ),
            core_tasks.PollBackendCheckTask().si(
                serialized_tenant,
                backend_check_method="are_all_tenant_snapshots_deleted",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant,
                backend_method="delete_tenant_instances",
            ),
            core_tasks.PollBackendCheckTask().si(
                serialized_tenant,
                backend_check_method="are_all_tenant_instances_deleted",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant,
                backend_method="delete_tenant_volumes",
            ),
            core_tasks.PollBackendCheckTask().si(
                serialized_tenant, backend_check_method="are_all_tenant_volumes_deleted"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant,
                backend_method="delete_tenant_server_groups",
            ),
        ]

    @classmethod
    def get_identity_cleanup_tasks(cls, serialized_tenant):
        return [
            core_tasks.BackendMethodTask().si(
                serialized_tenant,
                backend_method="delete_tenant_user",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant,
                backend_method="delete_tenant",
            ),
        ]


class TenantAllocateFloatingIPExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, tenant, serialized_tenant, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_tenant,
            "allocate_floating_ip_address",
            state_transition="begin_updating",
        )


class FloatingIPCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, floating_ip, serialized_floating_ip, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_floating_ip,
            "create_floating_ip",
            state_transition="begin_creating",
        )


class FloatingIPUpdateExecutor(core_executors.UpdateExecutor):
    @classmethod
    def get_task_signature(cls, floating_ip, serialized_floating_ip, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_floating_ip,
            "update_floating_ip_description",
            state_transition="begin_updating",
            serialized_description=kwargs.get("description"),
        )


class FloatingIPDeleteExecutor(core_executors.DeleteExecutor):
    @classmethod
    def get_task_signature(cls, floating_ip, serialized_floating_ip, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_floating_ip,
            "delete_floating_ip",
            state_transition="begin_deleting",
        )


class FloatingIPPullExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, floating_ip, serialized_floating_ip, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_floating_ip,
            "pull_floating_ip",
            state_transition="begin_updating",
        )


class FloatingIPAttachExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, floating_ip, serialized_floating_ip, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_floating_ip,
            "attach_floating_ip_to_port",
            state_transition="begin_updating",
            serialized_port=kwargs.get("port"),
        )


class FloatingIPDetachExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, floating_ip, serialized_floating_ip, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_floating_ip,
            "detach_floating_ip_from_port",
            state_transition="begin_updating",
        )


class TenantPullFloatingIPsExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, tenant, serialized_tenant, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_tenant,
            "pull_tenant_floating_ips",
            state_transition="begin_updating",
        )


class TenantPushQuotasExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, tenant, serialized_tenant, quotas=None, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_tenant,
            "push_tenant_quotas",
            quotas,
            state_transition="begin_updating",
        )


class TenantPullQuotasExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, tenant, serialized_tenant, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_tenant, "pull_tenant_quotas", state_transition="begin_updating"
        )


class ExistingTenantPullExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, tenant, serialized_tenant, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant", state_transition="begin_updating"
            ),
            core_tasks.BackendMethodTask().si(serialized_tenant, "pull_tenant_quotas"),
            core_tasks.BackendMethodTask().si(serialized_tenant, "pull_tenant_images"),
            core_tasks.BackendMethodTask().si(serialized_tenant, "pull_tenant_flavors"),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_volume_types"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_floating_ips"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_security_groups"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_server_groups"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_networks"
            ),
            core_tasks.BackendMethodTask().si(serialized_tenant, "pull_subnets"),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, backend_method="pull_tenant_routers"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, backend_method="pull_tenant_ports"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_instance_availability_zones"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_volume_availability_zones"
            ),
            core_tasks.BackendMethodTask().si(serialized_tenant, "pull_tenant_volumes"),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_snapshots"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_instances"
            ),
        )

    @classmethod
    def get_success_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            core_tasks.StateTransitionTask().si(
                serialized_instance,
                state_transition="set_ok",
                action="",
                action_details={},
            ),
            tasks.SendSignalTenantPullSucceeded().si(serialized_instance),
        )


class TenantPullExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, tenant, serialized_tenant, **kwargs):
        return tasks.check_existence_of_tenant.si(serialized_tenant)

    @classmethod
    def get_success_signature(cls, instance, serialized_instance, **kwargs):
        return ExistingTenantPullExecutor.as_signature(instance)

    @classmethod
    def get_failure_signature(cls, instance, serialized_instance, **kwargs):
        return tasks.mark_tenant_as_deleted.si(serialized_instance)


class TenantPullSecurityGroupsExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, tenant, serialized_tenant, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_tenant,
            "pull_tenant_security_groups",
            state_transition="begin_updating",
        )


class TenantPullServerGroupsExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, tenant, serialized_tenant, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_tenant,
            "pull_tenant_server_groups",
            state_transition="begin_updating",
        )


class TenantDetectExternalNetworkExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, tenant, serialized_tenant, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_tenant,
            "detect_external_network",
            state_transition="begin_updating",
        )


class TenantChangeUserPasswordExecutor(core_executors.ActionExecutor):
    @classmethod
    def get_task_signature(cls, tenant, serialized_tenant, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_tenant,
            "change_tenant_user_password",
            state_transition="begin_updating",
        )


class RouterCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, router, serialized_router, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_router,
            "create_router",
            state_transition="begin_creating",
        )


class RouterSetRoutesExecutor(core_executors.ActionExecutor):
    action = "set_static_routes"

    @classmethod
    def get_task_signature(cls, router, serialized_router, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_router, "set_static_routes", state_transition="begin_updating"
        )


class NetworkCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, network, serialized_network, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_network, "create_network", state_transition="begin_creating"
        )


class NetworkUpdateExecutor(core_executors.UpdateExecutor):
    @classmethod
    def get_task_signature(cls, network, serialized_network, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_network, "update_network", state_transition="begin_updating"
        )


class NetworkDeleteExecutor(core_executors.DeleteExecutor):
    @classmethod
    def get_task_signature(cls, network, serialized_network, **kwargs):
        if network.backend_id:
            return core_tasks.BackendMethodTask().si(
                serialized_network, "delete_network", state_transition="begin_deleting"
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_network, state_transition="begin_deleting"
            )


class NetworkPullExecutor(core_executors.ActionExecutor):
    action = "pull"

    @classmethod
    def get_task_signature(cls, network, serialized_network, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_network, "pull_network", state_transition="begin_updating"
        )


class SetMtuExecutor(core_executors.ActionExecutor):
    action = "set_mtu"

    @classmethod
    def get_task_signature(cls, network, serialized_network, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_network, "set_network_mtu", state_transition="begin_updating"
        )


class SubNetCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, subnet, serialized_subnet, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_subnet,
            "create_subnet",
            state_transition="begin_creating",
        )


class SubNetUpdateExecutor(core_executors.UpdateExecutor):
    @classmethod
    def get_task_signature(cls, subnet, serialized_subnet, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_subnet,
            "update_subnet",
            state_transition="begin_updating",
        )


class SubnetConnectExecutor(core_executors.ActionExecutor):
    action = "connect"

    @classmethod
    def get_task_signature(cls, subnet, serialized_subnet, **kwargs):
        serialized_tenant = core_utils.serialize_instance(subnet.network.tenant)
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_subnet,
                "connect_subnet",
                state_transition="begin_updating",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, backend_method="pull_tenant_routers"
            ),
        )


class SubnetDisconnectExecutor(core_executors.ActionExecutor):
    action = "disconnect"

    @classmethod
    def get_task_signature(cls, subnet, serialized_subnet, **kwargs):
        serialized_tenant = core_utils.serialize_instance(subnet.network.tenant)
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_subnet,
                "disconnect_subnet",
                state_transition="begin_updating",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_tenant, backend_method="pull_tenant_routers"
            ),
        )


class SubNetDeleteExecutor(core_executors.DeleteExecutor):
    @classmethod
    def get_task_signature(cls, subnet, serialized_subnet, **kwargs):
        if subnet.backend_id:
            return core_tasks.BackendMethodTask().si(
                serialized_subnet, "delete_subnet", state_transition="begin_deleting"
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_subnet, state_transition="begin_deleting"
            )


class SubNetPullExecutor(core_executors.ActionExecutor):
    action = "pull"

    @classmethod
    def get_task_signature(cls, subnet, serialized_subnet, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_subnet, "pull_subnet", state_transition="begin_updating"
        )


class PortCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, port, serialized_port, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_port,
            "create_port",
            state_transition="begin_creating",
            serialized_network=kwargs.get("network"),
        )


class PortDeleteExecutor(core_executors.DeleteExecutor):
    @classmethod
    def get_task_signature(cls, port, serialized_port, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_port,
            "delete_port",
            state_transition="begin_deleting",
        )


class VolumeCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return chain(
            tasks.ThrottleProvisionTask().si(
                serialized_volume, "create_volume", state_transition="begin_creating"
            ),
            core_tasks.PollRuntimeStateTask()
            .si(
                serialized_volume,
                backend_pull_method="pull_volume_runtime_state",
                success_state="available",
                erred_state="error",
            )
            .set(countdown=30),
        )


class VolumeUpdateExecutor(core_executors.UpdateExecutor):
    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        updated_fields = kwargs["updated_fields"]
        if "name" in updated_fields or "description" in updated_fields:
            return core_tasks.BackendMethodTask().si(
                serialized_volume, "update_volume", state_transition="begin_updating"
            )
        if "bootable" in updated_fields:
            return core_tasks.BackendMethodTask().si(
                serialized_volume,
                "toggle_bootable_flag",
                state_transition="begin_updating",
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_volume, state_transition="begin_updating"
            )


class VolumeDeleteExecutor(core_executors.DeleteExecutor):
    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        if volume.backend_id:
            return chain(
                core_tasks.BackendMethodTask().si(
                    serialized_volume,
                    "delete_volume",
                    state_transition="begin_deleting",
                ),
                core_tasks.PollBackendCheckTask().si(
                    serialized_volume, "is_volume_deleted"
                ),
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_volume, state_transition="begin_deleting"
            )


class VolumePullExecutor(core_executors.ActionExecutor):
    action = "Pull"

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_volume, "pull_volume", state_transition="begin_updating"
        )


class VolumeExtendExecutor(core_executors.ActionExecutor):
    action = "Extend"

    @classmethod
    def get_action_details(cls, volume, **kwargs):
        return {
            "message": "Extend volume from {} MB to {} MB".format(
                kwargs.get("old_size"), volume.size
            ),
            "old_size": kwargs.get("old_size"),
            "new_size": volume.size,
        }

    @classmethod
    def pre_apply(cls, volume, **kwargs):
        super().pre_apply(volume, **kwargs)
        if volume.instance is not None:
            volume.instance.action = "Extend volume"
            volume.instance.schedule_updating()
            volume.instance.save()

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        if volume.instance is None:
            return chain(
                core_tasks.BackendMethodTask().si(
                    serialized_volume,
                    backend_method="extend_volume",
                    state_transition="begin_updating",
                ),
                core_tasks.PollRuntimeStateTask().si(
                    serialized_volume,
                    backend_pull_method="pull_volume_runtime_state",
                    success_state="available",
                    erred_state="error",
                ),
            )

        return chain(
            core_tasks.StateTransitionTask().si(
                core_utils.serialize_instance(volume.instance),
                state_transition="begin_updating",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_volume,
                backend_method="detach_volume",
                state_transition="begin_updating",
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_volume,
                backend_pull_method="pull_volume_runtime_state",
                success_state="available",
                erred_state="error",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_volume,
                backend_method="extend_volume",
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_volume,
                backend_pull_method="pull_volume_runtime_state",
                success_state="available",
                erred_state="error",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_volume,
                instance_uuid=volume.instance.uuid.hex,
                device=volume.device,
                backend_method="attach_volume",
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_volume,
                backend_pull_method="pull_volume_runtime_state",
                success_state="in-use",
                erred_state="error",
            ),
        )

    @classmethod
    def get_success_signature(cls, volume, serialized_volume, **kwargs):
        if volume.instance is None:
            return super().get_success_signature(volume, serialized_volume, **kwargs)
        else:
            instance = volume.instance
            serialized_instance = core_utils.serialize_instance(instance)
            return chain(
                super().get_success_signature(volume, serialized_volume, **kwargs),
                super().get_success_signature(instance, serialized_instance, **kwargs),
            )

    @classmethod
    def get_failure_signature(cls, volume, serialized_volume, **kwargs):
        return tasks.VolumeExtendErredTask().s(serialized_volume)


class VolumeAttachExecutor(core_executors.ActionExecutor):
    action = "Attach"

    @classmethod
    def get_action_details(cls, volume, **kwargs):
        return {"message": "Attach volume to instance %s" % volume.instance.name}

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_volume,
                instance_uuid=volume.instance.uuid.hex,
                device=volume.device,
                backend_method="attach_volume",
                state_transition="begin_updating",
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_volume,
                backend_pull_method="pull_volume_runtime_state",
                success_state="in-use",
                erred_state="error",
            ),
            # additional pull to populate field "device".
            core_tasks.BackendMethodTask().si(
                serialized_volume, backend_method="pull_volume"
            ),
        )


class VolumeDetachExecutor(core_executors.ActionExecutor):
    action = "Detach"

    @classmethod
    def get_action_details(cls, volume, **kwargs):
        return {"message": "Detach volume from instance %s" % volume.instance.name}

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_volume,
                backend_method="detach_volume",
                state_transition="begin_updating",
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_volume,
                backend_pull_method="pull_volume_runtime_state",
                success_state="available",
                erred_state="error",
            ),
        )


class VolumeRetypeExecutor(core_executors.ActionExecutor):
    action = "Retype"

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_volume, "retype_volume", state_transition="begin_updating"
            ),
            core_tasks.PollRuntimeStateTask()
            .si(
                serialized_volume,
                backend_pull_method="pull_volume_runtime_state",
                success_state="available",
                erred_state="error",
            )
            .set(countdown=10),
            core_tasks.BackendMethodTask().si(
                serialized_volume,
                "pull_volume",
            ),
        )


class SnapshotCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, snapshot, serialized_snapshot, **kwargs):
        return chain(
            tasks.ThrottleProvisionTask().si(
                serialized_snapshot,
                "create_snapshot",
                state_transition="begin_creating",
            ),
            core_tasks.PollRuntimeStateTask()
            .si(
                serialized_snapshot,
                backend_pull_method="pull_snapshot_runtime_state",
                success_state="available",
                erred_state="error",
            )
            .set(countdown=10),
        )


class SnapshotUpdateExecutor(core_executors.UpdateExecutor):
    @classmethod
    def get_task_signature(cls, snapshot, serialized_snapshot, **kwargs):
        updated_fields = kwargs["updated_fields"]
        # TODO: call separate task on metadata update
        if "name" in updated_fields or "description" in updated_fields:
            return core_tasks.BackendMethodTask().si(
                serialized_snapshot,
                "update_snapshot",
                state_transition="begin_updating",
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_snapshot, state_transition="begin_updating"
            )


class SnapshotDeleteExecutor(core_executors.DeleteExecutor):
    @classmethod
    def get_task_signature(cls, snapshot, serialized_snapshot, **kwargs):
        if snapshot.backend_id:
            return chain(
                core_tasks.BackendMethodTask().si(
                    serialized_snapshot,
                    "delete_snapshot",
                    state_transition="begin_deleting",
                ),
                core_tasks.PollBackendCheckTask().si(
                    serialized_snapshot, "is_snapshot_deleted"
                ),
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_snapshot, state_transition="begin_deleting"
            )


class SnapshotPullExecutor(core_executors.ActionExecutor):
    action = "Pull"

    @classmethod
    def get_task_signature(cls, snapshot, serialized_snapshot, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_snapshot, "pull_snapshot", state_transition="begin_updating"
        )


class InstanceCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(
        cls,
        instance,
        serialized_instance,
        ssh_key=None,
        flavor=None,
        server_group=None,
    ):
        serialized_volumes = [
            core_utils.serialize_instance(volume) for volume in instance.volumes.all()
        ]
        _tasks = [
            tasks.ThrottleProvisionStateTask().si(
                serialized_instance, state_transition="begin_creating"
            )
        ]
        _tasks += cls.create_volumes(serialized_volumes)
        _tasks += cls.create_ports(serialized_instance)
        _tasks += cls.create_instance(
            serialized_instance, flavor, ssh_key, server_group
        )
        _tasks += cls.pull_volumes(serialized_volumes)
        _tasks += cls.pull_security_groups(serialized_instance)
        _tasks += cls.create_floating_ips(instance, serialized_instance)
        _tasks += cls.pull_server_group(serialized_instance)
        _tasks += cls.pull_instance(serialized_instance)
        return chain(*_tasks)

    @classmethod
    def create_volumes(cls, serialized_volumes):
        """
        Create all instance volumes and wait for them to provision.
        """
        _tasks = []

        # Create volumes
        for serialized_volume in serialized_volumes:
            _tasks.append(
                tasks.ThrottleProvisionTask().si(
                    serialized_volume,
                    "create_volume",
                    state_transition="begin_creating",
                )
            )

        for index, serialized_volume in enumerate(serialized_volumes):
            # Wait for volume creation
            _tasks.append(
                core_tasks.PollRuntimeStateTask()
                .si(
                    serialized_volume,
                    backend_pull_method="pull_volume_runtime_state",
                    success_state="available",
                    erred_state="error",
                )
                .set(countdown=30 if index == 0 else 0)
            )

            # Pull volume runtime state
            _tasks.append(
                core_tasks.BackendMethodTask().si(
                    serialized_volume,
                    "pull_volume",
                    update_fields=["runtime_state", "bootable"],
                )
            )

            # Mark volume as OK
            _tasks.append(
                core_tasks.StateTransitionTask().si(
                    serialized_volume, state_transition="set_ok"
                )
            )

        return _tasks

    @classmethod
    def create_ports(cls, serialized_instance):
        """
        Create all network ports for an OpenStack instance.
        Although OpenStack Nova REST API allows to create network ports implicitly,
        we're not using it, because it does not take into account subnets.
        See also: https://specs.openstack.org/openstack/nova-specs/specs/juno/approved/selecting-subnet-when-creating-vm.html
        Therefore we're creating network ports beforehand with correct subnet.
        """
        return [
            core_tasks.BackendMethodTask().si(
                serialized_instance, "create_instance_ports"
            )
        ]

    @classmethod
    def create_instance(
        cls, serialized_instance, flavor, ssh_key=None, server_group=None
    ):
        """
        It is assumed that volumes and network ports have been created beforehand.
        """
        _tasks = []
        kwargs = {
            "backend_flavor_id": flavor.backend_id,
        }
        if ssh_key is not None:
            kwargs["public_key"] = ssh_key.public_key

        if server_group is not None:
            kwargs["server_group"] = server_group.backend_id

        # Wait 10 seconds after volume creation due to OpenStack restrictions.
        _tasks.append(
            core_tasks.BackendMethodTask()
            .si(serialized_instance, "create_instance", **kwargs)
            .set(countdown=10)
        )

        # Wait for instance creation
        _tasks.append(
            core_tasks.PollRuntimeStateTask().si(
                serialized_instance,
                backend_pull_method="pull_instance_runtime_state",
                success_state=models.Instance.RuntimeStates.ACTIVE,
                erred_state=models.Instance.RuntimeStates.ERROR,
            )
        )
        return _tasks

    @classmethod
    def pull_volumes(cls, serialized_volumes):
        """
        Update volumes runtime state and device name
        """
        _tasks = []
        for serialized_volume in serialized_volumes:
            _tasks.append(
                core_tasks.BackendMethodTask().si(
                    serialized_volume,
                    backend_method="pull_volume",
                    update_fields=["runtime_state", "device"],
                )
            )
        return _tasks

    @classmethod
    def pull_security_groups(cls, serialized_instance):
        return [
            core_tasks.BackendMethodTask().si(
                serialized_instance, "pull_instance_security_groups"
            )
        ]

    @classmethod
    def create_floating_ips(cls, instance, serialized_instance):
        _tasks = []

        if not instance.floating_ips.exists():
            return _tasks

        # Create non-existing floating IPs
        for floating_ip in instance.floating_ips.filter(backend_id=""):
            serialized_floating_ip = core_utils.serialize_instance(floating_ip)
            _tasks.append(
                core_tasks.BackendMethodTask().si(
                    serialized_floating_ip, "create_floating_ip"
                )
            )

        # Push instance floating IPs
        _tasks.append(
            core_tasks.BackendMethodTask().si(
                serialized_instance, "push_instance_floating_ips"
            )
        )

        # Wait for operation completion
        for index, floating_ip in enumerate(instance.floating_ips):
            _tasks.append(
                core_tasks.PollRuntimeStateTask()
                .si(
                    core_utils.serialize_instance(floating_ip),
                    backend_pull_method="pull_floating_ip_runtime_state",
                    success_state="ACTIVE",
                    erred_state="ERRED",
                )
                .set(countdown=5 if not index else 0)
            )

        serialized_tenant = core_utils.serialize_instance(instance.tenant)
        _tasks.append(core_tasks.PollStateTask().si(serialized_tenant))
        _tasks.append(
            openstack_executors.TenantPullFloatingIPsExecutor.as_signature(
                instance.tenant
            )
        )

        return _tasks

    @classmethod
    def get_success_signature(cls, instance, serialized_instance, **kwargs):
        return tasks.SetInstanceOKTask().si(serialized_instance)

    @classmethod
    def get_failure_signature(cls, instance, serialized_instance, **kwargs):
        return tasks.SetInstanceErredTask().s(serialized_instance)

    @classmethod
    def pull_server_group(cls, serialized_instance):
        return [
            core_tasks.BackendMethodTask().si(
                serialized_instance, "pull_instance_server_group"
            )
        ]

    @classmethod
    def pull_instance(cls, serialized_instance):
        return [core_tasks.BackendMethodTask().si(serialized_instance, "pull_instance")]


class InstanceUpdateExecutor(core_executors.UpdateExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        updated_fields = kwargs["updated_fields"]
        if "name" in updated_fields:
            return core_tasks.BackendMethodTask().si(
                serialized_instance,
                "update_instance",
                state_transition="begin_updating",
            )
        else:
            return core_tasks.StateTransitionTask().si(
                serialized_instance, state_transition="begin_updating"
            )


class InstanceUpdateSecurityGroupsExecutor(core_executors.ActionExecutor):
    action = "Update security groups"

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_instance,
            backend_method="push_instance_security_groups",
            state_transition="begin_updating",
        )


class InstanceDeleteExecutor(core_executors.DeleteExecutor):
    @classmethod
    def get_task_signature(cls, instance, serialized_instance, force=False, **kwargs):
        delete_volumes = kwargs.pop("delete_volumes", True)
        release_floating_ips = kwargs.pop("release_floating_ips", True)

        delete_instance_tasks = cls.get_delete_instance_tasks(serialized_instance)
        release_floating_ips_tasks = cls.get_release_floating_ips_tasks(
            instance, release_floating_ips
        )
        detach_volumes_tasks = cls.get_detach_data_volumes_tasks(instance)
        delete_volumes_tasks = cls.get_delete_data_volumes_tasks(instance)
        delete_ports_tasks = cls.get_delete_ports_tasks(serialized_instance)

        # Case 1. Instance does not exist at backend
        if not instance.backend_id:
            return chain(
                cls.get_delete_incomplete_instance_tasks(instance, serialized_instance)
            )

        # Case 2. Instance exists at backend.
        # Data volumes are detached and deleted explicitly
        # because once volume is attached after instance is created,
        # it is not removed automatically.
        # System volume is deleted implicitly since delete_on_termination=True
        elif delete_volumes:
            return chain(
                detach_volumes_tasks
                + delete_volumes_tasks
                + delete_instance_tasks
                + release_floating_ips_tasks
                + delete_ports_tasks
            )

        # Case 3. Instance exists at backend.
        # Data volumes are detached and not deleted.
        else:
            return chain(
                detach_volumes_tasks
                + delete_instance_tasks
                + release_floating_ips_tasks
                + delete_ports_tasks
            )

    @classmethod
    def get_delete_incomplete_instance_tasks(cls, instance, serialized_instance):
        _tasks = []

        _tasks.append(
            core_tasks.StateTransitionTask().si(
                serialized_instance, state_transition="begin_deleting"
            )
        )

        _tasks += cls.get_delete_ports_tasks(serialized_instance)

        for volume in instance.volumes.all():
            if volume.backend_id:
                serialized_volume = core_utils.serialize_instance(volume)
                _tasks.append(core_tasks.PollStateTask().si(serialized_volume))
                _tasks.append(VolumeDeleteExecutor.as_signature(volume))

        _tasks += [tasks.DeleteIncompleteInstanceTask().si(serialized_instance)]

        return _tasks

    @classmethod
    def get_delete_ports_tasks(cls, serialized_instance):
        """
        OpenStack Neutron ports should be deleted explicitly because we're creating them explicitly.
        Otherwise when port quota is exhausted, user is not able to provision new VMs anymore.
        """
        return [
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                backend_method="delete_instance_ports",
            )
        ]

    @classmethod
    def get_delete_instance_tasks(cls, serialized_instance):
        return [
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                backend_method="delete_instance",
                state_transition="begin_deleting",
            ),
            core_tasks.PollBackendCheckTask().si(
                serialized_instance,
                backend_check_method="is_instance_deleted",
            ),
        ]

    @classmethod
    def get_release_floating_ips_tasks(cls, instance, release_floating_ips):
        if not instance.floating_ips.exists():
            return []

        _tasks = []
        if release_floating_ips:
            for index, floating_ip in enumerate(instance.floating_ips):
                _tasks.append(
                    core_tasks.BackendMethodTask()
                    .si(
                        core_utils.serialize_instance(floating_ip),
                        "delete_floating_ip",
                    )
                    .set(countdown=5 if not index else 0)
                )
        else:
            # pull related floating IPs state after instance deletion
            for index, floating_ip in enumerate(instance.floating_ips):
                _tasks.append(
                    core_tasks.BackendMethodTask()
                    .si(
                        core_utils.serialize_instance(floating_ip),
                        "pull_floating_ip_runtime_state",
                    )
                    .set(countdown=5 if not index else 0)
                )

        serialized_tenant = core_utils.serialize_instance(instance.tenant)
        _tasks.append(core_tasks.PollStateTask().si(serialized_tenant))
        _tasks.append(
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_floating_ips"
            )
        )

        return _tasks

    @classmethod
    def get_detach_data_volumes_tasks(cls, instance):
        data_volumes = instance.volumes.all().filter(bootable=False)
        detach_volumes = [
            core_tasks.BackendMethodTask().si(
                core_utils.serialize_instance(volume),
                backend_method="detach_volume",
            )
            for volume in data_volumes
        ]
        check_volumes = [
            core_tasks.PollRuntimeStateTask().si(
                core_utils.serialize_instance(volume),
                backend_pull_method="pull_volume_runtime_state",
                success_state="available",
                erred_state="error",
                deleted_state="deleted",
            )
            for volume in data_volumes
        ]
        return detach_volumes + check_volumes

    @classmethod
    def get_delete_data_volumes_tasks(cls, instance):
        data_volumes = instance.volumes.all().filter(bootable=False)
        return [VolumeDeleteExecutor.as_signature(volume) for volume in data_volumes]


class InstanceFlavorChangeExecutor(core_executors.ActionExecutor):
    action = "Change flavor"

    @classmethod
    def get_action_details(cls, instance, **kwargs):
        old_flavor_name = kwargs.get("old_flavor_name")
        new_flavor_name = kwargs.get("flavor").name
        return {
            "message": f"Change flavor from {old_flavor_name} to {new_flavor_name}",
            "old_flavor_name": old_flavor_name,
            "new_flavor_name": new_flavor_name,
        }

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        flavor = kwargs.pop("flavor")
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                backend_method="resize_instance",
                state_transition="begin_updating",
                flavor_id=flavor.backend_id,
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_instance,
                backend_pull_method="pull_instance_runtime_state",
                success_state="VERIFY_RESIZE",
                erred_state="ERRED",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_instance, backend_method="confirm_instance_resize"
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_instance,
                backend_pull_method="pull_instance_runtime_state",
                success_state="SHUTOFF",
                erred_state="ERRED",
            ),
        )


class InstancePullExecutor(core_executors.ActionExecutor):
    action = "Pull"

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                "pull_instance",
                state_transition="begin_updating",
            ),
            core_tasks.BackendMethodTask().si(
                serialized_instance, "pull_instance_security_groups"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_instance, "pull_instance_ports"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_instance, "pull_instance_floating_ips"
            ),
            core_tasks.BackendMethodTask().si(
                serialized_instance, "pull_instance_server_group"
            ),
        )


class InstanceFloatingIPsUpdateExecutor(core_executors.ActionExecutor):
    action = "Update floating IPs"

    @classmethod
    def get_action_details(cls, instance, **kwargs):
        attached = set(instance._new_floating_ips) - set(instance._old_floating_ips)
        detached = set(instance._old_floating_ips) - set(instance._new_floating_ips)

        messages = []
        if attached:
            messages.append("Attached floating IPs: %s." % ", ".join(attached))

        if detached:
            messages.append("Detached floating IPs: %s." % ", ".join(detached))

        if not messages:
            messages.append("Instance floating IPs have been updated.")

        return {
            "message": " ".join(messages),
            "attached": list(attached),
            "detached": list(detached),
        }

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        _tasks = [
            core_tasks.StateTransitionTask().si(
                serialized_instance, state_transition="begin_updating"
            )
        ]
        # Create non-exist floating IPs
        for floating_ip in instance.floating_ips.filter(backend_id=""):
            serialized_floating_ip = core_utils.serialize_instance(floating_ip)
            _tasks.append(
                core_tasks.BackendMethodTask().si(
                    serialized_floating_ip, "create_floating_ip"
                )
            )
        # Push instance floating IPs
        _tasks.append(
            core_tasks.BackendMethodTask().si(
                serialized_instance, "push_instance_floating_ips"
            )
        )
        # Wait for operation completion
        for index, floating_ip in enumerate(instance.floating_ips):
            _tasks.append(
                core_tasks.PollRuntimeStateTask()
                .si(
                    core_utils.serialize_instance(floating_ip),
                    backend_pull_method="pull_floating_ip_runtime_state",
                    success_state="ACTIVE",
                    erred_state="ERRED",
                )
                .set(countdown=5 if not index else 0)
            )
        # Pull floating IPs again to update state of disconnected IPs
        serialized_tenant = core_utils.serialize_instance(instance.tenant)
        _tasks.append(
            core_tasks.BackendMethodTask().si(
                serialized_tenant, "pull_tenant_floating_ips"
            )
        )
        return chain(*_tasks)

    @classmethod
    def get_success_signature(cls, instance, serialized_instance, **kwargs):
        return tasks.SetInstanceOKTask().si(serialized_instance)

    @classmethod
    def get_failure_signature(cls, instance, serialized_instance, **kwargs):
        return tasks.SetInstanceErredTask().s(serialized_instance)


class InstanceStopExecutor(core_executors.ActionExecutor):
    action = "Stop"

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                "stop_instance",
                state_transition="begin_updating",
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_instance,
                backend_pull_method="pull_instance_runtime_state",
                success_state="SHUTOFF",
                erred_state="ERRED",
            ),
        )


class InstanceStartExecutor(core_executors.ActionExecutor):
    action = "Start"

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                "start_instance",
                state_transition="begin_updating",
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_instance,
                backend_pull_method="pull_instance_runtime_state",
                success_state="ACTIVE",
                erred_state="ERRED",
            ),
        )


class InstanceRestartExecutor(core_executors.ActionExecutor):
    action = "Restart"

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                "restart_instance",
                state_transition="begin_updating",
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_instance,
                backend_pull_method="pull_instance_runtime_state",
                success_state="ACTIVE",
                erred_state="ERRED",
            ),
        )


class InstanceAllowedAddressPairsUpdateExecutor(core_executors.ActionExecutor):
    action = "Update allowed address pairs"

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_instance,
            "push_instance_allowed_address_pairs",
            state_transition="begin_updating",
            **kwargs,
        )


class InstancePortsUpdateExecutor(core_executors.ActionExecutor):
    action = "Update ports"

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_instance,
            "push_instance_ports",
            state_transition="begin_updating",
        )


class BackupCreateExecutor(core_executors.CreateExecutor):
    @classmethod
    def get_task_signature(cls, backup, serialized_backup, **kwargs):
        serialized_snapshots = [
            core_utils.serialize_instance(snapshot)
            for snapshot in backup.snapshots.all()
        ]

        _tasks = [
            core_tasks.StateTransitionTask().si(
                serialized_backup, state_transition="begin_creating"
            )
        ]
        for serialized_snapshot in serialized_snapshots:
            _tasks.append(
                tasks.ThrottleProvisionTask().si(
                    serialized_snapshot,
                    "create_snapshot",
                    force=True,
                    state_transition="begin_creating",
                )
            )
            _tasks.append(
                core_tasks.PollRuntimeStateTask().si(
                    serialized_snapshot,
                    backend_pull_method="pull_snapshot_runtime_state",
                    success_state="available",
                    erred_state="error",
                )
            )
            _tasks.append(
                core_tasks.StateTransitionTask().si(
                    serialized_snapshot, state_transition="set_ok"
                )
            )

        return chain(*_tasks)

    @classmethod
    def get_failure_signature(cls, backup, serialized_backup, **kwargs):
        return tasks.SetBackupErredTask().s(serialized_backup)


class BackupDeleteExecutor(core_executors.DeleteExecutor):
    @classmethod
    @transaction.atomic
    def pre_apply(cls, backup, **kwargs):
        for snapshot in backup.snapshots.all():
            snapshot.schedule_deleting()
            snapshot.save(update_fields=["state"])
        core_executors.DeleteExecutor.pre_apply(backup)

    @classmethod
    def get_task_signature(cls, backup, serialized_backup, force=False, **kwargs):
        serialized_snapshots = [
            core_utils.serialize_instance(snapshot)
            for snapshot in backup.snapshots.all()
        ]

        _tasks = [
            core_tasks.StateTransitionTask().si(
                serialized_backup, state_transition="begin_deleting"
            )
        ]
        for serialized_snapshot in serialized_snapshots:
            _tasks.append(
                core_tasks.BackendMethodTask().si(
                    serialized_snapshot,
                    "delete_snapshot",
                    state_transition="begin_deleting",
                )
            )
        for serialized_snapshot in serialized_snapshots:
            _tasks.append(
                core_tasks.PollBackendCheckTask().si(
                    serialized_snapshot, "is_snapshot_deleted"
                )
            )
            _tasks.append(core_tasks.DeletionTask().si(serialized_snapshot))

        return chain(*_tasks)

    @classmethod
    def get_failure_signature(cls, backup, serialized_backup, force=False, **kwargs):
        if not force:
            return tasks.SetBackupErredTask().s(serialized_backup)
        else:
            return tasks.ForceDeleteBackupTask().si(serialized_backup)


class SnapshotRestorationExecutor(core_executors.CreateExecutor):
    """Restores volume from snapshot instance"""

    @classmethod
    def get_task_signature(
        cls, snapshot_restoration, serialized_snapshot_restoration, **kwargs
    ):
        serialized_volume = core_utils.serialize_instance(snapshot_restoration.volume)

        _tasks = [
            tasks.ThrottleProvisionTask().si(
                serialized_volume, "create_volume", state_transition="begin_creating"
            ),
            core_tasks.PollRuntimeStateTask()
            .si(
                serialized_volume,
                "pull_volume_runtime_state",
                success_state="available",
                erred_state="error",
            )
            .set(countdown=30),
            core_tasks.BackendMethodTask().si(
                serialized_volume, "remove_bootable_flag"
            ),
            core_tasks.BackendMethodTask().si(serialized_volume, "pull_volume"),
        ]

        return chain(*_tasks)

    @classmethod
    def get_success_signature(
        cls, snapshot_restoration, serialized_snapshot_restoration, **kwargs
    ):
        serialized_volume = core_utils.serialize_instance(snapshot_restoration.volume)
        return core_tasks.StateTransitionTask().si(
            serialized_volume, state_transition="set_ok"
        )

    @classmethod
    def get_failure_signature(
        cls, snapshot_restoration, serialized_snapshot_restoration, **kwargs
    ):
        serialized_volume = core_utils.serialize_instance(snapshot_restoration.volume)
        return core_tasks.StateTransitionTask().si(
            serialized_volume, state_transition="set_erred"
        )


class OpenStackCleanupExecutor(structure_executors.BaseCleanupExecutor):
    pre_models = (
        models.SnapshotSchedule,
        models.BackupSchedule,
    )

    executors = (
        (models.SecurityGroup, SecurityGroupDeleteExecutor),
        (models.FloatingIP, FloatingIPDeleteExecutor),
        (models.SubNet, SubNetDeleteExecutor),
        (models.Network, NetworkDeleteExecutor),
        (models.Tenant, TenantDeleteExecutor),
        (models.ServerGroup, ServerGroupDeleteExecutor),
        (models.Snapshot, SnapshotDeleteExecutor),
        (models.Backup, BackupDeleteExecutor),
        (models.Instance, InstanceDeleteExecutor),
        (models.Volume, VolumeDeleteExecutor),
    )
