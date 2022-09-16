from django.db import transaction
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import decorators, permissions, status
from rest_framework.response import Response

from waldur_core.core import permissions as core_permissions
from waldur_core.core.views import ActionsViewSet

from . import filters, models, serializers, tasks, utils


class BroadcastMessageViewSet(ActionsViewSet):
    queryset = models.BroadcastMessage.objects.all().order_by('-created')
    create_serializer_class = serializers.CreateBroadcastMessageSerializer
    serializer_class = serializers.ReadBroadcastMessageSerializer
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.BroadcastMessageFilterSet

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        broadcast_message = serializer.save()
        if broadcast_message.emails:
            transaction.on_commit(
                lambda: tasks.send_broadcast_message_email.delay(broadcast_message.uuid)
            )

        headers = self.get_success_headers(serializer.data)
        return Response(
            serializers.ReadBroadcastMessageSerializer(instance=broadcast_message).data,
            status=status.HTTP_201_CREATED,
            headers=headers,
        )

    @decorators.action(detail=False, methods=['post'])
    def dry_run(self, request, *args, **kwargs):
        serializer = serializers.DryRunBroadcastMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        query = serializer.validated_data.get('query')
        matching_users = utils.get_users_for_query(query)

        return Response(
            len(matching_users),
            status=status.HTTP_200_OK,
        )
