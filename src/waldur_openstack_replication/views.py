from django.db import transaction
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import exceptions, status
from rest_framework.response import Response

from waldur_core.core import access
from waldur_core.core.permissions import has_staff_access
from waldur_core.core.views import ActionsViewSet
from waldur_core.structure.filters import GenericRoleFilter

from . import executors, filters, models, serializers


@access.describes(
    access.any_of(access.staff(), access.owned("The migration's creator.")),
    needs_object=True,
)
def is_creator_or_staff(request, view, obj=None):
    if obj is None or has_staff_access(request):
        return
    if obj.created_by != request.user:
        raise exceptions.PermissionDenied(
            _("Only the migration's creator or staff may delete it.")
        )


class MigrationViewSet(ActionsViewSet):
    queryset = models.Migration.objects.all().order_by("created")
    serializer_class = serializers.MigrationDetailsSerializer
    create_serializer_class = serializers.MigrationCreateSerializer
    filterset_class = filters.MigrationFilterSet
    filter_backends = [GenericRoleFilter, DjangoFilterBackend]
    lookup_field = "uuid"
    # Its only writable field, mappings, is read once, when the migration
    # starts; no client changes a migration.
    disabled_actions = ["update", "partial_update"]
    destroy_permissions = [is_creator_or_staff]
    # Checked by MigrationCreateSerializer.validate.
    access_declarations = {"create": [serializers.MIGRATES_RESOURCE]}

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        migration: models.Migration = serializer.save()

        transaction.on_commit(lambda: executors.MigrationExecutor.execute(migration))

        return Response(
            serializers.MigrationDetailsSerializer(instance=serializer.instance).data,
            status=status.HTTP_201_CREATED,
        )
