from rest_framework import serializers

from waldur_mastermind.marketplace import models as marketplace_models

from . import models


class LexisLinkSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.LexisLink
        fields = (
            'url',
            'uuid',
            'created',
            'modified',
            'robot_account',
            'state',
            'resource_uuid',
            'resource_name',
            'project_uuid',
            'project_name',
            'customer_uuid',
            'customer_name',
        )
        read_only_fields = ['state', 'robot_account']
        protected_fields = ['robot_account']
        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'lexis-link-detail'},
            robot_account={
                'lookup_field': 'uuid',
                'view_name': 'marketplace-robot-account-detail',
            },
        )

    resource_uuid = serializers.ReadOnlyField(source='robot_account.resource.uuid')
    resource_name = serializers.ReadOnlyField(source='robot_account.resource.name')
    project_uuid = serializers.ReadOnlyField(
        source='robot_account.resource.project.uuid'
    )
    project_name = serializers.ReadOnlyField(
        source='robot_account.resource.project.name'
    )
    customer_uuid = serializers.ReadOnlyField(
        source='robot_account.resource.project.customer.uuid'
    )
    customer_name = serializers.ReadOnlyField(
        source='robot_account.resource.project.customer.name'
    )


class LexisLinkCreateSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.LexisLink
        fields = ['resource']

    resource = serializers.HyperlinkedRelatedField(
        view_name='marketplace-resource-detail',
        lookup_field='uuid',
        queryset=marketplace_models.Resource.objects.all(),
        write_only=True,
    )

    def create(self, validated_data):
        resource = validated_data.pop('resource')
        prefix = "hl"
        previous_accounts = marketplace_models.RobotAccount.objects.filter(
            resource=resource, type__istartswith=prefix
        ).order_by('type')

        if previous_accounts.exists():
            last_type = previous_accounts.last().type
            last_number = int(last_type[-3:])
            number = str(last_number + 1).zfill(3)
        else:
            number = '0'.zfill(3)

        type_str = f"{prefix}{number}"
        robot_account = marketplace_models.RobotAccount.objects.create(
            username='',
            type=type_str,
            resource=resource,
        )

        validated_data['robot_account'] = robot_account
        return super().create(validated_data)
