from __future__ import unicode_literals

from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

from waldur_ansible.playbook_jobs import models
from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core import utils as core_utils
from waldur_core.structure.serializers import PermissionFieldFilteringMixin
from waldur_openstack.openstack_tenant import models as openstack_models


class JobEstimateSerializer(PermissionFieldFilteringMixin,
                            serializers.HyperlinkedModelSerializer):
    ssh_public_key = serializers.HyperlinkedRelatedField(
        lookup_field='uuid',
        view_name='sshpublickey-detail',
        queryset=core_models.SshPublicKey.objects.all(),
        required=True,
    )
    service_project_link = serializers.HyperlinkedRelatedField(
        lookup_field='pk',
        view_name='openstacktenant-spl-detail',
        queryset=openstack_models.OpenStackTenantServiceProjectLink.objects.all(),
    )
    playbook = serializers.HyperlinkedRelatedField(
        lookup_field='uuid',
        view_name=core_utils.get_detail_view_name(models.Playbook),
        queryset=models.Playbook.objects.all(),
    )
    arguments = core_serializers.JSONField(default={})

    class Meta(object):
        model = models.Job
        fields = ('ssh_public_key', 'service_project_link', 'playbook', 'arguments')

    def get_filtered_field_names(self):
        return 'service_project_link',

    def check_subnet(self, attrs):
        if not self.instance:
            settings = attrs['service_project_link'].service.settings
            if not openstack_models.SubNet.objects.filter(settings=settings).exists():
                raise serializers.ValidationError(_('Selected OpenStack provider does not have any subnet yet.'))
            else:
                attrs['subnet'] = openstack_models.SubNet.objects.filter(settings=settings).first()

    def validate(self, attrs):
        if not self.instance:
            attrs['user'] = self.context['request'].user

        self.check_subnet(attrs)
        return attrs
