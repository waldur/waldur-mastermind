import django_filters

from waldur_core.core import filters as core_filters


class MigrationFilterSet(django_filters.FilterSet):
    src_resource_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-resource-detail", field_name="src_resource__uuid"
    )
    dst_resource_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-resource-detail", field_name="dst_resource__uuid"
    )
