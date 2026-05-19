import datetime
import logging

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core import exceptions
from django.core.cache import cache
from django.core.exceptions import ValidationError
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
from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods

from . import enums, structures

logger = logging.getLogger(__name__)

# Mapping from OfferingComponent.limit_period to PeriodMixin.Periods
LIMIT_PERIOD_TO_POLICY_PERIOD = {
    LimitPeriods.MONTH: invoices_models.PeriodMixin.Periods.MONTH_1,
    LimitPeriods.QUARTERLY: invoices_models.PeriodMixin.Periods.MONTH_3,
    LimitPeriods.ANNUAL: invoices_models.PeriodMixin.Periods.MONTH_12,
    LimitPeriods.TOTAL: invoices_models.PeriodMixin.Periods.TOTAL,
}

# Reverse mapping: PeriodMixin.Periods → LimitPeriods
POLICY_PERIOD_TO_LIMIT_PERIOD = {v: k for k, v in LIMIT_PERIOD_TO_POLICY_PERIOD.items()}


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

    def __str__(self):
        return f"{self.__class__.__name__} for {self.scope.__class__.__name__} {getattr(self.scope, 'name')}. UUID: {self.scope.uuid.hex}."

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

    def __str__(self):
        return super().__str__() + f" Limit cost: {self.limit_cost}"

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
        project_compensation = compensation.get_project_compensation(project)

        if not self._is_triggered(invoice_items, project_compensation):
            return False

        # Cost exceeds limit even after compensation. Check whether
        # available credit can cover the overage. Credit values are
        # persisted in DB — unlike MonthlyCompensation (which simulates
        # credit allocation in-memory and is order-dependent), the DB
        # values are authoritative.
        # If a ProjectCredit exists, it defines the budget for this project.
        # Otherwise fall back to CustomerCredit.
        project_credit = invoices_models.ProjectCredit.objects.filter(
            project=project
        ).first()
        if project_credit:
            return project_credit.value <= self.limit_cost

        customer_credit = invoices_models.CustomerCredit.objects.filter(
            customer=project.customer
        ).first()
        if customer_credit and customer_credit.value > self.limit_cost:
            return False

        return True

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

        if not self._is_triggered(invoice_items, compensation.total_compensation):
            return False

        try:
            customer_credit = invoices_models.CustomerCredit.objects.get(
                customer=customer
            )
            if customer_credit.value > self.limit_cost:
                return False
        except invoices_models.CustomerCredit.DoesNotExist:
            pass

        return True

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
    organization_groups = models.ManyToManyField(
        structure_models.OrganizationGroup, blank=True
    )
    apply_to_all = models.BooleanField(
        default=False,
        help_text="If True, policy applies to all customers. "
        "Mutually exclusive with organization_groups.",
    )
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

    def clean(self):
        super().clean()
        # Validate mutual exclusivity for saved instances
        if self.pk:
            if self.apply_to_all and self.organization_groups.exists():
                raise ValidationError(
                    "Cannot set apply_to_all=True when organization_groups are specified. "
                    "Choose one approach: either apply_to_all or specific organization_groups."
                )
            if not self.apply_to_all and not self.organization_groups.exists():
                raise ValidationError(
                    "Must either set apply_to_all=True or specify organization_groups."
                )

    def get_affected_customers(self):
        """Get customers affected by this policy based on apply_to_all or organization_groups"""
        if self.apply_to_all:
            return structure_models.Customer.objects.filter(
                blocked=False,
                archived=False,
            )
        else:
            return structure_models.Customer.objects.filter(
                organization_groups__in=self.organization_groups.all(),
                blocked=False,
                archived=False,
            )

    class Meta:
        abstract = True


class OfferingEstimatedCostPolicy(EstimatedCostPolicyMixin, OfferingPolicy):
    def is_triggered(self):
        # Use optimized query based on apply_to_all setting
        if self.apply_to_all:
            # Direct filter on invoice items without customer IN clause
            items = invoices_models.InvoiceItem.objects.filter(
                resource__offering=self.scope,
                invoice__customer__blocked=False,
                invoice__customer__archived=False,
            )
        else:
            customers = structure_models.Customer.objects.filter(
                organization_groups__in=self.organization_groups.all(),
                blocked=False,
                archived=False,
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
        # Use optimized query based on apply_to_all setting
        if self.apply_to_all:
            # Direct filter without customer IN clause
            usages = marketplace_models.ComponentUsage.objects.filter(
                resource__offering=self.scope,
                resource__project__customer__blocked=False,
                resource__project__customer__archived=False,
            )
        else:
            customers = structure_models.Customer.objects.filter(
                organization_groups__in=self.organization_groups.all(),
                blocked=False,
                archived=False,
            )
            usages = marketplace_models.ComponentUsage.objects.filter(
                resource__offering=self.scope, resource__project__customer__in=customers
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

    # SLURM-specific actions for individual resource management
    available_actions = (
        OfferingPolicy.available_actions
        | {
            "request_slurm_resource_downscaling",  # SLURM-specific individual resource downscaling
            "request_slurm_resource_pausing",  # SLURM-specific individual resource pausing
        }
    )

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

    carryover_factor = models.PositiveIntegerField(
        default=50,
        help_text=_(
            "Maximum percentage of base allocation that can carry over from unused previous period (0-100)"
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

    def clean(self):
        """Validate that only one SlurmPeriodicUsagePolicy exists per offering."""
        super().clean()
        if self.scope:
            existing = SlurmPeriodicUsagePolicy.objects.filter(scope=self.scope)
            if self.pk:
                existing = existing.exclude(pk=self.pk)
            if existing.exists():
                from django.core.exceptions import ValidationError

                raise ValidationError(
                    _("A SLURM Periodic Usage Policy already exists for this offering.")
                )

    def save(self, *args, **kwargs):
        """Save with validation."""
        self.clean()
        super().save(*args, **kwargs)

    def _get_offering(self):
        """Return the linked offering, or None if missing/deleted."""
        try:
            return self.scope if self.scope else None
        except self.__class__.scope.RelatedObjectDoesNotExist:
            return None

    def _get_limit_based_component(self):
        """Return the offering's limit-based component, or None."""
        offering = self._get_offering()
        if not offering:
            return None
        return offering.components.filter(billing_type=BillingTypes.LIMIT).first()

    def _get_component_limit_period(self):
        """Return the single limit_period string from this offering's limit-based components.

        Returns None if:
        - offering is missing
        - no limit-based components have a limit_period set
        - multiple limit-based components have different limit_periods (ambiguous)
        """
        offering = self._get_offering()
        if not offering:
            return None
        periods = set(
            offering.components.filter(billing_type=BillingTypes.LIMIT)
            .exclude(limit_period__isnull=True)
            .exclude(limit_period="")
            .values_list("limit_period", flat=True)
            .distinct()
        )
        if len(periods) == 1:
            return periods.pop()
        return None

    def calculate_slurm_settings(self, resource, config_override=None):
        """Calculate SLURM settings with configurable behavior and decay logic.

        Produces per-component TRES limits (e.g. {"cpu": X, "mem": Y, "billing": B}).
        """
        final_config = self._resolve_configuration(config_override)

        current_period = self._get_current_period()
        base_allocation = self._get_base_allocation(resource)

        logger.info(
            f"Calculating SLURM settings for resource {resource.uuid} in period {current_period}"
        )
        logger.debug(f"Base allocation: {base_allocation}, Config: {final_config}")

        # Calculate total allocation with carryover if enabled
        if final_config.get("carryover_enabled", True) and base_allocation:
            total_allocation, carryover_details = (
                self._calculate_allocation_with_carryover(
                    resource, base_allocation, current_period, final_config
                )
            )
        else:
            total_allocation = dict(base_allocation)
            carryover_details = {"carryover_applied": False}

        # Calculate SLURM-specific values
        fairshare = self._calculate_fairshare(total_allocation, final_config)

        # Convert per-component allocation to TRES minutes
        tres_minutes = self._calculate_tres_minutes(total_allocation, final_config)

        # Calculate QoS thresholds (uses billing metric)
        qos_threshold, grace_limit = self._calculate_qos_thresholds(
            total_allocation, final_config
        )

        # When grace is configured, the SLURM hard limit (GrpTRESMins) must be
        # set at the grace level, not the base level. Otherwise SLURM blocks jobs
        # at 100% and the QoS slowdown/blocked transitions in the 100%-130% range
        # are unreachable — the site agent can never apply them.
        grace_ratio = final_config.get("grace_ratio", 0)
        if grace_ratio > 0:
            grace_multiplier = 1 + grace_ratio
            tres_minutes = {
                k: int(v * grace_multiplier) for k, v in tres_minutes.items()
            }

        # Determine limit key from limit_type config
        limit_type = final_config.get("limit_type", "GrpTRESMins")
        if "GrpTRESMins" in limit_type:
            limit_key = "grp_tres_mins"
        elif "MaxTRESMins" in limit_type:
            limit_key = "max_tres_mins"
        else:
            limit_key = "grp_tres"

        limits = {limit_key: tres_minutes}

        # reset_raw_usage wipes SLURM's RawUsage counter on the account.
        # That belongs at the period boundary only — emitting it on every
        # 10-min sync turns GrpTRESMins into a non-budget (usage never
        # accumulates between resets). The sync task records the synced
        # period on each successful send via _record_synced_period; we
        # gate on that here.
        raw_usage_reset_configured = final_config.get("raw_usage_reset", True)
        last_synced_period = self._get_last_synced_period(resource)
        reset_raw_usage = bool(
            raw_usage_reset_configured and last_synced_period != current_period
        )

        settings = {
            "fairshare": fairshare,
            "qos_threshold": qos_threshold,
            "grace_limit": grace_limit,
            "limit_type": limit_type,
            "reset_raw_usage": reset_raw_usage,
            "carryover_details": carryover_details,
            **limits,
        }

        logger.info(
            f"Calculated SLURM settings: fairshare={fairshare}, "
            f"allocation={total_allocation}, "
            f"threshold={list(qos_threshold.values())[0] if qos_threshold else 'N/A'}"
        )

        return settings

    def _last_synced_period_cache_key(self, resource):
        return f"slurm_periodic_settings_period:{self.uuid}:{resource.uuid}"

    def _get_last_synced_period(self, resource):
        return cache.get(self._last_synced_period_cache_key(resource))

    def _record_synced_period(self, resource, period):
        # 90-day TTL keeps the marker alive across longer-than-monthly
        # periods. A stale cache (cleared, evicted) just means an extra
        # reset on the next sync — same as a fresh install.
        cache.set(
            self._last_synced_period_cache_key(resource),
            period,
            timeout=86400 * 90,
        )

    def _resolve_configuration(self, config_override=None):
        """Resolve configuration from multiple sources with proper precedence."""
        # Start with policy defaults
        config = {
            "limit_type": self.limit_type,
            "tres_billing_enabled": self.tres_billing_enabled,
            "tres_billing_weights": self.tres_billing_weights
            or self._get_default_tres_weights(),
            "carryover_factor": self.carryover_factor,
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
        """Get current period, preferring the offering component's limit_period.

        Falls back to the DB period field if no limit-based component exists
        or if the component's limit_period is empty/null.

        Returns:
            str: Period string in format appropriate for the period type:
                - MONTH_1: "YYYY-MM" (e.g. "2026-03")
                - MONTH_3: "YYYY-Q#" (e.g. "2026-Q1")
                - MONTH_12: "YYYY" (e.g. "2026")
                - TOTAL: "total"
        """
        limit_period = self._get_component_limit_period()
        effective_period = (
            LIMIT_PERIOD_TO_POLICY_PERIOD.get(limit_period) if limit_period else None
        ) or self.period
        now = core_utils.datetime.date.today()
        Periods = invoices_models.PeriodMixin.Periods

        if effective_period == Periods.MONTH_1:
            return f"{now.year}-{now.month:02d}"
        elif effective_period == Periods.MONTH_12:
            return f"{now.year}"
        elif effective_period == Periods.TOTAL:
            return "total"
        else:
            # MONTH_3 (quarterly) - existing behavior
            quarter = (now.month - 1) // 3 + 1
            return f"{now.year}-Q{quarter}"

    def _get_base_allocation(self, resource):
        """Get base allocation for resource from resource.limits.

        Returns:
            dict[str, float]: Per-component allocation, e.g. {"cpu": 64000, "mem": 512000}
        """
        if hasattr(resource, "limits") and resource.limits:
            return {k: float(v) for k, v in resource.limits.items() if v}

        logger.warning(
            f"No limits found for resource {resource.uuid}, returning empty allocation"
        )
        return {}

    def _calculate_allocation_with_carryover(
        self, resource, base_allocation, current_period, config
    ):
        """Calculate per-component allocation with carryover logic.

        Args:
            resource: Resource object
            base_allocation: dict[str, float] - per-component base allocation
            current_period: Period string e.g. '2024-Q2'
            config: Policy configuration dict

        Returns:
            tuple[dict[str, float], dict]: (total_allocation, carryover_details)
        """
        if not config.get("carryover_enabled", True):
            return dict(base_allocation), {
                "carryover_applied": False,
                "reason": "carryover_disabled",
            }

        previous_period = self._get_previous_period(current_period)
        if not previous_period:
            return dict(base_allocation), {
                "carryover_applied": False,
                "reason": "no_previous_period",
            }

        previous_usage = self._get_previous_period_usage(resource, previous_period)

        carryover_pct = config.get("carryover_factor", 50)
        carryover_ratio = carryover_pct / 100.0

        total_allocation = {}
        per_component = {}

        for comp, base in base_allocation.items():
            prev_use = previous_usage.get(comp, 0.0)
            unused = max(0, base - prev_use)
            cap = carryover_ratio * base
            carry = min(unused, cap)
            total_allocation[comp] = base + carry
            per_component[comp] = {
                "base": base,
                "previous_usage": prev_use,
                "unused": unused,
                "carryover_cap": cap,
                "carryover": carry,
                "total": base + carry,
            }

        carryover_details = {
            "carryover_applied": True,
            "previous_period": previous_period,
            "carryover_factor": carryover_pct,
            "per_component": per_component,
        }

        logger.debug(
            f"Per-component carryover for resource {resource.uuid}: {per_component}"
        )

        return total_allocation, carryover_details

    def _get_previous_period(self, current_period):
        """Get the previous period for a given period string.

        Detects period format from the string:
        - "YYYY-MM" (monthly): go back one month
        - "YYYY-Q#" (quarterly): go back one quarter
        - "YYYY" (annual): go back one year
        - "total": no previous period
        """
        try:
            if current_period == "total":
                return None

            if "-Q" in current_period:
                # Quarterly: "2024-Q2" format
                year_str, q_str = current_period.split("-Q")
                year = int(year_str)
                quarter = int(q_str)

                if quarter == 1:
                    return f"{year - 1}-Q4"
                else:
                    return f"{year}-Q{quarter - 1}"

            elif "-" in current_period:
                # Monthly: "2026-03" format
                year_str, month_str = current_period.split("-")
                year = int(year_str)
                month = int(month_str)

                if month == 1:
                    return f"{year - 1}-12"
                else:
                    return f"{year}-{month - 1:02d}"

            else:
                # Annual: "2026" format
                year = int(current_period)
                return f"{year - 1}"

        except (ValueError, AttributeError):
            logger.error(f"Failed to parse period: {current_period}")
            return None

    def _get_period_date_range(self, period):
        """Parse a period string into (start_date, end_date).

        Supports formats:
        - "YYYY-MM" (monthly): e.g. "2026-03" → (2026-03-01, 2026-03-31)
        - "YYYY-Q#" (quarterly): e.g. "2026-Q1" → (2026-01-01, 2026-03-31)
        - "YYYY" (annual): e.g. "2026" → (2026-01-01, 2026-12-31)
        - "total": returns None (no date range)

        Returns:
            tuple[datetime.date, datetime.date] or None on parse error
        """
        try:
            if period == "total":
                return None

            if "-Q" in period:
                # Quarterly: "2024-Q2" format
                year_str, q_str = period.split("-Q")
                year = int(year_str)
                quarter = int(q_str)

                start_month = (quarter - 1) * 3 + 1
                start_date = datetime.date(year, start_month, 1)

                if quarter == 4:
                    end_date = datetime.date(year, 12, 31)
                else:
                    next_quarter_start = datetime.date(year, start_month + 3, 1)
                    end_date = next_quarter_start - datetime.timedelta(days=1)

                return start_date, end_date

            elif "-" in period:
                # Monthly: "2026-03" format
                year_str, month_str = period.split("-")
                year = int(year_str)
                month = int(month_str)

                start_date = datetime.date(year, month, 1)
                # Last day of the month
                if month == 12:
                    end_date = datetime.date(year, 12, 31)
                else:
                    end_date = datetime.date(year, month + 1, 1) - datetime.timedelta(
                        days=1
                    )

                return start_date, end_date

            else:
                # Annual: "2026" format
                year = int(period)
                start_date = datetime.date(year, 1, 1)
                end_date = datetime.date(year, 12, 31)
                return start_date, end_date

        except (ValueError, AttributeError):
            logger.error(f"Failed to parse period: {period}")
            return None

    def _get_period_usage(self, resource, period):
        """Get per-component usage for resource in the given period.

        Returns:
            dict[str, float]: Usage per component type, e.g. {"cpu": 3200.0, "mem": 128000.0}
        """
        date_range = self._get_period_date_range(period)

        qs = marketplace_models.ComponentUsage.objects.filter(
            resource=resource,
        ).select_related("component")

        if date_range:
            start_date, end_date = date_range
            qs = qs.filter(
                billing_period__gte=start_date,
                billing_period__lte=end_date,
            )
        elif period != "total":
            # Unknown period format with no date range — return empty
            return {}

        result = {}
        for usage in qs:
            comp_type = usage.component.type
            result[comp_type] = result.get(comp_type, 0.0) + float(usage.usage)

        return result

    def _get_previous_period_usage(self, resource, previous_period):
        """Get per-component usage for resource in previous period.

        Returns:
            dict[str, float]: Usage per component type
        """
        return self._get_period_usage(resource, previous_period)

    def _calculate_fairshare(self, allocation, config):
        """Calculate fairshare value based on allocation.

        Args:
            allocation: dict[str, float] per-component allocation, or float for backward compat
            config: Policy configuration dict
        """
        # Fairshare is account-level (single value in SLURM) — use sum of all components
        if isinstance(allocation, dict):
            total = sum(allocation.values()) if allocation else 0
        else:
            total = allocation
        num_accounts = 3  # Conservative estimate for fairshare calculation
        return max(1, int(total // num_accounts))

    def _calculate_tres_minutes(self, allocation, config):
        """Convert per-component allocation (hours) to TRES minutes.

        Args:
            allocation: dict[str, float] - per-component allocation in hours
            config: Policy configuration dict

        Returns:
            dict[str, int]: Per-component minutes, plus optional 'billing' key
                            if tres_billing_enabled.
        """
        # Convert each component: hours → minutes
        tres_minutes = {comp: int(hours * 60) for comp, hours in allocation.items()}

        if config.get("tres_billing_enabled", True):
            weights = config.get("tres_billing_weights", {})
            if weights:
                # Compute weighted billing sum
                billing_total = 0.0
                for comp, hours in allocation.items():
                    # Try to match component type to weight keys (case-insensitive)
                    weight = weights.get(comp, 0)
                    if not weight:
                        # Try uppercase match (weights use "CPU", limits use "cpu")
                        for wk, wv in weights.items():
                            if wk.lower() == comp.lower():
                                weight = wv
                                break
                    billing_total += hours * weight
                tres_minutes["billing"] = int(billing_total * 60)

        return tres_minutes

    def _calculate_qos_thresholds(self, total_allocation, config):
        """Calculate QoS thresholds for slowdown and blocking.

        QoS decisions use the unified billing metric (weighted sum of components).
        If TRES billing is disabled, falls back to sum of all component hours.

        Args:
            total_allocation: dict[str, float] per-component allocation in hours,
                              or float for backward compat
            config: Policy configuration dict

        Returns:
            tuple[dict, dict]: (qos_threshold, grace_limit) each with billing/node key
        """
        grace_ratio = config.get("grace_ratio", 0.2)
        tres_billing_enabled = config.get("tres_billing_enabled", True)

        # Compute scalar allocation value for threshold calculation
        if isinstance(total_allocation, dict):
            if tres_billing_enabled:
                weights = config.get("tres_billing_weights", {})
                if weights:
                    scalar = 0.0
                    for comp, hours in total_allocation.items():
                        weight = weights.get(comp, 0)
                        if not weight:
                            for wk, wv in weights.items():
                                if wk.lower() == comp.lower():
                                    weight = wv
                                    break
                        scalar += hours * weight
                else:
                    scalar = sum(total_allocation.values())
            else:
                scalar = sum(total_allocation.values())
        else:
            scalar = total_allocation

        qos_threshold_value = scalar
        grace_limit_value = scalar * (1 + grace_ratio)

        metric_key = "billing" if tres_billing_enabled else "node"
        qos_threshold = {metric_key: int(qos_threshold_value * 60)}
        grace_limit = {metric_key: int(grace_limit_value * 60)}

        return qos_threshold, grace_limit

    def is_triggered(self):
        """Check if policy should be triggered based on usage thresholds.

        Returns True if any resource exceeds the configured thresholds.
        This triggers the actions defined in the policy (e.g., request_downscaling, request_pausing).
        """
        # Get affected customers based on apply_to_all or organization_groups
        customers = self.get_affected_customers()

        # Get current period for usage calculation
        current_period = self._get_current_period()

        # Check usage for all resources in this offering
        resources = marketplace_models.Resource.objects.filter(
            project__customer__in=customers,
            offering=self.scope,
        ).exclude(
            state__in=(
                marketplace_models.ResourceStates.TERMINATED,
                marketplace_models.ResourceStates.TERMINATING,
            )
        )

        # Check each resource against thresholds
        for resource in resources:
            usage_percentage = self.get_resource_usage_percentage(
                resource, current_period
            )

            # Check if we've crossed any threshold
            if (
                "request_slurm_resource_pausing" in self.actions
                or "request_pausing" in self.actions
            ):
                # Check grace limit (typically 120%)
                grace_limit_percentage = (1 + self.grace_ratio) * 100
                if usage_percentage >= grace_limit_percentage:
                    logger.info(
                        f"Resource {resource.uuid} exceeds grace limit: {usage_percentage:.1f}% >= {grace_limit_percentage:.1f}%"
                    )
                    return True

            if (
                "request_slurm_resource_downscaling" in self.actions
                or "request_downscaling" in self.actions
            ):
                # Check normal threshold (100%)
                if usage_percentage >= 100:
                    logger.info(
                        f"Resource {resource.uuid} exceeds threshold: {usage_percentage:.1f}% >= 100%"
                    )
                    return True

            if (
                "notify_organization_owners" in self.actions
                or "notify_external_user" in self.actions
            ):
                # Notification threshold can be lower (e.g., 80%)
                notification_threshold = 80  # Could be made configurable
                if usage_percentage >= notification_threshold:
                    logger.info(
                        f"Resource {resource.uuid} exceeds notification threshold: {usage_percentage:.1f}% >= {notification_threshold}%"
                    )
                    return True

        return False

    def get_resource_usage_percentage(self, resource, current_period=None):
        """Calculate billing-weighted usage percentage for a resource.

        Uses TRES billing weights to produce a single percentage from
        per-component allocation and usage dicts.

        Returns:
            float: Usage percentage (0-inf), where 100 = full allocation used
        """
        if not current_period:
            current_period = self._get_current_period()

        base_allocation = self._get_base_allocation(resource)

        if self.carryover_enabled and base_allocation:
            total_allocation, _ = self._calculate_allocation_with_carryover(
                resource,
                base_allocation,
                current_period,
                {
                    "carryover_enabled": True,
                    "carryover_factor": self.carryover_factor,
                },
            )
        else:
            total_allocation = dict(base_allocation)

        current_usage = self._get_current_period_usage(resource, current_period)

        if not total_allocation:
            return 0

        # Compute billing-weighted scalar values for allocation and usage
        weights = self.tres_billing_weights or self._get_default_tres_weights()

        def _weighted_sum(data):
            total = 0.0
            for comp, value in data.items():
                weight = weights.get(comp, 0)
                if not weight:
                    for wk, wv in weights.items():
                        if wk.lower() == comp.lower():
                            weight = wv
                            break
                if weight:
                    total += value * weight
                else:
                    # No weight mapping — include raw value as fallback
                    total += value
            return total

        alloc_scalar = _weighted_sum(total_allocation)
        usage_scalar = _weighted_sum(current_usage)

        if alloc_scalar > 0:
            return (usage_scalar / alloc_scalar) * 100
        return 0

    def _get_current_period_usage(self, resource, current_period):
        """Get per-component usage for resource in current period.

        Delegates to the shared get_current_period_usage() so the UI
        panel and policy enforcement use the same ComponentUsage aggregation.

        Returns:
            dict[str, float]: Usage per component type
        """
        # Local import to avoid circular dependency:
        # policy.models → marketplace.utils → marketplace.models
        from waldur_mastermind.marketplace.utils import get_current_period_usage

        limit_period = self._get_component_limit_period()
        if not limit_period:
            limit_period = POLICY_PERIOD_TO_LIMIT_PERIOD.get(self.period)
        return get_current_period_usage(resource, limit_period=limit_period)

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
                logger.warning(
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
        from waldur_core.logging.enums import ObservableObjectType
        from waldur_mastermind.marketplace import utils as marketplace_utils

        from . import slurm_commands

        try:
            # Prepare STOMP message payload for periodic limits update
            message_payload = {
                "resource_uuid": str(resource.uuid),
                "backend_id": resource.backend_id,
                "offering_uuid": str(
                    resource.offering.uuid
                ),  # Required by prepare_messages
                "policy_uuid": str(self.uuid),
                "action": "apply_periodic_settings",
                "settings": settings,
                "timestamp": datetime.datetime.now().isoformat(),
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
                # Mark the period as synced so the next sync within this
                # same period emits reset_raw_usage=False.
                self._record_synced_period(resource, self._get_current_period())

                # Write SlurmCommandHistory records for the commands sent
                try:
                    account = resource.backend_id or resource.name
                    current_period = self._get_current_period()
                    preview_commands = slurm_commands.generate_preview_commands(
                        account=account,
                        settings=settings,
                        current_usage=0,
                        current_qos="normal",
                    )
                    for cmd in preview_commands:
                        SlurmCommandHistory.objects.create(
                            policy=self,
                            resource=resource,
                            billing_period=current_period,
                            command_type=cmd.get("type", "unknown"),
                            description=cmd.get("description", ""),
                            shell_command=cmd.get("command", ""),
                            parameters=cmd.get("parameters", {}),
                            execution_mode="production",
                        )
                except Exception as e:
                    logger.warning(
                        "Failed to create command history for resource %s: %s",
                        resource.uuid,
                        e,
                    )

                return True
            else:
                logger.warning(
                    "No STOMP messages prepared for resource %s (offering %s). "
                    "Ensure the site agent has periodic_limits.enabled=true "
                    "and has registered a queue for object_type=resource_periodic_limits.",
                    resource.backend_id,
                    resource.offering.uuid,
                )
                return False

        except Exception as e:
            logger.error(
                f"Failed to publish STOMP message for resource {resource.uuid}: {e}"
            )
            return False


class SlurmCommandHistory(core_models.UuidMixin, TimeStampedModel):
    """Stores history of SLURM commands executed via periodic policies.

    This model tracks:
    - Commands that were executed (or would be executed in emulator mode)
    - Command type, description, and actual shell command
    - Execution metadata: timestamp, mode, success/failure
    """

    policy = models.ForeignKey(
        "SlurmPeriodicUsagePolicy",
        on_delete=models.CASCADE,
        related_name="command_history",
    )
    resource = models.ForeignKey(
        "marketplace.Resource",
        on_delete=models.CASCADE,
        related_name="slurm_command_history",
    )
    billing_period = models.CharField(
        max_length=20,
        help_text=_("Billing period identifier, e.g. '2026-Q1'"),
    )

    # Command details
    command_type = models.CharField(
        max_length=50,
        help_text=_("Type of command: fairshare, limits, qos, reset_usage"),
    )
    description = models.TextField(
        help_text=_("Human-readable description of what the command does")
    )
    shell_command = models.TextField(
        help_text=_("Actual shell command that was/would be executed")
    )
    parameters = models.JSONField(
        default=dict,
        help_text=_("Command parameters as key-value pairs"),
    )

    # Execution metadata
    executed_at = models.DateTimeField(auto_now_add=True)
    execution_mode = models.CharField(
        max_length=20,
        choices=[("production", "Production"), ("emulator", "Emulator")],
        default="production",
        help_text=_("Whether command was executed in production or emulator mode"),
    )
    success = models.BooleanField(
        default=True,
        help_text=_("Whether the command execution was successful"),
    )
    error_message = models.TextField(
        blank=True,
        help_text=_("Error message if command execution failed"),
    )

    class Meta:
        ordering = ["-executed_at"]
        verbose_name = _("SLURM Command History")
        verbose_name_plural = _("SLURM Command History")
        indexes = [
            models.Index(fields=["resource", "billing_period"]),
            models.Index(fields=["policy", "billing_period"]),
        ]

    def __str__(self):
        return f"{self.command_type} for {self.resource} at {self.executed_at}"


class SlurmPolicyEvaluationLog(core_models.UuidMixin, TimeStampedModel):
    """Persistent log of SLURM periodic usage policy evaluations.

    Each record captures a single evaluation of a resource against a policy,
    including usage data, actions taken, state transitions, and site agent feedback.
    """

    policy = models.ForeignKey(
        "SlurmPeriodicUsagePolicy",
        on_delete=models.CASCADE,
        related_name="evaluation_logs",
    )
    resource = models.ForeignKey(
        "marketplace.Resource",
        on_delete=models.CASCADE,
        related_name="slurm_evaluation_logs",
    )
    billing_period = models.CharField(
        max_length=20,
        help_text=_("Billing period identifier, e.g. '2026-Q1'"),
    )

    # Usage data at evaluation time
    usage_percentage = models.FloatField(
        help_text=_("Resource usage percentage at the time of evaluation"),
    )
    grace_limit_percentage = models.FloatField(
        help_text=_("Grace limit percentage threshold (e.g. 120 for 20% grace)"),
    )

    # Actions and state transitions
    actions_taken = models.JSONField(
        default=list,
        help_text=_(
            "List of actions taken during this evaluation (e.g. ['pause', 'notify'])"
        ),
    )
    previous_state = models.JSONField(
        default=dict,
        help_text=_(
            "Resource state before evaluation: {paused: bool, downscaled: bool}"
        ),
    )
    new_state = models.JSONField(
        default=dict,
        help_text=_(
            "Resource state after evaluation: {paused: bool, downscaled: bool}"
        ),
    )

    # STOMP / site agent tracking
    stomp_message_sent = models.BooleanField(
        default=False,
        help_text=_("Whether a STOMP message was sent to the site agent"),
    )
    site_agent_confirmed = models.BooleanField(
        null=True,
        default=None,
        help_text=_(
            "Whether the site agent confirmed command execution (null = no response yet)"
        ),
    )
    site_agent_response = models.JSONField(
        null=True,
        default=None,
        help_text=_("Response payload from the site agent"),
    )

    # Timestamp
    evaluated_at = models.DateTimeField(
        auto_now_add=True,
        help_text=_("When this evaluation was performed"),
    )

    class Meta:
        ordering = ["-evaluated_at"]
        verbose_name = _("SLURM Policy Evaluation Log")
        verbose_name_plural = _("SLURM Policy Evaluation Logs")
        indexes = [
            models.Index(fields=["resource", "billing_period"]),
            models.Index(fields=["policy", "billing_period"]),
            models.Index(fields=["policy", "evaluated_at"]),
        ]

    def __str__(self):
        return (
            f"Evaluation of {self.resource} against policy {self.policy} "
            f"at {self.evaluated_at}: {self.usage_percentage:.1f}% usage"
        )
