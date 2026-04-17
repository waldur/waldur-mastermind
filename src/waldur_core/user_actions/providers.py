import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from django.contrib.auth import get_user_model

User = get_user_model()
logger = logging.getLogger(__name__)


class ActionCategory(StrEnum):
    """Semantic categories for corrective actions"""

    VIEW = "view"
    APPROVE = "approve"
    REJECT = "reject"
    EXTEND = "extend"
    TERMINATE = "terminate"
    BACKUP = "backup"
    MIGRATE = "migrate"
    CONTACT = "contact"
    ESCALATE = "escalate"
    CONFIGURE = "configure"
    REPAIR = "repair"
    MONITOR = "monitor"


class ActionSeverity(StrEnum):
    """Severity levels for risk assessment"""

    SAFE = "safe"  # No risk (view, contact)
    LOW = "low"  # Minimal risk (approve, extend)
    MEDIUM = "medium"  # Some risk (backup, configure)
    HIGH = "high"  # High risk (reject, terminate)
    CRITICAL = "critical"  # Irreversible (delete, destroy)


@dataclass
class CorrectiveAction:
    """Represents a corrective action a user can take"""

    label: str
    category: ActionCategory
    severity: ActionSeverity = ActionSeverity.SAFE
    method: str = "GET"  # HTTP method
    api_endpoint: bool = False  # Whether this is an API call vs navigation
    confirmation_required: bool = False  # Whether to show confirmation
    permissions_required: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)  # Additional semantic data

    # Frontend routing support
    route_name: str = (
        None  # UI-Router state name (e.g., 'marketplace-resource-details')
    )
    route_params: dict[str, Any] = field(default_factory=dict)  # Route parameters


class BaseActionProvider(ABC):
    """Base class for action providers"""

    action_type: str = None
    display_name: str = None

    @abstractmethod
    def get_actions_for_user(self, user: User) -> list[dict[str, Any]]:
        """Return list of actions for the given user"""
        pass

    @abstractmethod
    def get_affected_users(self) -> list[User]:
        """Return users who might have actions of this type"""
        pass

    def get_corrective_actions(self, user: User, obj) -> list[CorrectiveAction]:
        """Return available corrective actions for this object"""
        return []

    def execute_action(
        self, user: User, action: CorrectiveAction, obj, request=None, user_action=None
    ) -> dict[str, Any] | None:
        """
        Execute a corrective action.
        Return a dict with result metadata or None if not handled.
        """
        return None

    def can_user_perform_action(
        self, user: User, obj, action: CorrectiveAction
    ) -> bool:
        """Check if user has permission to perform the corrective action"""
        if not action.permissions_required:
            return True

        # Check user permissions (implement based on your permission system)
        return self._check_user_permissions(user, obj, action.permissions_required)

    def _check_user_permissions(
        self, user: User, obj, required_permissions: list[str]
    ) -> bool:
        """Override in subclasses for specific permission logic"""
        # Default implementation - can be overridden by specific providers
        return True

    def get_urgency(self, obj, days_remaining: int = None) -> str:
        """Determine urgency based on due date or other factors"""
        if days_remaining is not None:
            if days_remaining <= 7:
                return "high"
            elif days_remaining <= 14:
                return "medium"
        return "low"

    def should_create_action(self, user: User, obj) -> bool:
        """Override to add conditions for action creation"""
        return True


# Registry for providers
_providers = {}


def register_provider(provider_class: BaseActionProvider):
    """Register an action provider"""
    if not hasattr(provider_class, "action_type") or not provider_class.action_type:
        raise ValueError(
            f"Provider {provider_class} must have an action_type attribute"
        )

    provider = provider_class()
    _providers[provider.action_type] = provider


def get_provider(action_type: str) -> BaseActionProvider | None:
    """Get provider by action type"""
    return _providers.get(action_type)


def get_all_providers() -> dict[str, BaseActionProvider]:
    """Get all registered providers"""
    return _providers.copy()


def clear_providers():
    """Clear all providers (mainly for testing)"""
    global _providers
    _providers = {}
