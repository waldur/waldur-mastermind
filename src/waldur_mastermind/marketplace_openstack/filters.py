from django.conf import settings as django_settings

from waldur_core.core import filters as core_filters


class VpcExternalFilter(core_filters.BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        if request.user.is_staff:
            return queryset
        if not django_settings.WALDUR_CORE['ONLY_STAFF_MANAGES_SERVICES']:
            return queryset
        category_uuid = django_settings.WALDUR_MARKETPLACE_OPENSTACK['TENANT_CATEGORY_UUID']
        return queryset.exclude(category__uuid=category_uuid)
