from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import decorators, permissions, status
from rest_framework.response import Response

from waldur_core.core import permissions as core_permissions
from waldur_core.core import validators as core_validators
from waldur_core.core.views import ActionsViewSet

from . import filters, models, serializers, tasks, utils


class BroadcastMessageViewSet(ActionsViewSet):
    queryset = models.BroadcastMessage.objects.all().order_by('-created')
    serializer_class = serializers.BroadcastMessageSerializer
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.BroadcastMessageFilterSet
    update_validators = [
        core_validators.StateValidator(
            models.BroadcastMessage.States.DRAFT,
            models.BroadcastMessage.States.SCHEDULED,
        )
    ]
    lookup_field = 'uuid'

    @decorators.action(detail=True, methods=['post'])
    def send(self, request, *args, **kwargs):
        broadcast_message = self.get_object()
        tasks.send_broadcast_message_email.delay(broadcast_message.uuid)
        return Response(status=status.HTTP_202_ACCEPTED)

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
    queryset = models.MessageTemplate.objects.all().order_by('name')
    serializer_class = serializers.MessageTemplateSerializer
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.MessageTemplateFilterSet
    lookup_field = 'uuid'
