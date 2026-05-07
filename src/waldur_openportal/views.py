import logging

from django.http import Http404
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import filters as rf_filters
from rest_framework import permissions, response, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from waldur_core.core import executors as core_executors
from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core import views as core_views
from waldur_core.core.enums import ReviewStates
from waldur_core.core.permissions import IsAdminOrReadOnly
from waldur_core.core.serializers import ReviewCommentSerializer
from waldur_core.core.validators import StateValidator
from waldur_core.permissions.fixtures import ServiceProviderRole
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import views as structure_views
from waldur_core.structure.filters import GenericRoleFilter
from waldur_core.structure.permissions import IsAdminOrOwner, _has_owner_access

from . import executors, filters, models, serializers, tasks

logger = logging.getLogger(__name__)


class ShortOrderingFilter(rf_filters.OrderingFilter):
    ordering_param = "o"


class AllocationViewSet(structure_views.ResourceViewSet):
    queryset = models.Allocation.objects.all().order_by("name")
    serializer_class = serializers.AllocationSerializer
    filterset_class = filters.AllocationFilter

    create_executor = executors.AllocationCreateExecutor
    update_executor = core_executors.EmptyExecutor
    pull_executor = executors.AllocationPullExecutor

    destroy_permissions = [structure_permissions.is_administrator]
    delete_executor = executors.AllocationDeleteExecutor

    set_limits_permissions = [structure_permissions.is_staff]
    set_limits_serializer_class = serializers.AllocationSetLimitsSerializer

    @action(detail=True, methods=["post"])
    def set_limits(self, request, uuid=None):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.AllocationSetLimitsExecutor().execute(instance)
        return response.Response(
            {"status": _("Setting limits was scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )


class RemoteAllocationViewSet(structure_views.ResourceViewSet):
    queryset = models.RemoteAllocation.objects.all().order_by("name")
    serializer_class = serializers.RemoteAllocationSerializer
    filterset_class = filters.RemoteAllocationFilter

    create_executor = executors.RemoteAllocationCreateExecutor
    update_executor = core_executors.EmptyExecutor
    pull_executor = executors.RemoteAllocationPullExecutor

    destroy_permissions = [structure_permissions.is_administrator]
    delete_executor = executors.RemoteAllocationDeleteExecutor

    set_limits_permissions = [structure_permissions.is_staff]
    set_limits_serializer_class = serializers.RemoteAllocationSetLimitsSerializer

    @action(detail=True, methods=["post"])
    def set_limits(self, request, uuid=None):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        executors.RemoteAllocationSetLimitsExecutor().execute(instance)
        return response.Response(
            {"status": _("Setting limits was scheduled.")},
            status=status.HTTP_202_ACCEPTED,
        )


class AllocationUserUsageViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.AllocationUserUsage.objects.all().order_by("year", "month")
    serializer_class = serializers.AllocationUserUsageSerializer
    permission_classes = (
        permissions.IsAuthenticated,
        IsAdminOrOwner,
        IsAdminOrReadOnly,
    )
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.AllocationUserUsageFilter


class CachedProjectUsageReportViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.CachedProjectUsageReport.objects.none()
    serializer_class = serializers.CachedProjectUsageReportSerializer
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.CachedProjectUsageReportFilter

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return models.CachedProjectUsageReport.objects.none()
        user = self.request.user
        qs = models.CachedProjectUsageReport.objects.all().order_by(
            "year", "month", "project_identifier", "resource"
        )
        if user.is_staff or user.is_support:
            return qs
        # Restrict to project_identifiers reachable via allocations on projects
        # the user has any role in, including projects in customers where the user
        # is an organisation viewer.
        import openportal

        from waldur_core.structure.managers import get_visible_projects

        from . import config

        config.ensure_config_loaded()
        accessible_project_ids = list(get_visible_projects(user))
        portal = str(openportal.get_portal())
        # Identifiers from allocations (covers active projects with existing allocations)
        allocation_identifiers = set(
            models.Allocation.objects.filter(
                project_id__in=accessible_project_ids,
                backend_id__isnull=False,
            )
            .exclude(backend_id="")
            .values_list("backend_id", flat=True)
        )
        # Identifiers from remote allocations (local portal: projects on a remote cluster)
        remote_allocation_identifiers = set(
            models.RemoteAllocation.objects.filter(
                project_id__in=accessible_project_ids,
                backend_id__isnull=False,
            )
            .exclude(backend_id="")
            .values_list("backend_id", flat=True)
        )
        # Identifiers from ProjectInfo shortnames (covers all projects)
        shortnames = (
            models.ProjectInfo.objects.filter(
                project_id__in=accessible_project_ids,
                shortname__isnull=False,
            )
            .exclude(shortname="")
            .values_list("shortname", flat=True)
        )
        shortname_identifiers = {f"{sn}.{portal}" for sn in shortnames}
        accessible_identifiers = (
            allocation_identifiers
            | remote_allocation_identifiers
            | shortname_identifiers
        )
        return qs.filter(project_identifier__in=accessible_identifiers)


class CachedProjectStorageReportViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = models.CachedProjectStorageReport.objects.none()
    serializer_class = serializers.CachedProjectStorageReportSerializer
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.CachedProjectStorageReportFilter

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return models.CachedProjectStorageReport.objects.none()
        user = self.request.user
        qs = models.CachedProjectStorageReport.objects.all().order_by(
            "year", "month", "project_identifier", "resource"
        )
        if user.is_staff or user.is_support:
            return qs
        import openportal

        from waldur_core.structure.managers import get_visible_projects

        from . import config

        config.ensure_config_loaded()
        accessible_project_ids = list(get_visible_projects(user))
        portal = str(openportal.get_portal())
        # Identifiers from allocations (covers active projects with existing allocations)
        allocation_identifiers = set(
            models.Allocation.objects.filter(
                project_id__in=accessible_project_ids,
                backend_id__isnull=False,
            )
            .exclude(backend_id="")
            .values_list("backend_id", flat=True)
        )
        # Identifiers from remote allocations (local portal: projects on a remote cluster)
        remote_allocation_identifiers = set(
            models.RemoteAllocation.objects.filter(
                project_id__in=accessible_project_ids,
                backend_id__isnull=False,
            )
            .exclude(backend_id="")
            .values_list("backend_id", flat=True)
        )
        # Identifiers from ProjectInfo shortnames (covers all projects)
        shortnames = (
            models.ProjectInfo.objects.filter(
                project_id__in=accessible_project_ids,
                shortname__isnull=False,
            )
            .exclude(shortname="")
            .values_list("shortname", flat=True)
        )
        shortname_identifiers = {f"{sn}.{portal}" for sn in shortnames}
        accessible_identifiers = (
            allocation_identifiers
            | remote_allocation_identifiers
            | shortname_identifiers
        )
        return qs.filter(project_identifier__in=accessible_identifiers)


class AssociationViewSet(viewsets.ReadOnlyModelViewSet):
    lookup_field = "uuid"
    queryset = models.Association.objects.all().order_by("username")
    serializer_class = serializers.AssociationSerializer
    permission_classes = (
        permissions.IsAuthenticated,
        IsAdminOrOwner,
        IsAdminOrReadOnly,
    )
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.AssociationFilter


class RemoteAssociationViewSet(viewsets.ReadOnlyModelViewSet):
    lookup_field = "uuid"
    queryset = models.RemoteAssociation.objects.all().order_by("pk")
    serializer_class = serializers.RemoteAssociationSerializer
    permission_classes = (
        permissions.IsAuthenticated,
        IsAdminOrOwner,
        IsAdminOrReadOnly,
    )
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.RemoteAssociationFilter


class UserInfoViewSet(core_views.ActionsViewSet):
    queryset = models.UserInfo.objects.all().order_by("shortname")
    lookup_field = "user"
    serializer_class = serializers.UserInfoSerializer
    permission_classes = (
        permissions.IsAuthenticated,
        IsAdminOrOwner,
        IsAdminOrReadOnly,
    )

    filterset_class = filters.UserInfoFilter

    def _get(self, user):
        user = core_models.User.objects.get(uuid=user)

        userinfo, created = models.UserInfo.objects.get_or_create(user=user)
        userinfo.sanitise()

        if created:
            logger.info(f"Created UserInfo {userinfo} for user {user}")
        else:
            logger.info(f"Retrieved UserInfo {userinfo} for user {user}")

        return userinfo

    def retrieve(self, request, pk=None, user=None):
        logger.info(f"Retrieving UserInfo {pk} : {request} : {user}")
        try:
            userinfo = self._get(user)
        except Exception as e:
            logger.error(f"Error retrieving user {user} : {e}")
            return Response(status=status.HTTP_404_NOT_FOUND)

        serializer = serializers.UserInfoSerializer(
            instance=userinfo, context={"request": request}
        )

        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"])
    def me(self, request):
        logger.info(f"Retrieving UserInfo for 'me'=user {request.user}")

        try:
            userinfo = self._get(request.user.uuid)
        except Exception as e:
            logger.error(f"Error retrieving user {request.user}: {e}")
            return Response(status=status.HTTP_404_NOT_FOUND)

        serializer = serializers.UserInfoSerializer(
            instance=userinfo, context={"request": request}
        )

        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["PUT"])
    def set_shortname(self, request, user=None):
        try:
            shortname = str(request.data["shortname"])
        except Exception as e:
            logger.error(f"You must provide the 'shortname' field: {e}")
            return Response(status=status.HTTP_400_BAD_REQUEST)

        try:
            userinfo = self._get(user)
        except Exception as e:
            logger.error(f"Error retrieving user {user}: {e}")
            return Response(status=status.HTTP_404_NOT_FOUND)

        user = userinfo.user

        if request.user != user and not request.user.is_staff:
            logger.error(
                f"User {request.user} is not allowed to set shortname for user {user}"
            )
            return Response(status=status.HTTP_403_FORBIDDEN)

        try:
            userinfo.set_shortname(shortname)
            userinfo.save()
        except Exception as e:
            logger.error(f"Error setting shortname for user {user}: {e}")
            return Response(status=status.HTTP_400_BAD_REQUEST)

        serializer = serializers.UserInfoSerializer(
            instance=userinfo, context={"request": request}
        )

        return Response(serializer.data, status=status.HTTP_200_OK)


class ProjectInfoViewSet(core_views.ActionsViewSet):
    queryset = models.ProjectInfo.objects.all().order_by("shortname")
    lookup_field = "project"
    serializer_class = serializers.ProjectInfoSerializer
    permission_classes = (
        permissions.IsAuthenticated,
        IsAdminOrOwner,
        IsAdminOrReadOnly,
    )
    filterset_class = filters.ProjectInfoFilter

    def _get(self, project):
        project = structure_models.Project.objects.get(uuid=project)

        projectinfo, created = models.ProjectInfo.objects.get_or_create(project=project)
        projectinfo.sanitise()

        if created:
            logger.info(f"Created ProjectInfo {projectinfo} for project {project}")
        else:
            logger.info(f"Retrieved ProjectInfo {projectinfo} for project {project}")

        return projectinfo

    def retrieve(self, request, pk=None, project=None):
        logger.info(f"Retrieving ProjectInfo {pk} : {request} : {project}")
        try:
            projectinfo = self._get(project)
        except Exception as e:
            logger.error(f"Error retrieving project {project} : {e}")
            return Response(status=status.HTTP_404_NOT_FOUND)

        serializer = serializers.ProjectInfoSerializer(
            instance=projectinfo, context={"request": request}
        )

        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["PUT"])
    def set_shortname(self, request, project=None):
        try:
            shortname = str(request.data["shortname"])
        except Exception as e:
            logger.error(f"You must provide the 'shortname' field: {e}")
            return Response(status=status.HTTP_400_BAD_REQUEST)

        try:
            projectinfo = self._get(project)
        except Exception as e:
            logger.error(f"Error retrieving project {project}: {e}")
            return Response(status=status.HTTP_404_NOT_FOUND)

        project = projectinfo.project

        if not request.user.is_staff:
            logger.error(
                f"User {request.user} is not allowed to set shortname for project {project}"
            )
            return Response(status=status.HTTP_403_FORBIDDEN)

        try:
            projectinfo.set_shortname(shortname)
            projectinfo.save()
        except Exception as e:
            logger.error(f"Error setting shortname for project {project}: {e}")
            return Response(status=status.HTTP_400_BAD_REQUEST)

        serializer = serializers.ProjectInfoSerializer(
            instance=projectinfo, context={"request": request}
        )

        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["PUT"])
    def set_allowed_destinations(self, request, project=None):
        try:
            allowed_destinations = str(request.data["allowed_destinations"])
        except Exception as e:
            logger.error(f"You must provide the 'allowed_destinations' field: {e}")
            return Response(status=status.HTTP_400_BAD_REQUEST)

        try:
            projectinfo = self._get(project)
        except Exception as e:
            logger.error(f"Error retrieving project {project}: {e}")
            return Response(status=status.HTTP_404_NOT_FOUND)

        project = projectinfo.project

        if not request.user.is_staff:
            logger.error(
                f"User {request.user} is not allowed to set allowed_destinations for project {project}"
            )
            return Response(status=status.HTTP_403_FORBIDDEN)

        try:
            projectinfo.set_allowed_destinations(allowed_destinations)
            projectinfo.save()
        except Exception as e:
            logger.error(
                f"Error setting allowed_destinations for project {project}: {e}"
            )
            return Response(status=status.HTTP_400_BAD_REQUEST)

        serializer = serializers.ProjectInfoSerializer(
            instance=projectinfo, context={"request": request}
        )

        return Response(serializer.data, status=status.HTTP_200_OK)


def _has_owner_or_manager_access(user, customer):
    """
    Check if the user has owner or manager access to the project class.
    """
    return _has_owner_access(user, customer) or customer.has_user(
        user, role=ServiceProviderRole.MANAGER
    )


def user_is_staff_or_service_provider_owner_or_service_provider_manager(
    user, project: models.ManagedProject | None = None
):
    if not project:
        raise PermissionDenied()

    # Force getting the project_template, as this will
    # delete the project if there is no template or it is not valid
    project_template = project.get_project_template()

    if project_template is None:
        raise PermissionDenied()

    if user.is_staff:
        return True

    if project_template.provider is None:
        raise PermissionDenied()

    if project_template.customer is None:
        raise PermissionDenied()

    if _has_owner_or_manager_access(
        user, project_template.provider
    ) and _has_owner_access(user, project_template.customer):
        return True

    raise PermissionDenied()


def user_is_staff_or_project_template_owner(
    user, project_template: models.ProjectTemplate | None = None
):
    if not project_template:
        raise PermissionDenied()

    if project_template.provider is None:
        raise PermissionDenied()

    if project_template.customer is None:
        raise PermissionDenied()

    if user.is_staff:
        return True

    if _has_owner_access(user, project_template.provider):
        return True

    raise PermissionDenied()


class IsStaffOrProjectTemplateOwner(BasePermission):
    """
    Permission class for staff or project class owners
    """

    def has_permission(self, request, view):
        obj = view.get_object() if hasattr(view, "get_object") else None

        if isinstance(obj, models.ProjectTemplate):
            return user_is_staff_or_project_template_owner(request.user, obj)
        else:
            raise PermissionDenied()

    def has_object_permission(self, request, view, obj):
        if isinstance(obj, models.ProjectTemplate):
            return user_is_staff_or_project_template_owner(request.user, obj)
        else:
            raise PermissionDenied()


class ProjectTemplateViewSet(core_views.ActionsViewSet):
    lookup_field = "uuid"
    queryset = models.ProjectTemplate.objects.all().order_by("name")
    serializer_class = serializers.ProjectTemplateSerializer
    permission_classes = (
        permissions.IsAuthenticated,
        IsAdminOrOwner,
        IsAdminOrReadOnly,
    )
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.ProjectTemplateFilter

    def get_permissions(self):
        """
        Instantiates and returns the list of permissions that this view requires.
        """
        if self.action in ["delete", "update", "partial_update"]:
            permission_classes = (
                permissions.IsAuthenticated,
                IsStaffOrProjectTemplateOwner,
            )
        elif self.action == "create":
            permission_classes = (permissions.IsAuthenticated,)
        else:
            permission_classes = self.permission_classes

        return [permission() for permission in permission_classes]

    @extend_schema(
        request=serializers.ProjectTemplateSerializer,
        responses=serializers.ProjectTemplateSerializer,
        description="Create ProjectTemplate object",
    )
    def create(self, request, *args, **kwargs):
        try:
            logger.info(f"Creating ProjectTemplate by user {request.user}")
            logger.info(f"Request data: {request.data}")
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            logger.info(f"Validated data: {serializer.validated_data}")

            try:
                is_staff = request.user.is_staff
            except AttributeError:
                is_staff = False

            # we need to verify that the user has the right permission in the
            # provider organization
            if not (
                is_staff
                or _has_owner_access(
                    request.user, serializer.validated_data.get("provider")
                )
            ):
                logger.error(
                    f"User {request.user} is not allowed to create ProjectTemplate for provider {serializer.validated_data.get('provider')}"
                )
                return Response(
                    {
                        "detail": _(
                            "You do not have permission to create this project class."
                        )
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

            logger.info(
                f"Creating ProjectTemplate with data: {serializer.validated_data}"
            )
            project_template = serializer.save()

            logger.info(
                f"Created ProjectTemplate {project_template} by user {request.user}"
            )
        except Exception as e:
            logger.error(f"Error creating ProjectTemplate: {e}")
            raise

        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=serializers.ProjectTemplateSerializer,
        responses=serializers.ProjectTemplateSerializer,
        description="Update ProjectTemplate object (full update)",
    )
    def update(self, request, *args, **kwargs):
        project_template = self.get_object()

        try:
            logger.info(
                f"Updating ProjectTemplate {project_template} by user {request.user}"
            )
            logger.info(f"Request data: {request.data}")

            serializer = self.get_serializer(project_template, data=request.data)
            serializer.is_valid(raise_exception=True)
            logger.info(f"Validated data: {serializer.validated_data}")

            # Check if provider is being changed and validate permissions
            if "provider" in serializer.validated_data:
                try:
                    is_staff = request.user.is_staff
                except AttributeError:
                    is_staff = False

                if not (
                    is_staff
                    or _has_owner_access(
                        request.user, serializer.validated_data.get("provider")
                    )
                ):
                    logger.error(
                        f"User {request.user} is not allowed to update ProjectTemplate for provider {serializer.validated_data.get('provider')}"
                    )
                    return Response(
                        {
                            "detail": _(
                                "You do not have permission to update this project class with the specified provider."
                            )
                        },
                        status=status.HTTP_403_FORBIDDEN,
                    )

            updated_project_template = serializer.save()
            logger.info(
                f"Updated ProjectTemplate {updated_project_template} by user {request.user}"
            )

            return Response(serializer.data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error updating ProjectTemplate: {e}")
            raise

    @extend_schema(
        request=serializers.ProjectTemplateSerializer,
        responses=serializers.ProjectTemplateSerializer,
        description="Partially update ProjectTemplate object",
    )
    def partial_update(self, request, *args, **kwargs):
        project_template = self.get_object()

        try:
            logger.info(
                f"Partially updating ProjectTemplate {project_template} by user {request.user}"
            )
            logger.info(f"Request data: {request.data}")

            serializer = self.get_serializer(
                project_template, data=request.data, partial=True
            )
            serializer.is_valid(raise_exception=True)
            logger.info(f"Validated data: {serializer.validated_data}")

            # Check if provider is being changed and validate permissions
            if "provider" in serializer.validated_data:
                try:
                    is_staff = request.user.is_staff
                except AttributeError:
                    is_staff = False

                if not (
                    is_staff
                    or _has_owner_access(
                        request.user, serializer.validated_data.get("provider")
                    )
                ):
                    logger.error(
                        f"User {request.user} is not allowed to update ProjectTemplate for provider {serializer.validated_data.get('provider')}"
                    )
                    return Response(
                        {
                            "detail": _(
                                "You do not have permission to update this project class with the specified provider."
                            )
                        },
                        status=status.HTTP_403_FORBIDDEN,
                    )

            updated_project_template = serializer.save()
            logger.info(
                f"Partially updated ProjectTemplate {updated_project_template} by user {request.user}"
            )

            return Response(serializer.data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error partially updating ProjectTemplate: {e}")
            raise

    @extend_schema(
        responses=None,
        description="Delete ProjectTemplate object",
    )
    @action(detail=True, methods=["delete"])
    def delete(self, **kwargs):
        project: models.ProjectTemplate = self.get_object()

        logger.info(f"Deleting ProjectTemplate {project} by user {self.request.user}")

        return Response(status=status.HTTP_200_OK)


class IsStaffOrServiceProviderOwnerOrManager(BasePermission):
    """
    Permission class for staff or service provider owners/managers
    """

    def has_permission(self, request, view):
        obj = view.get_object() if hasattr(view, "get_object") else None

        if isinstance(obj, models.ManagedProject):
            return user_is_staff_or_service_provider_owner_or_service_provider_manager(
                request.user, obj
            )
        else:
            raise PermissionDenied()

    def has_object_permission(self, request, view, obj):
        if isinstance(obj, models.ManagedProject):
            return user_is_staff_or_service_provider_owner_or_service_provider_manager(
                request.user, obj
            )
        else:
            raise PermissionDenied()


@extend_schema_view(
    list=extend_schema(
        description="List all managed projects",
    ),
)
class ManagedProjectViewSet(core_views.ActionsViewSet):
    queryset = models.ManagedProject.objects.all().order_by("created")
    permission_classes = (
        permissions.IsAuthenticated,
        IsAdminOrOwner,
        IsAdminOrReadOnly,
    )
    approve_permissions = reject_permissions = delete_permissions = [
        permissions.IsAuthenticated,
        IsStaffOrServiceProviderOwnerOrManager,
    ]
    attach_permissions = detach_permissions = approve_permissions

    serializer_class = serializers.ManagedProjectSerializer
    attach_serializer_class = serializers.ProjectAttachSerializer
    approve_serializer_class = reject_serializer_class = ReviewCommentSerializer

    approve_validators = reject_validators = [
        StateValidator(ReviewStates.PENDING, state_enum=ReviewStates)
    ]

    filter_backends = [GenericRoleFilter, DjangoFilterBackend, ShortOrderingFilter]
    filterset_class = filters.ManagedProjectFilter
    ordering_fields = (
        "created",
        "state",
        "identifier",
        "details__name",
        "project_template__name",
        "project__customer__name",
        "project_template__offering",
    )

    disabled_actions = ["create", "update", "partial_update", "retrieve", "destroy"]

    # Remove single lookup configuration
    lookup_field = None
    lookup_url_kwarg = None

    def get_serializer_class(self):
        if self.action == "attach":
            return self.attach_serializer_class
        elif self.action == "detach":
            return self.detach_serializer_class
        elif self.action == "approve":
            return self.approve_serializer_class
        elif self.action == "reject":
            return self.reject_serializer_class

        return serializers.ManagedProjectSerializer

    def get_object(self):
        """
        Override get_object to lookup by both identifier and destination.
        """
        queryset = self.filter_queryset(self.get_queryset())

        # Get identifier and destination from URL kwargs
        identifier = self.kwargs.get("identifier")
        destination = self.kwargs.get("destination")

        if not identifier or not destination:
            raise ValueError("Both identifier and destination must be provided")

        # Perform the lookup
        filter_kwargs = {"identifier": identifier, "destination": destination}

        try:
            obj = queryset.get(**filter_kwargs)
        except models.ManagedProject.DoesNotExist:
            raise Http404("No ManagedProject matches the given query.")

        # May raise a permission denied
        self.check_object_permissions(self.request, obj)

        return obj

    def get_permissions(self):
        """
        Instantiates and returns the list of permissions that this view requires.
        """
        if self.action == "approve" or self.action == "reject":
            permission_classes = (
                self.approve_permissions
                if self.action == "approve"
                else self.reject_permissions
            )
        elif self.action == "delete":
            permission_classes = self.delete_permissions
        elif self.action == "attach":
            permission_classes = self.attach_permissions
        elif self.action == "detach":
            permission_classes = self.detach_permissions
        else:
            permission_classes = self.permission_classes

        return [permission() for permission in permission_classes]

    @extend_schema(
        methods=["GET"],
        operation_id="openportal_managed_projects_retrieve_get",  # Add unique operation_id
        parameters=[
            OpenApiParameter(
                name="identifier",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="The identifier of the managed project",
            ),
            OpenApiParameter(
                name="destination",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="The destination of the managed project",
            ),
        ],
        responses=serializers.ManagedProjectSerializer,
        description="Retrieve a managed project",
    )
    @extend_schema(
        methods=["HEAD"],
        operation_id="openportal_managed_projects_retrieve_head",
        parameters=[
            OpenApiParameter(
                name="identifier",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="The identifier of the managed project",
            ),
            OpenApiParameter(
                name="destination",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="The destination of the managed project",
            ),
        ],
        responses=serializers.ManagedProjectSerializer,
        description="Check if a managed project exists",
    )
    @action(
        detail=False,
        methods=["get", "head"],
        url_path=r"(?P<identifier>[^/.]+)/(?P<destination>[^/.]+)",
    )
    def retrieve_custom(self, request, identifier=None, destination=None, **kwargs):
        """Custom retrieve action with composite key"""
        obj = self.get_object()

        if request.method == "HEAD":
            # For HEAD requests, just return empty response with proper status
            return Response(status=status.HTTP_200_OK)

        # For GET requests, return the serialized data
        serializer = self.get_serializer(obj)
        return Response(serializer.data)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="identifier",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="The identifier of the managed project",
            ),
            OpenApiParameter(
                name="destination",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="The destination of the managed project",
            ),
        ],
        request=ReviewCommentSerializer,
        responses=None,
        description="Approve managed project request",
    )
    @action(
        detail=False,
        methods=["post"],
        url_path=r"(?P<identifier>[^/.]+)/(?P<destination>[^/.]+)/approve",
    )
    def approve(self, request, identifier=None, destination=None, **kwargs):
        project: models.ManagedProject = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get("comment")
        project.approve(request.user, comment)

        # trigger a task to update the project
        tasks.managed_project_approved.delay(core_utils.serialize_instance(project))

        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="identifier",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="The identifier of the managed project",
            ),
            OpenApiParameter(
                name="destination",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="The destination of the managed project",
            ),
        ],
        request=ReviewCommentSerializer,
        responses=None,
        description="Reject managed project request",
    )
    @action(
        detail=False,
        methods=["post"],
        url_path=r"(?P<identifier>[^/.]+)/(?P<destination>[^/.]+)/reject",
    )
    def reject(self, request, identifier=None, destination=None, **kwargs):
        project: models.ManagedProject = self.get_object()
        serializer = self.get_serializer(data=request.data)

        logger.info(f"Serializer = {serializer} {type(serializer)}")

        serializer.is_valid(raise_exception=True)
        comment = serializer.validated_data.get("comment")
        project.reject(request.user, comment)

        # notify project admins and managers about the rejection
        tasks.notify_users_about_rejected_allocation.delay(
            core_utils.serialize_instance(project)
        )

        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="identifier",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="The identifier of the managed project",
            ),
            OpenApiParameter(
                name="destination",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="The destination of the managed project",
            ),
        ],
        responses=None,
        description="Delete ManagedProject object",
    )
    @action(
        detail=False,
        methods=["delete"],
        url_path=r"(?P<identifier>[^/.]+)/(?P<destination>[^/.]+)/delete",
    )
    def delete(self, request, identifier=None, destination=None, **kwargs):
        project: models.ManagedProject = self.get_object()

        logger.info(f"Deleting {project} by user {request.user}")
        project.delete()

        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="identifier",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="The identifier of the managed project",
            ),
            OpenApiParameter(
                name="destination",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="The destination of the managed project",
            ),
        ],
        request=serializers.ProjectAttachSerializer,
        responses=None,
        description="Attach a project to this managed project",
    )
    @action(
        detail=False,
        methods=["post"],
        url_path=r"(?P<identifier>[^/.]+)/(?P<destination>[^/.]+)/attach",
    )
    def attach(self, request, identifier=None, destination=None, **kwargs):
        managed_project: models.ManagedProject = self.get_object()
        serializer = serializers.ProjectAttachSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        project_uuid = serializer.validated_data["project_uuid"]

        try:
            project = structure_models.Project.objects.get(uuid=project_uuid)

            # Check if project is already attached to another ManagedProject
            existing_managed = (
                models.ManagedProject.objects.filter(project=project)
                .exclude(id=managed_project.id)
                .first()
            )

            if existing_managed:
                return Response(
                    {
                        "error": f"Project is already attached to managed project {existing_managed.identifier}"
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            managed_project.project = project
            managed_project.save(update_fields=["project"])

            logger.info(
                f"Project {project.uuid} attached to ManagedProject {managed_project.identifier} "
                f"by user {request.user}"
            )

            return Response(
                {"message": "Project attached successfully"}, status=status.HTTP_200_OK
            )

        except structure_models.Project.DoesNotExist:
            return Response(
                {"error": "Project not found"}, status=status.HTTP_404_NOT_FOUND
            )

    @extend_schema(
        request=None,
        parameters=[
            OpenApiParameter(
                name="identifier",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="The identifier of the managed project",
            ),
            OpenApiParameter(
                name="destination",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="The destination of the managed project",
            ),
        ],
        responses=None,
        description="Detach the project from this managed project",
    )
    @action(
        detail=False,
        methods=["post"],
        url_path=r"(?P<identifier>[^/.]+)/(?P<destination>[^/.]+)/detach",
    )
    def detach(self, request, identifier=None, destination=None, **kwargs):
        managed_project: models.ManagedProject = self.get_object()

        if not managed_project.project:
            return Response(
                {"error": "No project is currently attached"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        old_project = managed_project.project
        managed_project.project = None
        managed_project.save(update_fields=["project"])

        # We will need to approve any further changes to this managed project
        managed_project.set_needs_approval(True)

        logger.info(
            f"Project {old_project.uuid} detached from ManagedProject {managed_project.identifier} "
            f"by user {request.user}"
        )

        return Response(
            {"message": "Project detached successfully"}, status=status.HTTP_200_OK
        )


class ProjectAccountingSummaryViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only endpoint returning accounting summaries for projects.

    Each summary contains project start/end dates, total lifetime credits,
    total historical spend (excluding the current month), and current month spend.
    Data is derived from invoice items and project credits via get_project_spend_info.

    Staff and support users see all projects. Regular users see only projects they
    have a membership role in (directly or via their organisation/customer).

    Filterable by:
      - project_uuid: return summary for a single project
      - customer_uuid: return summaries for all projects in an organisation
    """

    queryset = structure_models.Project.objects.none()
    serializer_class = serializers.ProjectAccountingSummarySerializer
    permission_classes = (permissions.IsAuthenticated,)
    filter_backends = (DjangoFilterBackend,)
    filterset_class = filters.ProjectAccountingSummaryFilter
    lookup_field = "uuid"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return structure_models.Project.objects.none()
        from waldur_core.structure.managers import get_visible_projects

        user = self.request.user
        qs = structure_models.Project.objects.all().select_related("customer")
        if user.is_staff or user.is_support:
            return qs
        accessible_project_ids = list(get_visible_projects(user))
        return qs.filter(id__in=accessible_project_ids)


class UnmanagedProjectViewSet(structure_views.ProjectViewSet):
    """
    ViewSet that only matches Projects that do not have an associated ManagedProject.
    """

    def get_queryset(self):
        base_queryset = super().get_queryset()

        managed_project_ids = models.ManagedProject.objects.filter(
            project__isnull=False
        ).values_list("project_id", flat=True)

        unmanaged_queryset = base_queryset.exclude(id__in=managed_project_ids)

        return unmanaged_queryset
