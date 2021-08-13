from waldur_core.structure.serializers import BaseResourceSerializer

from . import models


class JobSerializer(BaseResourceSerializer):
    class Meta(BaseResourceSerializer.Meta):
        model = models.Job
        fields = BaseResourceSerializer.Meta.fields + (
            'runtime_state',
            'file',
            'user',
            'user_uuid',
            'user_name',
            'report',
        )
        read_only_fields = BaseResourceSerializer.Meta.read_only_fields + (
            'user',
            'report',
        )
        protected_fields = BaseResourceSerializer.Meta.protected_fields + ('file',)
        extra_kwargs = {
            **BaseResourceSerializer.Meta.extra_kwargs,
            'user': {'lookup_field': 'uuid', 'view_name': 'user-detail'},
        }
        related_paths = {
            'user': ('uuid', 'name'),
        }

    def get_fields(self):
        fields = super().get_fields()
        if not self.instance:
            fields['file'].required = True
            fields['file'].allow_null = False
        return fields

    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user
        return super().create(validated_data)
