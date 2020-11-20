from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from oauthlib.oauth2.rfc6749.errors import OAuth2Error
from rest_framework import exceptions, status
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import permissions as structure_permissions
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import serializers as marketplace_serializers

from . import models, serializers
from .backend import GoogleAuthorize


class GoogleAuthViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = marketplace_models.ServiceProvider.objects.exclude(
        googlecredentials__isnull=True
    )
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    serializer_class = marketplace_serializers.ServiceProviderSerializer
    lookup_field = 'uuid'

    @action(detail=True, methods=['get'])
    def authorize(self, request, uuid=None):
        service_provider = self.get_object()
        redirect_uri = request.build_absolute_uri().replace('authorize', 'callback')
        backend = GoogleAuthorize(service_provider, redirect_uri)
        url = backend.get_authorization_url()
        return Response(
            {'request_url': url, 'redirect_uri': redirect_uri},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['get'])
    def callback(self, request, uuid=None):
        service_provider = self.get_object()
        code = request.query_params.get('code')
        if not code:
            raise exceptions.ValidationError(_('Google auth is failed.'))
        redirect_uri = request.build_absolute_uri(request.path)
        backend = GoogleAuthorize(service_provider, redirect_uri)
        try:
            backend.create_tokens(code)
        except OAuth2Error:
            raise exceptions.ValidationError(_('Google auth is failed.'))
        return Response(_('Google auth is success.'), status=status.HTTP_200_OK)


class GoogleCredentialsViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = marketplace_models.ServiceProvider.objects.all()
    serializer_class = marketplace_serializers.ServiceProviderSerializer
    lookup_field = 'uuid'
    filter_backends = (DjangoFilterBackend,)

    @action(detail=True, methods=['GET', 'POST'])
    def google_credentials(self, request, uuid=None):
        service_provider = self.get_object()
        if request.method == 'GET':
            google_credentials = getattr(service_provider, 'googlecredentials', None)

            if google_credentials:
                return Response(
                    self.get_serializer(google_credentials).data,
                    status=status.HTTP_200_OK,
                )
            else:
                return Response(status=status.HTTP_200_OK)
        else:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            models.GoogleCredentials.objects.update_or_create(
                service_provider=service_provider,
                defaults={
                    'client_id': serializer.validated_data['client_id'],
                    'project_id': serializer.validated_data['project_id'],
                    'client_secret': serializer.validated_data['client_secret'],
                },
            )
            return Response(status=status.HTTP_200_OK)

    google_credentials_permissions = [structure_permissions.is_owner]
    google_credentials_serializer_class = serializers.GoogleCredentialsSerializer
