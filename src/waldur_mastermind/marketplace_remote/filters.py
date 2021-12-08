import django_filters

from . import models


class ProjectUpdateRequestFilter(django_filters.FilterSet):
    project_uuid = django_filters.UUIDFilter(field_name='project__uuid')
    customer_uuid = django_filters.UUIDFilter(field_name='project__customer__uuid')
    offering_uuid = django_filters.UUIDFilter(field_name='offering__uuid')
    offering_customer_uuid = django_filters.UUIDFilter(
        field_name='offering__customer__uuid'
    )

    class Meta:
        model = models.ProjectUpdateRequest
        fields = []
