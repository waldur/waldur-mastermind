from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import decorators
from rest_framework import permissions as rf_permissions
from rest_framework import response, status, viewsets

from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters
from waldur_mastermind.marketplace.views import BaseMarketplaceView, PublicViewsetMixin
from waldur_mastermind.proposal import filters, models, serializers


class ManagerViewSet(PublicViewsetMixin, BaseMarketplaceView):
    lookup_field = 'uuid'
    queryset = models.Manager.objects.all().order_by('customer__name')
    serializer_class = serializers.ManagerSerializer
    filterset_class = filters.CallManagerFilter


class PublicCallViewSet(viewsets.ReadOnlyModelViewSet):
    lookup_field = 'uuid'
    queryset = models.Call.objects.filter(
        state__in=[models.Call.States.ACTIVE, models.Call.States.ARCHIVED]
    ).order_by('start_time')
    serializer_class = serializers.PublicCallSerializer
    filterset_class = filters.CallFilter
    permission_classes = (rf_permissions.AllowAny,)


class ProtectedCallViewSet(core_views.ActionsViewSet):
    lookup_field = 'uuid'
    queryset = models.Call.objects.all().order_by('start_time')
    serializer_class = serializers.ProtectedCallSerializer
    filterset_class = filters.CallFilter
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    destroy_validators = [core_validators.StateValidator(models.Call.States.DRAFT)]

    @decorators.action(detail=True, methods=['get', 'post'])
    def offerings(self, request, uuid=None):
        call = self.get_object()
        method = self.request.method

        if method == 'POST':
            serializer = self.get_serializer(
                context=self.get_serializer_context(),
                data=self.request.data,
            )
            serializer.is_valid(raise_exception=True)
            serializer.save(call=call, created_by=request.user)
            return response.Response(
                serializer.data,
                status=status.HTTP_201_CREATED,
            )

        return response.Response(
            self.get_serializer(
                call.requestedoffering_set,
                context=self.get_serializer_context(),
                many=True,
            ).data,
            status=status.HTTP_200_OK,
        )

    offerings_serializer_class = serializers.RequestedOfferingSerializer

    def offering_detail(self, request, uuid=None, requested_offering_uuid=None):
        call = self.get_object()
        method = self.request.method

        try:
            requested_offering = call.requestedoffering_set.get(
                uuid=requested_offering_uuid
            )

            if method == 'DELETE':
                requested_offering.delete()
                return response.Response(status=status.HTTP_204_NO_CONTENT)

            if method in ['PUT', 'PATCH']:
                if (
                    requested_offering.state
                    != models.RequestedOffering.States.REQUESTED
                ):
                    return response.Response(status=status.HTTP_409_CONFLICT)

                serializer = self.get_serializer(
                    requested_offering,
                    context=self.get_serializer_context(),
                    data=self.request.data,
                )
                serializer.is_valid(raise_exception=True)
                serializer.save()
                return response.Response(serializer.data, status=status.HTTP_200_OK)

            serializer = self.get_serializer(
                requested_offering, context=self.get_serializer_context()
            )
            return response.Response(serializer.data, status=status.HTTP_200_OK)
        except models.RequestedOffering.DoesNotExist:
            return response.Response(status=status.HTTP_404_NOT_FOUND)

    offering_detail_serializer_class = serializers.RequestedOfferingSerializer

    @decorators.action(detail=True, methods=['post'])
    def activate(self, request, uuid=None):
        call = self.get_object()
        call.state = models.Call.States.ACTIVE
        call.save()
        return response.Response(
            'Call has been activated.',
            status=status.HTTP_200_OK,
        )

    activate_validators = [core_validators.StateValidator(models.Call.States.DRAFT)]

    @decorators.action(detail=True, methods=['post'])
    def archive(self, request, uuid=None):
        call = self.get_object()
        call.state = models.Call.States.ARCHIVED
        call.save()
        return response.Response(
            'Call has been archived.',
            status=status.HTTP_200_OK,
        )

    archive_validators = [
        core_validators.StateValidator(
            models.Call.States.DRAFT, models.Call.States.ACTIVE
        )
    ]
