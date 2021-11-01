from django.db import transaction
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import decorators, permissions, status
from rest_framework.response import Response

from waldur_core.core import permissions as core_permissions
from waldur_core.core.views import ActionsViewSet

from . import filters, models, serializers, tasks, utils


class NotificationViewSet(ActionsViewSet):
    queryset = models.Notification.objects.all().order_by('-created')
    create_serializer_class = serializers.CreateNotificationSerializer
    serializer_class = serializers.ReadNotificationSerializer
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.NotificationFilterSet

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        notification = serializer.save()
        if notification.emails:
            transaction.on_commit(
                lambda: tasks.send_notification_email.delay(notification.uuid)
            )

        headers = self.get_success_headers(serializer.data)
        return Response(
            serializers.ReadNotificationSerializer(instance=notification).data,
            status=status.HTTP_201_CREATED,
            headers=headers,
        )

    @decorators.action(detail=False, methods=['post'])
    def dry_run(self, request, *args, **kwargs):
        serializer = serializers.DryRunNotificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        query = serializer.validated_data.get('query')
        matching_users = utils.get_users_for_query(query)

        return Response(len(matching_users), status=status.HTTP_200_OK,)
