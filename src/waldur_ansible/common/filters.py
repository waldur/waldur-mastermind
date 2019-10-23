import django_filters
from django.contrib import auth

from . import models

User = auth.get_user_model()


class ApplicationFilter(django_filters.FilterSet):
    project = django_filters.UUIDFilter(field_name='project__uuid')
    project_uuid = django_filters.UUIDFilter(field_name='project__uuid')
    project_name = django_filters.CharFilter(field_name='project__name', lookup_expr='icontains')

    class Meta:
        model = models.ApplicationModel
        fields = []
