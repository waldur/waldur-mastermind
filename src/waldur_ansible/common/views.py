import logging

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.mixins import ListModelMixin
from rest_framework.viewsets import GenericViewSet

from waldur_core.structure import views as structure_views, filters as structure_filters

from . import filters, managers, models, serializers

logger = logging.getLogger(__name__)


def get_applications_queryset():
    return managers.ApplicationSummaryQuerySet(models.ApplicationModel.get_application_models())


def get_project_apps_count(project):
    return get_applications_queryset().filter(project=project).count()


structure_views.ProjectCountersView.register_counter('ansible', get_project_apps_count)


class ApplicationsSummaryViewSet(ListModelMixin, GenericViewSet):
    serializer_class = serializers.SummaryApplicationSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.ApplicationFilter

    def get_queryset(self):
        return get_applications_queryset()
