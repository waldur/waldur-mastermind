from rest_framework import serializers

from waldur_mastermind.google import models as google_models


class GoogleCredentialsSerializer(serializers.ModelSerializer):
    class Meta:
        model = google_models.GoogleCredentials
        exclude = ('id', 'service_provider')
        read_only_fields = ('calendar_token', 'calendar_refresh_token')


class GoogleCalendarSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = google_models.GoogleCalendar
        fields = ('backend_id', 'public', 'http_link')
