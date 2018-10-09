from __future__ import unicode_literals

from celery import chain

from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils
from waldur_core.structure import executors as structure_executors
from waldur_openstack.openstack import executors as openstack_executors

from . import tasks, models


class VolumeCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return chain(
            tasks.ThrottleProvisionTask().si(
                serialized_volume,
                'create_volume',
                state_transition='begin_creating'
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_volume,
                backend_pull_method='pull_volume_runtime_state',
                success_state='available',
                erred_state='error',
            ).set(countdown=30)
        )


class VolumeUpdateExecutor(core_executors.UpdateExecutor):

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        updated_fields = kwargs['updated_fields']
        if 'name' in updated_fields or 'description' in updated_fields:
            return core_tasks.BackendMethodTask().si(
                serialized_volume, 'update_volume', state_transition='begin_updating')
        else:
            return core_tasks.StateTransitionTask().si(serialized_volume, state_transition='begin_updating')


class VolumeDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        if volume.backend_id:
            return chain(
                core_tasks.BackendMethodTask().si(
                    serialized_volume, 'delete_volume', state_transition='begin_deleting'),
                core_tasks.PollBackendCheckTask().si(serialized_volume, 'is_volume_deleted'),
            )
        else:
            return core_tasks.StateTransitionTask().si(serialized_volume, state_transition='begin_deleting')


class VolumePullExecutor(core_executors.ActionExecutor):
    action = 'Pull'

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_volume, 'pull_volume',
            state_transition='begin_updating')


class VolumeExtendExecutor(core_executors.ActionExecutor):
    action = 'Extend'

    @classmethod
    def get_action_details(cls, volume, **kwargs):
        return {
            'message': 'Extend volume from %s MB to %s MB' % (kwargs.get('old_size'), volume.size),
            'old_size': kwargs.get('old_size'),
            'new_size': volume.size,
        }

    @classmethod
    def pre_apply(cls, volume, **kwargs):
        super(VolumeExtendExecutor, cls).pre_apply(volume, **kwargs)
        if volume.instance is not None:
            volume.instance.action = 'Extend volume'
            volume.instance.schedule_updating()
            volume.instance.save()

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        if volume.instance is None:
            return chain(
                core_tasks.BackendMethodTask().si(
                    serialized_volume,
                    backend_method='extend_volume',
                    state_transition='begin_updating',
                ),
                core_tasks.PollRuntimeStateTask().si(
                    serialized_volume,
                    backend_pull_method='pull_volume_runtime_state',
                    success_state='available',
                    erred_state='error'
                )
            )

        return chain(
            core_tasks.StateTransitionTask().si(
                core_utils.serialize_instance(volume.instance),
                state_transition='begin_updating'
            ),
            core_tasks.BackendMethodTask().si(
                serialized_volume,
                backend_method='detach_volume',
                state_transition='begin_updating'
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_volume,
                backend_pull_method='pull_volume_runtime_state',
                success_state='available',
                erred_state='error'
            ),
            core_tasks.BackendMethodTask().si(
                serialized_volume,
                backend_method='extend_volume',
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_volume,
                backend_pull_method='pull_volume_runtime_state',
                success_state='available',
                erred_state='error'
            ),
            core_tasks.BackendMethodTask().si(
                serialized_volume,
                instance_uuid=volume.instance.uuid.hex,
                device=volume.device,
                backend_method='attach_volume',
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_volume,
                backend_pull_method='pull_volume_runtime_state',
                success_state='in-use',
                erred_state='error'
            ),
        )

    @classmethod
    def get_success_signature(cls, volume, serialized_volume, **kwargs):
        if volume.instance is None:
            return super(VolumeExtendExecutor, cls).get_success_signature(volume, serialized_volume, **kwargs)
        else:
            instance = volume.instance
            serialized_instance = core_utils.serialize_instance(instance)
            return chain(
                super(VolumeExtendExecutor, cls).get_success_signature(volume, serialized_volume, **kwargs),
                super(VolumeExtendExecutor, cls).get_success_signature(instance, serialized_instance, **kwargs),
            )

    @classmethod
    def get_failure_signature(cls, volume, serialized_volume, **kwargs):
        return tasks.VolumeExtendErredTask().s(serialized_volume)


class VolumeAttachExecutor(core_executors.ActionExecutor):
    action = 'Attach'

    @classmethod
    def get_action_details(cls, volume, **kwargs):
        return {'message': 'Attach volume to instance %s' % volume.instance.name}

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_volume,
                instance_uuid=volume.instance.uuid.hex,
                device=volume.device,
                backend_method='attach_volume',
                state_transition='begin_updating'
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_volume,
                backend_pull_method='pull_volume_runtime_state',
                success_state='in-use',
                erred_state='error',
            ),
            # additional pull to populate field "device".
            core_tasks.BackendMethodTask().si(serialized_volume, backend_method='pull_volume'),
        )


class VolumeDetachExecutor(core_executors.ActionExecutor):
    action = 'Detach'

    @classmethod
    def get_action_details(cls, volume, **kwargs):
        return {'message': 'Detach volume from instance %s' % volume.instance.name}

    @classmethod
    def get_task_signature(cls, volume, serialized_volume, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_volume, backend_method='detach_volume', state_transition='begin_updating'),
            core_tasks.PollRuntimeStateTask().si(
                serialized_volume,
                backend_pull_method='pull_volume_runtime_state',
                success_state='available',
                erred_state='error',
            )
        )


class SnapshotCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, snapshot, serialized_snapshot, **kwargs):
        return chain(
            tasks.ThrottleProvisionTask().si(
                serialized_snapshot,
                'create_snapshot',
                state_transition='begin_creating'
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_snapshot,
                backend_pull_method='pull_snapshot_runtime_state',
                success_state='available',
                erred_state='error',
            ).set(countdown=10)
        )


class SnapshotUpdateExecutor(core_executors.UpdateExecutor):

    @classmethod
    def get_task_signature(cls, snapshot, serialized_snapshot, **kwargs):
        updated_fields = kwargs['updated_fields']
        # TODO: call separate task on metadata update
        if 'name' in updated_fields or 'description' in updated_fields:
            return core_tasks.BackendMethodTask().si(
                serialized_snapshot, 'update_snapshot', state_transition='begin_updating')
        else:
            return core_tasks.StateTransitionTask().si(serialized_snapshot, state_transition='begin_updating')


class SnapshotDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, snapshot, serialized_snapshot, **kwargs):
        if snapshot.backend_id:
            return chain(
                core_tasks.BackendMethodTask().si(
                    serialized_snapshot, 'delete_snapshot', state_transition='begin_deleting'),
                core_tasks.PollBackendCheckTask().si(serialized_snapshot, 'is_snapshot_deleted'),
            )
        else:
            return core_tasks.StateTransitionTask().si(serialized_snapshot, state_transition='begin_deleting')


class SnapshotPullExecutor(core_executors.ActionExecutor):
    action = 'Pull'

    @classmethod
    def get_task_signature(cls, snapshot, serialized_snapshot, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_snapshot, 'pull_snapshot',
            state_transition='begin_updating')


class InstanceCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, ssh_key=None, flavor=None):
        serialized_volumes = [core_utils.serialize_instance(volume) for volume in instance.volumes.all()]
        _tasks = [tasks.ThrottleProvisionStateTask().si(serialized_instance, state_transition='begin_creating')]
        _tasks += cls.create_volumes(serialized_volumes)
        _tasks += cls.create_internal_ips(serialized_instance)
        _tasks += cls.create_instance(serialized_instance, flavor, ssh_key)
        _tasks += cls.pull_volumes(serialized_volumes)
        _tasks += cls.pull_security_groups(serialized_instance)
        _tasks += cls.create_floating_ips(instance, serialized_instance)
        return chain(*_tasks)

    @classmethod
    def create_volumes(cls, serialized_volumes):
        """
        Create all instance volumes and wait for them to provision.
        """
        _tasks = []

        # Create volumes
        for serialized_volume in serialized_volumes:
            _tasks.append(tasks.ThrottleProvisionTask().si(
                serialized_volume, 'create_volume', state_transition='begin_creating'))

        for index, serialized_volume in enumerate(serialized_volumes):
            # Wait for volume creation
            _tasks.append(core_tasks.PollRuntimeStateTask().si(
                serialized_volume,
                backend_pull_method='pull_volume_runtime_state',
                success_state='available',
                erred_state='error',
            ).set(countdown=30 if index == 0 else 0))

            # Pull volume to sure that it is bootable
            _tasks.append(core_tasks.BackendMethodTask().si(serialized_volume, 'pull_volume'))

            # Mark volume as OK
            _tasks.append(core_tasks.StateTransitionTask().si(serialized_volume, state_transition='set_ok'))

        return _tasks

    @classmethod
    def create_internal_ips(cls, serialized_instance):
        """
        Create all network ports for an OpenStack instance.
        Although OpenStack Nova REST API allows to create network ports implicitly,
        we're not using it, because it does not take into account subnets.
        See also: https://specs.openstack.org/openstack/nova-specs/specs/juno/approved/selecting-subnet-when-creating-vm.html
        Therefore we're creating network ports beforehand with correct subnet.
        """
        return [core_tasks.BackendMethodTask().si(serialized_instance, 'create_instance_internal_ips')]

    @classmethod
    def create_instance(cls, serialized_instance, flavor, ssh_key=None):
        """
        It is assumed that volumes and network ports have been created beforehand.
        """
        _tasks = []
        kwargs = {
            'backend_flavor_id': flavor.backend_id,
        }
        if ssh_key is not None:
            kwargs['public_key'] = ssh_key.public_key

        # Wait 10 seconds after volume creation due to OpenStack restrictions.
        _tasks.append(core_tasks.BackendMethodTask().si(
            serialized_instance, 'create_instance', **kwargs).set(countdown=10))

        # Wait for instance creation
        _tasks.append(core_tasks.PollRuntimeStateTask().si(
            serialized_instance,
            backend_pull_method='pull_instance_runtime_state',
            success_state=models.Instance.RuntimeStates.ACTIVE,
            erred_state=models.Instance.RuntimeStates.ERROR,
        ))
        return _tasks

    @classmethod
    def pull_volumes(cls, serialized_volumes):
        """
        Update volumes runtime state and device name
        """
        _tasks = []
        for serialized_volume in serialized_volumes:
            _tasks.append(core_tasks.BackendMethodTask().si(
                serialized_volume,
                backend_method='pull_volume',
                update_fields=['runtime_state', 'device']
            ))
        return _tasks

    @classmethod
    def pull_security_groups(cls, serialized_instance):
        return [core_tasks.BackendMethodTask().si(serialized_instance, 'pull_instance_security_groups')]

    @classmethod
    def create_floating_ips(cls, instance, serialized_instance):
        _tasks = []

        # Create non-existing floating IPs
        for floating_ip in instance.floating_ips.filter(backend_id=''):
            serialized_floating_ip = core_utils.serialize_instance(floating_ip)
            _tasks.append(core_tasks.BackendMethodTask().si(serialized_floating_ip, 'create_floating_ip'))

        # Push instance floating IPs
        _tasks.append(core_tasks.BackendMethodTask().si(serialized_instance, 'push_instance_floating_ips'))

        # Wait for operation completion
        for index, floating_ip in enumerate(instance.floating_ips):
            _tasks.append(core_tasks.PollRuntimeStateTask().si(
                core_utils.serialize_instance(floating_ip),
                backend_pull_method='pull_floating_ip_runtime_state',
                success_state='ACTIVE',
                erred_state='ERRED',
            ).set(countdown=5 if not index else 0))

        shared_tenant = instance.service_project_link.service.settings.scope
        if shared_tenant:
            serialized_executor = core_utils.serialize_class(openstack_executors.TenantPullFloatingIPsExecutor)
            serialized_tenant = core_utils.serialize_instance(shared_tenant)
            _tasks.append(core_tasks.ExecutorTask().si(serialized_executor, serialized_tenant))

        return _tasks

    @classmethod
    def get_success_signature(cls, instance, serialized_instance, **kwargs):
        return tasks.SetInstanceOKTask().si(serialized_instance)

    @classmethod
    def get_failure_signature(cls, instance, serialized_instance, **kwargs):
        return tasks.SetInstanceErredTask().s(serialized_instance)


class InstanceUpdateExecutor(core_executors.UpdateExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        updated_fields = kwargs['updated_fields']
        if 'name' in updated_fields:
            return core_tasks.BackendMethodTask().si(
                serialized_instance, 'update_instance', state_transition='begin_updating')
        else:
            return core_tasks.StateTransitionTask().si(serialized_instance, state_transition='begin_updating')


class InstanceUpdateSecurityGroupsExecutor(core_executors.ActionExecutor):
    action = 'Update security groups'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_instance,
            backend_method='push_instance_security_groups',
            state_transition='begin_updating',
        )


class InstanceDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, force=False, **kwargs):
        delete_volumes = kwargs.pop('delete_volumes', True)
        release_floating_ips = kwargs.pop('release_floating_ips', True)
        delete_instance = cls.get_delete_instance_tasks(instance, serialized_instance, release_floating_ips)

        # Case 1. Instance does not exist at backend
        if not instance.backend_id:
            return chain(cls.get_delete_incomplete_instance_tasks(instance, serialized_instance))

        # Case 2. Instance exists at backend.
        # Data volumes are deleted by OpenStack because delete_on_termination=True
        elif delete_volumes:
            return chain(delete_instance)

        # Case 3. Instance exists at backend.
        # Data volumes are detached and not deleted.
        else:
            detach_volumes = cls.get_detach_data_volumes_tasks(instance, serialized_instance)
            return chain(detach_volumes + delete_instance)

    @classmethod
    def get_delete_incomplete_instance_tasks(cls, instance, serialized_instance):
        _tasks = []

        _tasks.append(core_tasks.StateTransitionTask().si(
            serialized_instance,
            state_transition='begin_deleting'
        ))

        _tasks.append(core_tasks.BackendMethodTask().si(
            serialized_instance,
            backend_method='delete_instance_internal_ips',
        ))

        for volume in instance.volumes.all():
            if volume.backend_id:
                serialized_volume = core_utils.serialize_instance(volume)
                _tasks.append(core_tasks.BackendMethodTask().si(
                    serialized_volume,
                    'delete_volume',
                    state_transition='begin_deleting'
                ))
                _tasks.append(core_tasks.PollBackendCheckTask().si(
                    serialized_volume,
                    'is_volume_deleted'
                ))

        return _tasks

    @classmethod
    def get_delete_instance_tasks(cls, instance, serialized_instance, release_floating_ips):
        _tasks = [
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                backend_method='delete_instance',
                state_transition='begin_deleting',
            ),
            core_tasks.PollBackendCheckTask().si(
                serialized_instance,
                backend_check_method='is_instance_deleted',
            ),
        ]
        if release_floating_ips:
            for index, floating_ip in enumerate(instance.floating_ips):
                _tasks.append(core_tasks.BackendMethodTask().si(
                    core_utils.serialize_instance(floating_ip),
                    'delete_floating_ip',
                ).set(countdown=5 if not index else 0))
        else:
            # pull related floating IPs state after instance deletion
            for index, floating_ip in enumerate(instance.floating_ips):
                _tasks.append(core_tasks.BackendMethodTask().si(
                    core_utils.serialize_instance(floating_ip),
                    'pull_floating_ip_runtime_state',
                ).set(countdown=5 if not index else 0))

        shared_tenant = instance.service_project_link.service.settings.scope
        if shared_tenant:
            serialized_executor = core_utils.serialize_class(openstack_executors.TenantPullFloatingIPsExecutor)
            serialized_tenant = core_utils.serialize_instance(shared_tenant)
            _tasks.append(core_tasks.ExecutorTask().si(serialized_executor, serialized_tenant))

        return _tasks

    @classmethod
    def get_detach_data_volumes_tasks(cls, instance, serialized_instance):
        data_volumes = instance.volumes.all().filter(bootable=False)
        detach_volumes = [
            core_tasks.BackendMethodTask().si(
                core_utils.serialize_instance(volume),
                backend_method='detach_volume',
            )
            for volume in data_volumes
        ]
        check_volumes = [
            core_tasks.PollRuntimeStateTask().si(
                core_utils.serialize_instance(volume),
                backend_pull_method='pull_volume_runtime_state',
                success_state='available',
                erred_state='error'
            )
            for volume in data_volumes
        ]
        return detach_volumes + check_volumes


class InstanceFlavorChangeExecutor(core_executors.ActionExecutor):
    action = 'Change flavor'

    @classmethod
    def get_action_details(cls, instance, **kwargs):
        old_flavor_name = kwargs.get('old_flavor_name')
        new_flavor_name = kwargs.get('flavor').name
        return {
            'message': 'Change flavor from %s to %s' % (old_flavor_name, new_flavor_name),
            'old_flavor_name': old_flavor_name,
            'new_flavor_name': new_flavor_name,
        }

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        flavor = kwargs.pop('flavor')
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                backend_method='resize_instance',
                state_transition='begin_updating',
                flavor_id=flavor.backend_id
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_instance,
                backend_pull_method='pull_instance_runtime_state',
                success_state='VERIFY_RESIZE',
                erred_state='ERRED'
            ),
            core_tasks.BackendMethodTask().si(
                serialized_instance,
                backend_method='confirm_instance_resize'
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_instance,
                backend_pull_method='pull_instance_runtime_state',
                success_state='SHUTOFF',
                erred_state='ERRED'
            ),
        )


class InstancePullExecutor(core_executors.ActionExecutor):
    action = 'Pull'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance, 'pull_instance', state_transition='begin_updating',
            ),
            core_tasks.BackendMethodTask().si(serialized_instance, 'pull_instance_security_groups'),
            core_tasks.BackendMethodTask().si(serialized_instance, 'pull_instance_internal_ips'),
            core_tasks.BackendMethodTask().si(serialized_instance, 'pull_instance_floating_ips'),
        )


class InstanceFloatingIPsUpdateExecutor(core_executors.ActionExecutor):
    action = 'Update floating IPs'

    @classmethod
    def get_action_details(cls, instance, **kwargs):
        attached = set(instance._new_floating_ips) - set(instance._old_floating_ips)
        detached = set(instance._old_floating_ips) - set(instance._new_floating_ips)

        messages = []
        if attached:
            messages.append('Attached floating IPs: %s.' % ', '.join(attached))

        if detached:
            messages.append('Detached floating IPs: %s.' % ', '.join(detached))

        if not messages:
            messages.append('Instance floating IPs have been updated.')

        return {
            'message': ' '.join(messages),
            'attached': list(attached),
            'detached': list(detached),
        }

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        _tasks = [core_tasks.StateTransitionTask().si(serialized_instance, state_transition='begin_updating')]
        # Create non-exist floating IPs
        for floating_ip in instance.floating_ips.filter(backend_id=''):
            serialized_floating_ip = core_utils.serialize_instance(floating_ip)
            _tasks.append(core_tasks.BackendMethodTask().si(serialized_floating_ip, 'create_floating_ip'))
        # Push instance floating IPs
        _tasks.append(core_tasks.BackendMethodTask().si(serialized_instance, 'push_instance_floating_ips'))
        # Wait for operation completion
        for index, floating_ip in enumerate(instance.floating_ips):
            _tasks.append(core_tasks.PollRuntimeStateTask().si(
                core_utils.serialize_instance(floating_ip),
                backend_pull_method='pull_floating_ip_runtime_state',
                success_state='ACTIVE',
                erred_state='ERRED',
            ).set(countdown=5 if not index else 0))
        # Pull floating IPs again to update state of disconnected IPs
        _tasks.append(core_tasks.IndependentBackendMethodTask().si(serialized_instance, 'pull_floating_ips'))
        return chain(*_tasks)

    @classmethod
    def get_success_signature(cls, instance, serialized_instance, **kwargs):
        return tasks.SetInstanceOKTask().si(serialized_instance)

    @classmethod
    def get_failure_signature(cls, instance, serialized_instance, **kwargs):
        return tasks.SetInstanceErredTask().s(serialized_instance)


class InstanceStopExecutor(core_executors.ActionExecutor):
    action = 'Stop'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance, 'stop_instance', state_transition='begin_updating',
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_instance,
                backend_pull_method='pull_instance_runtime_state',
                success_state='SHUTOFF',
                erred_state='ERRED'
            ),
        )


class InstanceStartExecutor(core_executors.ActionExecutor):
    action = 'Start'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance, 'start_instance', state_transition='begin_updating',
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_instance,
                backend_pull_method='pull_instance_runtime_state',
                success_state='ACTIVE',
                erred_state='ERRED'
            ),
        )


class InstanceRestartExecutor(core_executors.ActionExecutor):
    action = 'Restart'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return chain(
            core_tasks.BackendMethodTask().si(
                serialized_instance, 'restart_instance', state_transition='begin_updating',
            ),
            core_tasks.PollRuntimeStateTask().si(
                serialized_instance,
                backend_pull_method='pull_instance_runtime_state',
                success_state='ACTIVE',
                erred_state='ERRED'
            ),
        )


class InstanceInternalIPsSetUpdateExecutor(core_executors.ActionExecutor):
    action = 'Update internal IPs'

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        return core_tasks.BackendMethodTask().si(
            serialized_instance, 'push_instance_internal_ips', state_transition='begin_updating',
        )


class BackupCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, backup, serialized_backup, **kwargs):
        serialized_snapshots = [core_utils.serialize_instance(snapshot) for snapshot in backup.snapshots.all()]

        _tasks = [core_tasks.StateTransitionTask().si(serialized_backup, state_transition='begin_creating')]
        for serialized_snapshot in serialized_snapshots:
            _tasks.append(tasks.ThrottleProvisionTask().si(
                serialized_snapshot, 'create_snapshot', force=True, state_transition='begin_creating'))
        for index, serialized_snapshot in enumerate(serialized_snapshots):
            _tasks.append(core_tasks.PollRuntimeStateTask().si(
                serialized_snapshot,
                backend_pull_method='pull_snapshot_runtime_state',
                success_state='available',
                erred_state='error',
            ).set(countdown=10 if index == 0 else 0))
            _tasks.append(core_tasks.StateTransitionTask().si(serialized_snapshot, state_transition='set_ok'))

        return chain(*_tasks)

    @classmethod
    def get_failure_signature(cls, backup, serialized_backup, **kwargs):
        return tasks.SetBackupErredTask().s(serialized_backup)


class BackupDeleteExecutor(core_executors.DeleteExecutor):

    @classmethod
    def pre_apply(cls, backup, **kwargs):
        for snapshot in backup.snapshots.all():
            snapshot.schedule_deleting()
            snapshot.save(update_fields=['state'])
        core_executors.DeleteExecutor.pre_apply(backup)

    @classmethod
    def get_task_signature(cls, backup, serialized_backup, force=False, **kwargs):
        serialized_snapshots = [core_utils.serialize_instance(snapshot) for snapshot in backup.snapshots.all()]

        _tasks = [core_tasks.StateTransitionTask().si(serialized_backup, state_transition='begin_deleting')]
        for serialized_snapshot in serialized_snapshots:
            _tasks.append(core_tasks.BackendMethodTask().si(
                serialized_snapshot, 'delete_snapshot', state_transition='begin_deleting'))
        for serialized_snapshot in serialized_snapshots:
            _tasks.append(core_tasks.PollBackendCheckTask().si(serialized_snapshot, 'is_snapshot_deleted'))
            _tasks.append(core_tasks.DeletionTask().si(serialized_snapshot))

        return chain(*_tasks)

    @classmethod
    def get_failure_signature(cls, backup, serialized_backup, force=False, **kwargs):
        if not force:
            return tasks.SetBackupErredTask().s(serialized_backup)
        else:
            return tasks.ForceDeleteBackupTask().si(serialized_backup)


class SnapshotRestorationExecutor(core_executors.CreateExecutor):
    """ Restores volume from snapshot instance """

    @classmethod
    def get_task_signature(cls, snapshot_restoration, serialized_snapshot_restoration, **kwargs):
        serialized_volume = core_utils.serialize_instance(snapshot_restoration.volume)

        _tasks = [
            tasks.ThrottleProvisionTask().si(
                serialized_volume, 'create_volume', state_transition='begin_creating'),
            core_tasks.PollRuntimeStateTask().si(
                serialized_volume, 'pull_volume_runtime_state', success_state='available', erred_state='error',
            ).set(countdown=30),
            core_tasks.BackendMethodTask().si(serialized_volume, 'remove_bootable_flag'),
            core_tasks.BackendMethodTask().si(serialized_volume, 'pull_volume'),
        ]

        return chain(*_tasks)

    @classmethod
    def get_success_signature(cls, snapshot_restoration, serialized_snapshot_restoration, **kwargs):
        serialized_volume = core_utils.serialize_instance(snapshot_restoration.volume)
        return core_tasks.StateTransitionTask().si(serialized_volume, state_transition='set_ok')

    @classmethod
    def get_failure_signature(cls, snapshot_restoration, serialized_snapshot_restoration, **kwargs):
        serialized_volume = core_utils.serialize_instance(snapshot_restoration.volume)
        return core_tasks.StateTransitionTask().si(serialized_volume, state_transition='set_erred')


class OpenStackTenantCleanupExecutor(structure_executors.BaseCleanupExecutor):
    related_executor = openstack_executors.OpenStackCleanupExecutor

    pre_models = (
        models.SnapshotSchedule,
        models.BackupSchedule,
    )

    executors = (
        (models.Snapshot, SnapshotDeleteExecutor),
        (models.Backup, BackupDeleteExecutor),
        (models.Instance, InstanceDeleteExecutor),
        (models.Volume, VolumeDeleteExecutor),
    )
