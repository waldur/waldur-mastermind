from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers

from . import models, utils


class ProfileSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    user = serializers.HyperlinkedRelatedField(
        source='author.user',
        view_name='user-detail',
        lookup_field='uuid',
        read_only=True,
    )
    user_uuid = serializers.ReadOnlyField(source='user.uuid')
    user_username = serializers.ReadOnlyField(source='user.username')
    user_full_name = serializers.ReadOnlyField(source='user.full_name')

    class Meta:
        model = models.Profile
        fields = (
            'uuid',
            'username',
            'agreement_date',
            'is_active',
            'user',
            'user_uuid',
            'user_username',
            'user_full_name',
        )
        protected_fields = ('username', 'agreement_date')
        read_only_fields = ('is_active',)

        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'freeipa-profile-detail'},
        )

    def create(self, validated_data):
        # Check if user already has FreeIPA profile
        user = self.context['request'].user
        if models.Profile.objects.filter(user=user).exists():
            raise serializers.ValidationError(
                {'details': _('User already has profile.')}
            )
        validated_data['user'] = user

        validated_data['username'] = utils.generate_username(validated_data['username'])

        validated_data['is_active'] = utils.is_profile_active_for_user(user)

        return super(ProfileSerializer, self).create(validated_data)
