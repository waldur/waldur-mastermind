import logging
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework import permissions as rf_permissions
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from waldur_core.core import utils as core_utils
from waldur_core.core.permissions import IsStaff, IsSupport
from waldur_core.core.views import ActionsViewSet
from waldur_core.logging import backend as logging_backend
from waldur_core.logging import models as logging_models
from waldur_core.logging import serializers as logging_serializers
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import has_permission
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure.models import Project
from waldur_mastermind.marketplace import enums as marketplace_enums
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_site_agent import filters, models, serializers
from waldur_mastermind.marketplace_site_agent.enums import AgentServiceState
from waldur_mastermind.marketplace_site_agent.utils import push_user_role_sync_message

logger = logging.getLogger(__name__)


def _can_manage_offering_agent(request, offering, agent_identity=None):
    """Check if user can manage agent identities/services for the given offering.

    Allowed for:
    1. Staff
    2. Customer-level permission (owner, service provider manager)
    3. Offering managers (offering-scoped role)
    4. Identity managers with managed_isds — can create for non-archived/draft
       offerings and manage only their own agent identities
    """
    user = request.user
    if user.is_staff:
        return True
    if has_permission(request, PermissionEnum.CREATE_OFFERING, offering.customer):
        return True
    if has_permission(request, PermissionEnum.UPDATE_OFFERING, offering):
        return True
    if user.is_identity_manager and user.managed_isds:
        if offering.state not in marketplace_enums.OfferingStates.ISD_ALLOWED_STATES:
            return False
        if agent_identity is not None:
            return agent_identity.created_by == user
        return True
    return False


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


class AgentIdentityViewSet(ActionsViewSet):
    lookup_field = "uuid"
    serializer_class = serializers.AgentIdentitySerializer
    filterset_class = filters.AgentIdentityFilter
    filter_backends = (DjangoFilterBackend,)
    queryset = models.AgentIdentity.objects.all()

    disabled_actions = ["partial_update"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user

        if not user.is_authenticated:
            return qs.none()

        if user.is_staff or user.is_support:
            return qs

        offerings = marketplace_models.Offering.objects.all().filter_for_user(user)
        base_q = Q(offering__in=offerings)

        if user.is_identity_manager and user.managed_isds:
            base_q = base_q | Q(
                offering__state__in=marketplace_enums.OfferingStates.ISD_ALLOWED_STATES,
                created_by=user,
            )

        return models.AgentIdentity.objects.filter(base_q).distinct()

    def check_create_permissions(request, view, obj=None):
        is_browsable_api_check = request.method == "POST" and (
            not hasattr(request, "_full_data") or not request.data
        )

        if request.method != "POST" or is_browsable_api_check:
            if not request.user.is_authenticated:
                raise PermissionDenied("Authentication required")

            # For OPTIONS, GET, or browsable API checks, just check if user can create in general
            if request.user.is_staff or getattr(request.user, "is_support", False):
                return

            # Check if user has permission to any agent-based offerings
            matching_offering_exists = (
                marketplace_models.Offering.filter_for_user(request.user)
                .filter(uuid=obj.uuid)
                .exists()
            )
            if not matching_offering_exists:
                raise PermissionDenied(
                    "Creating agent identities is not allowed due to insufficient offering permissions"
                )
            return

        # For actual POST requests with data, validate the data
        serializer = view.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        offering = serializer.validated_data.get("offering")
        if not offering:
            raise PermissionDenied()
        if not _can_manage_offering_agent(request, offering):
            raise PermissionDenied()

    create_permissions = [check_create_permissions]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def _check_agent_identity_permission(request, view, obj=None):
        if not obj:
            if not request.user.is_authenticated:
                raise PermissionDenied("Authentication required")
            return
        if not _can_manage_offering_agent(request, obj.offering, agent_identity=obj):
            raise PermissionDenied()

    partial_update_permissions = destroy_permissions = (
        register_event_subscription_permissions
    ) = register_service_permissions = [_check_agent_identity_permission]

    @extend_schema(
        description="Register an event subscription for the specified agent identity and observable object type. Returns existing subscription if already exists.",
        request=serializers.AgentEventSubscriptionCreateSerializer,
        responses={
            200: logging_serializers.EventSubscriptionSerializer,
            201: logging_serializers.EventSubscriptionSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def register_event_subscription(self, request, uuid=None):
        """
        Register an EventSubscription for the request user and the specified observable object type.
        This allows the user to receive notifications for events related to the specified object type.
        If a similar subscription already exists, returns the existing one instead of creating a duplicate.
        """

        def validate_event_subscription(
            rmq_backend, event_subscription: logging_models.EventSubscription
        ) -> bool:
            rmq_username = event_subscription.uuid.hex
            rmq_vhost = event_subscription.user.uuid.hex
            # Check if user exists
            rmq_user_info = rmq_backend.get_user(rmq_username)
            if rmq_user_info is None:
                return False
            # Check if user can access the vhost
            rmq_vhost_users = rmq_backend.list_rabbitmq_vhost_permissions(rmq_vhost)
            return rmq_username in rmq_vhost_users

        agent_identity = self.get_object()

        input_serializer = serializers.AgentEventSubscriptionCreateSerializer(
            data=request.data
        )

        input_serializer.is_valid(raise_exception=True)

        validated_data = input_serializer.validated_data
        observable_object_type = validated_data["observable_object_type"]
        logger.info(
            "Registering event subscription for agent identity %s, object type %s",
            agent_identity,
            observable_object_type,
        )
        description = validated_data.get(
            "description",
            f"Event subscription for {observable_object_type} ({agent_identity.name})",
        )

        # Create the observable objects list with the specified type
        observable_objects = [
            {
                "object_type": observable_object_type,
            }
        ]

        # Check if a similar subscription already exists
        existing_subscription = logging_models.EventSubscription.objects.filter(
            user=request.user, observable_objects=observable_objects
        ).first()

        if existing_subscription:
            # Return existing subscription
            logger.info(
                "The event subscription for agent identity %s and object type %s already exists. Returning existing subscription %s.",
                agent_identity,
                observable_object_type,
                existing_subscription.uuid.hex,
            )
            rmq_backend = logging_backend.RabbitMQManagementBackend()
            is_ready = validate_event_subscription(rmq_backend, existing_subscription)
            if is_ready:
                serializer = logging_serializers.EventSubscriptionSerializer(
                    existing_subscription, context={"request": request}
                )
                return Response(serializer.data, status=status.HTTP_200_OK)
            else:
                logger.info("The event subscription is not active, removing it")
                rmq_backend.delete_rabbitmq_user(existing_subscription.uuid.hex)
                existing_subscription.delete()

        # Use the existing EventSubscriptionSerializer to properly create the subscription
        # This ensures RabbitMQ setup is handled correctly
        logger.info(
            "Creating a new event subscription for the agent identity %s, object type %s",
            agent_identity,
            observable_object_type,
        )
        subscription_data = {
            "description": description,
            "observable_objects": observable_objects,
        }

        subscription_serializer = logging_serializers.EventSubscriptionSerializer(
            data=subscription_data, context={"request": request}
        )
        subscription_serializer.is_valid(raise_exception=True)
        subscription_serializer.save(
            user=request.user, source_ip=core_utils.get_ip_address(request)
        )

        return Response(subscription_serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(
        description="Register a new processor or get the existing one for the agent service",
        request=serializers.AgentServiceCreateSerializer,
        responses={
            200: serializers.AgentServiceSerializer,
            201: serializers.AgentServiceSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def register_service(self, request, *args, **kwargs):
        """
        Register a new service or update the existing service for the specified agent identity.
        """
        agent_identity = self.get_object()
        serializer = serializers.AgentServiceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        logger.info(
            "Registering service %s for agent identity %s",
            serializer.validated_data["name"],
            agent_identity,
        )
        status_code = status.HTTP_201_CREATED

        existing_service = models.AgentService.objects.filter(
            identity=agent_identity,
            name=serializer.validated_data["name"],
        ).first()
        if existing_service:
            logger.info(
                "The service %s for agent identity %s already exists. Returning existing service %s.",
                serializer.validated_data["name"],
                agent_identity,
                existing_service,
            )
            serializer.instance = existing_service
            status_code = status.HTTP_200_OK

        service = serializer.save(
            identity=agent_identity, state=AgentServiceState.ACTIVE
        )
        output_serializer = serializers.AgentServiceSerializer(
            service, context={"request": request}
        )
        return Response(output_serializer.data, status=status_code)

    cleanup_orphaned_permissions = [structure_permissions.is_staff]

    @extend_schema(
        description="Remove agent identities that have no active services. Staff only.",
        request=serializers.CleanupRequestSerializer,
        responses={200: serializers.CleanupResponseSerializer},
    )
    @action(detail=False, methods=["post"])
    def cleanup_orphaned(self, request):
        """
        Remove agent identities with no active services.
        Use dry_run=true to preview what would be deleted without actually deleting.
        """
        input_serializer = serializers.CleanupRequestSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        dry_run = input_serializer.validated_data.get("dry_run", True)

        # Find identities with no services
        orphaned_identities = models.AgentIdentity.objects.annotate(
            service_count=Count("agentservice")
        ).filter(service_count=0)

        items = list(
            orphaned_identities.values("uuid", "name", "offering__name", "created")
        )
        deleted_count = orphaned_identities.count()

        if not dry_run:
            logger.info(
                "Cleaning up %d orphaned agent identities (staff user: %s)",
                deleted_count,
                request.user.username,
            )
            orphaned_identities.delete()

        output_serializer = serializers.CleanupResponseSerializer(
            data={
                "deleted_count": deleted_count,
                "dry_run": dry_run,
                "items": items,
            }
        )
        output_serializer.is_valid(raise_exception=True)
        return Response(output_serializer.data, status=status.HTTP_200_OK)


class AgentServiceViewSet(ActionsViewSet):
    lookup_field = "uuid"
    serializer_class = serializers.AgentServiceSerializer
    filterset_class = filters.AgentServiceFilter
    filter_backends = (DjangoFilterBackend,)
    queryset = models.AgentService.objects.all()

    disabled_actions = ["create", "update", "partial_update"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user

        if not user.is_authenticated:
            return qs.none()

        if user.is_staff or user.is_support:
            return qs

        offerings = marketplace_models.Offering.objects.all().filter_for_user(user)
        base_q = Q(identity__offering__in=offerings)

        if user.is_identity_manager and user.managed_isds:
            base_q = base_q | Q(
                identity__offering__state__in=marketplace_enums.OfferingStates.ISD_ALLOWED_STATES,
                identity__created_by=user,
            )

        return models.AgentService.objects.filter(base_q).distinct()

    def _check_agent_service_permission(request, view, obj=None):
        if not obj:
            if not request.user.is_authenticated:
                raise PermissionDenied("Authentication required")
            return
        if not _can_manage_offering_agent(
            request, obj.identity.offering, agent_identity=obj.identity
        ):
            raise PermissionDenied()

    destroy_permissions = set_statistics_permissions = (
        register_processor_permissions
    ) = [_check_agent_service_permission]

    @extend_schema(
        description="Update statistics for the agent service",
        request=serializers.AgentServiceStatisticsSerializer,
        responses={200: serializers.AgentServiceSerializer},
    )
    @action(detail=True, methods=["post"])
    def set_statistics(self, request, uuid=None):
        """
        Update the statistics field for this agent service.
        """
        agent_service = self.get_object()
        serializer = serializers.AgentServiceStatisticsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        logger.info(
            "Updating statistics for agent service %s",
            agent_service,
        )

        agent_service.statistics = serializer.validated_data["statistics"]
        agent_service.save(update_fields=["statistics"])

        output_serializer = self.get_serializer(agent_service)
        return Response(output_serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        description="Register a new processor for the agent service",
        request=serializers.AgentProcessorCreateSerializer,
        responses={
            200: serializers.AgentProcessorSerializer,
            201: serializers.AgentProcessorSerializer,
        },
    )
    @action(detail=True, methods=["post"])
    def register_processor(self, request, uuid=None):
        """
        Register a new processor with the specified agent service.
        """
        agent_service = self.get_object()
        serializer = serializers.AgentProcessorCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        processor_name = serializer.validated_data["name"]
        logger.info(
            "Registering processor %s for agent service %s",
            processor_name,
            agent_service,
        )
        status_code = status.HTTP_201_CREATED

        existing_processor = models.AgentProcessor.objects.filter(
            service=agent_service, name=processor_name
        ).first()
        if existing_processor:
            logger.info(
                "The processor %s for agent service %s already exists. Returning existing processor %s.",
                processor_name,
                agent_service,
                existing_processor,
            )
            serializer.instance = existing_processor
            status_code = status.HTTP_200_OK

        processor = serializer.save(service=agent_service, last_run=timezone.now())
        output_serializer = serializers.AgentProcessorSerializer(
            processor, context={"request": request}
        )
        return Response(output_serializer.data, status=status_code)

    cleanup_stale_permissions = [structure_permissions.is_staff]

    @extend_schema(
        description="Remove agent services that have been inactive for a specified time. Staff only.",
        request=serializers.CleanupRequestSerializer,
        responses={200: serializers.CleanupResponseSerializer},
    )
    @action(detail=False, methods=["post"])
    def cleanup_stale(self, request):
        """
        Remove agent services that have been inactive for more than the specified hours.
        Use dry_run=true to preview what would be deleted without actually deleting.
        """
        input_serializer = serializers.CleanupRequestSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        dry_run = input_serializer.validated_data.get("dry_run", True)
        older_than_hours = input_serializer.validated_data.get("older_than_hours", 24)

        threshold = timezone.now() - timedelta(hours=older_than_hours)
        stale_services = models.AgentService.objects.filter(modified__lt=threshold)

        items = list(
            stale_services.values("uuid", "name", "identity__name", "state", "modified")
        )
        deleted_count = stale_services.count()

        if not dry_run:
            logger.info(
                "Cleaning up %d stale agent services older than %d hours (staff user: %s)",
                deleted_count,
                older_than_hours,
                request.user.username,
            )
            stale_services.delete()

        output_serializer = serializers.CleanupResponseSerializer(
            data={
                "deleted_count": deleted_count,
                "dry_run": dry_run,
                "items": items,
            }
        )
        output_serializer.is_valid(raise_exception=True)
        return Response(output_serializer.data, status=status.HTTP_200_OK)


class AgentProcessorViewSet(ActionsViewSet):
    lookup_field = "uuid"
    serializer_class = serializers.AgentProcessorSerializer
    filterset_class = filters.AgentProcessorFilter
    queryset = models.AgentProcessor.objects.all()
    filter_backends = (DjangoFilterBackend,)

    disabled_actions = ["create", "update", "partial_update"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user

        if not user.is_authenticated:
            return qs.none()

        if user.is_staff or user.is_support:
            return qs

        offerings = marketplace_models.Offering.objects.all().filter_for_user(user)
        base_q = Q(service__identity__offering__in=offerings)

        if user.is_identity_manager and user.managed_isds:
            base_q = base_q | Q(
                service__identity__offering__state__in=marketplace_enums.OfferingStates.ISD_ALLOWED_STATES,
                service__identity__created_by=user,
            )

        return models.AgentProcessor.objects.filter(base_q).distinct()

    def _check_agent_processor_permission(request, view, obj=None):
        if not obj:
            if not request.user.is_authenticated:
                raise PermissionDenied("Authentication required")
            return
        if not _can_manage_offering_agent(
            request,
            obj.service.identity.offering,
            agent_identity=obj.service.identity,
        ):
            raise PermissionDenied()

    destroy_permissions = [_check_agent_processor_permission]


class AgentStatsViewSet(generics.GenericAPIView):
    """API endpoint for agent monitoring statistics."""

    permission_classes = [rf_permissions.IsAuthenticated, IsSupport]
    serializer_class = serializers.AgentStatsResponseSerializer

    @extend_schema(
        description="Get aggregated statistics about agent identities, services, and processors. Support users only.",
        responses={200: serializers.AgentStatsResponseSerializer},
    )
    def get(self, request, *args, **kwargs):
        stale_service_threshold = timezone.now() - timedelta(hours=24)
        stale_processor_threshold = timezone.now() - timedelta(hours=1)

        data = {
            "identities": {
                "total": models.AgentIdentity.objects.count(),
                "by_offering": list(
                    models.AgentIdentity.objects.values(
                        "offering__name", "offering__uuid"
                    ).annotate(count=Count("id"))
                ),
            },
            "services": {
                "total": models.AgentService.objects.count(),
                "by_state": {
                    "active": models.AgentService.objects.filter(
                        state=AgentServiceState.ACTIVE
                    ).count(),
                    "idle": models.AgentService.objects.filter(
                        state=AgentServiceState.IDLE
                    ).count(),
                    "error": models.AgentService.objects.filter(
                        state=AgentServiceState.ERROR
                    ).count(),
                },
                "stale_count": models.AgentService.objects.filter(
                    modified__lt=stale_service_threshold
                ).count(),
            },
            "processors": {
                "total": models.AgentProcessor.objects.count(),
                "by_backend_type": list(
                    models.AgentProcessor.objects.values("backend_type").annotate(
                        count=Count("id")
                    )
                ),
                "stale_count": models.AgentProcessor.objects.filter(
                    last_run__lt=stale_processor_threshold
                ).count(),
            },
        }
        output_serializer = serializers.AgentStatsResponseSerializer(data=data)
        output_serializer.is_valid(raise_exception=True)
        return Response(output_serializer.data, status=status.HTTP_200_OK)


class AgentTaskStatsViewSet(generics.GenericAPIView):
    """API endpoint for agent-related Celery task statistics."""

    permission_classes = [rf_permissions.IsAuthenticated, IsSupport]
    serializer_class = serializers.AgentTaskStatsResponseSerializer

    @extend_schema(
        description="Get Celery task status for agent-related tasks. Support users only.",
        responses={200: serializers.AgentTaskStatsResponseSerializer},
    )
    def get(self, request, *args, **kwargs):
        from waldur_core.server.celeryconf import app

        agent_tasks = [
            "waldur_mastermind.marketplace_site_agent.tasks.sync_offering_users",
            "waldur_mastermind.marketplace_site_agent.tasks.mark_offering_backend_as_disconnected_after_timeout",
            "waldur_mastermind.marketplace_site_agent.tasks.sync_resources",
            "waldur_mastermind.marketplace_site_agent.tasks.send_messages_about_pending_orders",
            "waldur_mastermind.marketplace_site_agent.tasks.mark_agent_services_as_inactive",
        ]

        try:
            inspect = app.control.inspect()
            active = inspect.active() or {}
            scheduled = inspect.scheduled() or {}
            reserved = inspect.reserved() or {}

            data = {
                "active_tasks": [
                    {
                        "id": task.get("id"),
                        "name": task.get("name"),
                        "args": task.get("args"),
                        "worker": worker,
                    }
                    for worker, worker_tasks in active.items()
                    for task in worker_tasks
                    if task.get("name") in agent_tasks
                ],
                "scheduled_tasks": [
                    {
                        "id": task.get("request", {}).get("id"),
                        "name": task.get("request", {}).get("name"),
                        "eta": task.get("eta"),
                    }
                    for worker_tasks in scheduled.values()
                    for task in worker_tasks
                    if task.get("request", {}).get("name") in agent_tasks
                ],
                "reserved_tasks": [
                    {
                        "id": task.get("id"),
                        "name": task.get("name"),
                    }
                    for worker_tasks in reserved.values()
                    for task in worker_tasks
                    if task.get("name") in agent_tasks
                ],
            }
        except Exception as e:
            logger.warning("Failed to get Celery task stats: %s", e)
            data = {
                "active_tasks": [],
                "scheduled_tasks": [],
                "reserved_tasks": [],
                "error": str(e),
            }

        output_serializer = serializers.AgentTaskStatsResponseSerializer(data=data)
        output_serializer.is_valid(raise_exception=True)
        return Response(output_serializer.data, status=status.HTTP_200_OK)


class AgentConnectionStatsViewSet(generics.GenericAPIView):
    """
    API endpoint for viewing agent connection status with RabbitMQ.

    Cross-references AgentIdentity data with RabbitMQ connection information
    to provide a unified view of agent health and connectivity.
    """

    permission_classes = [rf_permissions.IsAuthenticated, IsSupport]
    serializer_class = serializers.AgentConnectionStatsResponseSerializer
    pagination_class = None

    @extend_schema(
        summary="Get agent connection statistics",
        description="""Returns connection status for all registered agents.

For each agent identity, provides:
- Agent metadata (name, version, offering)
- Services and their states
- Event subscriptions with RabbitMQ connection status
- RabbitMQ queues associated with the agent's offering

The RMQ connection data includes:
- Whether the agent is currently connected
- Connection source IP, timestamp, and state
- Traffic statistics (bytes sent/received)

Requires support user permissions.""",
        responses={
            200: serializers.AgentConnectionStatsResponseSerializer,
            503: {"description": "RabbitMQ unavailable"},
        },
    )
    def get(self, request, *args, **kwargs):
        rmq_backend = logging_backend.RabbitMQManagementBackend()

        # Get all RMQ users and their connections for quick lookup
        rmq_connections_by_user = {}
        try:
            rmq_users = rmq_backend.list_rabbitmq_users()
            for rmq_user in rmq_users:
                try:
                    connections = rmq_backend.get_user_connections_enriched(rmq_user)
                    rmq_connections_by_user[rmq_user] = connections
                except Exception:
                    rmq_connections_by_user[rmq_user] = []
        except Exception as e:
            logger.warning("Failed to get RMQ users: %s", e)
            rmq_users = []

        # Get all subscription queues for lookup
        queues_by_offering = {}
        try:
            vhost_data = rmq_backend.list_all_subscription_queues()
            for vhost in vhost_data:
                for queue in vhost.get("queues", []):
                    from waldur_core.logging import utils as logging_utils

                    parsed = logging_utils.parse_subscription_queue_name(queue["name"])
                    if parsed and parsed.get("offering_uuid"):
                        offering_uuid = parsed["offering_uuid"]
                        if offering_uuid not in queues_by_offering:
                            queues_by_offering[offering_uuid] = []
                        queues_by_offering[offering_uuid].append(
                            {
                                "name": queue["name"],
                                "messages": queue.get("messages", 0),
                                "consumers": queue.get("consumers", 0),
                                "object_type": parsed.get("object_type"),
                            }
                        )
        except Exception as e:
            logger.warning("Failed to get RMQ queues: %s", e)

        # Get all agent identities with their offerings
        agents_data = []
        total_queued_messages = 0
        connected_count = 0

        agent_identities = models.AgentIdentity.objects.select_related(
            "offering"
        ).prefetch_related("agentservice_set")

        for identity in agent_identities:
            # Get services for this identity
            services_data = []
            for service in identity.agentservice_set.all():
                services_data.append(
                    {
                        "uuid": service.uuid,
                        "name": service.name,
                        "state": service.get_state_display(),
                        "modified": service.modified,
                    }
                )

            # Find event subscriptions that could be related to this agent
            # We look for subscriptions where the user has access to this offering
            # and the subscription was created for relevant object types
            event_subscriptions_data = []
            agent_connected = False

            # Get subscriptions for users who have registered this agent
            # Since agents register subscriptions via register_event_subscription action,
            # we look for subscriptions that match the pattern
            subscriptions = logging_models.EventSubscription.objects.all()

            for subscription in subscriptions:
                rmq_username = subscription.uuid.hex
                connections = rmq_connections_by_user.get(rmq_username, [])

                rmq_connection = None
                if connections:
                    conn = connections[0]  # Take first active connection
                    rmq_connection = {
                        "connected": True,
                        "source_ip": conn.get("source_ip"),
                        "connected_at": conn.get("connected_at"),
                        "state": conn.get("state"),
                        "recv_oct": conn.get("recv_oct"),
                        "send_oct": conn.get("send_oct"),
                    }
                    agent_connected = True
                else:
                    rmq_connection = {
                        "connected": False,
                        "source_ip": None,
                        "connected_at": None,
                        "state": None,
                        "recv_oct": None,
                        "send_oct": None,
                    }

                event_subscriptions_data.append(
                    {
                        "uuid": subscription.uuid,
                        "created": subscription.created,
                        "observable_objects": subscription.observable_objects,
                        "rmq_connection": rmq_connection,
                    }
                )

            if agent_connected:
                connected_count += 1

            # Get queues for this offering
            offering_uuid_hex = identity.offering.uuid.hex
            queues = queues_by_offering.get(offering_uuid_hex, [])
            for queue in queues:
                total_queued_messages += queue.get("messages", 0)

            agents_data.append(
                {
                    "uuid": identity.uuid,
                    "name": identity.name,
                    "offering_uuid": identity.offering.uuid,
                    "offering_name": identity.offering.name,
                    "version": identity.version,
                    "last_restarted": identity.last_restarted,
                    "services": services_data,
                    "event_subscriptions": event_subscriptions_data,
                    "queues": queues,
                }
            )

        total_agents = len(agents_data)
        summary = {
            "total_agents": total_agents,
            "connected_agents": connected_count,
            "disconnected_agents": total_agents - connected_count,
            "total_queued_messages": total_queued_messages,
        }

        output = {"agents": agents_data, "summary": summary}

        output_serializer = serializers.AgentConnectionStatsResponseSerializer(
            instance=output
        )
        return Response(output_serializer.data, status=status.HTTP_200_OK)


class SiteAgentLogViewSet(ActionsViewSet):
    """Endpoint for site agents to push diagnostic logs to Waldur Mastermind."""

    queryset = models.SiteAgentLog.objects.all()
    serializer_class = serializers.SiteAgentLogSerializer
    filterset_class = filters.SiteAgentLogFilter
    filter_backends = (DjangoFilterBackend,)
    lookup_field = "uuid"
    disabled_actions = ["update", "partial_update", "destroy", "retrieve"]

    def get_queryset(self):
        qs = super().get_queryset().select_related("agent_identity__offering")
        offerings = marketplace_models.Offering.objects.filter(
            type__in=[
                marketplace_enums.SITE_AGENT_OFFERING,
                marketplace_enums.SCRIPT_OFFERING,
                marketplace_enums.OPENSTACK_TENANT_OFFERING,
                marketplace_enums.BASIC_OFFERING,
            ]
        ).filter_for_user(self.request.user)
        return qs.filter(agent_identity__offering__in=offerings)

    @extend_schema(
        summary="Push site agent logs",
        description="Receive a batch of log entries from a site agent. Send a list where each entry includes agent_identity_uuid.",
        request=serializers.SiteAgentLogCreateSerializer(many=True),
        responses={201: serializers.SiteAgentLogSerializer(many=True)},
    )
    def create(self, request, *args, **kwargs):
        input_serializer = serializers.SiteAgentLogCreateSerializer(
            data=request.data, many=True, context={"request": request}
        )
        input_serializer.is_valid(raise_exception=True)

        logs = models.SiteAgentLog.objects.bulk_create(
            [
                models.SiteAgentLog(
                    agent_identity=entry["agent_identity_uuid"],
                    timestamp=entry["timestamp"],
                    level=entry["level"],
                    message=entry["message"],
                    module=entry["module"],
                )
                for entry in input_serializer.validated_data
            ],
            batch_size=500,
        )
        output_serializer = self.get_serializer(logs, many=True)
        return Response(output_serializer.data, status=status.HTTP_201_CREATED)
