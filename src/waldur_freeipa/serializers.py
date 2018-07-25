from __future__ import unicode_literals

from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers

from . import models


class ProfileSerializer(core_serializers.AugmentedSerializerMixin,
                        serializers.HyperlinkedModelSerializer):

    agree_with_policy = serializers.BooleanField(write_only=True,
                                                 help_text=_('User must agree with the policy.'))

    class Meta(object):
        model = models.Profile
        fields = ('uuid', 'username', 'agreement_date', 'is_active', 'agree_with_policy')
        protected_fields = ('username', 'agreement_date')
        read_only_fields = ('is_active',)

        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'freeipa-profile-detail'},
        )

    def create(self, validated_data):
        # Check if user already has FreeIPA profile
        user = self.context['request'].user
        if models.Profile.objects.filter(user=user).exists():
            raise serializers.ValidationError({
                'details': _('User already has profile.')
            })
        validated_data['user'] = user

        # Check if user agrees with policy
        if not validated_data.pop('agree_with_policy'):
            raise serializers.ValidationError({
                'agree_with_policy': _('User must agree with the policy.')
            })

        # Prepend username suffix
        prefix = settings.WALDUR_FREEIPA['USERNAME_PREFIX']
        if prefix:
            validated_data['username'] = '%s%s' % (prefix, validated_data['username'])

        return super(ProfileSerializer, self).create(validated_data)
