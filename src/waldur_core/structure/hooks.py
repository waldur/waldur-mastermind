"""Extension registries for higher-layer apps integrating with ``waldur_core.structure``.

Apps such as ``waldur_mastermind.billing``, ``waldur_mastermind.invoices``,
``waldur_mastermind.marketplace_metrics``, and ``waldur_mastermind.marketplace_remote``
register their bulk fetchers, queryset expressions, VAT explainers, and admin task
dispatchers here from ``AppConfig.ready()`` so ``waldur_core.structure`` does not
import higher-layer apps directly.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProjectBulkFetcher:
    """Bulk data fetcher contributing one key to ``ProjectListSerializer`` context."""

    field_name: str
    bulk_key: str
    default_factory: Callable[[], Any]
    fetcher: Callable[[Any, list[int]], Any]


@dataclass(frozen=True)
class CustomerBulkFetcher:
    """Bulk data fetcher contributing one or more keys to ``CustomerListSerializer`` context."""

    name: str
    field_names: tuple[str, ...]
    defaults: dict[str, Callable[[], Any]]
    fetcher: Callable[[dict, list[int]], dict[str, Any]]


_project_bulk_fetchers: dict[str, ProjectBulkFetcher] = {}
_customer_bulk_fetchers: dict[str, CustomerBulkFetcher] = {}
_customer_vat_explainer: Callable[[Any, dict], tuple[Any, str | None] | None] | None = (
    None
)
_project_estimated_cost_subquery: Callable[[], Any] | None = None
_remote_project_sync_handler: Callable[[list[str]], Any] | None = None
_remote_project_clean_handler: Callable[[], Any] | None = None


def register_project_bulk_fetcher(
    field_name: str,
    bulk_key: str,
    default_factory: Callable[[], Any],
    fetcher: Callable[[Any, list[int]], Any],
) -> None:
    """Register a bulk data loader for ``ProjectListSerializer``."""
    _project_bulk_fetchers[bulk_key] = ProjectBulkFetcher(
        field_name=field_name,
        bulk_key=bulk_key,
        default_factory=default_factory,
        fetcher=fetcher,
    )


def get_project_bulk_fetchers() -> tuple[ProjectBulkFetcher, ...]:
    return tuple(_project_bulk_fetchers.values())


def register_customer_bulk_fetcher(
    name: str,
    field_names: tuple[str, ...],
    defaults: dict[str, Callable[[], Any]],
    fetcher: Callable[[dict, list[int]], dict[str, Any]],
) -> None:
    """Register a bulk data loader for ``CustomerListSerializer``."""
    _customer_bulk_fetchers[name] = CustomerBulkFetcher(
        name=name,
        field_names=field_names,
        defaults=defaults,
        fetcher=fetcher,
    )


def get_customer_bulk_fetchers() -> tuple[CustomerBulkFetcher, ...]:
    return tuple(_customer_bulk_fetchers.values())


def register_customer_vat_explainer(
    explainer: Callable[[Any, dict], tuple[Any, str | None] | None],
) -> None:
    """Register ``explainer(customer, serializer_context) -> (percent, rule_label) | None``."""
    global _customer_vat_explainer
    _customer_vat_explainer = explainer


def explain_customer_vat(customer: Any, context: dict) -> tuple[Any, str | None] | None:
    if _customer_vat_explainer is None:
        return None
    return _customer_vat_explainer(customer, context)


def register_project_estimated_cost_subquery(provider: Callable[[], Any]) -> None:
    """Register a callable returning a Django Subquery for a project's estimated cost."""
    global _project_estimated_cost_subquery
    _project_estimated_cost_subquery = provider


def get_project_estimated_cost_subquery() -> Any:
    if _project_estimated_cost_subquery is None:
        return None
    return _project_estimated_cost_subquery()


def register_remote_project_sync_handler(handler: Callable[[list[str]], Any]) -> None:
    """Register a handler scheduling remote synchronization for serialized projects."""
    global _remote_project_sync_handler
    _remote_project_sync_handler = handler


def schedule_remote_project_sync(serialized_projects: list[str]) -> bool:
    if _remote_project_sync_handler is None:
        return False
    _remote_project_sync_handler(serialized_projects)
    return True


def register_remote_project_clean_handler(handler: Callable[[], Any]) -> None:
    """Register a handler scheduling cleanup of stale remote projects."""
    global _remote_project_clean_handler
    _remote_project_clean_handler = handler


def schedule_remote_projects_cleanup() -> bool:
    if _remote_project_clean_handler is None:
        return False
    _remote_project_clean_handler()
    return True
