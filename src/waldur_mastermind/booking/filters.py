from django_filters.rest_framework import DjangoFilterBackend

from waldur_core.structure import models as structure_models


class OfferingCustomersFilterBackend(DjangoFilterBackend):
    def filter_queryset(self, request, queryset, view):
        user = request.user
        if user.is_staff:
            return queryset
        else:
            customers = structure_models.CustomerPermission.objects.filter(
                user=user,
                role=structure_models.CustomerRole.OWNER).values_list('customer', flat=True)
            return queryset.filter(offering__customer_id__in=customers)
