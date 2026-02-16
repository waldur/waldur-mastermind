import django_filters

from waldur_core.core import filters as core_filters
from waldur_mastermind.marketplace_remote import models


class ProjectUpdateRequestFilter(django_filters.FilterSet):
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="project__uuid"
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="project__customer__uuid"
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail", field_name="offering__uuid"
    )
    provider_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-service-provider-detail",
        field_name="offering__customer__uuid",
    )
    state = core_filters.ReviewStateFilter()

    class Meta:
        model = models.ProjectUpdateRequest
        fields = []
