import fnmatch
import logging

import rest_framework
from django.db import connection, transaction
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
from rest_framework.throttling import ScopedRateThrottle

from waldur_core.core import email_diagnostics
from waldur_core.core import filters as core_filters
from waldur_core.core import models as core_models
from waldur_core.core import permissions as core_permissions
from waldur_core.core import utils as core_utils
from waldur_core.core.serializers import StatusSerializer
from waldur_core.logging import backend, enums, filters, models, serializers, utils
from waldur_core.logging.availability import get_available_event_groups
from waldur_core.structure.serializers_data_access import (
    GlobalUserDataAccessLogSerializer,
)

logger = logging.getLogger(__name__)

# Legacy pub/sub surface — see "The legacy path" in docs/design/pubsub-architecture.md.
_LEGACY_SUBSCRIPTION_DEPRECATION = "DEPRECATED: superseded by the unified EventConsumer path (POST /api/event-consumers/register/). Removal tracked in WAL-10111."


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

        Narrowed to the groups this deployment can emit. Groups left out stay
        deliverable and writable -- see waldur_core.logging.availability.
        """
        return response.Response(get_available_event_groups())


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

    # Declared so the model stays introspectable; get_queryset() narrows it.
    queryset = models.BaseHook.objects.all()
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


@extend_schema_view(
    create=extend_schema(deprecated=True, description=_LEGACY_SUBSCRIPTION_DEPRECATION),
    retrieve=extend_schema(deprecated=True),
    list=extend_schema(deprecated=True),
    destroy=extend_schema(deprecated=True),
)
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


@extend_schema_view(
    retrieve=extend_schema(
        deprecated=True, description=_LEGACY_SUBSCRIPTION_DEPRECATION
    ),
    list=extend_schema(deprecated=True),
    destroy=extend_schema(deprecated=True),
)
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
                consumer_uuid = utils.parse_consumer_queue_name(queue["name"])
                enriched_queue = {
                    **queue,
                    "subscription_uuid": parsed["subscription_uuid"]
                    if parsed
                    else None,
                    "offering_uuid": parsed["offering_uuid"] if parsed else None,
                    "object_type": parsed["object_type"] if parsed else None,
                    "consumer_uuid": consumer_uuid,
                    # Not queue_type: that key already carries RabbitMQ's own
                    # x-queue-type (classic/quorum/stream) from **queue.
                    "queue_kind": enums.QueueKind.CONSUMER
                    if consumer_uuid
                    else (
                        enums.QueueKind.LEGACY if parsed else enums.QueueKind.UNKNOWN
                    ),
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


class EventConsumerViewSet(
    mixins.ListModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Event consumers: one unified queue per consumer, scoped by bindings.

    A consumer is bound to a list of entities (projects, customers, offerings…)
    and receives the events whose scope-keys intersect those bindings. Binding
    is guarded: you may only subscribe to what you already hold a role on.

    **An empty binding list means GLOBAL** — every event, including all-user
    PII (profiles, SSH keys, role changes) — and is therefore restricted to
    staff/support. That guard is the PII boundary, re-checked again at delivery.
    """

    lookup_field = "uuid"
    # scopes are prefetched for the serializer's bindings and for is_global,
    # which reads the populated cache instead of an exists() query per row.
    queryset = (
        models.EventConsumer.objects.all()
        .select_related("user")
        .prefetch_related("scopes__content_type", "scopes__scope")
        .order_by("-created")
    )
    serializer_class = serializers.EventConsumerSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.EventConsumerFilter

    def get_queryset(self):
        # Consumers owned by a site agent are excluded platform-wide: they are
        # lifecycle-managed by marketplace_site_agent (register_queue /
        # AgentIdentity delete), and exposing them here would let a plain API
        # call retune a running agent's object_types or — via DELETE — tear
        # down its live RabbitMQ queue, since AgentIdentity.event_consumer is
        # only SET_NULL. Manage those through the site-agent endpoints.
        queryset = super().get_queryset().filter(agent_identity__isnull=True)
        user = self.request.user
        if user.is_staff or user.is_support:
            return queryset
        return queryset.filter(user=user)

    @extend_schema(
        description="Register (or refresh) an event-consumer queue for the "
        "calling user. Pass `scopes` to bind the queue to entities you hold a "
        "role on; an EMPTY `scopes` list requests a GLOBAL queue (all events, "
        "including all-user PII) and requires staff/support. Idempotent per "
        "binding set.",
        request=serializers.EventConsumerRegistrationSerializer,
        responses={
            status.HTTP_200_OK: serializers.EventConsumerRegistrationResponseSerializer,
            status.HTTP_201_CREATED: serializers.EventConsumerRegistrationResponseSerializer,
        },
    )
    @decorators.action(detail=False, methods=["post"])
    def register(self, request):
        input_serializer = serializers.EventConsumerRegistrationSerializer(
            data=request.data, context={"request": request}
        )
        input_serializer.is_valid(raise_exception=True)
        # None = field omitted = keep whatever the consumer already has;
        # [] = explicitly "all types". See the serializer for why this matters.
        requested_object_types = input_serializer.validated_data.get("object_types")
        # Already resolved + permission-checked by the serializer.
        resolved_scopes = input_serializer.validated_data.get("scopes", [])
        all_object_types = [m.value for m in enums.ObservableObjectType]
        vhost = request.user.uuid.hex

        # Empty bindings == global == the all-user PII firehose. Same guard as
        # IsSupport and as the delivery-time re-auth in event_dispatch, so the
        # two can never disagree.
        if not resolved_scopes and not (
            request.user.is_staff or request.user.is_support
        ):
            raise rest_framework.exceptions.PermissionDenied(
                "A global event consumer (empty scopes) is restricted to "
                "staff/support. Pass `scopes` to bind the queue to entities you "
                "hold a role on."
            )

        # Idempotent per binding set: reuse the caller's consumer whose bindings
        # match exactly, else create a new one. The match and the create must
        # happen inside ONE transaction with the caller's rows locked — two
        # concurrent registrations (an agent retrying, two workers booting) would
        # otherwise both miss and each create a consumer + RMQ queue, and the
        # client only keeps the last response's credentials. The orphan queue
        # then fills to x-max-length while every event is published twice.
        # Site-agent-owned consumers are never reused here (see get_queryset).
        wanted = {(s["content_type_id"], s["object_id"]) for s in resolved_scopes}
        with transaction.atomic():
            # Serialize this user's registrations. select_for_update below cannot
            # lock rows that do not exist yet, so on a FIRST registration two
            # concurrent requests would both find no candidate and both create a
            # consumer (+ RMQ queue). A per-user advisory lock closes that window
            # for the first-time case; the row lock still guards re-registration.
            utils.lock_user_registration(request.user.id)
            consumer = None
            candidates = (
                # of=("self",): the agent_identity__isnull filter is a LEFT JOIN
                # and Postgres refuses FOR UPDATE on the nullable side of one.
                # We only need the consumer rows locked anyway.
                models.EventConsumer.objects.select_for_update(of=("self",))
                .filter(user=request.user, agent_identity__isnull=True)
                .prefetch_related("scopes")
            )
            for candidate in candidates:
                existing = {
                    (s.content_type_id, s.object_id) for s in candidate.scopes.all()
                }
                if existing == wanted:
                    consumer = candidate
                    break

            if consumer is None:
                # Consumer + bindings in ONE transaction: a consumer left without
                # bindings IS a global consumer, i.e. a fail-open PII exposure.
                consumer = models.EventConsumer.objects.create(
                    user=request.user, object_types=requested_object_types or []
                )
                models.EventConsumerScope.objects.bulk_create(
                    [
                        models.EventConsumerScope(
                            consumer=consumer,
                            content_type_id=scope["content_type_id"],
                            object_id=scope["object_id"],
                        )
                        for scope in resolved_scopes
                    ]
                )
            elif (
                requested_object_types is not None
                and consumer.object_types != requested_object_types
            ):
                consumer.object_types = requested_object_types
                consumer.save(update_fields=["object_types"])

        effective_object_types = consumer.object_types or all_object_types

        rmq_backend = backend.RabbitMQManagementBackend()

        # Fast path: already provisioned and valid — refresh the password.
        # Verify BOTH the RMQ user and its vhost permissions, mirroring
        # register_queue. The vhost (user.uuid.hex) is shared with any legacy
        # EventSubscription; EventSubscriptionViewSet.perform_destroy deletes it
        # when the user's subscription count drops to <=1 (a count that ignores
        # EventConsumer rows), which wipes this consumer's queue + permissions
        # while its distinct rmq_username survives. Checking get_user alone would
        # then wrongly report 200 and hand back credentials for a queue whose
        # vhost no longer exists.
        if consumer.rmq_username and consumer.queue_created:
            if (
                rmq_backend.get_user(consumer.rmq_username) is not None
                and consumer.rmq_username
                in rmq_backend.list_rabbitmq_vhost_permissions(vhost)
                and rmq_backend.create_rabbitmq_user(
                    consumer.rmq_username,
                    utils.resolve_consumer_rmq_password(request),
                )
            ):
                data = {
                    "rmq_username": consumer.rmq_username,
                    "queue_name": consumer.queue_name,
                    "vhost": vhost,
                    "observable_object_types": effective_object_types,
                }
                out = serializers.EventConsumerRegistrationResponseSerializer(data=data)
                out.is_valid(raise_exception=True)
                return response.Response(out.data, status=status.HTTP_200_OK)
            # Stale RMQ state — clean up and recreate.
            rmq_backend.delete_rabbitmq_user(consumer.rmq_username)
            consumer.rmq_username = ""
            consumer.queue_created = False
            consumer.save(update_fields=["rmq_username", "queue_created"])

        result = utils.provision_consumer_queue(
            consumer, utils.resolve_consumer_rmq_password(request)
        )
        result["observable_object_types"] = effective_object_types
        out = serializers.EventConsumerRegistrationResponseSerializer(data=result)
        out.is_valid(raise_exception=True)
        return response.Response(out.data, status=status.HTTP_201_CREATED)


class EmailDebugViewSet(viewsets.ViewSet):
    """
    Staff-only sanity check for the outgoing email configuration.

    Waldur ships no relay of its own and every notification type ships
    disabled, so a fresh installation sends nothing and logs nothing to
    explain why. This endpoint reports both halves, probes the relay on
    demand, and sends a test message.
    """

    permission_classes = [permissions.IsAuthenticated, core_permissions.IsStaff]
    serializer_class = (
        serializers.EmailDiagnosticsSerializer
    )  # Default for OpenAPI schema
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "email_diagnostics"

    def get_throttles(self):
        # Reading the audit opens no socket and sends nothing, so it is exempt
        # from the throttle that guards the two actions which do.
        if self.action == "config":
            return []
        return super().get_throttles()

    @extend_schema(
        summary="Audit the outgoing email configuration",
        description="""Reports the effective mail settings and the problems found in them.

Reads settings only — no connection is opened and no message is sent.
Covers the two independent halves of email delivery: a usable SMTP relay,
and at least one enabled notification type. Requires staff permissions.""",
        responses={
            status.HTTP_200_OK: serializers.EmailDiagnosticsSerializer,
        },
    )
    @decorators.action(detail=False, methods=["get"])
    def config(self, request):
        diagnostics = email_diagnostics.collect_diagnostics()
        return response.Response(diagnostics.to_dict(), status=status.HTTP_200_OK)

    @extend_schema(
        summary="Test the SMTP connection",
        description="""Opens and closes a connection to the configured relay without sending a message.

The connection is made from the API process, which may reach the network
differently than the Celery workers that send real notifications.
Requires staff permissions.""",
        request=None,
        responses={
            status.HTTP_200_OK: serializers.EmailProbeSerializer,
        },
    )
    @decorators.action(detail=False, methods=["post"])
    def probe(self, request):
        result = email_diagnostics.probe_smtp()
        logger.info(
            "User %s probed the SMTP connection: %s",
            request.user.uuid,
            "reachable" if result["success"] else result["error"],
        )
        return response.Response(result, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Send a test email",
        description="""Sends a test message through the same code path as real notifications.

Defaults to the address of the requesting user. Requires staff permissions.""",
        request=serializers.EmailTestSendRequestSerializer,
        responses={
            status.HTTP_200_OK: serializers.EmailTestSendResultSerializer,
            status.HTTP_400_BAD_REQUEST: None,
        },
    )
    @decorators.action(detail=False, methods=["post"])
    def send_test(self, request):
        input_serializer = serializers.EmailTestSendRequestSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        recipient = input_serializer.validated_data.get("email") or request.user.email
        if not recipient:
            raise rest_framework.serializers.ValidationError(
                {
                    "email": "Your account has no email address, so a recipient must be given."
                }
            )

        # An authenticated staff user can name any recipient here, so leave a
        # trail that ties the message to the person who asked for it.
        logger.info(
            "User %s is sending a test email to %s",
            request.user.uuid,
            recipient,
        )
        try:
            core_utils.send_mail(
                subject="Waldur test message",
                body=(
                    "This is a test message sent from the Waldur administration interface "
                    f"by {request.user.full_name or request.user.username}.\n\n"
                    "Receiving it confirms that the SMTP relay accepts and delivers mail "
                    "from this installation."
                ),
                to=[recipient],
                fail_silently=False,
                # An explicit timeout, for the same reason the probe carries one:
                # the deployments that reach for this button are the ones whose
                # relay may accept a connection and then never answer, and this
                # runs inline in the request.
                connection=email_diagnostics.open_connection(),
            )
        except Exception as e:
            logger.warning("Test email to %s failed: %s", recipient, e)
            return response.Response(
                {
                    "success": False,
                    "email": recipient,
                    "error": f"{type(e).__name__}: {e}",
                },
                status=status.HTTP_200_OK,
            )
        return response.Response(
            {"success": True, "email": recipient, "error": ""},
            status=status.HTTP_200_OK,
        )
