from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import viewsets as rf_viewsets

from waldur_core.structure import filters as structure_filters
from waldur_mastermind.marketplace.views import PublicViewsetMixin
from waldur_pid import filters, models, serializers


@extend_schema_view(
    list=extend_schema(
        summary="List Datacite referrals for offerings",
        description="Returns a paginated list of Datacite referrals associated with marketplace offerings. Referrals represent relationships between an offering (identified by a DOI) and other research outputs, such as publications or datasets. The list must be filtered by the offering's scope.",
    ),
    retrieve=extend_schema(
        summary="Retrieve a specific Datacite referral",
        description="Returns the details of a single Datacite referral record, identified by its UUID. Details include the related identifier (PID), the type of relationship, and metadata about the related work.",
    ),
)
class OfferingReferralsViewSet(PublicViewsetMixin, rf_viewsets.ReadOnlyModelViewSet):
    queryset = models.DataciteReferral.objects.all()
    serializer_class = serializers.OfferingReferralSerializer
    lookup_field = "uuid"
    filter_backends = (
        filters.OfferingReferralScopeFilterBackend,
        structure_filters.GenericRoleFilter,
        DjangoFilterBackend,
    )
    filterset_class = filters.OfferingReferralFilter
