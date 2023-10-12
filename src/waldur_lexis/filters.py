import django_filters

from . import models


class LexisLinkFilter(django_filters.FilterSet):
    uuid = django_filters.UUIDFilter(field_name='uuid')
    resource_uuid = django_filters.UUIDFilter(
        field_name='robot_account__resource__uuid'
    )
    project_uuid = django_filters.UUIDFilter(
        field_name='robot_account__resource__project__uuid'
    )
    customer_uuid = django_filters.UUIDFilter(
        field_name='robot_account__resource__customer__uuid'
    )

    class Meta:
        model = models.LexisLink
        fields = ('uuid', 'resource_uuid', 'project_uuid', 'customer_uuid')
