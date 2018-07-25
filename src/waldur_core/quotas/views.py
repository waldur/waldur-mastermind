from __future__ import unicode_literals

from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions as rf_exceptions, decorators, response, status
from rest_framework import mixins
from rest_framework import viewsets
from reversion.models import Version

from waldur_core.core.pagination import UnlimitedLinkHeaderPagination
from waldur_core.core.serializers import HistorySerializer
from waldur_core.core.utils import datetime_to_timestamp
from waldur_core.quotas import models, serializers, filters, exceptions


class QuotaViewSet(mixins.UpdateModelMixin,
                   viewsets.ReadOnlyModelViewSet):
    queryset = models.Quota.objects.all()
    serializer_class = serializers.QuotaSerializer
    lookup_field = 'uuid'
    # XXX: Remove a custom pagination class once the quota calculation has been made more efficient
    pagination_class = UnlimitedLinkHeaderPagination
    filter_class = filters.QuotaFilterSet

    def get_queryset(self):
        return models.Quota.objects.filtered_for_user(self.request.user)

    def list(self, request, *args, **kwargs):
        """
        To get an actual value for object quotas limit and usage issue a **GET** request against */api/<objects>/*.

        To get all quotas visible to the user issue a **GET** request against */api/quotas/*
        """
        return super(QuotaViewSet, self).list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        """
        To set quota limit issue a **PUT** request against */api/quotas/<quota uuid>** with limit values.

        Please note that if a quota is a cache of a backend quota (e.g. 'storage' size of an OpenStack tenant),
        it will be impossible to modify it through */api/quotas/<quota uuid>** endpoint.

        Example of changing quota limit:

        .. code-block:: http

            POST /api/quotas/6ad5f49d6d6c49648573b2b71f44a42b/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "limit": 2000.0
            }

        Example of changing quota threshold:

        .. code-block:: http

            PUT /api/quotas/6ad5f49d6d6c49648573b2b71f44a42b/ HTTP/1.1
            Content-Type: application/json
            Accept: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "threshold": 100.0
            }

        """
        return super(QuotaViewSet, self).retrieve(request, *args, **kwargs)

    def perform_update(self, serializer):
        if not serializer.instance.scope.can_user_update_quotas(self.request.user):
            raise rf_exceptions.PermissionDenied()
        quota = self.get_object()
        quota_field = quota.get_field()
        # old style quotas do not have quota_field
        if quota_field is not None and quota_field.is_backend:
            raise exceptions.BackendQuotaUpdateError()

        if 'limit' in serializer.validated_data:
            limit = serializer.validated_data['limit']
            if limit != -1 and quota.usage > limit:
                raise rf_exceptions.ValidationError(_('Current quota usage exceeds new limit.'))
            quota.scope.set_quota_limit(quota.name, limit)
            serializer.instance.refresh_from_db()

        if 'threshold' in serializer.validated_data:
            threshold = serializer.validated_data['threshold']
            quota.threshold = threshold
            quota.save(update_fields=['threshold'])
            serializer.instance.refresh_from_db()

    @decorators.detail_route()
    def history(self, request, uuid=None):
        """
        Historical data endpoints could be available for any objects (currently
        implemented for quotas and events count). The data is available at *<object_endpoint>/history/*,
        for example: */api/quotas/<uuid>/history/*.

        There are two ways to define datetime points for historical data.

        1. Send *?point=<timestamp>* parameter that can list. Response will contain historical data for each given point
            in the same order.
        2. Send *?start=<timestamp>*, *?end=<timestamp>*, *?points_count=<integer>* parameters.
           Result will contain <points_count> points from <start> to <end>.

        Response format:

        .. code-block:: javascript

            [
                {
                    "point": <timestamp>,
                    "object": {<object_representation>}
                },
                {
                    "point": <timestamp>
                    "object": {<object_representation>}
                },
            ...
            ]

        NB! There will not be any "object" for corresponding point in response if there
        is no data about object for a given timestamp.
        """
        mapped = {
            'start': request.query_params.get('start'),
            'end': request.query_params.get('end'),
            'points_count': request.query_params.get('points_count'),
            'point_list': request.query_params.getlist('point'),
        }
        history_serializer = HistorySerializer(data={k: v for k, v in mapped.items() if v})
        history_serializer.is_valid(raise_exception=True)

        quota = self.get_object()
        serializer = self.get_serializer(quota)
        serialized_versions = []
        for point_date in history_serializer.get_filter_data():
            serialized = {'point': datetime_to_timestamp(point_date)}
            version = Version.objects.get_for_object(quota).filter(revision__date_created__lte=point_date)
            if version.exists():
                # make copy of serialized data and update field that are stored in version
                version_object = version.first()._object_version.object
                serialized['object'] = serializer.data.copy()
                serialized['object'].update({
                    f: getattr(version_object, f) for f in quota.get_version_fields()
                })
            serialized_versions.append(serialized)
        return response.Response(serialized_versions, status=status.HTTP_200_OK)
