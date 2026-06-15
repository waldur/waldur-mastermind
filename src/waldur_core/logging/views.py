import fnmatch
import logging

import rest_framework
from django.db import connection
from django.db.models import Count, Max
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import (
    OpenApiExample,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
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
from waldur_core.core import utils as core_utils
from waldur_core.core.serializers import StatusSerializer
from waldur_core.logging import backend, filters, models, serializers, utils
from waldur_core.logging.event_logger import get_event_groups
from waldur_core.structure.serializers_data_access import (
    GlobalUserDataAccessLogSerializer,
)

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
        responses={
            status.HTTP_200_OK: inline_serializer(
                "EventCount",
                fields={"count": rest_framework.serializers.IntegerField()},
            )
        },
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

    @extend_schema(responses={status.HTTP_200_OK: list[str]})
    @decorators.action(detail=False)
    def scope_types(self, request, *args, **kwargs):
        """Returns a list of scope types acceptable by events filter."""
        return response.Response(utils.get_scope_types_mapping().keys())

    @extend_schema(responses={status.HTTP_200_OK: dict[str, list[str]]})
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
    filter_backends = (core_filters.StaffOrUserFilter, DjangoFilterBackend)
    filterset_class = filters.BaseHookFilter

    def get_queryset(self):
        return models.BaseHook.objects.select_related("webhook", "emailhook")


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
        source_ip = core_utils.get_ip_address(self.request)
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

    @extend_schema(
        description="Create a RabbitMQ queue for receiving events for a specific offering and object type. "
        "The receiver must call this endpoint before subscribing via STOMP to ensure "
        "the queue is created with correct arguments (DLX, max-length, etc.).",
        request=serializers.EventSubscriptionQueueCreateSerializer,
        responses={
            200: serializers.EventSubscriptionQueueSerializer,
            201: serializers.EventSubscriptionQueueSerializer,
        },
    )
    @decorators.action(detail=True, methods=["post"])
    def create_queue(self, request, uuid=None):
        """
        Create a RabbitMQ queue for the event subscription.

        This endpoint pre-creates queues with the correct arguments before
        the receiver subscribes via STOMP. This prevents precondition_failed
        errors that occur when queues are created with mismatched arguments.

        Returns 200 if queue already exists, 201 if newly created.
        """
        event_subscription = self.get_object()

        # Validate the request
        input_serializer = serializers.EventSubscriptionQueueCreateSerializer(
            data=request.data,
            context={
                "request": request,
                "event_subscription": event_subscription,
            },
        )
        input_serializer.is_valid(raise_exception=True)

        offering_uuid = input_serializer.validated_data["offering_uuid"]
        object_type = input_serializer.validated_data["object_type"]

        # Check if queue already exists
        existing_queue = models.EventSubscriptionQueue.objects.filter(
            event_subscription=event_subscription,
            offering_uuid=offering_uuid,
            object_type=object_type,
        ).first()

        if existing_queue:
            logger.info(
                "Queue already exists for subscription %s, offering %s, type %s",
                event_subscription.uuid.hex,
                offering_uuid.hex,
                object_type,
            )
            # Ensure queue exists in RabbitMQ (idempotent)
            rmq_backend = backend.RabbitMQManagementBackend()
            rmq_backend.create_queue(
                vhost=existing_queue.vhost,
                queue_name=existing_queue.queue_name,
                durable=True,
                auto_delete=False,
                arguments=backend.SUBSCRIPTION_QUEUE_ARGUMENTS,
            )
            output_serializer = serializers.EventSubscriptionQueueSerializer(
                existing_queue, context={"request": request}
            )
            return response.Response(output_serializer.data, status=status.HTTP_200_OK)

        # Create new queue
        queue = input_serializer.save()
        output_serializer = serializers.EventSubscriptionQueueSerializer(
            queue, context={"request": request}
        )
        return response.Response(output_serializer.data, status=status.HTTP_201_CREATED)


class EventSubscriptionQueueViewSet(
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    lookup_field = "uuid"
    queryset = models.EventSubscriptionQueue.objects.all().order_by("-created")
    serializer_class = serializers.EventSubscriptionQueueSerializer
    filterset_class = filters.EventSubscriptionQueueFilter

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.user.is_staff or self.request.user.is_support:
            return queryset

        return queryset.filter(event_subscription__user=self.request.user)


class RabbitMQVhostStats(generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]
    serializer_class = serializers.RmqVHostStatsSerializer
    filter_backends = []
    pagination_class = None

    def get_queryset(self):
        # This view doesn't use a queryset, but we need this method
        # for the browsable API to work
        return models.EventSubscription.objects.none()

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
    """
    API endpoint for viewing RabbitMQ user connection statistics.

    Returns detailed connection information for each RabbitMQ user including
    traffic statistics, client properties, and connection state.
    """

    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]
    serializer_class = serializers.RmqEnrichedUserStatsSerializer
    filter_backends = []

    def get_queryset(self):
        # This view doesn't use a queryset, but we need this method
        # for the browsable API to work
        return models.EventSubscription.objects.none()

    @extend_schema(
        summary="Get RabbitMQ user connection statistics",
        description="""Returns enriched connection data for all RabbitMQ users.

For each user (which corresponds to an EventSubscription), provides:
- Connection state (running, blocked, blocking)
- Traffic statistics (bytes sent/received)
- Connection timestamp
- Client properties (product, version, platform)
- Channel count and heartbeat timeout

Requires support user permissions.""",
        responses={
            status.HTTP_200_OK: serializers.RmqEnrichedUserStatsItemSerializer(
                many=True
            ),
        },
    )
    def get(self, request, *args, **kwargs):
        rmq_backend = backend.RabbitMQManagementBackend()
        users = rmq_backend.list_rabbitmq_users()
        output = []

        for user in users:
            try:
                connections = rmq_backend.get_user_connections_enriched(user)
            except Exception:
                logger.warning(
                    "Failed to get connections for RMQ user %s, skipping", user
                )
                connections = []

            user_record = {"username": user, "connections": connections}
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


class SystemLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Staff and support endpoint to view system logs from API, Worker, and Beat processes.

    Supports multi-pod Kubernetes deployments - each pod identified by `instance`.

    Filters:
    - source: 'api', 'worker', or 'beat'
    - instance: pod name (K8s) or container name (Docker)
    - level: 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
    - level_gte: minimum level number (20=INFO, 30=WARNING, 40=ERROR, 50=CRITICAL)
    - created_from/created_to: timestamp range
    - logger_name: partial match
    - message: partial match
    """

    queryset = models.SystemLog.objects.all()
    permission_classes = (
        permissions.IsAuthenticated,
        core_permissions.IsSupport,
    )
    serializer_class = serializers.SystemLogSerializer
    filterset_class = filters.SystemLogFilter

    @extend_schema(
        summary="Get system log statistics",
        responses={200: serializers.SystemLogStatsResponseSerializer},
    )
    @decorators.action(detail=False)
    def stats(self, request, *args, **kwargs):
        """Return log count statistics per source and instance, plus total table size."""
        # Row counts per source/instance — uses composite index, no heap scan
        rows = (
            models.SystemLog.objects.values("source", "instance")
            .annotate(count=Count("id"))
            .order_by("source", "instance")
        )

        # Total table size from pg catalog — O(1), no per-row computation
        table_name = models.SystemLog._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_total_relation_size(%s)", [table_name])
            total_size_bytes = cursor.fetchone()[0]

        stats = [
            {
                "source": row["source"],
                "instance": row["instance"],
                "count": row["count"],
            }
            for row in rows
        ]
        return response.Response(
            {
                "instances": stats,
                "total_size_bytes": total_size_bytes,
                "total_size_mb": round(total_size_bytes / (1024 * 1024), 2),
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="List system log instances",
        responses={200: serializers.SystemLogInstanceSerializer(many=True)},
    )
    @decorators.action(detail=False)
    def instances(self, request, *args, **kwargs):
        """List all known instances (pods/containers) with their last activity."""
        instances = list(
            models.SystemLog.objects.values("source", "instance")
            .annotate(last_seen=Max("created"), count=Count("id"))
            .order_by("source", "instance")
        )
        paginated = self.paginate_queryset(instances)
        if paginated is not None:
            return self.get_paginated_response(paginated)
        return response.Response(instances, status=status.HTTP_200_OK)


class RabbitMQStatsViewSet(generics.GenericAPIView):
    """
    API endpoint for viewing and managing RabbitMQ subscription queues.

    GET: Lists all subscription queues across vhosts with message counts.
         Requires support user permissions.

    POST: Purges or deletes messages from specified queues.
          Requires staff permissions.
    """

    filter_backends = []
    pagination_class = None

    def get_permissions(self):
        if self.request.method == "POST":
            return [permissions.IsAuthenticated(), core_permissions.IsStaff()]
        return [permissions.IsAuthenticated(), core_permissions.IsSupport()]

    def get_queryset(self):
        return models.EventSubscription.objects.none()

    @extend_schema(
        summary="Get RabbitMQ subscription queue statistics",
        description="""Provides statistics about RabbitMQ subscription queues.

Returns information about all vhosts with their subscription queues, including:
- Queue names and message counts
- Waldur user and subscription information linked to each vhost
- Total message counts per vhost and across all vhosts

Requires support user permissions.""",
        responses={
            status.HTTP_200_OK: serializers.RmqStatsResponseSerializer,
            status.HTTP_503_SERVICE_UNAVAILABLE: serializers.RmqStatsErrorSerializer,
        },
    )
    def get(self, request, *args, **kwargs):
        rmq_backend = backend.RabbitMQManagementBackend()

        try:
            vhost_stats = rmq_backend.list_all_subscription_queues()
        except Exception as e:
            logger.exception("Failed to get RabbitMQ stats: %s", e)
            return response.Response(
                {"error": "Failed to retrieve RabbitMQ statistics"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Enrich with Waldur user information
        output = {"vhosts": [], "total_messages": 0, "total_queues": 0}

        for vhost_data in vhost_stats:
            vhost_name = vhost_data["vhost"]

            # Look up Waldur user by vhost name (which is user.uuid.hex)
            waldur_user = core_models.User.objects.filter(uuid=vhost_name).first()
            waldur_user_data = None
            if waldur_user:
                waldur_user_data = {
                    "uuid": str(waldur_user.uuid),
                    "username": waldur_user.username,
                    "full_name": waldur_user.get_full_name(),
                }

            # Enrich queue data with parsed information
            enriched_queues = []
            for queue in vhost_data["queues"]:
                parsed = utils.parse_subscription_queue_name(queue["name"])
                enriched_queue = {
                    **queue,
                    "subscription_uuid": parsed["subscription_uuid"]
                    if parsed
                    else None,
                    "offering_uuid": parsed["offering_uuid"] if parsed else None,
                    "object_type": parsed["object_type"] if parsed else None,
                }
                enriched_queues.append(enriched_queue)

            vhost_record = {
                "name": vhost_name,
                "user": waldur_user_data,
                "queues": enriched_queues,
                "total_messages": vhost_data["total_messages"],
            }
            output["vhosts"].append(vhost_record)
            output["total_messages"] += vhost_data["total_messages"]
            output["total_queues"] += len(enriched_queues)

        return response.Response(output, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Purge or delete RabbitMQ subscription queues",
        description="""Purges messages from or deletes specified RabbitMQ subscription queues.

**Purge operations** (remove messages, keep queue):
- `vhost` and `queue_name`: Purge a specific queue
- `vhost` and `queue_pattern`: Purge queues matching pattern (e.g., '*_resource')
- `purge_all_subscription_queues`: Purge all subscription queues across all vhosts

**Delete operations** (remove queue entirely):
- `vhost`, `queue_name`, and `delete_queue=true`: Delete a specific queue
- `vhost`, `queue_pattern`, and `delete_queue=true`: Delete queues matching pattern
- `delete_all_subscription_queues`: Delete all subscription queues across all vhosts

Requires staff permissions (more restrictive than viewing).""",
        request=serializers.RmqPurgeRequestSerializer,
        responses={
            status.HTTP_200_OK: serializers.RmqPurgeResponseSerializer,
            status.HTTP_400_BAD_REQUEST: serializers.RmqStatsErrorSerializer,
            status.HTTP_404_NOT_FOUND: serializers.RmqStatsErrorSerializer,
            status.HTTP_503_SERVICE_UNAVAILABLE: serializers.RmqStatsErrorSerializer,
        },
    )
    def post(self, request, *args, **kwargs):
        vhost = request.data.get("vhost")
        queue_name = request.data.get("queue_name")
        queue_pattern = request.data.get("queue_pattern")
        purge_all = request.data.get("purge_all_subscription_queues", False)
        delete_queue_flag = request.data.get("delete_queue", False)
        delete_all = request.data.get("delete_all_subscription_queues", False)

        rmq_backend = backend.RabbitMQManagementBackend()
        purged_queues = 0
        purged_messages = 0
        deleted_queues = 0

        try:
            if delete_all:
                # Delete all subscription queues across all vhosts
                vhost_stats = rmq_backend.list_all_subscription_queues()
                for vhost_data in vhost_stats:
                    for queue in vhost_data["queues"]:
                        if rmq_backend.delete_queue(vhost_data["vhost"], queue["name"]):
                            deleted_queues += 1

            elif purge_all:
                # Purge all subscription queues across all vhosts
                vhost_stats = rmq_backend.list_all_subscription_queues()
                for vhost_data in vhost_stats:
                    for queue in vhost_data["queues"]:
                        msg_count = queue.get("messages", 0)
                        rmq_backend.purge_queue(vhost_data["vhost"], queue["name"])
                        purged_queues += 1
                        purged_messages += msg_count

            elif vhost and queue_name:
                # Handle specific queue
                queues = rmq_backend.list_queues(vhost)
                queue_info = next((q for q in queues if q["name"] == queue_name), None)
                if queue_info:
                    if delete_queue_flag:
                        if rmq_backend.delete_queue(vhost, queue_name):
                            deleted_queues = 1
                    else:
                        msg_count = queue_info.get("messages", 0)
                        rmq_backend.purge_queue(vhost, queue_name)
                        purged_queues = 1
                        purged_messages = msg_count
                else:
                    return response.Response(
                        {"error": f"Queue {queue_name} not found in vhost {vhost}"},
                        status=status.HTTP_404_NOT_FOUND,
                    )

            elif vhost and queue_pattern:
                # Handle queues matching pattern in specific vhost
                queues = rmq_backend.list_queues(vhost)
                for queue in queues:
                    if fnmatch.fnmatch(queue["name"], queue_pattern):
                        if delete_queue_flag:
                            if rmq_backend.delete_queue(vhost, queue["name"]):
                                deleted_queues += 1
                        else:
                            msg_count = queue.get("messages", 0)
                            rmq_backend.purge_queue(vhost, queue["name"])
                            purged_queues += 1
                            purged_messages += msg_count

            else:
                return response.Response(
                    {
                        "error": "Must specify either: (vhost + queue_name), (vhost + queue_pattern), "
                        "purge_all_subscription_queues, or delete_all_subscription_queues"
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        except Exception as e:
            logger.exception("Failed to purge/delete RabbitMQ queues: %s", e)
            return response.Response(
                {"error": f"Failed to purge/delete queues: {str(e)}"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        output_serializer = serializers.RmqPurgeResponseSerializer(
            instance={
                "purged_queues": purged_queues,
                "purged_messages": purged_messages,
                "deleted_queues": deleted_queues,
            }
        )
        return response.Response(output_serializer.data, status=status.HTTP_200_OK)


class RabbitMQOverviewStats(generics.GenericAPIView):
    """
    API endpoint for viewing RabbitMQ cluster overview statistics.

    Provides global cluster health metrics including message throughput,
    queue totals, and object counts.
    """

    permission_classes = [permissions.IsAuthenticated, core_permissions.IsSupport]
    serializer_class = serializers.RmqOverviewSerializer
    filter_backends = []
    pagination_class = None

    def get_queryset(self):
        return models.EventSubscription.objects.none()

    @extend_schema(
        summary="Get RabbitMQ cluster overview statistics",
        description="""Returns global RabbitMQ cluster health and performance metrics.

Includes:
- **Cluster info**: Name, RabbitMQ version, Erlang version
- **Message stats**: Publish/deliver/confirm/ack counts and rates (per second)
- **Queue totals**: Total messages, ready messages, unacknowledged messages
- **Object totals**: Connection, channel, exchange, queue, and consumer counts
- **Listeners**: Active protocol listeners (AMQP, HTTP, etc.)

Requires support user permissions.""",
        responses={
            status.HTTP_200_OK: serializers.RmqOverviewSerializer,
            status.HTTP_503_SERVICE_UNAVAILABLE: serializers.RmqStatsErrorSerializer,
        },
    )
    def get(self, request, *args, **kwargs):
        rmq_backend = backend.RabbitMQManagementBackend()

        try:
            overview = rmq_backend.get_overview()
        except Exception as e:
            logger.exception("Failed to get RabbitMQ overview: %s", e)
            return response.Response(
                {"error": "Failed to retrieve RabbitMQ overview statistics"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        output_serializer = serializers.RmqOverviewSerializer(instance=overview)
        return response.Response(output_serializer.data, status=status.HTTP_200_OK)


class PubsubDebugViewSet(viewsets.ViewSet):
    """
    Staff-only API for monitoring and debugging the pubsub system.

    Provides visibility into:
    - Circuit breaker state and history
    - Message publishing metrics
    - Connection pool status (if available)
    - Message state tracker (idempotency cache)
    """

    permission_classes = [permissions.IsAuthenticated, core_permissions.IsStaff]
    serializer_class = (
        serializers.PubsubOverviewSerializer
    )  # Default for OpenAPI schema

    @extend_schema(
        summary="Get pubsub system health overview",
        description="""Dashboard overview of pubsub system health.

Combines circuit breaker state, publishing metrics, and health indicators
into a single response suitable for monitoring dashboards.

Requires staff permissions.""",
        responses={
            status.HTTP_200_OK: serializers.PubsubOverviewSerializer,
        },
    )
    @decorators.action(detail=False, methods=["get"])
    def overview(self, request):
        """Get comprehensive pubsub system health overview."""
        from django.utils import timezone

        from waldur_core.logging.circuit_breaker import stomp_circuit_breaker
        from waldur_core.logging.utils import PublishingMetrics

        # Circuit breaker status
        cb_state = stomp_circuit_breaker.get_state()
        cb_healthy = cb_state == "closed"

        # Metrics summary
        metrics = PublishingMetrics.get_metrics()

        # Calculate health score
        total_attempts = metrics["messages_sent"] + metrics["messages_failed"]
        failure_rate = 0.0
        if total_attempts > 0:
            failure_rate = metrics["messages_failed"] / total_attempts

        # Determine overall health
        health_status = "healthy"
        issues = []

        if not cb_healthy:
            health_status = "degraded"
            issues.append(f"Circuit breaker is {cb_state}")

        if failure_rate > 0.1:  # More than 10% failures
            health_status = "degraded"
            issues.append(f"High failure rate: {failure_rate:.1%}")

        if failure_rate > 0.5:  # More than 50% failures
            health_status = "critical"

        return response.Response(
            {
                "health_status": health_status,
                "issues": issues,
                "circuit_breaker": {
                    "state": cb_state,
                    "healthy": cb_healthy,
                    "failure_count": stomp_circuit_breaker._failure_count,
                },
                "metrics": {
                    "messages_sent": metrics["messages_sent"],
                    "messages_failed": metrics["messages_failed"],
                    "failure_rate": f"{failure_rate:.1%}",
                    "avg_latency_ms": metrics.get("avg_publish_time_ms", 0),
                },
                "last_updated": timezone.now().isoformat(),
            }
        )

    @extend_schema(
        summary="Get circuit breaker state",
        description="""Get current STOMP circuit breaker state and statistics.

Returns:
- state: Current state (closed/open/half_open)
- failure_count: Number of consecutive failures
- success_count: Successes since last state change
- last_failure_time: Timestamp of last failure
- last_state_change: When state last changed
- config: Circuit breaker configuration
- state_history: Recent state transitions

Requires staff permissions.""",
        responses={
            status.HTTP_200_OK: serializers.CircuitBreakerStatusSerializer,
        },
    )
    @decorators.action(detail=False, methods=["get"])
    def circuit_breaker(self, request):
        """Get circuit breaker state and statistics."""
        from waldur_core.logging.circuit_breaker import stomp_circuit_breaker

        return response.Response(stomp_circuit_breaker.get_status())

    @extend_schema(
        request=None,
        summary="Reset circuit breaker",
        description="""Manually reset the STOMP circuit breaker to CLOSED state.

Use with caution - only when RabbitMQ is confirmed healthy.

Requires staff permissions.""",
        responses={
            status.HTTP_200_OK: serializers.CircuitBreakerResetSerializer,
        },
    )
    @decorators.action(detail=False, methods=["post"])
    def circuit_breaker_reset(self, request):
        """Manually reset circuit breaker to CLOSED state."""
        from waldur_core.logging.circuit_breaker import stomp_circuit_breaker

        stomp_circuit_breaker.reset()
        return response.Response({"status": "reset", "state": "closed"})

    @extend_schema(
        summary="Get publishing metrics",
        description="""Get message publishing metrics and statistics.

Returns:
- messages_sent: Total messages successfully sent
- messages_failed: Total failed message attempts
- messages_retried: Messages that required retry
- messages_skipped: Messages skipped due to circuit breaker
- circuit_breaker_trips: Number of times circuit opened
- rate_limiter_rejections: Messages rejected by rate limiter
- avg_publish_time_ms: Average publish latency
- last_publish_time: Timestamp of last publish attempt

Requires staff permissions.""",
        responses={
            status.HTTP_200_OK: serializers.PublishingMetricsSerializer,
        },
    )
    @decorators.action(detail=False, methods=["get"])
    def metrics(self, request):
        """Get publishing metrics and statistics."""
        from waldur_core.logging.utils import PublishingMetrics

        return response.Response(PublishingMetrics.get_metrics())

    @extend_schema(
        request=None,
        summary="Reset publishing metrics",
        description="""Reset all publishing metrics counters to zero.

Requires staff permissions.""",
        responses={
            status.HTTP_200_OK: StatusSerializer,
        },
    )
    @decorators.action(detail=False, methods=["post"])
    def metrics_reset(self, request):
        """Reset all metrics counters to zero."""
        from waldur_core.logging.utils import PublishingMetrics

        PublishingMetrics.reset()
        return response.Response({"status": "reset"})

    @extend_schema(
        summary="Get message state cache statistics",
        description="""Get message state tracker cache statistics for idempotency.

The message state tracker prevents duplicate message sends by caching
the hash of message content. This endpoint provides cache statistics.

Query params:
- resource_uuid: Filter by specific resource
- message_type: Filter by message type

Requires staff permissions.""",
        responses={
            status.HTTP_200_OK: serializers.MessageStateCacheSerializer,
        },
    )
    @decorators.action(detail=False, methods=["get"])
    def message_state_cache(self, request):
        """Get message state cache statistics."""
        from waldur_core.logging.utils import MessageStateTracker

        resource_uuid = request.query_params.get("resource_uuid")
        message_type = request.query_params.get("message_type")

        stats = MessageStateTracker.get_cache_stats(
            resource_uuid=resource_uuid, message_type=message_type
        )
        return response.Response(stats)

    @extend_schema(
        summary="Get subscription queues overview",
        description="""Get overview of subscription queues from RabbitMQ.

Returns summary of subscription queues across all vhosts including
message counts and queue statistics.

Note: For detailed queue management, use /api/rabbitmq-stats/ endpoint.

Requires staff permissions.""",
        responses={
            status.HTTP_200_OK: serializers.EventSubscriptionQueuesOverviewSerializer,
            status.HTTP_503_SERVICE_UNAVAILABLE: serializers.RmqStatsErrorSerializer,
        },
    )
    @decorators.action(detail=False, methods=["get"])
    def queues(self, request):
        """Get subscription queues overview."""
        rmq_backend = backend.RabbitMQManagementBackend()

        try:
            vhost_stats = rmq_backend.list_all_subscription_queues()
        except Exception as e:
            logger.exception("Failed to get subscription queues: %s", e)
            return response.Response(
                {"error": "Failed to retrieve subscription queue statistics"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Calculate totals
        total_queues = sum(len(v["queues"]) for v in vhost_stats)
        total_messages = sum(v["total_messages"] for v in vhost_stats)

        # Get top queues by message count
        all_queues = []
        for vhost_data in vhost_stats:
            for queue in vhost_data["queues"]:
                all_queues.append(
                    {
                        "vhost": vhost_data["vhost"],
                        "name": queue["name"],
                        "messages": queue.get("messages", 0),
                        "consumers": queue.get("consumers", 0),
                    }
                )
        top_queues = sorted(all_queues, key=lambda x: x["messages"], reverse=True)[:10]

        return response.Response(
            {
                "total_vhosts": len(vhost_stats),
                "total_queues": total_queues,
                "total_messages": total_messages,
                "top_queues_by_messages": top_queues,
            }
        )

    @extend_schema(
        summary="Get dead letter queue status",
        description="""Get dead letter queue (DLQ) statistics.

The DLQ receives messages that failed to be delivered to their original
destination. This endpoint shows the current DLQ status.

Note: DLQ is configured per-vhost. This endpoint checks all vhosts
for queues with 'dlq' in the name.

Requires staff permissions.""",
        responses={
            status.HTTP_200_OK: serializers.DeadLetterQueueSerializer,
            status.HTTP_503_SERVICE_UNAVAILABLE: serializers.RmqStatsErrorSerializer,
        },
    )
    @decorators.action(detail=False, methods=["get"])
    def dead_letter_queue(self, request):
        """Get dead letter queue statistics."""
        rmq_backend = backend.RabbitMQManagementBackend()

        try:
            vhosts = rmq_backend.list_rabbitmq_virtual_hosts()
        except Exception as e:
            logger.exception("Failed to list vhosts: %s", e)
            return response.Response(
                {"error": "Failed to retrieve vhost list"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        dlq_stats = []
        total_dlq_messages = 0

        for vhost in vhosts:
            try:
                queues = rmq_backend.list_queues(vhost)
                # Find DLQ queues
                for queue in queues:
                    if "dlq" in queue["name"].lower():
                        msg_count = queue.get("messages", 0)
                        total_dlq_messages += msg_count
                        dlq_stats.append(
                            {
                                "vhost": vhost,
                                "queue_name": queue["name"],
                                "messages": msg_count,
                                "messages_ready": queue.get("messages_ready", 0),
                                "consumers": queue.get("consumers", 0),
                            }
                        )
            except Exception as e:
                logger.warning("Failed to list queues for vhost %s: %s", vhost, e)
                continue

        return response.Response(
            {
                "total_dlq_messages": total_dlq_messages,
                "dlq_count": len(dlq_stats),
                "dlq_queues": dlq_stats,
                "note": "DLQ queues contain messages that failed delivery",
            }
        )


class UserDataAccessLogViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """
    Global endpoint for viewing and managing user data access logs.

    Staff and support users can view logs.
    Only staff users can delete log entries.
    """

    queryset = models.UserDataAccessLog.objects.select_related(
        "target_user", "accessor"
    ).order_by("-timestamp")
    lookup_field = "uuid"
    serializer_class = GlobalUserDataAccessLogSerializer
    permission_classes = (
        permissions.IsAuthenticated,
        core_permissions.IsSupport,
    )
    filterset_class = filters.UserDataAccessLogFilter

    def get_permissions(self):
        # Only staff can delete logs
        if self.action == "destroy":
            return [permissions.IsAuthenticated(), core_permissions.IsStaff()]
        return super().get_permissions()
