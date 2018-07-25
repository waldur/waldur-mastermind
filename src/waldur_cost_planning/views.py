from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets, decorators, response, status

from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters, permissions as structure_permissions

from . import models, serializers, filters, optimizers


class DeploymentPlanViewSet(core_views.ActionsViewSet):
    queryset = models.DeploymentPlan.objects.all()
    serializer_class = serializers.DeploymentPlanSerializer
    lookup_field = 'uuid'
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.DeploymentPlanFilter
    unsafe_methods_permissions = [structure_permissions.is_administrator]

    def retrieve(self, request, *args, **kwargs):
        """
        Example rendering of deployment plan and configuration.

        .. code-block:: javascript

            GET /api/deployment-plans/c218cbb2f56c4d52a82638ca9fffd85a/
            Accept: application/json
            Content-Type: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "url": "http://example.com/api/deployment-plans/b12bb98a661749ffb02c8a8439299288/",
                "uuid": "b12bb98a661749ffb02c8a8439299288",
                "name": "Webapp for Monster Inc",
                "customer": "http://example.com/api/customers/790b3c131e894581b3dcf66796d9fa30/",
                "items": [
                    {
                        "preset": {
                            "url": "http://example.com/api/deployment-presets/628cd853ba2a4ce7af5d4fff510b5bd2/",
                            "uuid": "628cd853ba2a4ce7af5d4fff510b5bd2",
                            "name": "MySQL",
                            "category": "Databases",
                            "variant": "Large",
                            "ram": 2048,
                            "cores": 2,
                            "storage": 1024000
                        },
                        "quantity": 1
                    }
                ]
            }
        """
        return super(DeploymentPlanViewSet, self).retrieve(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        """
        Example request for creating deployment plan.

        .. code-block:: javascript

            POST /api/deployment-plans/
            Accept: application/json
            Content-Type: application/json
            Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
            Host: example.com

            {
                "name": "WebApps",
                "customer": "http://example.com/api/customers/2f8b4e0f101545508d52c7655d6386c8/",
                "items": [
                    {
                        "preset": "http://example.com/api/deployment-presets/2debb6d109954afaa03910ba1c6791a6/",
                        "quantity": 1
                    }
                ]
            }
        """
        return super(DeploymentPlanViewSet, self).create(request, *args, **kwargs)

    create_serializer_class = serializers.DeploymentPlanCreateSerializer

    def update(self, request, *args, **kwargs):
        """
        Run **PUT** request against */api/deployment-plans/<uuid>/* to update deployment plan.
        Only name and list of items can be updated.
        List of items should have the same format as POST request.
        Only customer owner and staff can update deployment plan.
        """
        return super(DeploymentPlanViewSet, self).update(request, *args, **kwargs)

    update_serializer_class = partial_update_serializer_class = serializers.DeploymentPlanCreateSerializer

    @decorators.detail_route(methods=['GET'])
    def evaluate(self, request, *args, **kwargs):
        strategy = optimizers.SingleServiceStrategy(self.get_object())
        optimized_services = strategy.get_optimized()
        serializer = self.get_serializer(optimized_services, many=True)
        return response.Response(serializer.data, status=status.HTTP_200_OK)

    evaluate_serializer_class = serializers.OptimizedServiceSummarySerializer


class PresetViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.Preset.objects.all()
    serializer_class = serializers.PresetSerializer
    lookup_field = 'uuid'
