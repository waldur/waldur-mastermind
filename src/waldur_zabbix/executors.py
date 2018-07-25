from celery import chain

from waldur_core.core import executors, tasks, utils

from . import models
from .tasks import SMSTask, UpdateSettingsCredentials


class HostCreateExecutor(executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, host, serialized_host, **kwargs):
        return tasks.BackendMethodTask().si(
            serialized_host, 'create_host', state_transition='begin_creating')


class HostUpdateExecutor(executors.UpdateExecutor):

    @classmethod
    def get_task_signature(cls, host, serialized_host, **kwargs):
        return tasks.BackendMethodTask().si(
            serialized_host, 'update_host', state_transition='begin_updating')


class HostDeleteExecutor(executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, host, serialized_host, **kwargs):
        if host.backend_id:
            return tasks.BackendMethodTask().si(
                serialized_host, 'delete_host', state_transition='begin_deleting')
        else:
            return tasks.StateTransitionTask().si(serialized_host, state_transition='begin_deleting')


class HostPullExecutor(executors.BaseExecutor):

    @classmethod
    def get_task_signature(cls, host, serialized_host, **kwargs):
        return tasks.BackendMethodTask().si(serialized_host, 'pull_host')


class ITServiceCreateExecutor(executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, itservice, serialized_itservice, **kwargs):
        return tasks.BackendMethodTask().si(
            serialized_itservice, 'create_itservice', state_transition='begin_creating')


class ITServiceDeleteExecutor(executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, itservice, serialized_itservice, **kwargs):
        if itservice.backend_id:
            return tasks.BackendMethodTask().si(
                serialized_itservice, 'delete_itservice', state_transition='begin_deleting')
        else:
            return tasks.StateTransitionTask().si(serialized_itservice, state_transition='begin_deleting')


class UserCreateExecutor(executors.CreateExecutor):

    @classmethod
    def get_task_signature(cls, user, serialized_user, **kwargs):
        creation_tasks = [tasks.BackendMethodTask().si(
            serialized_user, 'create_user', state_transition='begin_creating')]
        # send SMS if user has phone number
        if user.phone:
            serialized_settings = utils.serialize_instance(user.settings)
            message = 'Zabbix "%s" password: %s' % (user.settings.name, user.password)
            creation_tasks.append(SMSTask().si(serialized_settings, message, user.phone))
        return chain(*creation_tasks)


class UserUpdateExecutor(executors.UpdateExecutor):

    @classmethod
    def get_task_signature(cls, user, serialized_user, **kwargs):
        update_tasks = [tasks.BackendMethodTask().si(
            serialized_user, 'update_user', state_transition='begin_updating')]
        # send SMS if user password has been updated
        if 'password' in kwargs.get('updated_fields', []) and user.phone:
            serialized_settings = utils.serialize_instance(user.settings)
            message = 'Zabbix "%s" password: %s' % (user.settings.name, user.password)
            update_tasks.append(SMSTask().si(serialized_settings, message, user.phone))
        return chain(*update_tasks)


class UserDeleteExecutor(executors.DeleteExecutor):

    @classmethod
    def get_task_signature(cls, user, serialized_user, **kwargs):
        if user.backend_id:
            return tasks.BackendMethodTask().si(
                serialized_user, 'delete_user', state_transition='begin_deleting')
        else:
            return tasks.StateTransitionTask().si(serialized_user, state_transition='begin_deleting')


class ServiceSettingsPasswordResetExecutor(executors.ActionExecutor):
    """ Reset user password and update service settings options. """

    @classmethod
    def get_task_signature(cls, service_settings, serialized_service_settings, **kwargs):
        user = models.User.objects.get(settings=service_settings, alias=service_settings.username)
        user.password = kwargs.pop('password')
        user.schedule_updating()
        user.save()
        serialized_user = utils.serialize_instance(user)
        _tasks = [
            tasks.StateTransitionTask().si(serialized_service_settings, state_transition='begin_updating'),
            tasks.BackendMethodTask().si(serialized_user, 'update_user', state_transition='begin_updating'),
            UpdateSettingsCredentials().si(serialized_service_settings, serialized_user),
            tasks.StateTransitionTask().si(serialized_user, state_transition='set_ok'),
        ]
        if user.phone:
            message = 'Zabbix "%s" password: %s' % (user.settings.name, user.password)
            _tasks.append(SMSTask().si(serialized_service_settings, message, user.phone))
        return chain(*_tasks)
