import logging

from django.db.models import Q
from django.http import Http404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import filters as rf_filters
from rest_framework import permissions, response, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
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
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.permissions import IsAdminOrOwner, _has_owner_access

from . import config, executors, filters, models, serializers, tasks, utils

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

    @extend_schema(
        request=serializers.AllocationSetLimitsSerializer,
        responses={status.HTTP_202_ACCEPTED: None},
        description="Set limits for allocation",
    )
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

    @extend_schema(
        request=serializers.AllocationSetLimitsSerializer,
        responses={status.HTTP_202_ACCEPTED: None},
        description="Set limits for allocation",
    )
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
    queryset = models.RemoteAssociation.objects.all().order_by("id")
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

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.UserInfoSerializer},
        description="Retrieve UserInfo for current user",
    )
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

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.UserInfoSerializer},
        description="Set shortname for user",
    )
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

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.ProjectInfoSerializer},
        description="Set shortname for project",
    )
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

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.ProjectInfoSerializer},
        description="Set allowed destinations for project",
    )
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

        details = project.get_details()
        earliest_approve = details.earliest_approve
        if earliest_approve is not None and timezone.now() < earliest_approve:
            return Response(
                {
                    "detail": _(
                        f"This project cannot be approved until {earliest_approve.isoformat()}."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        project.approve(request.user, comment)

        models.ManagedProjectAuditEntry.record(
            project,
            models.ManagedProjectAuditEventType.APPROVED,
            performed_by=request.user,
            note=comment or "",
            new_details=project.details,
        )

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
        project.notify_rejected()

        # notify project admins and managers about the rejection
        tasks.notify_users_about_rejected_allocation.delay(
            core_utils.serialize_instance(project)
        )

        models.ManagedProjectAuditEntry.record(
            project,
            models.ManagedProjectAuditEventType.REJECTED,
            performed_by=request.user,
            note=comment or "",
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

        # Record audit entry and notify BEFORE deletion so the FK/fields are still valid
        models.ManagedProjectAuditEntry.record(
            project,
            models.ManagedProjectAuditEventType.DELETED,
            performed_by=request.user,
            note=f"Deleted by {request.user}",
        )

        project.notify_removed()
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

            managed_project.set_project(project)
            managed_project.notify_changed()

            logger.info(
                f"Project {project.uuid} attached to ManagedProject {managed_project.identifier} "
                f"by user {request.user}"
            )

            models.ManagedProjectAuditEntry.record(
                managed_project,
                models.ManagedProjectAuditEventType.PROJECT_ATTACHED,
                performed_by=request.user,
                note=f"Attached project {project_uuid}",
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
        managed_project.set_project(None)
        managed_project.notify_changed()

        # We will need to approve any further changes to this managed project
        managed_project.set_needs_approval(True)

        logger.info(
            f"Project {old_project.uuid} detached from ManagedProject {managed_project.identifier} "
            f"by user {request.user}"
        )

        models.ManagedProjectAuditEntry.record(
            managed_project,
            models.ManagedProjectAuditEventType.PROJECT_DETACHED,
            performed_by=request.user,
        )

        return Response(
            {"message": "Project detached successfully"}, status=status.HTTP_200_OK
        )

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
        request=serializers.AddManagedProjectNoteSerializer,
        responses=serializers.ManagedProjectSerializer,
        description="Append a note to the managed project. Author and timestamp are set automatically.",
    )
    @action(
        detail=False,
        methods=["post"],
        url_path=r"(?P<identifier>[^/]+)/(?P<destination>[^/]+)/add-note",
    )
    def add_note(self, request, identifier=None, destination=None, **kwargs):
        managed_project: models.ManagedProject = self.get_object()
        serializer = serializers.AddManagedProjectNoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        import openportal

        details = managed_project.get_details()
        details.add_note(
            openportal.Note(
                request.user.full_name or request.user.email,
                serializer.validated_data["text"],
            )
        )
        managed_project.set_details(details)
        managed_project.notify_changed()

        logger.info(
            f"Note added to ManagedProject {managed_project.identifier} by user {request.user}"
        )

        models.ManagedProjectAuditEntry.record(
            managed_project,
            models.ManagedProjectAuditEventType.NOTE_ADDED,
            performed_by=request.user,
            note=serializer.validated_data["text"],
        )

        return Response(
            serializers.ManagedProjectSerializer(
                managed_project, context={"request": request}
            ).data
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


class RemoteProjectViewSet(core_views.ActionsViewSet):
    """
    RemoteProject API.

    List / retrieve: any authenticated user who has access to
    current_project.

    Write actions (add_note, set_earliest_approve,
    set_membership_control, set_allowed_domains, set_links): staff,
    support, or CustomerOwner of the organisation.

    Sensitive fields in the serializer (raw AwardDetails JSON, notes,
    earliest_approve) are filtered to privileged users by the
    serializer itself.
    """

    serializer_class = serializers.RemoteProjectSerializer
    filterset_class = filters.RemoteProjectFilter
    filter_backends = [DjangoFilterBackend, ShortOrderingFilter]
    lookup_field = "uuid"
    disabled_actions = [
        "create",
        "update",
        "partial_update",
        "destroy",
    ]
    ordering_fields = ("created", "state", "identifier", "destination")

    queryset = models.RemoteProject.objects.none()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return models.RemoteProject.objects.none()
        user = self.request.user
        if user.is_staff or user.is_support:
            return models.RemoteProject.objects.all().order_by("-created")
        accessible_projects = filter_queryset_for_user(
            structure_models.Project.objects.all(), user
        )
        return models.RemoteProject.objects.filter(
            current_project__in=accessible_projects
        ).order_by("-created")

    def get_serializer_class(self):
        action_map = {
            "add_note": serializers.AddNoteSerializer,
            "set_earliest_approve": (serializers.SetEarliestApproveSerializer),
            "set_membership_control": (serializers.SetMembershipControlSerializer),
            "set_allowed_domains": (serializers.SetAllowedDomainsSerializer),
            "set_links": serializers.SetLinksSerializer,
        }
        return action_map.get(self.action, serializers.RemoteProjectSerializer)

    def _check_write_permission(self, request, remote_project):
        """
        Raise PermissionDenied unless the user is staff, support, or
        CustomerOwner of the organisation that owns current_project.
        """
        user = request.user
        if user.is_staff or user.is_support:
            return
        if remote_project.current_project is None:
            raise PermissionDenied(
                "Cannot write: remote project has no current project."
            )
        customer = remote_project.current_project.customer
        from waldur_core.permissions.fixtures import CustomerRole

        if not customer.has_user(user, CustomerRole.OWNER):
            raise PermissionDenied("Organisation owner access required.")

    def _trigger_update(self, remote_project):
        """
        Schedule an update_award for the current RemoteAllocation, if
        one exists.
        """
        if remote_project.remote_allocation is not None:
            project = remote_project.current_project
            if project is not None:
                from waldur_core.core import utils as core_utils

                tasks.update_remote_project.delay(
                    core_utils.serialize_instance(project)
                )

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.RemoteProjectSerializer},
        request=serializers.AddNoteSerializer,
        description="Add note to remote project",
    )
    @action(detail=True, methods=["post"], url_path="add-note")
    def add_note(self, request, uuid=None):
        """
        Append a timestamped note to the award.  The note is merged
        into AwardDetails and sent to the remote portal on the next
        update.
        """
        remote_project = self.get_object()
        self._check_write_permission(request, remote_project)
        remote_project.ensure_not_erred()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        author = request.user.full_name or request.user.email
        text = serializer.validated_data["text"]

        note = {
            "timestamp": timezone.now().isoformat(),
            "author": author,
            "text": text,
        }
        notes = list(remote_project.notes or [])
        notes.append(note)
        remote_project.notes = notes
        remote_project.save(update_fields=["notes", "modified"])

        models.RemoteProjectAuditEntry.objects.create(
            remote_project=remote_project,
            event_type=(models.RemoteProjectAuditEventType.AWARD_UPDATED),
            performed_by=request.user,
            note=f"Note added by {author}: {text[:120]}",
        )
        self._trigger_update(remote_project)

        return Response(
            serializers.RemoteProjectSerializer(
                remote_project, context={"request": request}
            ).data
        )

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.RemoteProjectSerializer},
        request=serializers.SetEarliestApproveSerializer,
        description="Set earliest approve date for remote project",
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="set-earliest-approve",
    )
    def set_earliest_approve(self, request, uuid=None):
        """
        Set or clear the earliest time the remote portal may approve
        this award.  Pass null to clear.
        """
        remote_project = self.get_object()
        self._check_write_permission(request, remote_project)
        remote_project.ensure_not_erred()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        remote_project.earliest_approve = serializer.validated_data["earliest_approve"]
        remote_project.save(update_fields=["earliest_approve", "modified"])

        models.RemoteProjectAuditEntry.objects.create(
            remote_project=remote_project,
            event_type=(models.RemoteProjectAuditEventType.AWARD_UPDATED),
            performed_by=request.user,
            note=(f"earliest_approve set to {remote_project.earliest_approve}"),
        )
        self._trigger_update(remote_project)

        return Response(
            serializers.RemoteProjectSerializer(
                remote_project, context={"request": request}
            ).data
        )

    @extend_schema(
        responses={status.HTTP_202_ACCEPTED: None},
        request=serializers.SetMembershipControlSerializer,
        description="Set membership control for remote project",
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="set-membership-control",
    )
    def set_membership_control(self, request, uuid=None):
        """
        Queue a membership control transition.  The actual work (including any
        remote portal sync) runs in a background task to avoid blocking the API.
        """
        remote_project = self.get_object()
        self._check_write_permission(request, remote_project)
        remote_project.ensure_not_erred()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        tasks.apply_membership_control.delay(  # type: ignore[attr-defined]
            core_utils.serialize_instance(remote_project),
            new_control=serializer.validated_data["membership_control"],
            performed_by_id=request.user.id,
        )

        return Response(status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.RemoteProjectSerializer},
        request=serializers.SetAllowedDomainsSerializer,
        description="Set allowed domains for remote project",
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="set-allowed-domains",
    )
    def set_allowed_domains(self, request, uuid=None):
        """
        Replace the list of allowed email domain patterns.  Pass null to
        remove all restrictions; an empty list means that no address is
        allowed to join.
        """
        remote_project = self.get_object()
        self._check_write_permission(request, remote_project)
        remote_project.ensure_not_erred()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        import openportal

        for domain in serializer.validated_data["allowed_domains"] or []:
            if "@" not in domain and utils.is_likely_personal_email_address(domain):
                raise ValidationError(
                    {
                        "allowed_domains": f"'{domain}' looks like a personal email address domain. "
                        "Personal email addresses must be added one by one, not as domain patterns."
                    }
                )
            try:
                openportal.DomainPattern(domain)
            except Exception as e:
                raise ValidationError({"allowed_domains": str(e)}) from e

        remote_project.allowed_domains = serializer.validated_data["allowed_domains"]
        remote_project.save(update_fields=["allowed_domains", "modified"])

        models.RemoteProjectAuditEntry.objects.create(
            remote_project=remote_project,
            event_type=(models.RemoteProjectAuditEventType.AWARD_UPDATED),
            performed_by=request.user,
            note=(f"allowed_domains set to {remote_project.allowed_domains}"),
        )
        self._trigger_update(remote_project)

        return Response(
            serializers.RemoteProjectSerializer(
                remote_project, context={"request": request}
            ).data
        )

    @extend_schema(
        responses={status.HTTP_200_OK: serializers.RemoteProjectSerializer},
        request=serializers.SetLinksSerializer,
        description="Set links for remote project",
    )
    @action(detail=True, methods=["post"], url_path="set-links")
    def set_links(self, request, uuid=None):
        """
        Set or clear any combination of the four award links in one
        call.  Fields not present in the request are left unchanged.
        Pass null to clear a specific link.
        """
        remote_project = self.get_object()
        self._check_write_permission(request, remote_project)
        remote_project.ensure_not_erred()

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        changed = []

        if "award" in data:
            remote_project.link_award = data["award"]
            changed.append("link_award")
        if "call" in data:
            remote_project.link_call = data["call"]
            changed.append("link_call")
        if "project_link" in data:
            remote_project.link_project = data["project_link"]
            changed.append("link_project")
        if "renewal" in data:
            remote_project.link_renewal = data["renewal"]
            changed.append("link_renewal")

        if changed:
            remote_project.save(update_fields=changed + ["modified"])
            models.RemoteProjectAuditEntry.objects.create(
                remote_project=remote_project,
                event_type=(models.RemoteProjectAuditEventType.AWARD_UPDATED),
                performed_by=request.user,
                note=f"Links updated: {', '.join(changed)}",
            )
            self._trigger_update(remote_project)

        return Response(
            serializers.RemoteProjectSerializer(
                remote_project, context={"request": request}
            ).data
        )

    @extend_schema(
        request=None,
        responses={status.HTTP_200_OK: serializers.RemoteProjectSerializer},
        description="Approve remote project now",
    )
    @action(detail=True, methods=["post"], url_path="approve-now")
    def approve_now(self, request, uuid=None):
        """
        Remove the earliest_approve gate so the remote portal may
        approve this award immediately.
        """
        remote_project = self.get_object()
        self._check_write_permission(request, remote_project)
        remote_project.ensure_not_erred()

        remote_project.earliest_approve = None
        remote_project.save(update_fields=["earliest_approve", "modified"])

        models.RemoteProjectAuditEntry.objects.create(
            remote_project=remote_project,
            event_type=(models.RemoteProjectAuditEventType.AWARD_UPDATED),
            performed_by=request.user,
            note="earliest_approve cleared — award may be approved now",
        )
        self._trigger_update(remote_project)

        return Response(
            serializers.RemoteProjectSerializer(
                remote_project, context={"request": request}
            ).data
        )

    @extend_schema(
        request=None,
        responses={status.HTTP_200_OK: serializers.RemoteProjectSerializer},
        description="Hold remote project indefinitely",
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="hold-indefinitely",
    )
    def hold_indefinitely(self, request, uuid=None):
        """
        Prevent the remote portal from approving this award for 100
        years, effectively placing it on indefinite hold.  Use
        approve_now to release the hold.
        """
        from datetime import timedelta

        remote_project = self.get_object()
        self._check_write_permission(request, remote_project)
        remote_project.ensure_not_erred()

        remote_project.earliest_approve = timezone.now() + timedelta(days=36500)
        remote_project.save(update_fields=["earliest_approve", "modified"])

        models.RemoteProjectAuditEntry.objects.create(
            remote_project=remote_project,
            event_type=(models.RemoteProjectAuditEventType.AWARD_UPDATED),
            performed_by=request.user,
            note=(
                "Award placed on indefinite hold "
                "(earliest_approve set ~100 years ahead)"
            ),
        )
        self._trigger_update(remote_project)

        return Response(
            serializers.RemoteProjectSerializer(
                remote_project, context={"request": request}
            ).data
        )

    @extend_schema(
        request=None,
        responses={status.HTTP_200_OK: serializers.RemoteProjectSerializer},
        description="Reset remote project to pending",
    )
    @action(detail=True, methods=["post"], url_path="reset-to-pending")
    def reset_to_pending(self, request, uuid=None):
        """
        Clear a rejection error and return the award to PENDING state.

        Resets RemoteProject.state to PENDING and RemoteAllocation.state
        to OK so that subsequent changes or a manual resend can proceed.
        Does not send anything to the remote portal.
        """
        remote_project = self.get_object()
        self._check_write_permission(request, remote_project)

        remote_project.reset_to_pending()

        return Response(
            serializers.RemoteProjectSerializer(
                remote_project, context={"request": request}
            ).data
        )

    @extend_schema(
        request=None,
        responses={status.HTTP_200_OK: serializers.RemoteProjectSerializer},
        description="Resend remote project request",
    )
    @action(detail=True, methods=["post"], url_path="resend-request")
    def resend_request(self, request, uuid=None):
        """
        Reset to PENDING and immediately resend the current award details
        to the remote portal.

        Equivalent to reset_to_pending followed by triggering an update.
        Use this when the operator wants to re-submit without making any
        other changes first.
        """
        remote_project = self.get_object()
        self._check_write_permission(request, remote_project)

        remote_project.reset_to_pending()
        self._trigger_update(remote_project)

        return Response(
            serializers.RemoteProjectSerializer(
                remote_project, context={"request": request}
            ).data
        )

    @extend_schema(
        responses={status.HTTP_200_OK: OpenApiTypes.OBJECT},
        description="Get total usage for remote project",
    )
    @action(detail=True, methods=["get"], url_path="total-usage")
    def total_usage(self, request, uuid=None):
        """
        Return the total usage hours for this remote project, summed
        across all cached monthly usage reports.

        Returns 0.0 if the project has no remote identifier yet or no
        usage reports have been cached.
        """
        remote_project = self.get_object()
        if not remote_project.current_project or not remote_project.destination:
            return Response({"total_hours": 0.0})

        # we need to build the local identifier for the project from
        # its shortname and the portal
        try:
            project_identifier = utils.get_local_project_identifier(
                remote_project.current_project
            )
        except Exception as e:
            logger.warning(
                f"total_usage: could not get local identifier for project "
                f"{remote_project.current_project!r}: {e}"
            )
            return Response({"total_hours": 0.0})

        reports = models.CachedProjectUsageReport.objects.filter(
            project_identifier=project_identifier,
            resource=remote_project.destination,
        )
        total_hours = sum(float(r.get_report().total_usage.hours) for r in reports)
        return Response({"total_hours": total_hours})


class RemoteProjectAuditEntryViewSet(core_views.ActionsViewSet):
    """
    Read-only audit log for RemoteProject.

    Accessible to any user who can see the associated RemoteProject.
    """

    serializer_class = serializers.RemoteProjectAuditEntrySerializer
    filterset_class = filters.RemoteProjectAuditEntryFilter
    filter_backends = [DjangoFilterBackend, ShortOrderingFilter]
    disabled_actions = [
        "create",
        "update",
        "partial_update",
        "destroy",
    ]
    queryset = models.RemoteProjectAuditEntry.objects.none()
    ordering_fields = ("timestamp", "event_type")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return models.RemoteProjectAuditEntry.objects.none()
        user = self.request.user
        if user.is_staff or user.is_support:
            return models.RemoteProjectAuditEntry.objects.all().order_by("-timestamp")
        accessible_projects = filter_queryset_for_user(
            structure_models.Project.objects.all(), user
        )
        return models.RemoteProjectAuditEntry.objects.filter(
            remote_project__current_project__in=accessible_projects
        ).order_by("-timestamp")


class RemoteProjectAllocationEntryViewSet(core_views.ActionsViewSet):
    """
    Read-only allocation ledger for RemoteProject.

    Accessible to any user who can see the associated RemoteProject.
    """

    serializer_class = serializers.RemoteProjectAllocationEntrySerializer
    filterset_class = filters.RemoteProjectAllocationEntryFilter
    filter_backends = [DjangoFilterBackend, ShortOrderingFilter]
    disabled_actions = [
        "create",
        "update",
        "partial_update",
        "destroy",
    ]
    queryset = models.RemoteProjectAllocationEntry.objects.none()
    ordering_fields = ("submitted_at", "allocation")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return models.RemoteProjectAllocationEntry.objects.none()
        user = self.request.user
        if user.is_staff or user.is_support:
            return models.RemoteProjectAllocationEntry.objects.all().order_by(
                "-submitted_at"
            )
        accessible_projects = filter_queryset_for_user(
            structure_models.Project.objects.all(), user
        )
        return models.RemoteProjectAllocationEntry.objects.filter(
            remote_project__current_project__in=accessible_projects
        ).order_by("-submitted_at")


class ManagedProjectAuditEntryViewSet(core_views.ActionsViewSet):
    """
    Read-only audit log for ManagedProject.

    Accessible to staff, support, and organisation owners only.
    Project members cannot see these entries as they may contain privileged
    information.
    """

    serializer_class = serializers.ManagedProjectAuditEntrySerializer
    filterset_class = filters.ManagedProjectAuditEntryFilter
    filter_backends = [DjangoFilterBackend, ShortOrderingFilter]
    disabled_actions = [
        "create",
        "update",
        "partial_update",
        "destroy",
    ]
    ordering_fields = ("timestamp", "event_type")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return models.ManagedProjectAuditEntry.objects.none()

        from waldur_core.permissions.enums import RoleEnum
        from waldur_core.structure.managers import get_connected_customers

        user = self.request.user

        if user.is_staff or user.is_support:
            return models.ManagedProjectAuditEntry.objects.all().order_by("-timestamp")

        # Restrict to organisations where the user is an owner
        owned_customer_ids = get_connected_customers(user, role=RoleEnum.CUSTOMER_OWNER)
        accessible_managed = models.ManagedProject.objects.filter(
            project__customer_id__in=owned_customer_ids
        )

        return models.ManagedProjectAuditEntry.objects.filter(
            Q(managed_project__in=accessible_managed)
            | Q(
                managed_project__isnull=True,
                identifier__in=accessible_managed.values("identifier"),
                destination__in=accessible_managed.values("destination"),
            )
        ).order_by("-timestamp")
