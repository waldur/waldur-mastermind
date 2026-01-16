import logging
from datetime import timedelta
from typing import Any

from constance import config
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils import timezone

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import has_permission
from waldur_core.structure.models import Customer, Project
from waldur_core.user_actions.providers import (
    ActionCategory,
    ActionSeverity,
    BaseActionProvider,
    CorrectiveAction,
    register_provider,
)

from . import models
from .enums import OrderStates

User = get_user_model()
logger = logging.getLogger(__name__)


class PendingOrderProvider(BaseActionProvider):
    """Provider for pending marketplace orders"""

    action_type = "pending_order"
    display_name = "Pending Orders"

    def get_actions_for_user(self, user: User) -> list[dict[str, Any]]:
        """Find orders pending approval for configurable hours (default 24)"""
        pending_hours = config.USER_ACTIONS_PENDING_ORDER_HOURS
        cutoff = timezone.now() - timedelta(hours=pending_hours)

        # Find orders pending consumer approval where user has APPROVE_ORDER permission
        project_ct = ContentType.objects.get_for_model(Project)
        user_projects = UserRole.objects.filter(
            user=user,
            content_type=project_ct,
            is_active=True,
            role__permissions__permission=PermissionEnum.APPROVE_ORDER,
        ).values_list("object_id", flat=True)

        orders = models.Order.objects.filter(
            state=models.OrderStates.PENDING_CONSUMER,
            created__lt=cutoff,
            project_id__in=user_projects,
        ).distinct()

        actions = []
        for order in orders:
            days_pending = (timezone.now() - order.created).days
            urgency_days = max(
                0, 7 - days_pending
            )  # Gets more urgent as it approaches 7 days

            actions.append(
                {
                    "title": f"Approve pending order: {order.offering.name}",
                    "description": f"Order has been pending approval for {days_pending} days.",
                    "urgency": self.get_urgency(order, days_remaining=urgency_days),
                    "due_date": order.created + timedelta(days=7),
                    "related_object": order,
                    # Use specific typed fields instead of metadata
                    "route_name": "marketplace-orders.details",
                    "route_params": {"order_uuid": str(order.uuid)},
                    "project_name": order.project.name,
                    "project_uuid": str(order.project.uuid),
                    "organization_name": order.project.customer.name,
                    "organization_uuid": str(order.project.customer.uuid),
                    "offering_name": order.offering.name,
                    "offering_uuid": str(order.offering.uuid),
                    "offering_type": order.offering.type,
                    "resource_name": order.resource.name,
                    "resource_uuid": str(order.resource.uuid),
                    "order_type": order.get_type_display(),
                }
            )

        return actions

    def get_affected_users(self) -> list[User]:
        """Get users who can approve orders (have APPROVE_ORDER permission on projects or customers)"""
        project_ct = ContentType.objects.get_for_model(Project)
        customer_ct = ContentType.objects.get_for_model(Customer)

        # Users with APPROVE_ORDER permission on projects or customers
        user_ids = UserRole.objects.filter(
            content_type__in=[project_ct, customer_ct],
            is_active=True,
            role__permissions__permission=PermissionEnum.APPROVE_ORDER,
        ).values_list("user_id", flat=True)

        return User.objects.filter(id__in=user_ids).distinct()

    def get_corrective_actions(self, user: User, order) -> list[CorrectiveAction]:
        """Return corrective actions for pending orders"""
        actions = []

        # View order details (always available)
        actions.append(
            CorrectiveAction(
                label="View Order Details",
                category=ActionCategory.VIEW,
                severity=ActionSeverity.SAFE,
                route_name="marketplace-orders.details",
                route_params={"order_uuid": str(order.uuid)},
            )
        )

        # Approve order (if user has permission)
        if self._can_approve_order(user, order):
            actions.append(
                CorrectiveAction(
                    label="Approve Order",
                    method="POST",
                    category=ActionCategory.APPROVE,
                    severity=ActionSeverity.LOW,
                    api_endpoint=True,
                    confirmation_required=True,
                    permissions_required=[PermissionEnum.APPROVE_ORDER],
                    metadata={
                        "order_type": order.type,
                        "offering_type": order.offering.type,
                        "estimated_cost": str(order.cost) if order.cost else None,
                    },
                )
            )

        # Reject order (if user has permission)
        if self._can_reject_order(user, order):
            actions.append(
                CorrectiveAction(
                    label="Reject Order",
                    category=ActionCategory.REJECT,
                    severity=ActionSeverity.HIGH,
                    confirmation_required=True,
                    route_name="marketplace-orders.details",
                    route_params={"order_uuid": str(order.uuid), "tab": "reject"},
                    metadata={
                        "order_type": order.type,
                        "customer_contact": (
                            order.created_by.email if order.created_by else None
                        ),
                    },
                )
            )
        return actions

    def _can_approve_order(self, user: User, order) -> bool:
        """Check if user can approve the order"""
        # Check if user has APPROVE_ORDER permission on the project
        return has_permission(user, PermissionEnum.APPROVE_ORDER, order.project)

    def _can_reject_order(self, user: User, order) -> bool:
        """Check if user can reject the order"""
        # Same permissions as approval for now
        return self._can_approve_order(user, order)


class ExpiringResourceProvider(BaseActionProvider):
    """Provider for resources nearing expiration"""

    action_type = "expiring_resource"
    display_name = "Expiring Resources"

    def _get_reminder_schedule(self, offering) -> list[int]:
        """Get reminder schedule for offering, falling back to global default."""
        # Try offering-specific reminders first
        reminders = offering.plugin_options.get("resource_expiration_reminders")
        if reminders and isinstance(reminders, list):
            try:
                return sorted([int(r) for r in reminders], reverse=True)
            except (ValueError, TypeError):
                pass

        # Fall back to global default from constance
        default_reminders = config.USER_ACTIONS_DEFAULT_EXPIRATION_REMINDERS
        if default_reminders and isinstance(default_reminders, list):
            try:
                return sorted([int(r) for r in default_reminders], reverse=True)
            except (ValueError, TypeError):
                pass

        # Ultimate fallback
        return [30, 14, 7, 1]

    def _get_urgency_from_schedule(
        self, days_remaining: int, reminders: list[int]
    ) -> str:
        """Determine urgency based on position in reminder schedule.

        - First ~1/3 of reminders → low
        - Middle ~1/3 of reminders → medium
        - Last ~1/3 of reminders → high
        """
        if not reminders:
            return "low"

        # Find which reminder milestones apply (reminders >= days_remaining)
        # More active reminders = closer to expiration
        active_reminders = [r for r in reminders if r >= days_remaining]
        if not active_reminders:
            return "low"

        # Position based on how many milestones we've entered (0 = first, len-1 = last)
        position = len(active_reminders) - 1
        total = len(reminders)

        # Divide into thirds
        if total <= 2:
            # For very short schedules, last item is high
            if position == total - 1:
                return "high"
            return "low"

        third = total / 3
        if position >= 2 * third:
            return "high"
        elif position >= third:
            return "medium"
        return "low"

    def get_urgency(
        self, obj, days_remaining: int = None, reminders: list[int] = None
    ) -> str:
        """Determine urgency based on days remaining and reminder schedule."""
        if reminders:
            return self._get_urgency_from_schedule(days_remaining, reminders)
        # Fallback for backward compatibility
        if days_remaining is not None:
            if days_remaining < 7:
                return "high"
            elif days_remaining < 14:
                return "medium"
            elif days_remaining < 30:
                return "low"
        return "low"

    def get_actions_for_user(self, user: User) -> list[dict[str, Any]]:
        """Find resources expiring within their offering's reminder schedule."""
        # Find resources where user has access to the project
        project_ct = ContentType.objects.get_for_model(Project)
        user_projects = UserRole.objects.filter(
            user=user,
            content_type=project_ct,
            is_active=True,
        ).values_list("object_id", flat=True)

        if not user_projects:
            return []

        # Optimization: Fetch involved offerings first and group by threshold
        # We cannot use reverse relation (resource__...) because related_name is "+"
        offering_ids = (
            models.Resource.objects.filter(
                project_id__in=user_projects,
                state=models.ResourceStates.OK,
            )
            .values_list("offering_id", flat=True)
            .distinct()
        )

        offerings = models.Offering.objects.filter(
            id__in=offering_ids, components__is_prepaid=True
        )

        # Build offering configuration map
        offering_config = {}  # offering_id -> {'reminders': [...], 'threshold': int}
        threshold_map = {}  # days -> [offering_ids]

        for offering in offerings:
            reminders = self._get_reminder_schedule(offering)
            threshold = max(reminders) if reminders else 30

            offering_config[offering.id] = {
                "reminders": reminders,
                "threshold": threshold,
            }

            if threshold not in threshold_map:
                threshold_map[threshold] = []
            threshold_map[threshold].append(offering.id)

        # Build optimized query
        if not threshold_map:
            return []

        query = Q()
        now = timezone.now()

        for days, off_ids in threshold_map.items():
            cutoff = now + timedelta(days=days)
            query |= Q(offering_id__in=off_ids, end_date__lte=cutoff.date())

        # Get resources that have pending orders (to exclude them)
        pending_order_states = [
            OrderStates.PENDING_START_DATE,
            OrderStates.PENDING_PROJECT,
            OrderStates.PENDING_CONSUMER,
            OrderStates.PENDING_PROVIDER,
            OrderStates.EXECUTING,
        ]
        resources_with_pending_orders = models.Order.objects.filter(
            state__in=pending_order_states,
            resource__isnull=False,
        ).values_list("resource_id", flat=True)

        # Execute optimized query
        resources = (
            models.Resource.objects.filter(
                query,
                end_date__isnull=False,
                end_date__gt=now.date(),
                project_id__in=user_projects,
                state=models.ResourceStates.OK,
            )
            .exclude(id__in=resources_with_pending_orders)
            .select_related("project", "project__customer", "offering")
            .distinct()
        )

        actions = []
        for resource in resources:
            days_remaining = (resource.end_date - now.date()).days

            # Get offering-specific reminder schedule
            off_config = offering_config.get(resource.offering_id, {})
            reminders = off_config.get("reminders", [30, 14, 7, 1])

            # Check if we're within the reminder threshold
            if days_remaining > max(reminders):
                continue

            # Create timezone-aware due_date for the action
            due_date_aware = timezone.make_aware(
                timezone.datetime.combine(
                    resource.end_date, timezone.datetime.min.time()
                )
            )

            # Generate description with next milestone info
            active_reminders = sorted([r for r in reminders if r >= days_remaining])
            active_reminders[0] if active_reminders else days_remaining

            if days_remaining == 0:
                description = "Resource expires today."
            elif days_remaining == 1:
                description = "Resource expires tomorrow."
            else:
                description = f"Resource will expire in {days_remaining} days."

            actions.append(
                {
                    "title": f"Resource expiring: {resource.name}",
                    "description": description,
                    "urgency": self.get_urgency(resource, days_remaining, reminders),
                    "due_date": due_date_aware,
                    "related_object": resource,
                    # Use specific typed fields instead of metadata
                    "route_name": "marketplace-resource-details",
                    "route_params": {"resource_uuid": str(resource.uuid)},
                    "project_name": resource.project.name,
                    "project_uuid": str(resource.project.uuid),
                    "organization_name": resource.project.customer.name,
                    "organization_uuid": str(resource.project.customer.uuid),
                    "offering_name": resource.offering.name,
                    "offering_uuid": str(resource.offering.uuid),
                    "offering_type": resource.offering.type,
                    "resource_name": resource.name,
                    "resource_uuid": str(resource.uuid),
                }
            )

        return actions

    def get_affected_users(self) -> list[User]:
        """Get users who have access to resources with end dates"""
        project_ct = ContentType.objects.get_for_model(Project)
        project_ids_with_expiring_resources = models.Resource.objects.filter(
            state=models.ResourceStates.OK,
            end_date__isnull=False,
            end_date__gt=timezone.now(),
        ).values_list("project_id", flat=True)

        return User.objects.filter(
            userrole__content_type=project_ct,
            userrole__object_id__in=project_ids_with_expiring_resources,
            userrole__is_active=True,
        ).distinct()

    def get_corrective_actions(self, user: User, resource) -> list[CorrectiveAction]:
        """Return corrective actions for expiring resources"""
        actions = []

        # View resource details (always available)
        actions.append(
            CorrectiveAction(
                label="View Resource",
                category=ActionCategory.VIEW,
                severity=ActionSeverity.SAFE,
                route_name="marketplace-resource-details",
                route_params={"resource_uuid": str(resource.uuid)},
            )
        )

        actions.append(
            CorrectiveAction(
                label="Renew Resource",
                category=ActionCategory.EXTEND,
                severity=ActionSeverity.LOW,
                route_name="marketplace-resource-details",
                route_params={
                    "resource_uuid": str(resource.uuid),
                    "tab": "actions",
                },
            )
        )

        # Terminate (API action)
        if self._can_terminate_resource(user, resource):
            actions.append(
                CorrectiveAction(
                    label="Terminate Resource",
                    method="POST",
                    category=ActionCategory.TERMINATE,
                    severity=ActionSeverity.HIGH,
                    api_endpoint=True,
                    confirmation_required=True,
                    metadata={
                        "resource_name": resource.name,
                        "resource_uuid": str(resource.uuid),
                    },
                )
            )

        return actions

    def execute_action(
        self,
        user: User,
        action: CorrectiveAction,
        obj,
        request=None,
        user_action=None,
    ) -> dict[str, Any] | None:
        """Execute termination action (acknowledge expiration)"""
        if action.category == ActionCategory.TERMINATE and user_action:
            # Silence the action to acknowledge the expiration
            # This effectively confirms the user is aware and accepts the resource will expire
            user_action.silence()

            return {
                "action": "completed",
                "message": "Resource expiration acknowledged. Resource will terminate on end date.",
            }

        return None

    def _can_terminate_resource(self, user: User, resource) -> bool:
        """Check if user can terminate the resource"""
        # User needs to be owner or have specific permissions
        # For simplicity, checking if user has permissions on project
        return has_permission(user, PermissionEnum.TERMINATE_RESOURCE, resource.project)


# Register all providers
register_provider(PendingOrderProvider)
register_provider(ExpiringResourceProvider)
