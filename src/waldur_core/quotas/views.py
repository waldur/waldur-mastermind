from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions as rf_exceptions
from rest_framework import mixins, viewsets

from waldur_core.quotas import exceptions, filters, models, serializers


class QuotaViewSet(mixins.UpdateModelMixin, viewsets.ReadOnlyModelViewSet):
    queryset = models.Quota.objects.all()
    serializer_class = serializers.QuotaSerializer
    lookup_field = 'uuid'
    filterset_class = filters.QuotaFilterSet

    def get_queryset(self):
        return models.Quota.objects.filtered_for_user(self.request.user)

    def list(self, request, *args, **kwargs):
        """
        To get an actual value for object quotas limit and usage issue a **GET** request against */api/<objects>/*.

        To get all quotas visible to the user issue a **GET** request against */api/quotas/*
        """
        return super().list(request, *args, **kwargs)

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
        return super().retrieve(request, *args, **kwargs)

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
                raise rf_exceptions.ValidationError(
                    _('Current quota usage exceeds new limit.')
                )
            quota.scope.set_quota_limit(quota.name, limit)
            serializer.instance.refresh_from_db()

        if 'threshold' in serializer.validated_data:
            threshold = serializer.validated_data['threshold']
            quota.threshold = threshold
            quota.save(update_fields=['threshold'])
            serializer.instance.refresh_from_db()
