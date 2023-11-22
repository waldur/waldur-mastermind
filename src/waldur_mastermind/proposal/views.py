from waldur_mastermind.marketplace.views import BaseMarketplaceView, PublicViewsetMixin
from waldur_mastermind.proposal import filters, models, serializers


class CallManagerViewSet(PublicViewsetMixin, BaseMarketplaceView):
    queryset = models.CallManager.objects.all().order_by('customer__name')
    serializer_class = serializers.CallManagerSerializer
    filterset_class = filters.CallManagerFilter
