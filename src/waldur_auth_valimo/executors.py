from celery import chain

from waldur_core.core import executors

from .tasks import AuthErrorTask, AuthTask, PollTask


class AuthExecutor(executors.ErrorExecutorMixin, executors.BaseExecutor):
    @classmethod
    def get_failure_signature(cls, instance, serialized_instance, **kwargs):
        return AuthErrorTask().s(serialized_instance)

    @classmethod
    def get_task_signature(cls, instance, serialized_instance):
        return chain(
            AuthTask().si(serialized_instance, state_transition="begin_processing"),
            PollTask().si(serialized_instance).set(countdown=30),
        )
