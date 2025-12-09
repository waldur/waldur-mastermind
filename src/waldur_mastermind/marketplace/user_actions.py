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
        from django.contrib.contenttypes.models import ContentType

        from waldur_core.permissions.models import UserRole

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
        """Get users who can approve orders (project admins/managers)"""
        from django.contrib.contenttypes.models import ContentType

        project_ct = ContentType.objects.get_for_model(Project)
        return User.objects.filter(
            userrole__content_type=project_ct,
            userrole__role__name__in=["PROJECT.ADMIN", "PROJECT.MANAGER"],
            userrole__is_active=True,
        ).distinct()

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
        from django.contrib.contenttypes.models import ContentType

        from waldur_core.permissions.models import UserRole

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
        from django.contrib.contenttypes.models import ContentType

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
        days_remaining = (end_datetime - timezone.now()).days

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

        # Extend resource (if supported by offering)
        if self._can_extend_resource(user, resource):
            actions.append(
                CorrectiveAction(
                    label="Extend Resource",
                    url=format_homeport_link(
                        "marketplace-resource-details/{resource_uuid}/?tab=extend",
                        project_uuid=resource.project.uuid,
                        resource_uuid=resource.uuid,
                    ),
                    category=ActionCategory.EXTEND,
                    severity=ActionSeverity.LOW,
                    metadata={
                        "current_end_date": resource.end_date.isoformat(),
                        "days_remaining": days_remaining,
                        "max_extension_days": self._get_max_extension_days(resource),
                        "cost_impact": self._estimate_extension_cost(resource),
                    },
                )
            )

        # Create backup before termination (if supported)
        if self._supports_backup(resource):
            actions.append(
                CorrectiveAction(
                    label="Create Backup",
                    url=f"/api/marketplace-resources/{resource.uuid}/create-backup/",
                    method="POST",
                    category=ActionCategory.BACKUP,
                    severity=ActionSeverity.MEDIUM,
                    api_endpoint=True,
                    confirmation_required=True,
                    metadata={
                        "backup_type": self._get_backup_type(resource),
                        "estimated_duration": "15-30 minutes",
                        "backup_cost": self._get_backup_cost(resource),
                    },
                )
            )

        # Terminate early (if user wants to avoid charges)
        if self._can_terminate_resource(user, resource):
            actions.append(
                CorrectiveAction(
                    label="Terminate Now",
                    url=format_homeport_link(
                        "marketplace-resource-details/{resource_uuid}/?tab=terminate",
                        project_uuid=resource.project.uuid,
                        resource_uuid=resource.uuid,
                    ),
                    category=ActionCategory.TERMINATE,
                    severity=ActionSeverity.HIGH,
                    confirmation_required=True,
                    metadata={
                        "data_loss_risk": True,
                        "cost_savings": self._calculate_early_termination_savings(
                            resource
                        ),
                        "dependencies": self._get_dependent_resources(resource),
                    },
                )
            )

        # Migration options (if available)
        migration_options = self._get_migration_options(resource)
        if migration_options:
            actions.append(
                CorrectiveAction(
                    label="Migrate to New Resource",
                    url=format_homeport_link(
                        "marketplace-resource-migrate/{resource_uuid}/",
                        project_uuid=resource.project.uuid,
                        resource_uuid=resource.uuid,
                    ),
                    category=ActionCategory.MIGRATE,
                    severity=ActionSeverity.MEDIUM,
                    metadata={
                        "migration_options": migration_options,
                        "estimated_downtime": self._estimate_migration_downtime(
                            resource
                        ),
                        "data_transfer_required": True,
                    },
                )
            )

        return actions

    def _can_extend_resource(self, user: User, resource) -> bool:
        """Check if resource can be extended"""
        # Basic check - would need to implement offering-specific logic
        return hasattr(
            resource.offering, "plugin_options"
        ) and resource.offering.plugin_options.get("can_extend", False)

    def _supports_backup(self, resource) -> bool:
        """Check if resource supports backup"""
        # Implementation would depend on offering type
        return resource.offering.type in ["openstack-tenant", "vmware-vm"]

    def _can_terminate_resource(self, user: User, resource) -> bool:
        """Check if user can terminate the resource"""
        return UserRole.objects.filter(
            user=user,
            content_type=ContentType.objects.get_for_model(Project),
            object_id=resource.project.id,
            role__name__in=["PROJECT.ADMIN", "PROJECT.MANAGER"],
            is_active=True,
        ).exists()

    def _get_max_extension_days(self, resource) -> int:
        """Get maximum days resource can be extended"""
        # Default to 365 days, could be offering-specific
        return 365

    def _estimate_extension_cost(self, resource):
        """Calculate estimated cost for resource extension"""
        if resource.plan and hasattr(resource.plan, "unit_price"):
            return {
                "monthly_cost": float(resource.plan.unit_price or 0),
                "currency": getattr(resource.plan, "unit", "USD"),
                "billing_type": getattr(resource.plan, "billing_type", "monthly"),
            }
        return None

    def _get_backup_type(self, resource) -> str:
        """Get backup type based on resource offering"""
        if "openstack" in resource.offering.type:
            return "snapshot"
        elif "vmware" in resource.offering.type:
            return "vm-backup"
        return "full-backup"

    def _get_backup_cost(self, resource):
        """Estimate backup cost"""
        # Simplified cost calculation
        return {"estimated_cost": "5-15 USD", "currency": "USD"}

    def _calculate_early_termination_savings(self, resource):
        """Calculate savings from early termination"""
        if not resource.end_date or not resource.plan:
            return None

        days_remaining = (resource.end_date - timezone.now()).days
        if days_remaining <= 0:
            return None

        monthly_cost = getattr(resource.plan, "unit_price", 0) or 0
        daily_cost = float(monthly_cost) / 30

        return {
            "total_savings": round(daily_cost * days_remaining, 2),
            "currency": getattr(resource.plan, "unit", "USD"),
            "days_remaining": days_remaining,
        }

    def _get_dependent_resources(self, resource):
        """Find resources that depend on this one"""
        # Check for child resources
        dependent_count = (
            resource.children.count() if hasattr(resource, "children") else 0
        )

        return {
            "count": dependent_count,
            "will_be_affected": dependent_count > 0,
            "types": (
                list(
                    resource.children.values_list(
                        "offering__type", flat=True
                    ).distinct()
                )
                if hasattr(resource, "children")
                else []
            ),
        }

    def _get_migration_options(self, resource):
        """Get available migration targets"""
        # Find compatible offerings in the same category
        compatible_offerings = models.Offering.objects.filter(
            category=resource.offering.category,
            state=models.OfferingStates.ACTIVE,
        ).exclude(uuid=resource.offering.uuid)[:5]  # Limit to 5 options

        if not compatible_offerings:
            return []

        return [
            {
                "offering_uuid": str(offering.uuid),
                "offering_name": offering.name,
                "cost_difference": self._calculate_cost_difference(resource, offering),
                "compatibility_score": self._calculate_compatibility(
                    resource, offering
                ),
            }
            for offering in compatible_offerings
        ]

    def _calculate_cost_difference(self, current_resource, target_offering):
        """Calculate cost difference between current and target offering"""
        # Simplified calculation - would need more complex logic
        return {"difference": "Similar", "currency": "USD"}

    def _calculate_compatibility(self, current_resource, target_offering):
        """Calculate compatibility score between current and target offering"""
        # Simplified scoring - would need detailed compatibility matrix
        return 0.8

    def _estimate_migration_downtime(self, resource):
        """Estimate migration downtime"""
        # Default estimates based on offering type
        if "openstack" in resource.offering.type:
            return "2-4 hours"
        elif "vmware" in resource.offering.type:
            return "1-2 hours"
        return "4-8 hours"


# Register all providers
register_provider(PendingOrderProvider)
register_provider(ExpiringResourceProvider)
