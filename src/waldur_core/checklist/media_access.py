"""Media access rules for files owned by the checklist app.

Lifted verbatim from ``waldur_core.media.views`` when media access moved to a
registry; the rules themselves are unchanged. See
:mod:`waldur_core.media.access`.
"""

from django.db.models import Q

from waldur_core.checklist.enums import ChecklistTypes
from waldur_core.checklist.models import (
    CHECKLIST_FILE_PREFIX,
    Answer,
    ChecklistCompletion,
)
from waldur_core.core.models import User
from waldur_core.media import access
from waldur_core.media import models as media_models
from waldur_core.permissions.fixtures import CallRole
from waldur_core.permissions.models import UserRole


def user_can_access_checklist_file(file: media_models.File, user: User) -> bool:
    """A user who can reach a checklist completion can reach its answers' files."""
    if user.is_anonymous:
        return False

    # Find answers that reference this file
    # Need to handle both single file and multiple files answer formats
    #
    # Must be str(), not .hex: _store_file_content writes str(file_obj.uuid)
    # into answer_data["stored_file_id"], so the stored value is hyphenated.
    # Matching against the unhyphenated .hex form never hits, and the failure
    # is a silent 404 on every checklist download.
    file_uuid_str = str(file.uuid)

    # Two answer shapes reference a file: the single-file format is an object,
    # {"stored_file_id": "uuid", ...}, and the multiple-files format is an
    # array of them. Both are matched with jsonb containment (@>), which the
    # GIN index on answer_data can serve. This previously used a raw
    # answer_data::text LIKE '%...%', which no index can satisfy.
    answer_ids = set(
        Answer.objects.filter(
            Q(answer_data__contains={"stored_file_id": file_uuid_str})
            | Q(answer_data__contains=[{"stored_file_id": file_uuid_str}])
        ).values_list("id", flat=True)
    )

    if not answer_ids:
        # File not found in any checklist answers
        return False

    # Iterate distinct completions rather than answers. Several answers can
    # reference the same file, and checking a completion is ~5 queries, so
    # looping over answers re-ran the identical check once per answer.
    # Filtering completions by their answers also drops the answers whose
    # completion is null, which the previous loop skipped explicitly.
    completions = (
        ChecklistCompletion.objects.filter(answers__id__in=answer_ids)
        .select_related("checklist")
        .prefetch_related("scope")
        .distinct()
    )

    # Check if user has access to any completion that references this file
    for completion in completions:
        if _user_can_access_completion(user, completion):
            return True

    # Deny access if user cannot access any completion that references this file
    return False


def _user_can_access_completion(user: User, completion) -> bool:
    """
    Check if user can access a checklist completion using the same logic as checklist ViewSets.
    This ensures consistency with how checklist completion data is accessed throughout the system.
    """
    # The user who created any answer in this completion can access it
    if completion.answers.filter(user=user).exists():
        return True

    # Staff and support can access all completions (reviewer access). Waldur
    # gates on is_staff / is_support throughout; is_superuser is a Django flag
    # that carries no access meaning here.
    # Note this short-circuits before the scope check below, so it also covers
    # PROPOSAL_COMPLIANCE checklists, which _user_can_access_scope otherwise
    # restricts to call managers.
    if user.is_staff or user.is_support:
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
                if checklist.checklist_type == ChecklistTypes.PROPOSAL_COMPLIANCE:
                    # Only call managers can access compliance checklist files
                    # Access call through round: proposal.round.call
                    if hasattr(scope_obj, "round") and hasattr(scope_obj.round, "call"):
                        call_obj = scope_obj.round.call
                        # Check UserRole table for call manager permissions
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


access.register(CHECKLIST_FILE_PREFIX, user_can_access_checklist_file)
