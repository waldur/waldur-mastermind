from django.conf import settings
from django.db import models


class OAuthToken(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, related_name='auth_profile', on_delete=models.CASCADE
    )
    provider = models.CharField(max_length=32)
    access_token = models.TextField()
    refresh_token = models.TextField()

    class Meta:
        unique_together = ('user', 'provider')
