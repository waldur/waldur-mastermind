from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import decorators, permissions, status
from rest_framework.response import Response

from waldur_core.core import permissions as core_permissions
from waldur_core.core import validators as core_validators
from waldur_core.core.views import ActionsViewSet, ReadOnlyActionsViewSet

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

    @decorators.action(detail=False, methods=['post'])
    def users(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    users_serializer_class = serializers.UsersBroadcastMessageSerializer

    @decorators.action(detail=False)
    def recipients(self, request, *args, **kwargs):
        serializer = serializers.QuerySerializer(
            context=self.get_serializer_context(), data=request.query_params
        )
        serializer.is_valid(raise_exception=True)
        users = utils.get_recipients_for_query(serializer.validated_data)
        return Response(
            users,
            status=status.HTTP_200_OK,
        )


class MessageTemplateViewSet(ReadOnlyActionsViewSet):
    queryset = models.MessageTemplate.objects.all().order_by('name')
    serializer_class = serializers.MessageTemplateSerializer
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]
    filter_backends = [DjangoFilterBackend]
    filterset_class = filters.MessageTemplateFilterSet
    lookup_field = 'uuid'
