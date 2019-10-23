from django.conf import settings
from django.db import models
from rest_framework import serializers

from waldur_core.media.utils import encode_protected_url
from waldur_core.structure.metadata import merge_dictionaries


class ProtectedFileMixin:
    def to_representation(self, value):
        if not value:
            return None

        if settings.DEBUG:
            return super(ProtectedFileMixin, self).to_representation(value)

        return encode_protected_url(value.instance,
                                    field=self.source_attrs[-1],
                                    request=self.context['request'])


class ProtectedFileField(ProtectedFileMixin, serializers.FileField):
    pass


class ProtectedImageField(ProtectedFileMixin, serializers.ImageField):
    pass


class ProtectedMediaSerializerMixin(serializers.ModelSerializer):
    serializer_field_mapping = merge_dictionaries(
        serializers.ModelSerializer.serializer_field_mapping,
        {
            models.FileField: ProtectedFileField,
            models.ImageField: ProtectedImageField,
        },
    )
