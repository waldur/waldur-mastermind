from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from oauthlib.oauth2.rfc6749.errors import OAuth2Error
from rest_framework import exceptions, status
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.marketplace import models as marketplace_models

from . import filters, serializers
from .backend import GoogleAuthorize


class GoogleAuthViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = marketplace_models.ServiceProvider.objects.exclude().order_by(
        "customer__name"
    )
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.GoogleAuthFilter
    serializer_class = serializers.GoogleCredentialsSerializer
    lookup_field = "uuid"

    @action(detail=True, methods=["get"])
    def authorize(self, request, uuid=None):
        service_provider = self.get_object()
        redirect_uri = request.build_absolute_uri("../../") + "callback/"
        backend = GoogleAuthorize(service_provider, redirect_uri)
        url = backend.get_authorization_url(service_provider.uuid.hex)
        return Response(
            {"request_url": url},
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"])
    def callback(self, request):
        service_provider_uuid = request.query_params.get("state")
        service_provider = filter_queryset_for_user(
            marketplace_models.ServiceProvider.objects.filter(
                uuid=service_provider_uuid
            ),
            request.user,
        ).first()

        if not service_provider:
            raise exceptions.ValidationError(
                _("Service provider has not been found. Google auth has failed.")
            )

        code = request.query_params.get("code")
        if not code:
            raise exceptions.ValidationError(_("Google auth has failed."))
        redirect_uri = request.build_absolute_uri(request.path)
        backend = GoogleAuthorize(service_provider, redirect_uri)
        try:
            backend.create_tokens(code)
        except OAuth2Error:
            raise exceptions.ValidationError(
                _("Tokens have not been created. Google auth has failed.")
            )
        return Response(
            _("Google authorization is successful."), status=status.HTTP_200_OK
        )
