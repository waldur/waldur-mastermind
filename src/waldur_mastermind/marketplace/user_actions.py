import logging
from datetime import timedelta
from typing import Any

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

User = get_user_model()
logger = logging.getLogger(__name__)


class PendingOrderProvider(BaseActionProvider):
    """Provider for pending marketplace orders"""

    action_type = "pending_order"
    display_name = "Pending Orders"

    def get_actions_for_user(self, user: User) -> list[dict[str, Any]]:
        """Find orders pending approval for more than 24 hours"""
        cutoff = timezone.now() - timedelta(hours=24)

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
                    "project_uuid": order.project.uuid,
                    "organization_name": order.project.customer.name,
                    "organization_uuid": order.project.customer.uuid,
                    "offering_name": order.offering.name,
                    "offering_uuid": order.offering.uuid,
                    "offering_type": order.offering.type,
                    "resource_name": order.resource.name,
                    "resource_uuid": order.resource.uuid,
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

    def get_urgency(self, obj, days_remaining: int = None) -> str:
        """Determine urgency based on days remaining until expiration"""
        if days_remaining is not None:
            if days_remaining < 7:
                return "high"
            elif days_remaining < 14:
                return "medium"
            elif days_remaining < 30:
                return "low"
        return "low"

    def get_actions_for_user(self, user: User) -> list[dict[str, Any]]:
        """Find resources expiring in the configurable timeframe (default 30 days)"""
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

        # Group offerings by threshold
        threshold_map = {}  # days -> [offering_ids]
        default_threshold = 30

        for offering in offerings:
            threshold = offering.plugin_options.get(
                "resource_expiration_threshold", default_threshold
            )
            # Ensure threshold is an integer
            try:
                threshold = int(threshold)
            except (ValueError, TypeError):
                threshold = default_threshold

            if threshold not in threshold_map:
                threshold_map[threshold] = []
            threshold_map[threshold].append(offering.id)

        # Build optimized query
        if not threshold_map:
            return []

        query = Q()
        now = timezone.now()

        for days, offering_ids in threshold_map.items():
            cutoff = now + timedelta(days=days)
            query |= Q(offering_id__in=offering_ids, end_date__lt=cutoff)

        # Execute optimized query
        resources = models.Resource.objects.filter(
            query,
            end_date__isnull=False,
            end_date__gt=now,
            project_id__in=user_projects,
            state=models.ResourceStates.OK,
        ).distinct()

        actions = []
        for resource in resources:
            end_datetime = timezone.datetime.combine(
                resource.end_date, timezone.datetime.min.time()
            )
            if timezone.is_naive(end_datetime):
                end_datetime = timezone.make_aware(end_datetime)
            days_remaining = (end_datetime - timezone.now()).days

            # Create timezone-aware due_date for the action
            due_date_aware = timezone.make_aware(
                timezone.datetime.combine(
                    resource.end_date, timezone.datetime.min.time()
                )
            )

            actions.append(
                {
                    "title": f"Resource expiring: {resource.name}",
                    "description": f"Resource will expire in {days_remaining} days.",
                    "urgency": self.get_urgency(resource, days_remaining),
                    "due_date": due_date_aware,
                    "related_object": resource,
                    # Use specific typed fields instead of metadata
                    "route_name": "marketplace-resource-details",
                    "route_params": {"resource_uuid": str(resource.uuid)},
                    "project_name": resource.project.name,
                    "project_uuid": resource.project.uuid,
                    "organization_name": resource.project.customer.name,
                    "organization_uuid": resource.project.customer.uuid,
                    "offering_name": resource.offering.name,
                    "offering_uuid": resource.offering.uuid,
                    "offering_type": resource.offering.type,
                    "resource_name": resource.name,
                    "resource_uuid": resource.uuid,
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
