import os

from django.http import Http404, HttpResponse
from django.utils.http import content_disposition_header
from drf_spectacular.utils import extend_schema
from rest_framework.generics import GenericAPIView

# Import checklist models to avoid mid-function imports
from waldur_core.checklist.models import Answer
from waldur_core.core.models import User

from . import models
from .utils import MARKDOWN_IMAGE_PREFIX


def check_file_permissions(file: models.File, user: User):
    """
    Check if user has permissions to access the file.
    Handles both support ticket attachments and checklist files.
    """
    # Check if file is a support ticket attachment
    from waldur_mastermind.support.models import Attachment

    try:
        attachment = Attachment.objects.get(file=file.name)
        if user.is_anonymous:
            raise Http404
        if (
            not Attachment.objects.filter_for_user(user)
            .filter(id=attachment.id)
            .exists()
        ):
            raise Http404
        return
    except Attachment.DoesNotExist:
        pass

    # Check if file is a checklist file
    if file.name.startswith("checklist_files/"):
        _check_checklist_file_permissions(file, user)


def _check_checklist_file_permissions(file: models.File, user: User):
    """
    Check permissions for checklist files by leveraging existing checklist completion access patterns.

    The logic is simple: if a user can access a checklist completion (via existing ViewSet permissions),
    they can access the files referenced in that completion's answers.
    """
    if user.is_anonymous:
        raise Http404

    # Find answers that reference this file
    # Need to handle both single file and multiple files answer formats
    file_uuid_str = str(file.uuid)

    # Check for single file format: {"stored_file_id": "uuid", ...}
    single_file_answers = Answer.objects.filter(
        answer_data__stored_file_id=file_uuid_str
    )

    # Check for multiple files format: [{"stored_file_id": "uuid", ...}, ...]
    # Use a more robust approach for multiple files by checking JSON contains
    multiple_files_answers = Answer.objects.extra(
        where=["answer_data::text LIKE %s"],
        params=[f'%"stored_file_id": "{file_uuid_str}"%'],
    )

    # Combine both querysets
    answers = single_file_answers.union(multiple_files_answers)

    if not answers.exists():
        # File not found in any checklist answers
        raise Http404

    # Check if user has access to any completion that references this file
    for answer in answers:
        if not answer.completion:
            continue

        completion = answer.completion

        # Use the same permission checking logic as the checklist system
        if _user_can_access_completion(user, completion):
            return

    # Deny access if user cannot access any completion that references this file
    raise Http404


def _user_can_access_completion(user: User, completion) -> bool:
    """
    Check if user can access a checklist completion using the same logic as checklist ViewSets.
    This ensures consistency with how checklist completion data is accessed throughout the system.
    """
    # The user who created any answer in this completion can access it
    if completion.answers.filter(user=user).exists():
        return True

    # Staff and superuser can access all completions (reviewer access)
    if user.is_staff or user.is_superuser:
        return True

    # If completion has a scope, check if user has access to that scope
    if completion.scope:
        return _user_can_access_scope(user, completion.scope, completion.checklist)

    return False


def _user_can_access_scope(user: User, scope_obj, checklist=None) -> bool:
    """
    Check if user has access to the scope object by delegating to the
    existing permission system for that object type.

    Each scope type (Project, Proposal, etc.) has its own ViewSet with
    proper permission checking. We leverage those existing patterns.
    """
    try:
        # Use Django's permission framework and established Waldur patterns
        # This delegates to the same permission logic used by ViewSets

        # For Projects - use the established structure permission patterns
        if hasattr(scope_obj, "_meta") and scope_obj._meta.model_name == "project":
            # Projects use role-based permissions through the PermissionsMixin
            if hasattr(scope_obj, "has_user"):
                return scope_obj.has_user(user)

        # For Proposals - delegate to proposal permission patterns
        elif hasattr(scope_obj, "_meta") and scope_obj._meta.model_name == "proposal":
            # For compliance checklists, only call managers can access files
            if checklist and hasattr(checklist, "checklist_type"):
                from waldur_core.checklist.enums import ChecklistTypes

                if checklist.checklist_type == ChecklistTypes.PROPOSAL_COMPLIANCE:
                    # Only call managers can access compliance checklist files
                    from waldur_core.permissions.fixtures import CallRole

                    # Access call through round: proposal.round.call
                    if hasattr(scope_obj, "round") and hasattr(scope_obj.round, "call"):
                        call_obj = scope_obj.round.call
                        # Check UserRole table for call manager permissions
                        from waldur_core.permissions.models import UserRole

                        return UserRole.objects.filter(
                            user=user,
                            role=CallRole.MANAGER,
                            scope=call_obj,
                            is_active=True,
                        ).exists()
                    return False

            # For other proposal checklists, check via call/round permissions
            if (
                hasattr(scope_obj, "round")
                and hasattr(scope_obj.round, "call")
                and hasattr(scope_obj.round.call, "permissions")
            ):
                return scope_obj.round.call.permissions.filter(user=user).exists()
            # Also check if user created the proposal
            if hasattr(scope_obj, "created_by"):
                return scope_obj.created_by == user

        # For Marketplace offerings/resources - delegate to marketplace patterns
        elif hasattr(scope_obj, "_meta") and "marketplace" in scope_obj._meta.app_label:
            # Marketplace items typically check via customer/project access
            if hasattr(scope_obj, "project"):
                return _user_can_access_scope(user, scope_obj.project, checklist)
            elif hasattr(scope_obj, "customer"):
                if hasattr(scope_obj.customer, "has_user"):
                    return scope_obj.customer.has_user(user)

        # For other scope types, check for common Waldur permission patterns
        else:
            # Many Waldur models follow the customer -> project hierarchy
            if hasattr(scope_obj, "project"):
                return _user_can_access_scope(user, scope_obj.project, checklist)
            elif hasattr(scope_obj, "customer"):
                if hasattr(scope_obj.customer, "has_user"):
                    return scope_obj.customer.has_user(user)
            # Check if object has direct user relationship
            elif hasattr(scope_obj, "user"):
                return scope_obj.user == user
            elif hasattr(scope_obj, "created_by"):
                return scope_obj.created_by == user

    except Exception:
        # If permission checking fails, err on the side of security
        pass

    return False


def _serve_inline(file: models.File) -> bool:
    if file.name.startswith(MARKDOWN_IMAGE_PREFIX):
        return True
    return bool(file.mime_type and file.mime_type.startswith("image/"))


class MediaView(GenericAPIView):
    permission_classes = ()
    filter_backends = ()

    @extend_schema(
        description="Get media file",
        responses={200: bytes},
    )
    def get(self, request, uuid):
        try:
            file = models.File.objects.get(uuid=uuid)
        except models.File.DoesNotExist:
            raise Http404
        check_file_permissions(file, request.user)
        filename = os.path.split(file.name)[-1]
        response = HttpResponse(file.content)
        response.headers["Content-Length"] = file.size
        response.headers["Content-Type"] = file.mime_type or "application/octet-stream"
        response.headers["Content-Disposition"] = content_disposition_header(
            as_attachment=not _serve_inline(file),
            filename=filename,
        )
        return response
