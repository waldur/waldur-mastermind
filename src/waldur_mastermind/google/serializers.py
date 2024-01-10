from rest_framework import serializers

from waldur_mastermind.google import models as google_models
from waldur_mastermind.marketplace import serializers as marketplace_serializers


class GoogleCredentialsSerializer(marketplace_serializers.ServiceProviderSerializer):
    calendar_token = serializers.CharField(
        source="googlecredentials.calendar_token", read_only=True
    )
    calendar_refresh_token = serializers.CharField(
        source="googlecredentials.calendar_refresh_token", read_only=True
    )
    google_auth_url = serializers.SerializerMethodField()

    class Meta(marketplace_serializers.ServiceProviderSerializer.Meta):
        fields = marketplace_serializers.ServiceProviderSerializer.Meta.fields + (
            "calendar_token",
            "calendar_refresh_token",
            "google_auth_url",
        )
        view_name = "google-auth-detail"

    def get_google_auth_url(self, service_provider):
        from .backend import GoogleAuthorize

        request = self.context["request"]
        redirect_uri = request.build_absolute_uri("../../") + "callback/"
        backend = GoogleAuthorize(service_provider, redirect_uri)
        return backend.get_authorization_url(service_provider.uuid.hex)


class GoogleCalendarSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = google_models.GoogleCalendar
        fields = ("backend_id", "public", "http_link")
