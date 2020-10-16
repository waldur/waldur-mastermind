from django.conf import settings
from django.db import models


class AuthProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, related_name='auth_profile', on_delete=models.CASCADE
    )
    facebook = models.CharField(max_length=120, blank=True, null=True, unique=True)
