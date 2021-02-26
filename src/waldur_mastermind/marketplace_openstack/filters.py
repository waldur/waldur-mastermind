from django.conf import settings as django_settings
from django.core.exceptions import ObjectDoesNotExist

from waldur_core.core import filters as core_filters
from waldur_mastermind.marketplace.models import Category


class VpcExternalFilter(core_filters.BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        if request.user.is_staff:
            return queryset
        if not django_settings.WALDUR_CORE['ONLY_STAFF_MANAGES_SERVICES']:
            return queryset
        try:
            category_uuid = Category.objects.get(default_tenant_category=True).uuid
        except ObjectDoesNotExist:
            return queryset
        else:
            return queryset.exclude(category__uuid=category_uuid)
