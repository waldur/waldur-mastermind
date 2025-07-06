import logging

import rest_framework
from django.db.models import Count
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import OpenApiExample, extend_schema, extend_schema_view
from rest_framework import (
    decorators,
    generics,
    mixins,
    permissions,
    response,
    status,
    viewsets,
)

from waldur_core.core import filters as core_filters
from waldur_core.core import models as core_models
from waldur_core.core import permissions as core_permissions
from waldur_core.core.managers import SummaryQuerySet
from waldur_core.logging import backend, filters, models, serializers, utils
from waldur_core.logging.event_logger import get_event_groups

logger = logging.getLogger(__name__)


class EventViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.Event.objects.all()
    permission_classes = (
        permissions.IsAuthenticated,
        core_permissions.IsAdminOrReadOnly,
    )
    serializer_class = serializers.EventSerializer
    filter_backends = (DjangoFilterBackend, filters.EventFilterBackend)
    filterset_class = filters.EventFilter

    @extend_schema(
        examples=[
            OpenApiExample(
                name="Valid example",
                value={"count": 12321},
                response_only=True,
            )
        ],
    )
    @decorators.action(detail=False)
    def count(self, request, *args, **kwargs):
        self.queryset = self.filter_queryset(self.get_queryset())
        return response.Response(
            {"count": self.queryset.count()}, status=status.HTTP_200_OK
        )

    @decorators.action(detail=False)
    def scope_types(self, request, *args, **kwargs):
        """Returns a list of scope types acceptable by events filter."""
        return response.Response(utils.get_scope_types_mapping().keys())

    @decorators.action(detail=False)
    def event_groups(self, request, *args, **kwargs):
        """
        Returns a list of groups with event types.
        Group is used in exclude_features query param.
        """
        return response.Response(get_event_groups())


class BaseHookViewSet(viewsets.ModelViewSet):
    """
    Hooks API allows user to receive event notifications via different channel, like email or webhook.
    """

    filter_backends = (core_filters.StaffOrUserFilter, DjangoFilterBackend)
    lookup_field = "uuid"


class WebHookViewSet(BaseHookViewSet):
    queryset = models.WebHook.objects.all()
    filterset_class = filters.WebHookFilter
    serializer_class = serializers.WebHookSerializer

    @extend_schema(
        examples=[
            OpenApiExample(
                request_only=True,
                name="webhook-create",
                value={
                    "event_types": ["customer_update_succeeded"],
                    "event_groups": ["users"],
                    "destination_url": "http://example.com/",
                },
                description="You should specify list of event_types or event_groups.",
            )
        ]
    )
    def create(self, request, *args, **kwargs):
        """
        When hook is activated, POST request is issued against destination URL with the following data:

        .. code-block:: javascript

            {
                "timestamp": "2015-07-14T12:12:56.000000",
                "message": "Customer ABC LLC has been updated.",
                "type": "customer_update_succeeded",
                "context": {
                    "user_native_name": "Walter Lebrowski",
                    "customer_contact_details": "",
                    "user_username": "Walter",
                    "user_uuid": "1c3323fc4ae44120b57ec40dea1be6e6",
                    "customer_uuid": "4633bbbb0b3a4b91bffc0e18f853de85",
                    "ip_address": "8.8.8.8",
                    "user_full_name": "Walter Lebrowski",
                    "customer_abbreviation": "ABC LLC",
                    "customer_name": "ABC LLC"
                },
                "levelname": "INFO"
            }

        Note that context depends on event type.
        """
        return super().create(request, *args, **kwargs)


@extend_schema_view(
    create=extend_schema(
        examples=[
            OpenApiExample(
                request_only=True,
                name="email-hook-create",
                value={
                    "event_types": ["openstack_instance_start_succeeded"],
                    "event_groups": ["users"],
                    "email": "test@example.com",
                },
                description="You should specify list of event_types or event_groups.",
            )
        ]
    ),
    partial_update=extend_schema(
        examples=[
            OpenApiExample(
                request_only=True,
                name="email-hook-update",
                value={"is_active": "false"},
                description="temporarily disable hook without deleting it.",
            )
        ]
    ),
)
class EmailHookViewSet(BaseHookViewSet):
    queryset = models.EmailHook.objects.all()
    filterset_class = filters.EmailHookFilter
    serializer_class = serializers.EmailHookSerializer


class HookSummary(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    Use /api/hooks/ to get a list of all the hooks of any type that a user can see.
    """

    serializer_class = serializers.SummaryHookSerializer
    filter_backends = (core_filters.StaffOrUserFilter, filters.HookSummaryFilterBackend)

    def get_queryset(self):
        return SummaryQuerySet(models.BaseHook.get_all_models())


class EventsStatsViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = models.Event.objects.all()
    filter_backends = (filters.EventFilterBackend,)

    @extend_schema(responses=serializers.EventStatsSerializer(many=True))
    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        aggregated_result = (
            queryset.values("created__year", "created__month")
            .annotate(count=Count("*"))
            .order_by("-created__year", "-created__month")
        )
        paginated_result = self.paginate_queryset(aggregated_result)
        final_result = [
            {
                "year": item["created__year"],
                "month": item["created__month"],
                "count": item["count"],
            }
            for item in paginated_result
        ]

        return self.get_paginated_response(final_result)


class EventSubscriptionViewSet(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    lookup_field = "uuid"
    queryset = models.EventSubscription.objects.all().order_by("-created")
    serializer_class = serializers.EventSubscriptionSerializer
    filterset_class = filters.EventSubscriptionFilter

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.user.is_staff or self.request.user.is_support:
            return queryset

        return queryset.filter(user=self.request.user)

    def perform_create(self, serializer):
        user = self.request.user
        source_ip = self.request.META.get("REMOTE_ADDR")
        serializer.save(user=user, source_ip=source_ip)

    def perform_destroy(self, instance):
        rmq_backend = backend.RabbitMQManagementBackend()
        if not rmq_backend.delete_rabbitmq_user(instance.uuid):
            logger.error("Failed to delete RabbitMQ user: %s", instance.uuid)
            raise rest_framework.exceptions.APIException(
                detail=f"Failed to delete RabbitMQ user: {instance.uuid}"
            )
        if models.EventSubscription.objects.filter(user=instance.user).count() > 1:
            logger.info(
                "Skipping deletion of RabbitMQ virtual host %s because user %s has other subscriptions",
                instance.user.uuid.hex,
                instance.user,
            )
        else:
            if not rmq_backend.delete_rabbitmq_virtual_host(instance.user.uuid.hex):
                logger.error(
                    "Failed to delete RabbitMQ virtual host: %s", instance.user.uuid.hex
                )
                raise rest_framework.exceptions.APIException(
                    detail=f"Failed to delete RabbitMQ virtual host: {instance.user.uuid.hex}"
                )
        super().perform_destroy(instance)


class RabbitMQVhostStats(generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]
    serializer_class = serializers.RmqVHostStatsSerializer
    filter_backends = []
    pagination_class = None

    def get(self, request, *args, **kwargs):
        rmq_backend = backend.RabbitMQManagementBackend()
        vhosts = rmq_backend.list_rabbitmq_virtual_hosts()
        output = []

        for vhost in vhosts:
            vhost_record = {"name": vhost, "waldur_user": None, "subscriptions": []}
            rmq_users = rmq_backend.list_rabbitmq_vhost_permissions(vhost)
            for rmq_user in rmq_users:
                event_subscription = models.EventSubscription.objects.filter(
                    uuid=rmq_user
                ).first()
                if event_subscription is None:
                    continue
                event_subscription_data = {
                    "created": event_subscription.created.isoformat(),
                    "uuid": rmq_user,
                    "source_ip": event_subscription.source_ip,
                }
                vhost_record["subscriptions"].append(event_subscription_data)

            waldur_user = core_models.User.objects.filter(uuid=vhost).first()
            if waldur_user is not None:
                waldur_user_data = {
                    "full_name": waldur_user.get_full_name(),
                    "username": waldur_user.username,
                    "email": waldur_user.email,
                }
                vhost_record["waldur_user"] = waldur_user_data

            output.append(vhost_record)

        return response.Response(output, status=status.HTTP_200_OK)


class RabbitMQUserStats(generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]
    serializer_class = serializers.RmqUserStatsSerializer
    filter_backends = []

    def get(self, request, *args, **kwargs):
        rmq_backend = backend.RabbitMQManagementBackend()
        users = rmq_backend.list_rabbitmq_users()
        output = []

        for user in users:
            connections = rmq_backend.get_user_connections(user)
            user_record = {"username": user, "connections": []}
            for connection in connections:
                source_ip = connection["name"].split("->")[0]
                vhost = connection["vhost"]
                user_record["connections"].append(
                    {"source_ip": source_ip, "vhost": vhost}
                )
            output.append(user_record)

        paginator = self.pagination_class()
        paginated_output = paginator.paginate_queryset(output, request)
        return paginator.get_paginated_response(paginated_output)


class EmailLogView(viewsets.ReadOnlyModelViewSet):
    queryset = models.EmailLog.objects.all()
    lookup_field = "uuid"
    permission_classes = (
        permissions.IsAuthenticated,
        core_permissions.IsSupport,
    )
    filterset_class = filters.EmailLogFilter
    serializer_class = serializers.EmailLogSerializer
