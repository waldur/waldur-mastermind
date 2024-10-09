import django_filters


class MigrationFilterSet(django_filters.FilterSet):
    src_resource_uuid = django_filters.UUIDFilter(field_name="src_resource__uuid")
    dst_resource_uuid = django_filters.UUIDFilter(field_name="dst_resource__uuid")
