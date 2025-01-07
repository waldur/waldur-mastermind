from celery import shared_task
from django.utils import timezone

from . import models


@shared_task(name="waldur_core.permissions.check_expired_permissions")
def check_expired_permissions():
    for permission in models.UserRole.objects.filter(
        expiration_time__lt=timezone.now(), is_active=True
    ):
        permission.revoke()
