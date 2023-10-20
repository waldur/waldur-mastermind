from rest_framework import mixins, viewsets

from waldur_core.quotas import filters, models, serializers


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
        """
        return super().retrieve(request, *args, **kwargs)
