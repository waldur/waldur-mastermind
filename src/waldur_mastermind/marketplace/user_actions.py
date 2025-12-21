import logging
from datetime import timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
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
from .enums import OrderStates, ResourceStates

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
            state=OrderStates.PENDING_CONSUMER,
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
                    "route_name": "marketplace-order-details",
                    "route_params": {"uuid": str(order.uuid)},
                    "project_name": order.project.name,
                    "project_uuid": order.project.uuid,
                    "organization_name": order.project.customer.name,
                    "organization_uuid": order.project.customer.uuid,
                    "offering_name": order.offering.name,
                    "offering_type": order.offering.type,
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
                route_name="marketplace-order-details",
                route_params={"uuid": str(order.uuid)},
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
                    route_name="marketplace-order-details",
                    route_params={"uuid": str(order.uuid), "tab": "reject"},
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
        """Find resources expiring in the next 30 days"""
        cutoff = timezone.now() + timedelta(days=30)

        # Find resources where user has access to the project
        project_ct = ContentType.objects.get_for_model(Project)
        user_projects = UserRole.objects.filter(
            user=user,
            content_type=project_ct,
            is_active=True,
        ).values_list("object_id", flat=True)

        resources = models.Resource.objects.filter(
            end_date__isnull=False,
            end_date__lt=cutoff,
            end_date__gt=timezone.now(),
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
                    "offering_type": resource.offering.type,
                }
            )

        return actions

    def get_affected_users(self) -> list[User]:
        """Get users who have access to resources with end dates"""
        project_ct = ContentType.objects.get_for_model(Project)
        project_ids_with_expiring_resources = models.Resource.objects.filter(
            state=ResourceStates.OK,
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
        end_datetime = timezone.datetime.combine(
            resource.end_date, timezone.datetime.min.time()
        )
        if timezone.is_naive(end_datetime):
            end_datetime = timezone.make_aware(end_datetime)
        (end_datetime - timezone.now()).days

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

        return actions


# Register all providers
register_provider(PendingOrderProvider)
register_provider(ExpiringResourceProvider)
