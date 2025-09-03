from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.response import Response

from waldur_core.core.permissions import IsStaff
from waldur_core.structure.models import Project
from waldur_mastermind.marketplace_site_agent.utils import push_user_role_sync_message


class ProjectSyncUserRolesView(generics.GenericAPIView):
    """
    A view dedicated to triggering user role synchronization for a specific project.
    """

    queryset = Project.available_objects.all()
    lookup_field = "uuid"
    permission_classes = [IsStaff]

    @extend_schema(
        description="Trigger user role sync for this project. "
        "Sends a notification to RabbitMQ that this project needs user role synchronization.",
        request=None,
        responses={200: None},
    )
    def post(self, request, *args, **kwargs):
        """
        Trigger user role sync message for this project.
        """
        project = self.get_object()
        push_user_role_sync_message(project)
        return Response(status=status.HTTP_200_OK)
