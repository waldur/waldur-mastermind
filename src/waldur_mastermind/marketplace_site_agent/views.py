import logging

from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from waldur_core.core import utils as core_utils
from waldur_core.core.permissions import IsStaff
from waldur_core.core.views import ActionsViewSet, ReadOnlyActionsViewSet
from waldur_core.logging import backend as logging_backend
from waldur_core.logging import models as logging_models
from waldur_core.logging import serializers as logging_serializers
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import has_permission, permission_factory
from waldur_core.structure.models import Project
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_site_agent import filters, models, serializers
from waldur_mastermind.marketplace_site_agent.enums import AgentServiceState
from waldur_mastermind.marketplace_site_agent.utils import push_user_role_sync_message

logger = logging.getLogger(__name__)


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
        return models.AgentIdentity.objects.filter(offering__in=offerings)

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
        if offering is None or not (
            has_permission(request, PermissionEnum.CREATE_OFFERING, offering.customer)
        ):
            raise PermissionDenied()

    create_permissions = [check_create_permissions]

    partial_update_permissions = destroy_permissions = (
        register_event_subscription_permissions
    ) = register_service_permissions = [
        permission_factory(
            PermissionEnum.CREATE_OFFERING,
            ["offering.customer"],
        )
    ]

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


class AgentServiceViewSet(ReadOnlyActionsViewSet):
    lookup_field = "uuid"
    serializer_class = serializers.AgentServiceSerializer
    filterset_class = filters.AgentServiceFilter
    filter_backends = (DjangoFilterBackend,)
    queryset = models.AgentService.objects.all()

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user

        if not user.is_authenticated:
            return qs.none()

        if user.is_staff or user.is_support:
            return qs

        offerings = marketplace_models.Offering.objects.all().filter_for_user(user)
        return models.AgentService.objects.filter(identity__offering__in=offerings)

    set_statistics_permissions = register_processor_permissions = [
        permission_factory(
            PermissionEnum.CREATE_OFFERING,
            ["identity.offering.customer"],
        )
    ]

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


class AgentProcessorViewSet(ReadOnlyActionsViewSet):
    lookup_field = "uuid"
    serializer_class = serializers.AgentProcessorSerializer
    filterset_class = filters.AgentProcessorFilter
    queryset = models.AgentProcessor.objects.all()
    filter_backends = (DjangoFilterBackend,)

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user

        if not user.is_authenticated:
            return qs.none()

        if user.is_staff or user.is_support:
            return qs

        offerings = marketplace_models.Offering.objects.all().filter_for_user(user)
        return models.AgentProcessor.objects.filter(
            service__identity__offering__in=offerings
        )
