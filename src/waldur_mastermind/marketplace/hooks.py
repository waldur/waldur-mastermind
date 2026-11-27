"""Extension registries for higher-layer apps integrating with ``waldur_mastermind.marketplace``.

Higher-layer supporting and integration apps (such as ``support``, ``analytics``,
``billing``, ``promotions``, ``waldur_freeipa``, and ``waldur_pid``) register
their callbacks here from ``AppConfig.ready()`` so ``waldur_mastermind.marketplace``
does not import upward across layers.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class QuotaHistoryProvider:
    """Provider recording and querying daily quota history (e.g. ``analytics``)."""

    record_daily_quotas: Callable[[list[tuple[Any, str, int, date]]], None]
    get_daily_quota_history: Callable[[Any, str], list[dict[str, Any]]]


@dataclass(frozen=True)
class PromotionsProvider:
    """Provider for campaign discounts and provider statistics (e.g. ``promotions``)."""

    create_discounted_resource_on_activation: Callable[[Any], None]
    get_discount_for_resource: Callable[[Any, int, int, Any], tuple[Any | None, Any]]
    count_active_campaigns: Callable[[Any, date], int]


@dataclass(frozen=True)
class PidActionsProvider:
    """Provider for DataCite PID admin actions on offerings (e.g. ``waldur_pid``)."""

    create_doi: Callable[[Any], None]
    update_doi: Callable[[Any], None]
    schedule_link_doi_with_collection: Callable[[str], None]
    schedule_update_referrable: Callable[[str], None]


@dataclass(frozen=True)
class OpenStackAdminProvider:
    """Provider for OpenStack-specific Django admin actions on resources."""

    get_restore_limits_executor: Callable[[], Any]
    schedule_create_lost_resources: Callable[[], None]


@dataclass(frozen=True)
class OpenStackInstanceReportProvider:
    """Provider for OpenStack instance list and aggregation reports."""

    get_instance_queryset: Callable[[Any], Any]
    serialize_instances: Callable[[Any], list[dict[str, Any]]]
    aggregate_instances: Callable[
        [Any, dict[str, dict[str, Any]]], list[dict[str, Any]]
    ]


@dataclass(frozen=True)
class SiteAgentProvider:
    """Provider for site-agent messaging and config generation."""

    push_resource_update_message: Callable[[Any], None]
    generate_agent_config: Callable[[list[Any], dict[str, Any], str], Any]
    offering_agent_config: Callable[[Any], dict[str, Any]]
    # (request, view, offering, validated request) -> the import's response data
    import_agent_configuration: Callable[
        [Any, Any, Any, dict[str, Any]], dict[str, Any]
    ]


@dataclass(frozen=True)
class PolicyProvider:
    """Provider for policy triggers, SLURM periodic policy checks, and rebill preview."""

    on_component_usage_billed: Callable[[Any, bool], None]
    schedule_slurm_policy_evaluation: Callable[[int, str], None]
    has_slurm_periodic_policy: Callable[[Any], bool]
    snapshot_estimated_cost_policies: Callable[[Any], list[tuple[Any, bool]]]
    get_policy_scope_label: Callable[[Any], str]
    preview_policy_action_resources: Callable[
        [Any, str, dict[str, Any], bool], tuple[bool, list[Any] | None]
    ]


@dataclass(frozen=True)
class ProposalProvider:
    """Provider for proposal-based offering annotations and order award filters."""

    filter_orders_from_award: Callable[[Any, bool], Any]
    annotate_offerings_open_for_proposals: Callable[[Any], Any]
    is_offering_open_for_proposals: Callable[[Any], bool]
    offering_ids_in_active_calls: Callable[[], Any]
    offering_ids_open_for_proposals: Callable[[], Any]


_unresolved_tickets_counter: Callable[[Any], int] | None = None
_quota_history_provider: QuotaHistoryProvider | None = None
_estimates_updater: Callable[[list[Any]], None] | None = None
_promotions_provider: PromotionsProvider | None = None
_freeipa_username_resolver: Callable[[Any], str] | None = None
_pid_actions_provider: PidActionsProvider | None = None
_openstack_admin_provider: OpenStackAdminProvider | None = None
_mb_component_types_provider: Callable[[], frozenset[str]] | None = None
_openstack_instance_report_provider: OpenStackInstanceReportProvider | None = None
_site_agent_provider: SiteAgentProvider | None = None
_policy_provider: PolicyProvider | None = None
_proposal_provider: ProposalProvider | None = None


def register_unresolved_tickets_counter(counter: Callable[[Any], int]) -> None:
    """Register ``counter(active_resources_qs) -> int`` for service provider stats."""
    global _unresolved_tickets_counter
    _unresolved_tickets_counter = counter


def count_unresolved_tickets(active_resources: Any) -> int:
    if _unresolved_tickets_counter is None:
        return 0
    return _unresolved_tickets_counter(active_resources)


def register_quota_history_provider(provider: QuotaHistoryProvider) -> None:
    """Register daily quota history persistence and queries."""
    global _quota_history_provider
    _quota_history_provider = provider


def get_quota_history_provider() -> QuotaHistoryProvider | None:
    return _quota_history_provider


def record_daily_quotas(entries: list[tuple[Any, str, int, date]]) -> None:
    if _quota_history_provider is not None:
        _quota_history_provider.record_daily_quotas(entries)


def get_daily_quota_history(scope: Any, name: str) -> list[dict[str, Any]]:
    if _quota_history_provider is None:
        return []
    return _quota_history_provider.get_daily_quota_history(scope, name)


def register_estimates_updater(updater: Callable[[list[Any]], None]) -> None:
    """Register ``updater(scopes)`` to refresh cached billing price estimates."""
    global _estimates_updater
    _estimates_updater = updater


def update_estimates_for_scopes(scopes: list[Any]) -> None:
    if _estimates_updater is not None:
        _estimates_updater(scopes)


def register_promotions_provider(provider: PromotionsProvider) -> None:
    """Register campaign discount lookup and active campaign counter."""
    global _promotions_provider
    _promotions_provider = provider


def get_promotions_provider() -> PromotionsProvider | None:
    return _promotions_provider


def create_discounted_resource_on_activation(resource: Any) -> None:
    if _promotions_provider is not None:
        _promotions_provider.create_discounted_resource_on_activation(resource)


def get_discount_for_resource(
    resource: Any, year: int, month: int, unit_price: Any
) -> tuple[Any | None, Any]:
    if _promotions_provider is None:
        return None, unit_price
    return _promotions_provider.get_discount_for_resource(
        resource, year, month, unit_price
    )


def count_active_campaigns(service_provider: Any, today: date) -> int:
    if _promotions_provider is None:
        return 0
    return _promotions_provider.count_active_campaigns(service_provider, today)


def register_freeipa_username_resolver(resolver: Callable[[Any], str]) -> None:
    """Register ``resolver(user) -> str`` returning the user's FreeIPA username."""
    global _freeipa_username_resolver
    _freeipa_username_resolver = resolver


def resolve_freeipa_username(user: Any) -> str:
    if _freeipa_username_resolver is None:
        return ""
    return _freeipa_username_resolver(user)


def register_pid_actions_provider(provider: PidActionsProvider) -> None:
    """Register DataCite PID actions invoked from ``OfferingAdmin``."""
    global _pid_actions_provider
    _pid_actions_provider = provider


def get_pid_actions_provider() -> PidActionsProvider | None:
    return _pid_actions_provider


def register_openstack_admin_provider(provider: OpenStackAdminProvider) -> None:
    """Register OpenStack-specific admin actions invoked from ``ResourceAdmin``."""
    global _openstack_admin_provider
    _openstack_admin_provider = provider


def get_openstack_admin_provider() -> OpenStackAdminProvider | None:
    return _openstack_admin_provider


def register_mb_component_types_provider(
    provider: Callable[[], frozenset[str]],
) -> None:
    """Register provider returning MB-scaled component type names (e.g. OpenStack)."""
    global _mb_component_types_provider
    _mb_component_types_provider = provider


def get_mb_component_types() -> frozenset[str]:
    if _mb_component_types_provider is None:
        return frozenset()
    return _mb_component_types_provider()


def register_openstack_instance_report_provider(
    provider: OpenStackInstanceReportProvider,
) -> None:
    """Register OpenStack instance report queries for ``MarketplaceStatsViewSet``."""
    global _openstack_instance_report_provider
    _openstack_instance_report_provider = provider


def get_openstack_instance_report_provider() -> OpenStackInstanceReportProvider | None:
    return _openstack_instance_report_provider


def register_site_agent_provider(provider: SiteAgentProvider) -> None:
    """Register site-agent messaging and config builder callbacks."""
    global _site_agent_provider
    _site_agent_provider = provider


def get_site_agent_provider() -> SiteAgentProvider | None:
    return _site_agent_provider


def push_site_agent_resource_update(resource: Any) -> None:
    if _site_agent_provider is not None:
        _site_agent_provider.push_resource_update_message(resource)


def register_policy_provider(provider: PolicyProvider) -> None:
    """Register policy evaluation triggers, SLURM checks, and rebill preview helpers."""
    global _policy_provider
    _policy_provider = provider


def get_policy_provider() -> PolicyProvider | None:
    return _policy_provider


def on_component_usage_billed(usage: Any, created: bool) -> None:
    if _policy_provider is not None:
        _policy_provider.on_component_usage_billed(usage, created)


def schedule_slurm_policy_evaluation(offering_id: int, resource_uuid: str) -> None:
    if _policy_provider is not None:
        _policy_provider.schedule_slurm_policy_evaluation(offering_id, resource_uuid)


def has_slurm_periodic_policy(offering: Any) -> bool:
    if _policy_provider is None:
        return False
    return _policy_provider.has_slurm_periodic_policy(offering)


def register_proposal_provider(provider: ProposalProvider) -> None:
    """Register proposal award order filters and offering open-for-proposals helpers."""
    global _proposal_provider
    _proposal_provider = provider


def get_proposal_provider() -> ProposalProvider | None:
    return _proposal_provider


def filter_orders_from_award(queryset: Any, value: bool) -> Any:
    if _proposal_provider is None:
        return queryset.none() if value else queryset
    return _proposal_provider.filter_orders_from_award(queryset, value)


def annotate_offerings_open_for_proposals(queryset: Any) -> Any:
    if _proposal_provider is None:
        return queryset
    return _proposal_provider.annotate_offerings_open_for_proposals(queryset)


def is_offering_open_for_proposals(offering: Any) -> bool:
    if _proposal_provider is None:
        return False
    return _proposal_provider.is_offering_open_for_proposals(offering)


def offering_ids_in_active_calls() -> Any:
    if _proposal_provider is None:
        return ()
    return _proposal_provider.offering_ids_in_active_calls()


def offering_ids_open_for_proposals() -> Any:
    if _proposal_provider is None:
        return ()
    return _proposal_provider.offering_ids_open_for_proposals()


_service_settings_flavors_provider: Callable[[Any], Any] | None = None


def register_service_settings_flavors_provider(
    provider: Callable[[Any], Any],
) -> None:
    """Register ``provider(service_settings)`` returning the flavors of a cloud.

    Each flavor has ``uuid``, ``name``, ``cores``, ``ram`` and ``disk``; the
    Kubernetes site checks compare them across OpenStack sites.
    """
    global _service_settings_flavors_provider
    _service_settings_flavors_provider = provider


def get_service_settings_flavors(service_settings: Any) -> Any:
    if _service_settings_flavors_provider is None:
        return []
    return _service_settings_flavors_provider(service_settings)
