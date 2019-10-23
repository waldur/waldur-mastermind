import os

from django.db import models


def get_upload_path(instance, filename):
    path = '%s/%s' % (instance._meta.model_name, instance.uuid.hex)
    _, ext = os.path.splitext(filename)
    return '%s%s' % (path, ext)


class ImageModelMixin(models.Model):
    class Meta:
        abstract = True

    image = models.ImageField(upload_to=get_upload_path, null=True, blank=True)
