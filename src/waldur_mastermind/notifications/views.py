from rest_framework import status
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import decorators, permissions, status
from rest_framework.response import Response

from waldur_core.core import permissions as core_permissions
from waldur_core.core import validators as core_validators
from waldur_core.core.views import ActionsViewSet


from . import filters, models, serializers, tasks, utils


class BroadcastMessageViewSet(ActionsViewSet):
    queryset = models.BroadcastMessage.objects.all().order_by("-created")
    serializer_class = serializers.BroadcastMessageSerializer
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.BroadcastMessageFilterSet
    update_validators = destroy_validators = [
        core_validators.StateValidator(
            models.BroadcastMessage.States.DRAFT,
            models.BroadcastMessage.States.SCHEDULED,
        )
    ]
    lookup_field = "uuid"

    @extend_schema(request=None, responses=None)
    @decorators.action(detail=True, methods=["post"])
    def send(self, request, *args, **kwargs):
        broadcast_message: models.BroadcastMessage = self.get_object()
        tasks.send_broadcast_message_email.delay(broadcast_message.uuid)
        return Response(status=status.HTTP_202_ACCEPTED)

    @extend_schema(request=None, responses=None)
    @decorators.action(detail=True, methods=["post"])
    def schedule(self, request, *args, **kwargs):
        broadcast_message: models.BroadcastMessage = self.get_object()
        broadcast_message.state = models.BroadcastMessage.States.SCHEDULED
        broadcast_message.save(update_fields=["state"])
        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.NotificationRecipientSerializer},
        request=serializers.QuerySerializer,
    )
    @decorators.action(detail=False)
    def recipients(self, request, *args, **kwargs):
        serializer = serializers.QuerySerializer(
            context=self.get_serializer_context(), data=request.query_params
        )
        serializer.is_valid(raise_exception=True)
        users = utils.get_recipients_for_query(serializer.validated_data)
        paginated_result = self.paginate_queryset(users)
        return self.get_paginated_response(paginated_result)


class MessageTemplateViewSet(ActionsViewSet):
    queryset = models.MessageTemplate.objects.all().order_by("name")
    serializer_class = serializers.MessageTemplateSerializer
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.MessageTemplateFilterSet
    lookup_field = "uuid"


class AdminAnnouncementViewSet(ActionsViewSet):
    queryset = models.AdminAnnouncement.objects.all().order_by("-created")
    serializer_class = serializers.AdminAnnouncementSerializer
    permission_classes = [core_permissions.IsSupportOrReadOnly]
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.AdminAnnouncementFilterSet
    lookup_field = "uuid"
