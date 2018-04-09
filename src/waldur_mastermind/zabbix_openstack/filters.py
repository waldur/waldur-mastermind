from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import models as structure_models
from waldur_zabbix.apps import ZabbixConfig


class LinkFilterBackend(DjangoFilterBackend):
    """
    This filter allows to filter Zabbix service project link by URL of virtual machine.
    Consider for example the following use case.

    There're two OpenStack virtual machines in the Waldur project.
    Zabbix server is installed on the first VM.
    Zabbix agent is to be installed on the second VM.
    Note, that both of them share the same OpenStack tenant.
    Therefore, they should be able to communicate directly, ie without proxy or virtual router.

    There's service settings for Zabbix provider in Waldur database.
    It is configured with scope field equal to the Zabbix server VM.
    Also, there are Zabbix service and Zabbix service project link configured for the project.

    By supplying URL of the OpenStack service project link to this filter backend,
    we should be able to get list of all Zabbix service project links
    which could be used as Zabbix monitoring in the same OpenStack tenant.
    """

    def filter_queryset(self, request, queryset, view):
        resource_url = request.query_params.get('resource')
        if resource_url:
            try:
                resource = self.get_resource_by_url(request, resource_url)
            except serializers.ValidationError:
                return queryset.none()

            link = resource.service_project_link
            siblings = resource._meta.model.objects.filter(
                service_project_link=link
            ).exclude(uuid=resource.uuid)
            if siblings.count() == 0:
                return queryset.none()

            service_settings = structure_models.ServiceSettings.objects.filter(
                type=ZabbixConfig.service_name,
                scope__in=siblings,
            )
            queryset = queryset.filter(project=link.project, service__settings=service_settings)
        return queryset

    def get_resource_by_url(self, request, resource_url):
        related_models = structure_models.VirtualMachine.get_all_models()
        field = core_serializers.GenericRelatedField(related_models=related_models)
        # Trick to set field context without serializer
        field._context = {'request': request}
        return field.to_internal_value(resource_url)
