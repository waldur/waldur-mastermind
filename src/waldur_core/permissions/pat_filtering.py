"""List-endpoint queryset filtering for scoped PersonalAccessTokens.

When a request authenticated by a PAT with non-empty ``allowed_scopes``
hits a list endpoint, this backend narrows the queryset to objects whose
ancestor chain (per :func:`get_scope_ancestors`) intersects one of the
PAT's bindings.

Rules are registered per-model via :func:`register_pat_filter`. Models
without a registered rule pass through unfiltered — that's documented as
partial coverage on the API doc.
"""

from collections.abc import Callable
from functools import reduce
from operator import or_

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from rest_framework.filters import BaseFilterBackend

from waldur_core.core.models import User
from waldur_core.permissions.enums import TYPE_KEY_BY_CT
from waldur_core.permissions.utils import _is_pat_auth, _pat_allowed_pairs

# Builders return None when no binding matches the model — the caller
# should treat that as ``queryset.none()`` to deny everything.
FilterBuilder = Callable[[dict[str, set[int]]], Q | None]

_FILTER_BUILDERS: dict[type, FilterBuilder] = {}


def register_pat_filter(model):
    """Register a filter builder for a model.

    The builder receives a dict mapping TYPE_MAP keys to sets of object
    IDs the caller's PAT is bound to, and returns a Q expression that
    selects objects reachable from those bindings (or None for "no
    reachable objects").
    """

    def decorator(fn: FilterBuilder) -> FilterBuilder:
        _FILTER_BUILDERS[model] = fn
        return fn

    return decorator


def _or(*qs: Q | None) -> Q | None:
    filtered = [q for q in qs if q is not None]
    if not filtered:
        return None
    return reduce(or_, filtered)


def _ids_q(field: str, ids: set[int] | None) -> Q | None:
    if not ids:
        return None
    return Q(**{f"{field}__in": ids})


def _bindings_by_type(auth) -> dict[str, set[int]]:
    """Group the PAT's bindings into ``{type_key: {object_ids}}``."""
    by_type: dict[str, set[int]] = {}
    for ct_id, obj_id in _pat_allowed_pairs(auth):
        try:
            ct = ContentType.objects.get_for_id(ct_id)
        except ContentType.DoesNotExist:
            continue
        type_key = TYPE_KEY_BY_CT.get((ct.app_label, ct.model))
        if type_key:
            by_type.setdefault(type_key, set()).add(obj_id)
    return by_type


class PATScopeListFilter(BaseFilterBackend):
    """DRF filter backend: restrict list & detail querysets by PAT bindings.

    Installed globally via ``_install_global_filter`` rather than via
    ``DEFAULT_FILTER_BACKENDS``, because most Waldur viewsets override
    ``filter_backends`` (which suppresses the global default). The
    install hooks into ``GenericAPIView.filter_queryset`` so the PAT
    filter always runs after the viewset's own backends, regardless of
    viewset configuration. Detail endpoints get the same treatment for
    free — ``get_object`` calls ``filter_queryset`` before lookup.
    """

    def filter_queryset(self, request, queryset, view):
        if isinstance(request, User):
            return queryset
        auth = getattr(request, "auth", None)
        if not _is_pat_auth(auth):
            return queryset
        if not _pat_allowed_pairs(auth):
            # Unscoped PAT — no entity restriction to apply.
            return queryset

        builder = _FILTER_BUILDERS.get(queryset.model)
        if builder is None:
            # No rule registered for this model — pass through unfiltered.
            return queryset

        q = builder(_bindings_by_type(auth))
        if q is None:
            return queryset.none()
        return queryset.filter(q).distinct()


def _install_global_filter() -> None:
    """Patch ``GenericAPIView.filter_queryset`` to always apply the PAT filter.

    Idempotent — installs once even if called twice. Done at app ready()
    so DRF is fully imported before we touch it.
    """
    from rest_framework.generics import GenericAPIView

    if getattr(GenericAPIView.filter_queryset, "_pat_patched", False):
        return

    original = GenericAPIView.filter_queryset
    pat_filter = PATScopeListFilter()

    def filter_queryset(self, queryset):
        queryset = original(self, queryset)
        return pat_filter.filter_queryset(self.request, queryset, self)

    filter_queryset._pat_patched = True
    GenericAPIView.filter_queryset = filter_queryset


# ---------------------------------------------------------------------------
# Per-model rules. Each rule mirrors the upward walk in
# `get_scope_ancestors`, but expressed as queryset filters back down.
# ---------------------------------------------------------------------------


def _register_rules() -> None:
    """Register filter rules for the 9 TYPE_MAP entity models.

    Done in a function so the imports stay local — these modules import
    from each other and from this one transitively, so importing them at
    module load time of `pat_filtering.py` would risk cycles.
    """
    from waldur_core.structure import models as structure_models
    from waldur_mastermind.marketplace import models as marketplace_models
    from waldur_mastermind.proposal import models as proposal_models

    @register_pat_filter(structure_models.Customer)
    def _customer(ids):
        return _ids_q("id", ids.get("customer"))

    @register_pat_filter(structure_models.Project)
    def _project(ids):
        return _or(
            _ids_q("id", ids.get("project")),
            _ids_q("customer_id", ids.get("customer")),
        )

    @register_pat_filter(marketplace_models.Offering)
    def _offering(ids):
        return _or(
            _ids_q("id", ids.get("offering")),
            _ids_q("customer_id", ids.get("customer")),
        )

    @register_pat_filter(marketplace_models.Resource)
    def _resource(ids):
        # NOTE: ``Resource.customer`` is a property that returns
        # ``project.customer`` — the *consumer* customer. ``get_scope_ancestors``
        # therefore only reaches ``project.customer``, never ``offering.customer``.
        # We must mirror that exactly: a binding to the offering's owner customer
        # (e.g. a service provider) is NOT permitted to act on resources sold via
        # that offering when the consumer is a different customer, so listing
        # those resources here would over-disclose.
        return _or(
            _ids_q("id", ids.get("resource")),
            _ids_q("offering_id", ids.get("offering")),
            _ids_q("project_id", ids.get("project")),
            _ids_q("project__customer_id", ids.get("customer")),
        )

    @register_pat_filter(marketplace_models.ResourceProject)
    def _resource_project(ids):
        # Same reasoning as ``_resource`` — only the consumer-side customer
        # chain is reachable from ``get_scope_ancestors``.
        return _or(
            _ids_q("id", ids.get("resource_project")),
            _ids_q("resource_id", ids.get("resource")),
            _ids_q("resource__offering_id", ids.get("offering")),
            _ids_q("resource__project_id", ids.get("project")),
            _ids_q("resource__project__customer_id", ids.get("customer")),
        )

    @register_pat_filter(marketplace_models.ServiceProvider)
    def _service_provider(ids):
        return _or(
            _ids_q("id", ids.get("service_provider")),
            _ids_q("customer_id", ids.get("customer")),
        )

    @register_pat_filter(proposal_models.CallManagingOrganisation)
    def _call_organizer(ids):
        return _or(
            _ids_q("id", ids.get("call_organizer")),
            _ids_q("customer_id", ids.get("customer")),
        )

    @register_pat_filter(proposal_models.Call)
    def _call(ids):
        # `get_scope_ancestors` does not walk a Call's manager, so we
        # don't either — bindings on customer/manager don't reach Call.
        return _ids_q("id", ids.get("call"))

    @register_pat_filter(proposal_models.Proposal)
    def _proposal(ids):
        # Same — no ancestor inheritance for Proposal.
        return _ids_q("id", ids.get("proposal"))
