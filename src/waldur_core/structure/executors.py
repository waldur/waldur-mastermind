from celery import chain
from django.core import checks
from django.db.migrations.topological_sort import stable_topological_sort
from django.db.models import Model

from waldur_core.core import WaldurExtension
from waldur_core.core import executors as core_executors
from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils
from waldur_core.structure.tasks import ConnectSharedSettingsTask


class ServiceSettingsCreateExecutor(core_executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, settings, serialized_settings, **kwargs):
        creation_tasks = [core_tasks.StateTransitionTask().si(serialized_settings, state_transition='begin_creating')]
        # connect settings to all customers if they are shared
        if settings.shared:
            creation_tasks.append(ConnectSharedSettingsTask().si(serialized_settings))
        # sync settings if they have not only global properties
        backend = settings.get_backend()
        if not backend.has_global_properties():
            creation_tasks.append(core_tasks.IndependentBackendMethodTask().si(serialized_settings, 'sync'))
        return chain(*creation_tasks)


class ServiceSettingsPullExecutor(core_executors.ActionExecutor):

    @classmethod
    def get_task_signature(cls, settings, serialized_settings, **kwargs):
        return core_tasks.IndependentBackendMethodTask().si(
            serialized_settings, 'sync', state_transition='begin_updating')


class ServiceSettingsConnectSharedExecutor(core_executors.BaseExecutor):

    @classmethod
    def get_task_signature(cls, settings, serialized_settings, **kwargs):
        return ConnectSharedSettingsTask().si(serialized_settings)


class BaseCleanupExecutor(core_executors.BaseExecutor):
    """
    In order to delete project and related resources, we need to take into account three constraints:

    1) dependencies between applications;
    2) dependencies between resources;
    3) backend API calls to delete resources.

    All these steps are optional, because some applications don't have all these constraints.

    1) If `related_executor` is specified, then current executor is applied before related executor.

    2) Project's resources are specified by the `pre_models` field.
       It is assumed that each model class can be filtered by project.

    3) The value of `executors` field is list of tuples (model class, executor class).
       Executors are applied after resources specified by pre_models field are deleted.
       When executor is applied, all resources in the project are deleted using this executor.

    Consider for example:

    class OpenStackCleanupExecutor(structure_executors.BaseCleanupExecutor):
        executors = (
            (models.SecurityGroup, SecurityGroupDeleteExecutor),
            (models.FloatingIP, FloatingIPDeleteExecutor),
            (models.SubNet, SubNetDeleteExecutor),
            (models.Network, NetworkDeleteExecutor),
            (models.Tenant, TenantDeleteExecutor),
        )

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
    """

    pre_models = []
    executors = []
    related_executor = None

    @classmethod
    def pre_apply(cls, instance, **kwargs):
        for model_cls in cls.pre_models:
            model_cls.objects.filter(project=instance).delete()

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        """
        Delete each resource using specific executor.
        Convert executors to task and combine all deletion task into single sequential task.
        """
        cleanup_tasks = [
            ProjectResourceCleanupTask().si(
                core_utils.serialize_class(executor_cls),
                core_utils.serialize_class(model_cls),
                serialized_instance,
            )
            for (model_cls, executor_cls) in cls.executors
        ]

        if not cleanup_tasks:
            return core_tasks.EmptyTask()

        return chain(cleanup_tasks)


class ProjectResourceCleanupTask(core_tasks.Task):

    @classmethod
    def get_description(cls, executor, model, project, *args, **kwargs):
        return 'Delete "%s" resources for project "%s" using executor %s.' % (model, project, executor)

    def run(self, serialized_executor, serialized_model, serialized_project, *args, **kwargs):
        executor = core_utils.deserialize_class(serialized_executor)
        model_cls = core_utils.deserialize_class(serialized_model)
        project = core_utils.deserialize_instance(serialized_project)

        for resource in model_cls.objects.filter(project=project):
            executor.execute(resource, async=False, force=True, **kwargs)


class ProjectCleanupExecutor(core_executors.BaseExecutor):

    @classmethod
    def get_task_signature(cls, instance, serialized_instance, **kwargs):
        executors = cls.get_executors()

        # Combine all executors into single sequential task
        # to cleanup related resources in correct order
        cleanup_tasks = [executor.as_signature(instance) for executor in executors]

        if not cleanup_tasks:
            return core_tasks.EmptyTask()

        return chain(cleanup_tasks)

    @classmethod
    def get_success_signature(cls, instance, serialized_instance, **kwargs):
        # Delete project itself if all task have been completed successfully
        return core_tasks.DeletionTask().si(serialized_instance)

    @classmethod
    def get_executors(cls):
        # Get cleanup executors from extensions
        executors = [
            extension.get_cleanup_executor()
            for extension in WaldurExtension.get_extensions()
        ]

        # Filter empty items from list because cleanup executor is optional
        executors = [executor for executor in executors if executor]

        # Apply topological sort with respect to dependencies between executors
        dependencies = {}
        for executor in executors:
            dependencies[executor] = set()
            if executor.related_executor:
                dependencies.setdefault(executor.related_executor, set())
                dependencies[executor.related_executor].add(executor)
        return stable_topological_sort(executors, dependencies)


def is_valid_executor(item):
    return (
        isinstance(item, tuple) and
        issubclass(item[0], Model) and
        issubclass(item[1], core_executors.BaseExecutor)
    )


def check_cleanup_executors(app_configs, **kwargs):
    errors = []

    for extension in WaldurExtension.get_extensions():
        cleanup_executor = extension.get_cleanup_executor()
        if not cleanup_executor:
            continue

        for model in cleanup_executor.pre_models:
            if not issubclass(model, Model):
                errors.append(
                    checks.Error(
                        'Invalid resource model is detected in project cleanup executor.',
                        obj=cleanup_executor,
                        id='waldur.E001',
                    )
                )

        for item in cleanup_executor.executors:
            if not is_valid_executor(item):
                errors.append(
                    checks.Error(
                        'Invalid resource executor is detected in project cleanup executor.',
                        obj=cleanup_executor,
                        id='waldur.E001',
                    )
                )

    return errors
