from django.contrib.auth import get_user_model
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

from waldur_core.structure import models as structure_models
from waldur_core.users import models

User = get_user_model()


class InvitationSerializer(serializers.HyperlinkedModelSerializer):
    project = serializers.HyperlinkedRelatedField(
        view_name='project-detail',
        lookup_field='uuid',
        queryset=structure_models.Project.objects.all(),
        required=False,
        allow_null=True
    )
    project_name = serializers.ReadOnlyField(source='project.name')
    customer = serializers.HyperlinkedRelatedField(
        view_name='customer-detail',
        lookup_field='uuid',
        queryset=structure_models.Customer.objects.all(),
        required=False,
        allow_null=True
    )
    customer_name = serializers.ReadOnlyField(source='customer.name')

    expires = serializers.DateTimeField(source='get_expiration_time', read_only=True)

    class Meta(object):
        model = models.Invitation
        fields = ('url', 'uuid', 'link_template', 'email', 'civil_number',
                  'project', 'project_role', 'project_name',
                  'customer', 'customer_role', 'customer_name',
                  'state', 'error_message', 'created', 'expires')
        read_only_fields = ('url', 'uuid', 'state', 'error_message', 'created', 'expires')
        extra_kwargs = {
            'url': {
                'lookup_field': 'uuid',
                'view_name': 'user-invitation-detail',
            },
            'project_role': {
                'required': False,
                'allow_null': True,
            },
            'customer_role': {
                'required': False,
                'allow_null': True,
            },
        }

    def validate(self, attrs):
        link_template = attrs['link_template']
        if '{uuid}' not in link_template:
            raise serializers.ValidationError({'link_template': _("Link template must include '{uuid}' parameter.")})

        project = attrs.get('project')
        customer = attrs.get('customer')

        project_role = attrs.get('project_role', '')
        customer_role = attrs.get('customer_role', '')

        if customer and project:
            raise serializers.ValidationError(_('Cannot create invitation to project and customer simultaneously.'))
        elif not (customer or project):
            raise serializers.ValidationError(_('Customer or project must be provided.'))
        elif (customer and not customer_role) or (customer_role and not customer):
            raise serializers.ValidationError({'customer_role': _('Customer and its role must be provided.')})
        elif (project and not project_role) or (project_role and not project):
            raise serializers.ValidationError({'project_role': _('Project and its role must be provided.')})

        return attrs

    def create(self, validated_data):
        validated_data['created_by'] = self.context['request'].user
        project = validated_data.get('project')
        if project:
            validated_data['customer'] = project.customer
        return super(InvitationSerializer, self).create(validated_data)
