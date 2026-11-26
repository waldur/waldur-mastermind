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


_unresolved_tickets_counter: Callable[[Any], int] | None = None
_quota_history_provider: QuotaHistoryProvider | None = None
_estimates_updater: Callable[[list[Any]], None] | None = None
_promotions_provider: PromotionsProvider | None = None
_freeipa_username_resolver: Callable[[Any], str] | None = None
_pid_actions_provider: PidActionsProvider | None = None


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
