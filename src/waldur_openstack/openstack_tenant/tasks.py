from __future__ import unicode_literals

import logging

from django.conf import settings
from django.db.models import Q
from django.db import transaction
from django.utils import timezone

from waldur_core.core import tasks as core_tasks
from waldur_core.core import models as core_models
from waldur_core.quotas import exceptions as quotas_exceptions
from waldur_core.structure import (tasks as structure_tasks,
                                   SupportedServices)

from . import models, serializers, log


logger = logging.getLogger(__name__)


class SetInstanceOKTask(core_tasks.StateTransitionTask):
    """ Additionally mark or related floating IPs as free """

    def pre_execute(self, instance):
        self.kwargs['state_transition'] = 'set_ok'
        self.kwargs['action'] = ''
        self.kwargs['action_details'] = {}
        super(SetInstanceOKTask, self).pre_execute(instance)

    def execute(self, instance, *args, **kwargs):
        super(SetInstanceOKTask, self).execute(instance)
        instance.floating_ips.update(is_booked=False)


class SetInstanceErredTask(core_tasks.ErrorStateTransitionTask):
    """ Mark instance as erred and delete resources that were not created. """

    def execute(self, instance):
        super(SetInstanceErredTask, self).execute(instance)

        # delete volumes if they were not created on backend,
        # mark as erred if creation was started, but not ended,
        # leave as is, if they are OK.
        for volume in instance.volumes.all():
            if volume.state == models.Volume.States.CREATION_SCHEDULED:
                volume.delete()
            elif volume.state == models.Volume.States.OK:
                pass
            else:
                volume.set_erred()
                volume.save(update_fields=['state'])

        # set instance floating IPs as free, delete not created ones.
        instance.floating_ips.filter(backend_id='').delete()
        instance.floating_ips.update(is_booked=False)


class SetBackupErredTask(core_tasks.ErrorStateTransitionTask):
    """ Mark DR backup and all related resources that are not in state OK as Erred """

    def execute(self, backup):
        super(SetBackupErredTask, self).execute(backup)
        for snapshot in backup.snapshots.all():
            # If snapshot creation was not started - delete it from NC DB.
            if snapshot.state == models.Snapshot.States.CREATION_SCHEDULED:
                snapshot.decrease_backend_quotas_usage()
                snapshot.delete()
            else:
                snapshot.set_erred()
                snapshot.save(update_fields=['state'])

        # Deactivate schedule if its backup become erred.
        schedule = backup.backup_schedule
        if schedule:
            schedule.error_message = 'Failed to execute backup schedule for %s. Error: %s' % (
                backup.instance, backup.error_message)
            schedule.is_active = False
            schedule.save()


class ForceDeleteBackupTask(core_tasks.DeletionTask):

    def execute(self, backup):
        backup.snapshots.all().delete()
        super(ForceDeleteBackupTask, self).execute(backup)


class VolumeExtendErredTask(core_tasks.ErrorStateTransitionTask):
    """ Mark volume and its instance as erred on fail """

    def execute(self, volume):
        super(VolumeExtendErredTask, self).execute(volume)
        if volume.instance is not None:
            super(VolumeExtendErredTask, self).execute(volume.instance)


class BaseScheduleTask(core_tasks.BackgroundTask):
    """
    This task has several important caveats to consider.

    1. If user has decreased value of maximal_number_of_resources attribute,
       but exceeding resources have been already created, we would try to automatically delete
       exceeding resources before creating new resources.
       However, if new resource creation fails, old resources cannot be restored.

       Therefore, it is strongly advised to modify value of maximal_number_of_resources attribute very carefully.
       Also it is better to delete exceeding resources manually instead of relying on automatic deletion
       so that it is easier to explicitly select resources to be removed.

    2. _remove_exceeding_resources method orders resources by value of kept_until attribute in ASC order.
       It assumes that NULL values come *last* with ascending sort order.
       Therefore it would work correctly only in PostgreSQL.
       It would not work correctly in MySQL because in MySQL NULL values come *first*.

    3. Value of kept_until attribute is ignored as long as there are exceeding resources.
       It means that existing resources are deleted even if it is requested to be kept forever.
       Essentially, retention_time and maximal_number_of_resources attributes are mutually exclusive.

       Consider, for example, case when value of maximal_number_of_resources is 3 and there are 6 resources,
       out of which 2 with non-null value of kept_until attribute and 4 resources to be kept forever.
       As you can see, there are 3 exceeding resources, which should be removed.

       Then, both 2 first resources would be deleted, and 1 resource to be kept forever is deleted as well.
       Please note that last resource for deletion is chosen by value of *created* attribute.
       It means that oldest resource is selected for deletion.

    4. Database records for resources are created and deleted synchronously,
       but actual backend API task are scheduled asynchronously.
       Therefore, next iteration of schedule task does not wait
       until previous iteration tasks are completed.
       That's why there may several concurrent execution of the same schedule.

    5. Actual execution of schedule depends on number of Celery workers and their load.
       For example, even if schedule is expected to create new resources each hour,
       but all Celery workers have been overloaded for 2 hours, only one resource would be created.

    6. Schedule is disabled as long as resource quota is exceeded.
       Schedule is not reactivated automatically whenever quota limit
       is increased or quota usage is decreased.
       Instead it is expected that user would manually reactivate schedule in this case.

    7. Schedule is skipped and new resources are not created as long as schedule is disabled or
       number of resources has reached value of maximal_number_of_resources attribute.
    """

    model = NotImplemented
    resource_attribute = NotImplemented

    def is_equal(self, other_task):
        return self.name == other_task.get('name')

    @transaction.atomic()
    def run(self):
        schedules = self.model.objects.filter(is_active=True, next_trigger_at__lt=timezone.now())
        for schedule in schedules:
            existing_resources = self._get_number_of_resources(schedule)
            if existing_resources > schedule.maximal_number_of_resources:
                self._schedule_exceeding_resources_deletion(schedule, existing_resources)
                continue
            elif existing_resources == schedule.maximal_number_of_resources:
                logger.debug('Skipping schedule %s because number of resources %s has reached limit %s.',
                             schedule, existing_resources, schedule.maximal_number_of_resources)
                continue

            kept_until = None
            if schedule.retention_time:
                kept_until = timezone.now() + timezone.timedelta(days=schedule.retention_time)

            try:
                # Value of call_count attribute is used as suffix of new resource name
                schedule.call_count += 1
                schedule.save()
                resource = self._create_resource(schedule, kept_until=kept_until)
            except quotas_exceptions.QuotaValidationError as e:
                message = 'Failed to schedule "%s" creation. Error: %s' % (self.model.__name__, e)
                logger.debug(
                    'Resource schedule (PK: %s), (Name: %s) execution failed. %s' % (schedule.pk,
                                                                                     schedule.name,
                                                                                     message))
                schedule.is_active = False
                schedule.error_message = message
                schedule.save()
            else:
                executor = self._get_create_executor()
                executor.execute(resource)
                schedule.update_next_trigger_at()
                schedule.save()

    def _schedule_exceeding_resources_deletion(self, schedule, resources_count):
        amount_to_remove = resources_count - schedule.maximal_number_of_resources
        self._log_backup_cleanup(schedule, amount_to_remove, resources_count)
        ok_or_erred = Q(state=core_models.StateMixin.States.OK) | Q(state=core_models.StateMixin.States.ERRED)
        queryset = getattr(schedule, self.resource_attribute)
        resources = queryset.filter(ok_or_erred).order_by('kept_until', 'created')
        resources_to_remove = resources[:amount_to_remove]
        executor = self._get_delete_executor()
        for resource in resources_to_remove:
            executor.execute(resource)

    def _log_backup_cleanup(self, schedule, amount_to_remove, resources_count):
        raise NotImplementedError()

    def _create_resource(self, schedule, kept_until):
        raise NotImplementedError()

    def _get_create_executor(self):
        raise NotImplementedError()

    def _get_delete_executor(self):
        raise NotImplementedError()

    def _get_number_of_resources(self, schedule):
        resources = getattr(schedule, self.resource_attribute)
        return resources.count()


class ScheduleBackups(BaseScheduleTask):
    name = 'openstack_tenant.ScheduleBackups'
    model = models.BackupSchedule
    resource_attribute = 'backups'

    @transaction.atomic()
    def _create_resource(self, schedule, kept_until):
        backup = models.Backup.objects.create(
            name='Backup#%s of %s' % (schedule.call_count, schedule.instance.name),
            description='Scheduled backup of instance "%s"' % schedule.instance,
            service_project_link=schedule.instance.service_project_link,
            instance=schedule.instance,
            backup_schedule=schedule,
            metadata=serializers.BackupSerializer.get_backup_metadata(schedule.instance),
            kept_until=kept_until,
        )
        serializers.BackupSerializer.create_backup_snapshots(backup)
        return backup

    def _get_create_executor(self):
        from . import executors
        return executors.BackupCreateExecutor

    def _get_delete_executor(self):
        from . import executors
        return executors.BackupDeleteExecutor

    def _log_backup_cleanup(self, schedule, amount_to_remove, resources_count):
        message_template = ('Maximum resource count "%s" has been reached.'
                            '"%s" from "%s" resources are going to be removed.')
        log.event_logger.openstack_backup_schedule.info(
            message_template % (schedule.maximal_number_of_resources, amount_to_remove, resources_count),
            event_type='resource_backup_schedule_cleaned_up',
            event_context={'resource': schedule.instance, 'backup_schedule': schedule},
        )


class BaseDeleteExpiredResourcesTask(core_tasks.BackgroundTask):
    model = NotImplemented

    def is_equal(self, other_task):
        return self.name == other_task.get('name')

    def _get_executor(self):
        raise NotImplementedError()

    @transaction.atomic
    def run(self):
        executor = self._get_delete_executor()
        resources = self.model.objects.filter(kept_until__lt=timezone.now(),
                                              state=core_models.StateMixin.States.OK)
        for resource in resources:
            executor.execute(resource)


class DeleteExpiredBackups(BaseDeleteExpiredResourcesTask):
    name = 'openstack_tenant.DeleteExpiredBackups'
    model = models.Backup

    def _get_delete_executor(self):
        from . import executors
        return executors.BackupDeleteExecutor


class ScheduleSnapshots(BaseScheduleTask):
    name = 'openstack_tenant.ScheduleSnapshots'
    model = models.SnapshotSchedule
    resource_attribute = 'snapshots'

    @transaction.atomic()
    def _create_resource(self, schedule, kept_until):
        snapshot = models.Snapshot.objects.create(
            name='Snapshot#%s of %s' % (schedule.call_count, schedule.source_volume.name),
            description='Scheduled snapshot of volume "%s"' % schedule.source_volume,
            service_project_link=schedule.source_volume.service_project_link,
            source_volume=schedule.source_volume,
            snapshot_schedule=schedule,
            size=schedule.source_volume.size,
            kept_until=kept_until,
        )
        snapshot.increase_backend_quotas_usage()
        return snapshot

    def _get_create_executor(self):
        from . import executors
        return executors.SnapshotCreateExecutor

    def _get_delete_executor(self):
        from . import executors
        return executors.SnapshotDeleteExecutor

    def _log_backup_cleanup(self, schedule, amount_to_remove, resources_count):
        message_template = ('Maximum resource count "%s" has been reached.'
                            '"%s" from "%s" resources are going to be removed.')
        log.event_logger.openstack_snapshot_schedule.info(
            message_template % (schedule.maximal_number_of_resources, amount_to_remove, resources_count),
            event_type='resource_snapshot_schedule_cleaned_up',
            event_context={'resource': schedule.source_volume, 'snapshot_schedule': schedule},
        )


class DeleteExpiredSnapshots(BaseDeleteExpiredResourcesTask):
    name = 'openstack_tenant.DeleteExpiredSnapshots'
    model = models.Snapshot

    def _get_delete_executor(self):
        from . import executors
        return executors.SnapshotDeleteExecutor


class LimitedPerTypeThrottleMixin(object):

    def get_limit(self, resource):
        nc_settings = getattr(settings, 'WALDUR_OPENSTACK', {})
        limit_per_type = nc_settings.get('MAX_CONCURRENT_PROVISION', {})
        model_name = SupportedServices.get_name_for_model(resource)
        return limit_per_type.get(model_name, super(LimitedPerTypeThrottleMixin, self).get_limit(resource))


class ThrottleProvisionTask(LimitedPerTypeThrottleMixin, structure_tasks.ThrottleProvisionTask):
    pass


class ThrottleProvisionStateTask(LimitedPerTypeThrottleMixin, structure_tasks.ThrottleProvisionStateTask):
    pass
