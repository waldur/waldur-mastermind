from celery import Task

from . import backend, models


class SupportUserPullTask(Task):
    """ Pull support users from backend """
    name = 'support.SupportUserPullTask'

    def run(self):
        backend_users = backend.get_active_backend().get_users()
        for backend_user in backend_users:
            user, created = models.SupportUser.objects.get_or_create(
                backend_id=backend_user.backend_id, defaults={'name': backend_user.name})
            if not created and user.name != backend_user.name:
                user.name = backend_user.name
                user.save()
