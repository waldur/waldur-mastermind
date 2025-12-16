import logging
from datetime import timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from waldur_core.core.utils import format_homeport_link
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import UserRole
from waldur_core.structure.models import Project
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

        # Find orders pending consumer approval where user has admin/manager role
        project_ct = ContentType.objects.get_for_model(Project)
        user_projects = UserRole.objects.filter(
            user=user,
            content_type=project_ct,
            role__name__in=["PROJECT.ADMIN", "PROJECT.MANAGER"],
            is_active=True,
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
                    "description": f"Order has been pending approval for {days_pending} days. "
                    f"Customer: {order.project.customer.name}",
                    "urgency": self.get_urgency(order, days_remaining=urgency_days),
                    "due_date": order.created + timedelta(days=7),  # 7 day SLA
                    "action_url": format_homeport_link(
                        "marketplace-order-details/{order_uuid}/",
                        project_uuid=order.project.uuid,
                        order_uuid=order.uuid,
                    ),
                    "related_object": order,
                    "metadata": {
                        "days_pending": days_pending,
                        "customer_name": order.project.customer.name,
                        "offering_type": order.offering.type,
                        "estimated_cost": str(order.cost) if order.cost else None,
                    },
                }
            )

        return actions

    def get_affected_users(self) -> list[User]:
        """Get users who can approve orders (have APPROVE_ORDER permission on projects)"""
        project_ct = ContentType.objects.get_for_model(Project)
        user_ids = UserRole.objects.filter(
            content_type=project_ct,
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
                url=format_homeport_link(
                    "marketplace-order-details/{order_uuid}/",
                    project_uuid=order.project.uuid,
                    order_uuid=order.uuid,
                ),
                category=ActionCategory.VIEW,
                severity=ActionSeverity.SAFE,
            )
        )

        # Approve order (if user has permission)
        if self._can_approve_order(user, order):
            actions.append(
                CorrectiveAction(
                    label="Approve Order",
                    url=f"/api/marketplace-orders/{order.uuid}/approve/",
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
                    url=format_homeport_link(
                        "marketplace-order-details/{order_uuid}/?tab=reject",
                        project_uuid=order.project.uuid,
                        order_uuid=order.uuid,
                    ),
                    category=ActionCategory.REJECT,
                    severity=ActionSeverity.HIGH,
                    confirmation_required=True,
                    metadata={
                        "order_type": order.type,
                        "customer_contact": (
                            order.created_by.email if order.created_by else None
                        ),
                    },
                )
            )

        # Contact customer
        if hasattr(order, "created_by") and order.created_by and order.created_by.email:
            actions.append(
                CorrectiveAction(
                    label="Contact Customer",
                    url=f"mailto:{order.created_by.email}?subject=Regarding Order {order.uuid}",
                    category=ActionCategory.CONTACT,
                    severity=ActionSeverity.SAFE,
                    metadata={
                        "contact_method": "email",
                        "contact_address": order.created_by.email,
                        "customer_name": order.created_by.get_full_name(),
                    },
                )
            )

        return actions

    def _can_approve_order(self, user: User, order) -> bool:
        """Check if user can approve the order"""
        # Check if user has admin or manager role on the project
        return UserRole.objects.filter(
            user=user,
            content_type=ContentType.objects.get_for_model(Project),
            object_id=order.project.id,
            role__name__in=["PROJECT.ADMIN", "PROJECT.MANAGER"],
            is_active=True,
        ).exists()

    def _can_reject_order(self, user: User, order) -> bool:
        """Check if user can reject the order"""
        # Same permissions as approval for now
        return self._can_approve_order(user, order)


class ExpiringResourceProvider(BaseActionProvider):
    """Provider for resources nearing expiration"""

    action_type = "expiring_resource"
    display_name = "Expiring Resources"

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

            actions.append(
                {
                    "title": f"Resource expiring: {resource.name}",
                    "description": f"Resource will expire in {days_remaining} days. "
                    f"Project: {resource.project.name}",
                    "urgency": self.get_urgency(resource, days_remaining),
                    "due_date": resource.end_date,
                    "action_url": format_homeport_link(
                        "marketplace-resource-details/{resource_uuid}/",
                        project_uuid=resource.project.uuid,
                        resource_uuid=resource.uuid,
                    ),
                    "related_object": resource,
                    "metadata": {
                        "days_remaining": days_remaining,
                        "project_name": resource.project.name,
                        "offering_type": resource.offering.type,
                        "plan_name": resource.plan.name if resource.plan else None,
                    },
                }
            )

        return actions

    def get_affected_users(self) -> list[User]:
        """Get users who have access to resources with end dates"""
        project_ct = ContentType.objects.get_for_model(Project)
        project_ids_with_expiring_resources = models.Resource.objects.filter(
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
                url=format_homeport_link(
                    "marketplace-resource-details/{resource_uuid}/",
                    project_uuid=resource.project.uuid,
                    resource_uuid=resource.uuid,
                ),
                category=ActionCategory.VIEW,
                severity=ActionSeverity.SAFE,
            )
        )

        return actions


# Register all providers
register_provider(PendingOrderProvider)
register_provider(ExpiringResourceProvider)
