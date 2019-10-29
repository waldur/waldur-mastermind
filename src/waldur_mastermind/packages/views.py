from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import response, status
from rest_framework.decorators import action

from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters, permissions as structure_permissions

from .log import event_logger
from . import models, serializers, executors


class OpenStackPackageViewSet(core_views.ActionsViewSet):
    queryset = models.OpenStackPackage.objects.all()
    serializer_class = serializers.OpenStackPackageSerializer
    lookup_field = 'uuid'
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    disabled_actions = ['update', 'partial_update', 'destroy']

    def create(self, request, *args, **kwargs):
        # package creation is a little bit tricky:
        # We need to use OpenStackPackageCreateSerializer to create package
        # And OpenStackPackageSerializer to display it.
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        package = serializer.save()
        skip = (settings.WALDUR_CORE['ONLY_STAFF_MANAGES_SERVICES'] and
                serializer.validated_data['skip_connection_extnet'])
        executors.OpenStackPackageCreateExecutor.execute(package, skip_connection_extnet=skip)

        display_serializer = serializers.OpenStackPackageSerializer(instance=package, context={'request': request})
        return response.Response(display_serializer.data, status=status.HTTP_201_CREATED)

    create_serializer_class = serializers.OpenStackPackageCreateSerializer
    create_permissions = [structure_permissions.check_access_to_services_management]

    @action(detail=False, methods=['post'])
    def change(self, request, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        package = serializer.validated_data['package']
        new_template = serializer.validated_data['template']
        service_settings = package.service_settings
        tenant = package.tenant

        event_logger.openstack_package.info(
            'Tenant package change has been scheduled. '
            'Old value: %s, new value: {package_template_name}' % package.template.name,
            event_type='openstack_package_change_scheduled',
            event_context={
                'tenant': tenant,
                'package_template_name': new_template.name,
                'service_settings': service_settings,
            })

        executors.OpenStackPackageChangeExecutor.execute(tenant,
                                                         new_template=new_template,
                                                         old_package=package,
                                                         service_settings=service_settings)

        return response.Response({'detail': _('OpenStack package extend has been scheduled')},
                                 status=status.HTTP_202_ACCEPTED)

    change_serializer_class = serializers.OpenStackPackageChangeSerializer
    change_permissions = create_permissions
