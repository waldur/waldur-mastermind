import datetime
import logging

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core import exceptions
from django.db import models
from django.db.models import Q, Sum
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_mastermind.invoices import (
    compensations as invoices_compensation,
)
from waldur_mastermind.invoices import (
    models as invoices_models,
)
from waldur_mastermind.marketplace import models as marketplace_models

from . import enums, structures

logger = logging.getLogger(__name__)

# Import for TRES billing calculations


class Policy(
    TimeStampedModel,
    core_models.UuidMixin,
):
    trigger_class = NotImplemented
    observable_classes = [marketplace_models.Resource]
    available_actions: set[str] = NotImplemented

    has_fired = models.BooleanField(default=False)
    fired_datetime = models.DateTimeField(null=True, blank=True, editable=False)
    created_by = models.ForeignKey[core_models.User](
        on_delete=models.CASCADE,
        to=settings.AUTH_USER_MODEL,
        related_name="+",
        blank=True,
        null=True,
    )
    options = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Fields for saving actions extra data. Keys are name of actions."),
    )
    actions = NotImplemented
    scope = NotImplemented

    @staticmethod
    def get_scope_from_observable_object(observable_object):
        return

    def get_scope_homeport_url(self):
        return

    def is_triggered(self):
        """Checking if the policy needs to be applied."""
        raise NotImplementedError()

    def get_all_actions(self) -> list[structures.PolicyAction]:
        from . import policy_actions

        actions: list[structures.PolicyAction] = []

        for action_name in self.actions.split(","):
            if action_name in self.available_actions:
                actions.append(policy_actions.POLICY_ACTIONS[action_name])

        return actions

    def get_threshold_actions(self) -> list[structures.PolicyAction]:
        actions = self.get_all_actions()
        return [
            action
            for action in actions
            if action.action_type == enums.PolicyActionTypes.THRESHOLD
        ]

    def get_immediate_actions(self) -> list[structures.PolicyAction]:
        actions = self.get_all_actions()
        return [
            action
            for action in actions
            if action.action_type == enums.PolicyActionTypes.IMMEDIATE
        ]

    class Meta:
        abstract = True


class EstimatedCostPolicyMixin(invoices_models.PeriodMixin):
    trigger_class = invoices_models.InvoiceItem

    limit_cost = models.IntegerField()

    def _is_triggered(self, invoice_items, compensation=0):
        customers = structure_models.Customer.objects.filter(
            blocked=False,
            archived=False,
        )
        invoice_items = invoice_items.filter(
            invoice__customer__in=customers,
        ).exclude(invoice__state=invoices_models.Invoice.States.CANCELED)
        month_start = core_utils.month_start(datetime.date.today())
        period = 0

        if self.period == self.Periods.MONTH_1:
            period = 1
        elif self.period == self.Periods.MONTH_3:
            period = 3
        elif self.period == self.Periods.MONTH_12:
            period = 12

        query = Q()

        for n in range(period):
            previous_month_date = month_start - relativedelta(months=n)
            query |= Q(
                invoice__month=previous_month_date.month,
                invoice__year=previous_month_date.year,
            )

        invoice_items = invoice_items.filter(query)

        total = sum([i.total for i in invoice_items])
        return total - compensation > self.limit_cost

    class Meta:
        abstract = True


class ProjectPolicy(Policy):
    class Permissions:
        customer_path = "scope__customer"
        project_path = "scope"

    available_actions: set[str] = {
        "notify_project_team",
        "notify_organization_owners",
        "notify_external_user",
        "block_creation_of_new_resources",
        "block_modification_of_existing_resources",
        "terminate_resources",
        "request_downscaling",
        "restrict_members",
        "request_pausing",
    }

    scope = models.ForeignKey(structure_models.Project, on_delete=models.CASCADE)
    actions = models.CharField(max_length=255)

    @staticmethod
    def get_scope_from_observable_object(observable_object):
        return structure_permissions._get_project(observable_object)

    def get_scope_homeport_url(self):
        return core_utils.format_homeport_link(
            "projects/{uuid}/", uuid=self.scope.uuid.hex
        )

    class Meta:
        abstract = True


class ProjectEstimatedCostPolicy(EstimatedCostPolicyMixin, ProjectPolicy):
    def is_triggered(self):
        project = self.scope
        invoice_items = invoices_models.InvoiceItem.objects.filter(project=project)
        compensation = invoices_compensation.MonthlyCompensation(project.customer)
        return self._is_triggered(
            invoice_items, compensation.get_project_compensation(project)
        )

    class Meta:
        verbose_name_plural = "Project estimated cost policies"


class CustomerPolicy(Policy):
    class Permissions:
        customer_path = "scope"

    available_actions: set[str] = {
        "notify_organization_owners",
        "notify_external_user",
        "block_creation_of_new_resources",
        "block_modification_of_existing_resources",
        "terminate_resources",
        "request_downscaling",
        "restrict_members",
        "request_pausing",
    }

    scope = models.ForeignKey(structure_models.Customer, on_delete=models.CASCADE)
    actions = models.CharField(max_length=255)

    @staticmethod
    def get_scope_from_observable_object(observable_object):
        return structure_permissions._get_customer(observable_object)

    def get_scope_homeport_url(self):
        return core_utils.format_homeport_link(
            "/organizations/{uuid}/dashboard/", uuid=self.scope.uuid.hex
        )

    class Meta:
        abstract = True


class CustomerEstimatedCostPolicy(EstimatedCostPolicyMixin, CustomerPolicy):
    def is_triggered(self):
        customer = self.scope
        invoice_items = invoices_models.InvoiceItem.objects.filter(
            invoice__customer=customer
        )
        compensation = invoices_compensation.MonthlyCompensation(customer)

        return self._is_triggered(invoice_items, compensation.total_compensation)

    class Meta:
        verbose_name_plural = "Customer estimated cost policies"


class OfferingPolicy(Policy):
    class Permissions:
        customer_path = "scope__customer"

    available_actions: set[str] = {
        "notify_organization_owners",
        "notify_external_user",
        "block_creation_of_new_resources",
    }
    observable_classes = []

    scope = models.ForeignKey(marketplace_models.Offering, on_delete=models.CASCADE)
    organization_groups = models.ManyToManyField(structure_models.OrganizationGroup)
    actions = models.CharField(max_length=255)

    @staticmethod
    def get_scope_from_observable_object(resource):
        return resource.offering

    def get_scope_homeport_url(self):
        return core_utils.format_homeport_link(
            "/providers/{customer_uuid}/marketplace-provider-offering-details/{uuid}/",
            customer_uuid=self.scope.customer.uuid.hex,
            uuid=self.scope.uuid.hex,
        )

    class Meta:
        abstract = True


class OfferingEstimatedCostPolicy(EstimatedCostPolicyMixin, OfferingPolicy):
    def is_triggered(self):
        customers = structure_models.Customer.objects.filter(
            organization_groups__in=self.organization_groups.all()
        )
        items = invoices_models.InvoiceItem.objects.filter(
            resource__offering=self.scope,
            invoice__customer__in=customers,
        )
        return self._is_triggered(items)

    class Meta:
        verbose_name_plural = "Offering estimated cost policies"


class OfferingUsagePolicy(invoices_models.PeriodMixin, OfferingPolicy):
    component_limits_set: models.Manager["OfferingComponentLimit"]

    trigger_class = marketplace_models.ComponentUsage

    component_limit = models.ManyToManyField(
        marketplace_models.OfferingComponent, through="OfferingComponentLimit"
    )

    def is_triggered(self):
        customers = structure_models.Customer.objects.filter(
            organization_groups__in=self.organization_groups.all(),
            blocked=False,
            archived=False,
        )
        usages = marketplace_models.ComponentUsage.objects.filter(
            resource__project__customer__in=customers
        )

        usages = usages.filter(
            billing_period__lte=core_utils.month_end(datetime.date.today())
        )

        start = self.get_start_date()

        if start:
            usages = usages.filter(billing_period__gte=start)

        for component_limit in self.component_limits_set.all():
            total = (
                usages.filter(component=component_limit.component).aggregate(
                    usage=Sum("usage")
                )["usage"]
                or 0
            )
            if total > component_limit.limit:
                return True
            else:
                return False


class OfferingComponentLimit(TimeStampedModel):
    policy = models.ForeignKey(
        OfferingUsagePolicy,
        on_delete=models.CASCADE,
        null=False,
        related_name="component_limits_set",
    )
    component = models.ForeignKey(
        marketplace_models.OfferingComponent, on_delete=models.CASCADE, null=False
    )
    limit = models.IntegerField()

    class Meta:
        unique_together = (("policy", "component"),)

    def save(self, *args, **kwargs):
        if self.component not in self.policy.scope.components.all():
            raise exceptions.ValidationError(
                _("The selected component does not match the offering.")
            )

        return super().save(*args, **kwargs)


class CustomerComponentUsagePolicy(CustomerPolicy):
    component_limits_set: models.Manager["CustomerUsagePolicyComponent"]

    trigger_class = marketplace_models.ComponentUsage
    component_limit = models.ManyToManyField(
        marketplace_models.OfferingComponent,
        through="CustomerUsagePolicyComponent",
    )

    def is_triggered(self):
        customer = self.scope
        usages = marketplace_models.ComponentUsage.objects.filter(
            resource__project__customer=customer
        )

        for component_limit in self.component_limits_set.all():
            component_usages = usages.filter(
                component=component_limit.component,
                billing_period__lte=core_utils.month_end(datetime.date.today()),
            )
            start = component_limit.get_start_date()

            if start:
                component_usages = component_usages.filter(billing_period__gte=start)

            total = component_usages.aggregate(usage=Sum("usage"))["usage"] or 0

            if total > component_limit.limit:
                return True

        return False

    class Meta:
        verbose_name_plural = "Customer component usage policies"


class CustomerUsagePolicyComponent(invoices_models.PeriodMixin, TimeStampedModel):
    policy = models.ForeignKey(
        CustomerComponentUsagePolicy,
        on_delete=models.CASCADE,
        null=False,
        related_name="component_limits_set",
    )
    component = models.ForeignKey(
        marketplace_models.OfferingComponent, on_delete=models.CASCADE, null=False
    )
    limit = models.IntegerField()

    class Meta:
        unique_together = (("policy", "component", "period"),)


class SlurmPeriodicUsagePolicy(OfferingUsagePolicy):
    """SLURM-specific periodic usage policy with decay and carryover logic."""

    # Core SLURM configuration options
    limit_type = models.CharField(
        max_length=20,
        choices=[
            ("GrpTRESMins", "Group TRES Minutes"),
            ("MaxTRESMins", "Max TRES Minutes"),
            ("GrpTRES", "Group TRES (concurrent)"),
        ],
        default="GrpTRESMins",
        help_text=_("SLURM limit type to apply"),
    )

    tres_billing_enabled = models.BooleanField(
        default=True, help_text=_("Use TRES billing units instead of raw TRES values")
    )

    tres_billing_weights = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            'TRES billing weights (e.g., {"CPU": 0.015625, "Mem": 0.001953125, "GRES/gpu": 0.25})'
        ),
    )

    fairshare_decay_half_life = models.PositiveIntegerField(
        default=15,
        help_text=_(
            "Fairshare decay half-life in days (matches SLURM PriorityDecayHalfLife)"
        ),
    )

    # Policy-specific settings
    grace_ratio = models.FloatField(
        default=0.2,
        help_text=_("Grace period ratio (0.2 = 20% overconsumption allowed)"),
    )

    carryover_enabled = models.BooleanField(
        default=True, help_text=_("Enable unused allocation carryover to next period")
    )

    raw_usage_reset = models.BooleanField(
        default=True,
        help_text=_(
            "Reset raw usage at period transitions (PriorityUsageResetPeriod=None)"
        ),
    )

    # QoS strategy configuration
    qos_strategy = models.CharField(
        max_length=20,
        choices=[
            ("threshold", "Threshold-based (single threshold)"),
            ("progressive", "Progressive (multiple thresholds)"),
        ],
        default="threshold",
        help_text=_("QoS management strategy"),
    )

    class Meta:
        verbose_name = _("SLURM Periodic Usage Policy")
        verbose_name_plural = _("SLURM Periodic Usage Policies")

    def calculate_slurm_settings(self, resource, config_override=None):
        """Calculate SLURM settings with configurable behavior and decay logic."""

        # Get configuration from multiple sources (priority: override > policy > defaults)
        final_config = self._resolve_configuration(config_override)

        # Get current period and allocation information
        current_period = self._get_current_period()
        base_allocation = self._get_base_allocation(resource)

        logger.info(
            f"Calculating SLURM settings for resource {resource.uuid} in period {current_period}"
        )
        logger.debug(f"Base allocation: {base_allocation}, Config: {final_config}")

        # Calculate total allocation with carryover if enabled
        if final_config.get("carryover_enabled", True):
            total_allocation, carryover_details = (
                self._calculate_allocation_with_carryover(
                    resource, base_allocation, current_period, final_config
                )
            )
        else:
            total_allocation = base_allocation
            carryover_details = {"carryover_applied": False}

        # Calculate SLURM-specific values
        fairshare = self._calculate_fairshare(total_allocation, final_config)

        if final_config.get("tres_billing_enabled", True):
            billing_minutes = self._calculate_billing_minutes(
                total_allocation, final_config
            )
            limit_key = (
                "grp_tres_mins"
                if "Grp" in final_config.get("limit_type", "GrpTRESMins")
                else "max_tres_mins"
            )
            limits = {limit_key: {"billing": billing_minutes}}
        else:
            # Raw TRES mode - convert to node-minutes
            node_minutes = int(total_allocation * 60)
            limit_key = (
                "grp_tres_mins"
                if "Grp" in final_config.get("limit_type", "GrpTRESMins")
                else "max_tres_mins"
            )
            limits = {limit_key: {"node": node_minutes}}

        # Calculate QoS thresholds
        qos_threshold, grace_limit = self._calculate_qos_thresholds(
            total_allocation, final_config
        )

        settings = {
            "fairshare": fairshare,
            "qos_threshold": qos_threshold,
            "grace_limit": grace_limit,
            "limit_type": final_config.get("limit_type", "GrpTRESMins"),
            "reset_raw_usage": final_config.get("raw_usage_reset", True),
            "carryover_details": carryover_details,
            **limits,
        }

        logger.info(
            f"Calculated SLURM settings: fairshare={fairshare}, "
            f"allocation={total_allocation:.1f}Nh, "
            f"threshold={list(qos_threshold.values())[0] if qos_threshold else 'N/A'}"
        )

        return settings

    def _resolve_configuration(self, config_override=None):
        """Resolve configuration from multiple sources with proper precedence."""
        # Start with policy defaults
        config = {
            "limit_type": self.limit_type,
            "tres_billing_enabled": self.tres_billing_enabled,
            "tres_billing_weights": self.tres_billing_weights
            or self._get_default_tres_weights(),
            "fairshare_decay_half_life": self.fairshare_decay_half_life,
            "grace_ratio": self.grace_ratio,
            "carryover_enabled": self.carryover_enabled,
            "raw_usage_reset": self.raw_usage_reset,
            "qos_strategy": self.qos_strategy,
        }

        # Apply runtime overrides if provided
        if config_override:
            config.update(config_override)

        return config

    def _get_default_tres_weights(self):
        """Get default TRES billing weights."""
        return {
            "CPU": 0.015625,  # 64 CPUs = 1 billing unit
            "Mem": 0.001953125,  # 512 GB = 1 billing unit (per GB)
            "GRES/gpu": 0.25,  # 4 GPUs = 1 billing unit
        }

    def _get_current_period(self):
        """Get current period (quarterly by default)."""
        now = core_utils.datetime.date.today()
        quarter = (now.month - 1) // 3 + 1
        return f"{now.year}-Q{quarter}"

    def _get_base_allocation(self, resource):
        """Get base allocation for resource from offering components."""
        # Get primary component allocation (typically 'nodeHours')
        try:
            # Look for nodeHours component or first component with usage accounting
            for component in resource.offering.components.all():
                if component.type in ["nodeHours", "node_hours", "node-hours"]:
                    # Get current plan allocation for this component
                    plan_component = resource.plan.components.filter(
                        component=component
                    ).first()
                    if plan_component:
                        return float(plan_component.amount)

            # Fallback: get first component with usage accounting
            for component in resource.offering.components.all():
                if component.billing_type == marketplace_models.BillingTypes.USAGE:
                    plan_component = resource.plan.components.filter(
                        component=component
                    ).first()
                    if plan_component:
                        return float(plan_component.amount)

            logger.warning(
                f"No suitable allocation component found for resource {resource.uuid}"
            )
            return 1000.0  # Default fallback

        except Exception as e:
            logger.error(
                f"Error getting base allocation for resource {resource.uuid}: {e}"
            )
            return 1000.0  # Safe fallback

    def _calculate_allocation_with_carryover(
        self, resource, base_allocation, current_period, config
    ):
        """Calculate allocation with carryover logic and decay."""

        # Check if carryover is enabled in config
        if not config.get("carryover_enabled", True):
            return base_allocation, {
                "carryover_applied": False,
                "reason": "carryover_disabled",
            }

        previous_period = self._get_previous_period(current_period)
        if not previous_period:
            return base_allocation, {
                "carryover_applied": False,
                "reason": "no_previous_period",
            }

        # Get previous period usage
        previous_usage = self._get_previous_period_usage(resource, previous_period)

        # Calculate decay factor
        days_elapsed = 90  # Standard quarter transition
        half_life = config.get("fairshare_decay_half_life", 15)
        decay_factor = 2 ** (-days_elapsed / half_life)

        # Apply decay to previous usage
        effective_previous_usage = previous_usage * decay_factor

        # Calculate unused allocation (carryover)
        unused_allocation = max(0, base_allocation - effective_previous_usage)

        # New total allocation
        total_allocation = base_allocation + unused_allocation

        carryover_details = {
            "carryover_applied": True,
            "previous_period": previous_period,
            "previous_usage": previous_usage,
            "days_elapsed": days_elapsed,
            "decay_factor": decay_factor,
            "effective_previous_usage": effective_previous_usage,
            "unused_allocation": unused_allocation,
            "base_allocation": base_allocation,
            "total_allocation": total_allocation,
        }

        logger.debug(
            f"Carryover calculation: {previous_usage}Nh -> {effective_previous_usage:.1f}Nh "
            f"(decay={decay_factor:.4f}) -> +{unused_allocation:.1f}Nh carryover"
        )

        return total_allocation, carryover_details

    def _get_previous_period(self, current_period):
        """Get the previous quarter for a given quarter."""
        try:
            # Parse "2024-Q2" format
            year_str, q_str = current_period.split("-Q")
            year = int(year_str)
            quarter = int(q_str)

            if quarter == 1:
                # Q1 -> previous year Q4
                prev_quarter = 4
                prev_year = year - 1
            else:
                # Q2->Q1, Q3->Q2, Q4->Q3
                prev_quarter = quarter - 1
                prev_year = year

            return f"{prev_year}-Q{prev_quarter}"
        except (ValueError, AttributeError):
            logger.error(f"Failed to parse period: {current_period}")
            return None

    def _get_previous_period_usage(self, resource, previous_period):
        """Get total usage for resource in previous period."""
        try:
            # Parse period to get date range
            year, quarter = previous_period.split("-Q")
            year = int(year)
            quarter = int(quarter)

            # Calculate quarter date range
            start_month = (quarter - 1) * 3 + 1
            start_date = core_utils.datetime.date(year, start_month, 1)

            if quarter == 4:
                end_date = core_utils.datetime.date(year, 12, 31)
            else:
                next_quarter_start = core_utils.datetime.date(year, start_month + 3, 1)
                end_date = next_quarter_start - relativedelta(days=1)

            # Get component usage for the period
            usage_total = 0.0
            usages = marketplace_models.ComponentUsage.objects.filter(
                resource=resource,
                billing_period__gte=start_date,
                billing_period__lte=end_date,
            )

            for usage in usages:
                usage_total += float(usage.usage)

            logger.debug(
                f"Previous period {previous_period} usage for resource {resource.uuid}: {usage_total}"
            )
            return usage_total

        except Exception as e:
            logger.error(f"Error getting previous period usage: {e}")
            return 0.0

    def _calculate_fairshare(self, allocation, config):
        """Calculate fairshare value based on allocation."""
        # Simple fairshare calculation: allocation divided by assumed number of sibling accounts
        # In practice, this might be more sophisticated based on the organization structure
        num_accounts = 3  # Conservative estimate for fairshare calculation
        return max(1, int(allocation // num_accounts))

    def _calculate_billing_minutes(self, node_hours, config):
        """Convert node-hours to billing minutes using TRES weights."""
        if not config.get("tres_billing_enabled", True):
            return int(node_hours * 60)  # Simple node-hour conversion

        # For billing units, we assume 1 node-hour = 1 billing unit by default
        # This can be configured based on the actual node specification
        billing_units = node_hours  # 1:1 mapping as default

        # Convert to billing minutes
        return int(billing_units * 60)

    def _calculate_qos_thresholds(self, total_allocation, config):
        """Calculate QoS thresholds for slowdown and blocking."""
        grace_ratio = config.get("grace_ratio", 0.2)
        tres_billing_enabled = config.get("tres_billing_enabled", True)

        # QoS threshold at 100% of allocation (slowdown trigger)
        qos_threshold_value = total_allocation

        # Grace limit at allocation + grace ratio (blocking trigger)
        grace_limit_value = total_allocation * (1 + grace_ratio)

        if tres_billing_enabled:
            # Convert to billing minutes
            qos_threshold = {"billing": int(qos_threshold_value * 60)}
            grace_limit = {"billing": int(grace_limit_value * 60)}
        else:
            # Use raw node minutes
            qos_threshold = {"node": int(qos_threshold_value * 60)}
            grace_limit = {"node": int(grace_limit_value * 60)}

        return qos_threshold, grace_limit

    def is_triggered(self):
        """Check if policy should be triggered based on usage changes."""
        # This method is called by the policy framework when ComponentUsage changes
        # We trigger on any usage change for SLURM resources

        # Get all customers in scope
        customers = structure_models.Customer.objects.filter(
            organization_groups__in=self.organization_groups.all(),
            blocked=False,
            archived=False,
        )

        # Check for recent usage changes in SLURM resources
        recent_usage = marketplace_models.ComponentUsage.objects.filter(
            resource__project__customer__in=customers,
            resource__offering=self.offering,
            modified__gte=self.modified
            - datetime.timedelta(minutes=5),  # Recent changes
        )

        return recent_usage.exists()

    def apply_policy_actions(self, resource):
        """Apply policy actions - calculate and send settings to site agent."""
        try:
            # Calculate SLURM settings
            settings = self.calculate_slurm_settings(resource)

            # Send settings to site agent via API
            # This would integrate with the site agent's API endpoints
            success = self._send_settings_to_site_agent(resource, settings)

            if success:
                logger.info(
                    f"Successfully applied periodic settings for resource {resource.uuid}"
                )
                return True
            else:
                logger.error(
                    f"Failed to apply periodic settings for resource {resource.uuid}"
                )
                return False

        except Exception as e:
            logger.error(f"Error applying policy for resource {resource.uuid}: {e}")
            return False

    def _send_settings_to_site_agent(self, resource, settings):
        """Send calculated settings to site agent via STOMP message."""
        import datetime

        from waldur_core.logging import tasks as logging_tasks
        from waldur_core.logging.utils import ObservableObjectType
        from waldur_mastermind.marketplace import utils as marketplace_utils

        try:
            # Prepare STOMP message payload for periodic limits update
            message_payload = {
                "resource_uuid": str(resource.uuid),
                "backend_id": resource.backend_id,
                "offering_uuid": str(
                    resource.offering.uuid
                ),  # Required by prepare_messages
                "action": "apply_periodic_settings",
                "settings": settings,
                "timestamp": datetime.datetime.now().isoformat(),  # Use ISO timestamp
            }

            # Prepare messages using marketplace utils
            messages = marketplace_utils.prepare_messages(
                offering=resource.offering,
                message_payload=message_payload,
                affected_object=ObservableObjectType.RESOURCE_PERIODIC_LIMITS,
            )

            # Publish STOMP messages to site agent
            if messages:
                logging_tasks.publish_messages.delay(messages)
                logger.info(
                    f"Published periodic limits STOMP message for resource {resource.backend_id}"
                )
                return True
            else:
                logger.warning(
                    f"No messages prepared for resource {resource.backend_id}"
                )
                return False

        except Exception as e:
            logger.error(
                f"Failed to publish STOMP message for resource {resource.uuid}: {e}"
            )
            return False
