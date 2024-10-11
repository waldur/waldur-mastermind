from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_core.core.validators import StateValidator
from waldur_core.core.views import ActionsViewSet
from waldur_core.structure.filters import GenericRoleFilter

from . import executors, filters, models, serializers


class MigrationViewSet(ActionsViewSet):
    queryset = models.Migration.objects.all().order_by("created")
    serializer_class = serializers.MigrationDetailsSerializer
    create_serializer_class = serializers.MigrationCreateSerializer
    filterset_class = filters.MigrationFilterSet
    filter_backends = [GenericRoleFilter, DjangoFilterBackend]
    lookup_field = "uuid"

    @action(detail=True, methods=["post"])
    def run(self, request, uuid=None):
        migration = self.get_object()
        executors.MigrationExecutor.execute(migration)
        return Response(status=status.HTTP_200_OK)

    run_validators = [StateValidator(models.Migration.States.CREATION_SCHEDULED)]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response(
            serializers.MigrationDetailsSerializer(instance=serializer.instance).data,
            status=status.HTTP_201_CREATED,
        )
