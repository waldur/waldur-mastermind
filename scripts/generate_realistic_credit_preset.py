#!/usr/bin/env python3
"""Generate a realistic credit-management demo preset for tool validation.

Builds a self-contained JSON preset shaped after typical HPC-platform
production distributions (3 customers small/mid/large, ~30 projects,
mixed credit configurations covering every utilisation bucket, 6 months
of invoices and component usages, fired policies).

Output structure exercises the full surface of the new credit-explainer
chat tools (explain_project_credit_balance, list_overdrawn_projects)
plus prepares ground for resource-state and policy-fired scenarios.

Run::

    python scripts/generate_realistic_credit_preset.py \\
        --output src/waldur_mastermind/marketplace/demo_presets/presets/credit_realistic.json \\
        --seed 42

Then load with::

    waldur demo_presets load credit_realistic -y
"""

from __future__ import annotations

import argparse
import json
import random
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import UTC, date
from decimal import Decimal
from pathlib import Path

# --------------------------------------------------------------------------
# UUID helpers — preset UUIDs are 32-char hex (no hyphens). Prefix scheme
# follows CLAUDE.md.

UUID_PREFIXES = {
    "user": "00",
    "customer": "a3",
    "project": "c3",
    "resource": "d3",
    "offering": "f3",
    "plan": "b3",
    "category": "e3",
    "category_group": "e4",
    "order": "73",
    "policy": "93",
    "offering_component": "ce",
    "plan_component": "cf",
    "customer_credit": "cc",  # "cca…"
    "project_credit": "cd",  # "cdb…"
    "invoice": "cc",  # "ccc…"
    "invoice_item": "cc",  # "ccd…"
    "component_usage": "ca",
    "service_provider": "aa",
    "user_role": "01",
    "user_agreement": "02",
}


def make_uuid(kind: str, n: int, sub: str = "0") -> str:
    """Return a 32-hex-char UUID for the given entity kind and counter."""
    base = UUID_PREFIXES[kind]
    body = f"{n:028d}"
    out = f"{base}{sub}{body}"
    if len(out) > 32:
        out = out[:32]
    elif len(out) < 32:
        out = out.ljust(32, "0")
    return out.replace("-", "")[:32]


# --------------------------------------------------------------------------
# Bucket spec — drives every project's spend pattern. Each bucket is a
# (utilisation ratio, count) pair: ratio is "spent / project_credit_value",
# applied AFTER credit value is set per-project.

UTILISATION_BUCKETS = [
    # (label, ratio_lo, ratio_hi, count, credit_value_band)
    ("unspent", 0.0, 0.0, 4, (5_000, 30_000)),
    ("light", 0.05, 0.45, 6, (10_000, 40_000)),
    ("warning", 0.55, 0.85, 4, (15_000, 35_000)),
    ("critical", 0.90, 0.99, 2, (20_000, 30_000)),
    ("mild_overdraw", 1.05, 1.45, 2, (5_000, 20_000)),
    ("severe_overdraw", 2.0, 3.5, 1, (1_000, 5_000)),
    # No project credit at all — spend still recorded.
    ("no_credit", None, None, 11, (0, 0)),
]


@dataclass
class CustomerSpec:
    name: str
    short: str  # used in project naming
    project_count: int
    has_customer_credit: bool
    customer_credit_value: int
    customer_credit_expected: int
    members: list[str] = field(default_factory=list)


CUSTOMER_SPECS = [
    CustomerSpec(
        name="Acme Research Lab",
        short="acme",
        project_count=5,
        has_customer_credit=True,
        customer_credit_value=100_000,
        customer_credit_expected=80_000,
        members=["acme_owner", "acme_manager", "acme_member"],
    ),
    CustomerSpec(
        name="Bluewave Genomics",
        short="bluewave",
        project_count=15,
        has_customer_credit=True,
        customer_credit_value=750_000,
        customer_credit_expected=600_000,
        members=["bw_owner", "bw_manager", "bw_member"],
    ),
    CustomerSpec(
        name="Crestford Climate Centre",
        short="crestford",
        project_count=10,
        has_customer_credit=False,
        customer_credit_value=0,
        customer_credit_expected=0,
        members=["cf_owner", "cf_manager", "cf_member"],
    ),
]

# Project-name fragments to pick from (deterministic given seed).
PROJECT_NAME_FRAGMENTS = [
    "Atlas",
    "Bonfire",
    "Comet",
    "Delta",
    "Elysium",
    "Fjord",
    "Glacier",
    "Helios",
    "Ionia",
    "Juniper",
    "Kestrel",
    "Lumen",
    "Mistral",
    "Nimbus",
    "Onyx",
    "Pioneer",
    "Quantum",
    "Reverie",
    "Sirius",
    "Tundra",
    "Umbra",
    "Vela",
    "Wraith",
    "Xanadu",
    "Yarrow",
    "Zenith",
    "Alpha",
    "Beta",
    "Gamma",
    "Delta II",
]


# --------------------------------------------------------------------------


class CreditPresetGenerator:
    def __init__(self, months: int = 6, seed: int = 42):
        self.months = months
        self.rng = random.Random(seed)
        self.preset: dict = {}

        # counters per kind
        self.cnt: dict[str, int] = {}

    def _next_uuid(self, kind: str, sub: str = "0") -> str:
        self.cnt[kind] = self.cnt.get(kind, 0) + 1
        return make_uuid(kind, self.cnt[kind], sub)

    # ---- billing periods --------------------------------------------------
    def _periods(self) -> list[tuple[int, int]]:
        today = date.today()
        out = []
        for i in range(self.months - 1, -1, -1):
            year, month = today.year, today.month - i
            while month <= 0:
                month += 12
                year -= 1
            out.append((year, month))
        return out

    # ---- skeleton: users, agreements, categories, offerings ----------------
    def build_skeleton(self) -> None:
        # Hard-coded staff/support seed users (always present, shared across presets).
        users = [
            {
                "uuid": self._next_uuid("user"),
                "username": "staff",
                "first_name": "Demo",
                "last_name": "Staff",
                "email": "staff@demo.waldur.com",
                "is_staff": True,
                "is_support": True,
                "is_active": True,
                "password": "demo",
            },
            {
                "uuid": self._next_uuid("user"),
                "username": "support",
                "first_name": "Demo",
                "last_name": "Support",
                "email": "support@demo.waldur.com",
                "is_staff": False,
                "is_support": True,
                "is_active": True,
                "password": "demo",
            },
        ]
        # Per-customer members.
        for spec in CUSTOMER_SPECS:
            for username in spec.members:
                users.append(
                    {
                        "uuid": self._next_uuid("user"),
                        "username": username,
                        "first_name": username.split("_")[0].title(),
                        "last_name": username.split("_")[1].title(),
                        "email": f"{username}@demo.waldur.com",
                        "is_staff": False,
                        "is_support": False,
                        "is_active": True,
                        "password": "demo",
                    }
                )
        self.preset["users"] = users

        # User agreements (TOS / Privacy) — required for new users.
        self.preset["user_agreements"] = [
            {
                "uuid": self._next_uuid("user_agreement"),
                "agreement_type": "TOS",
                "content": "Demo terms of service.",
            },
            {
                "uuid": self._next_uuid("user_agreement"),
                "agreement_type": "PP",
                "content": "Demo privacy policy.",
            },
        ]

        # One service-provider customer + 3 consumer customers.
        provider_customer = {
            "uuid": self._next_uuid("customer"),
            "name": "Demo Cloud Services",
            "abbreviation": "DCS",
            "country": "EE",
            "email": "ops@dcs.demo",
        }
        consumer_customers = []
        for spec in CUSTOMER_SPECS:
            consumer_customers.append(
                {
                    "uuid": self._next_uuid("customer"),
                    "name": spec.name,
                    "abbreviation": spec.short.upper(),
                    "country": "EE",
                    "email": f"contact@{spec.short}.demo",
                    "_spec": spec,  # private — stripped before write
                }
            )
        self.preset["customers"] = [
            {k: v for k, v in c.items() if not k.startswith("_")}
            for c in [provider_customer] + consumer_customers
        ]

        self.preset["service_providers"] = [
            {
                "uuid": self._next_uuid("service_provider"),
                "customer_uuid": provider_customer["uuid"],
                "description": "Demo provider used by credit_realistic preset.",
            }
        ]

        # Category + group
        cat_group_uuid = self._next_uuid("category_group")
        cat_uuid = self._next_uuid("category")
        self.preset["category_groups"] = [
            {
                "uuid": cat_group_uuid,
                "title": "Compute",
                "description": "Compute services.",
            }
        ]
        self.preset["categories"] = [
            {
                "uuid": cat_uuid,
                "title": "Virtual machines",
                "group_uuid": cat_group_uuid,
            }
        ]

        # One offering (USAGE-billed compute) — gives every project a
        # consistent shape so we can run aggregations cleanly.
        offering_uuid = self._next_uuid("offering")
        comp_cpu_uuid = self._next_uuid("offering_component")
        comp_gpu_uuid = self._next_uuid("offering_component")
        plan_uuid = self._next_uuid("plan")
        plan_cpu_uuid = self._next_uuid("plan_component")
        plan_gpu_uuid = self._next_uuid("plan_component")

        self.preset["offerings"] = [
            {
                "uuid": offering_uuid,
                "name": "Compute (Demo Realistic)",
                "type": "Marketplace.Basic",
                "billable": True,
                "customer_uuid": provider_customer["uuid"],
                "category_uuid": cat_uuid,
                "state": 3,  # active
                "shared": True,
                "description": "Demo compute offering with cpu+gpu components.",
                # supports_pausing is checked by the policy actions
                # queryset filter — required for paused resources to
                # appear in the data the new explain_resource_paused_reason
                # tool reads.
                "plugin_options": {"supports_pausing": True},
            }
        ]
        self.preset["offering_components"] = [
            {
                "uuid": comp_cpu_uuid,
                "offering_uuid": offering_uuid,
                "type": "cpu",
                "name": "CPU hours",
                "measured_unit": "hours",
                "billing_type": "usage",
            },
            {
                "uuid": comp_gpu_uuid,
                "offering_uuid": offering_uuid,
                "type": "gpu",
                "name": "GPU hours",
                "measured_unit": "hours",
                "billing_type": "usage",
            },
        ]
        self.preset["plans"] = [
            {
                "uuid": plan_uuid,
                "offering_uuid": offering_uuid,
                "name": "Standard",
                "unit": "month",
            }
        ]
        self.preset["plan_components"] = [
            {
                "uuid": plan_cpu_uuid,
                "plan_uuid": plan_uuid,
                "component_type": "cpu",
                "amount": 0,
                "price": "1.50",
            },
            {
                "uuid": plan_gpu_uuid,
                "plan_uuid": plan_uuid,
                "component_type": "gpu",
                "amount": 0,
                "price": "12.00",
            },
        ]

        # Stash for later phases.
        self._provider_customer = provider_customer
        self._consumer_customers = consumer_customers
        self._offering_uuid = offering_uuid
        self._cpu_component_uuid = comp_cpu_uuid
        self._gpu_component_uuid = comp_gpu_uuid
        self._plan_uuid = plan_uuid

    # ---- projects + resources + roles --------------------------------------
    def build_projects_and_resources(self) -> None:
        # Build 30 projects total (sum of CUSTOMER_SPECS.project_count) and
        # assign each to a utilisation bucket from UTILISATION_BUCKETS so the
        # downstream credit/spend phase can target ratios deterministically.
        bucket_pool = []
        for label, lo, hi, n, band in UTILISATION_BUCKETS:
            bucket_pool.extend([(label, lo, hi, band)] * n)
        # Guard: 30 projects expected.
        assert len(bucket_pool) == sum(s.project_count for s in CUSTOMER_SPECS), (
            f"bucket_pool={len(bucket_pool)} but project_count_total="
            f"{sum(s.project_count for s in CUSTOMER_SPECS)}"
        )
        self.rng.shuffle(bucket_pool)

        projects: list[dict] = []
        resources: list[dict] = []
        user_roles: list[dict] = []
        project_specs: list[dict] = []  # carries _bucket for spend phase
        name_pool = list(PROJECT_NAME_FRAGMENTS)
        self.rng.shuffle(name_pool)

        for cust in self._consumer_customers:
            spec: CustomerSpec = cust["_spec"]

            # Customer-owner role on this customer.
            owner_username = spec.members[0]
            owner_uuid = next(
                u["uuid"]
                for u in self.preset["users"]
                if u["username"] == owner_username
            )
            user_roles.append(
                {
                    "uuid": self._next_uuid("user_role"),
                    "user_username": owner_username,
                    "user_uuid": owner_uuid,
                    "role_name": "CUSTOMER.OWNER",
                    "scope_type": "structure.customer",
                    "scope_uuid": cust["uuid"],
                    "is_active": True,
                }
            )

            for _ in range(spec.project_count):
                bucket_label, lo, hi, band = bucket_pool.pop()
                pname = f"{spec.short.title()} {name_pool.pop()}"
                proj_uuid = self._next_uuid("project")
                proj = {
                    "uuid": proj_uuid,
                    "name": pname,
                    "customer_uuid": cust["uuid"],
                    "description": f"Project under {cust['name']}.",
                }
                projects.append(proj)
                project_specs.append(
                    {
                        "project_uuid": proj_uuid,
                        "customer_uuid": cust["uuid"],
                        "bucket": bucket_label,
                        "ratio": (self.rng.uniform(lo, hi) if lo is not None else None),
                        "credit_band": band,
                    }
                )

                # 1-3 resources per project.
                res_count = self.rng.choice([1, 1, 2, 2, 2, 3])
                for ri in range(res_count):
                    resources.append(
                        {
                            "uuid": self._next_uuid("resource"),
                            "name": f"{pname} VM {ri + 1}",
                            "offering_uuid": self._offering_uuid,
                            "plan_uuid": self._plan_uuid,
                            "project_uuid": proj_uuid,
                            "state": 2,  # OK
                            "limits": {
                                "cpu": self.rng.choice([2, 4, 8, 16]),
                                "ram": self.rng.choice([4, 8, 16, 32]),
                            },
                            "created": "2025-09-01T00:00:00Z",
                        }
                    )

                # Project member role (the customer's "_member" user).
                member_username = spec.members[2]
                member_uuid = next(
                    u["uuid"]
                    for u in self.preset["users"]
                    if u["username"] == member_username
                )
                user_roles.append(
                    {
                        "uuid": self._next_uuid("user_role"),
                        "user_username": member_username,
                        "user_uuid": member_uuid,
                        "role_name": "PROJECT.MEMBER",
                        "scope_type": "structure.project",
                        "scope_uuid": proj_uuid,
                        "is_active": True,
                    }
                )

        # Sanity: all bucket entries used.
        assert not bucket_pool, f"unused buckets: {bucket_pool}"

        self.preset["projects"] = projects
        self.preset["resources"] = resources
        self.preset["user_roles"] = user_roles
        self._project_specs = project_specs

    # ---- credits -----------------------------------------------------------
    def build_credits(self) -> None:
        # Customer credits (only customers flagged has_customer_credit).
        today = date.today()
        end_date_curr_year = date(today.year, 12, 1).isoformat()
        end_date_next_year = date(today.year + 1, 12, 1).isoformat()

        customer_credits: list[dict] = []
        for cust in self._consumer_customers:
            spec: CustomerSpec = cust["_spec"]
            if not spec.has_customer_credit:
                continue
            customer_credits.append(
                {
                    "uuid": self._next_uuid("customer_credit", sub="a"),
                    "customer_uuid": cust["uuid"],
                    "value": str(spec.customer_credit_value),
                    "expected_consumption": str(spec.customer_credit_expected),
                    "minimal_consumption_logic": "fixed",
                    "grace_coefficient": "10",
                    "apply_as_minimal_consumption": True,
                    "end_date": end_date_next_year,
                    "created": f"{today.year}-01-15T00:00:00",
                    "offering_uuids": [self._offering_uuid],
                }
            )
        self.preset["customer_credits"] = customer_credits

        # Project credits (skip the "no_credit" bucket).
        project_credits: list[dict] = []
        for spec in self._project_specs:
            if spec["bucket"] == "no_credit":
                spec["credit_value"] = 0
                continue
            value = self.rng.randint(*spec["credit_band"])
            spec["credit_value"] = value
            project_credits.append(
                {
                    "uuid": self._next_uuid("project_credit", sub="b"),
                    "project_uuid": spec["project_uuid"],
                    "value": str(value),
                    "end_date": (
                        end_date_curr_year
                        if self.rng.random() < 0.85
                        else end_date_next_year
                    ),
                    "grace_coefficient": "15",
                    "created": f"{today.year}-01-20T00:00:00",
                }
            )
        self.preset["project_credits"] = project_credits

    # ---- invoices + items + usages -----------------------------------------
    def build_billing(self) -> None:
        periods = self._periods()  # list[(year, month)]
        invoices: list[dict] = []
        items: list[dict] = []
        usages: list[dict] = []

        # One invoice per customer per month.
        invoice_lookup: dict[tuple[str, int, int], str] = {}
        for cust in self._consumer_customers:
            for year, month in periods:
                inv_uuid = self._next_uuid("invoice", sub="c")
                invoice_lookup[(cust["uuid"], year, month)] = inv_uuid
                invoices.append(
                    {
                        "uuid": inv_uuid,
                        "customer_uuid": cust["uuid"],
                        "year": year,
                        "month": month,
                        "state": "created",
                        "total_cost": "0",
                        "total_price": "0",
                        "tax_percent": "0",
                        "created": f"{year}-{month:02d}-01",
                    }
                )

        # Compute target lifetime spend per project from credit_value × ratio.
        for spec in self._project_specs:
            ratio = spec.get("ratio")
            credit_value = spec.get("credit_value", 0)
            if spec["bucket"] == "no_credit":
                # Still spend something — pick an absolute amount.
                target_spend = float(self.rng.randint(500, 8_000))
            elif ratio is None or ratio == 0.0:
                target_spend = 0.0
            else:
                target_spend = float(credit_value) * ratio
            spec["target_spend"] = target_spend

            if target_spend == 0.0:
                continue

            # Distribute target spend across the 6 months. Front-loaded for
            # overdrawn buckets so the most-recent month shows the spike.
            weights = [self.rng.uniform(0.7, 1.3) for _ in periods]
            if "overdraw" in spec["bucket"]:
                # bias toward later months
                weights = [w * (0.6 + 0.15 * i) for i, w in enumerate(weights)]
            wsum = sum(weights)
            month_spends = [target_spend * w / wsum for w in weights]

            # Resources for this project — distribute month spend across
            # them via cpu/gpu invoice items.
            proj_resources = [
                r
                for r in self.preset["resources"]
                if r["project_uuid"] == spec["project_uuid"]
            ]
            if not proj_resources:
                continue

            for (year, month), spend in zip(periods, month_spends):
                inv_uuid = invoice_lookup[(spec["customer_uuid"], year, month)]
                # Split month spend across resources, then into cpu+gpu lines.
                per_res = spend / len(proj_resources)
                for res in proj_resources:
                    cpu_share = self.rng.uniform(0.55, 0.85)
                    cpu_total = per_res * cpu_share
                    gpu_total = per_res - cpu_total

                    days = monthrange(year, month)[1]
                    end_iso = f"{year}-{month:02d}-{days:02d}T23:59:59"
                    start_iso = f"{year}-{month:02d}-01T00:00:00"

                    # CPU line: pick quantity in 100..3000 hours, derive unit_price.
                    cpu_qty = self.rng.randint(100, 3000)
                    cpu_unit_price = round(cpu_total / cpu_qty, 4) if cpu_qty else "0"
                    items.append(
                        {
                            "uuid": self._next_uuid("invoice_item", sub="d"),
                            "invoice_uuid": inv_uuid,
                            "resource_uuid": res["uuid"],
                            "project_uuid": spec["project_uuid"],
                            "name": f"{res['name']} (CPU hours)",
                            "quantity": cpu_qty,
                            "measured_unit": "hours",
                            "unit_price": str(cpu_unit_price),
                            "start": start_iso,
                            "end": end_iso,
                        }
                    )
                    # GPU line: smaller quantity, higher unit price.
                    gpu_qty = self.rng.randint(0, 200)
                    gpu_unit_price = round(gpu_total / gpu_qty, 4) if gpu_qty else "0"
                    if gpu_qty > 0:
                        items.append(
                            {
                                "uuid": self._next_uuid("invoice_item", sub="d"),
                                "invoice_uuid": inv_uuid,
                                "resource_uuid": res["uuid"],
                                "project_uuid": spec["project_uuid"],
                                "name": f"{res['name']} (GPU hours)",
                                "quantity": gpu_qty,
                                "measured_unit": "hours",
                                "unit_price": str(gpu_unit_price),
                                "start": start_iso,
                                "end": end_iso,
                            }
                        )

                    # Component usages — match the invoice item quantities so
                    # explain_project_credit_balance numbers reconcile.
                    usages.append(
                        {
                            "uuid": self._next_uuid("component_usage"),
                            "resource_uuid": res["uuid"],
                            "component_uuid": self._cpu_component_uuid,
                            "usage": str(cpu_qty),
                            "date": f"{year}-{month:02d}-15T12:00:00",
                            "billing_period": f"{year}-{month:02d}-01",
                            "recurring": False,
                            "description": f"CPU hours / {year}-{month:02d}",
                        }
                    )
                    if gpu_qty > 0:
                        usages.append(
                            {
                                "uuid": self._next_uuid("component_usage"),
                                "resource_uuid": res["uuid"],
                                "component_uuid": self._gpu_component_uuid,
                                "usage": str(gpu_qty),
                                "date": f"{year}-{month:02d}-15T12:00:00",
                                "billing_period": f"{year}-{month:02d}-01",
                                "recurring": False,
                                "description": f"GPU hours / {year}-{month:02d}",
                            }
                        )

        self.preset["invoices"] = invoices
        self.preset["invoice_items"] = items
        self.preset["component_usages"] = usages

    # ---- policies ----------------------------------------------------------
    def build_policies(self) -> None:
        """Add cost policies; for fired ones, also mark resources paused.

        Mirrors what the production ``request_pausing`` action does in
        ``policy/policy_actions.py``: sets ``Resource.paused=True`` and
        writes the ``_policy_attribution.paused`` blob inside
        ``Resource.attributes`` so the explain_resource_paused_reason
        tool has the full structured cause to read.
        """
        from datetime import datetime

        policies: list[dict] = []
        # Index resources by project so we can flip paused on the right ones.
        resources_by_project: dict[str, list[dict]] = {}
        for r in self.preset["resources"]:
            resources_by_project.setdefault(r["project_uuid"], []).append(r)

        for spec in self._project_specs:
            bucket = spec["bucket"]
            if bucket not in {
                "critical",
                "mild_overdraw",
                "severe_overdraw",
            }:
                continue
            limit_cost = max(int(spec.get("credit_value", 0) or 0), 1)
            has_fired = "overdraw" in bucket
            policy_uuid = self._next_uuid("policy")
            policies.append(
                {
                    "uuid": policy_uuid,
                    "project_uuid": spec["project_uuid"],
                    "limit_cost": limit_cost,
                    "period": 1,  # monthly
                    "actions": "request_pausing",
                    "has_fired": has_fired,
                }
            )

            if not has_fired:
                continue

            # Flip every resource of this project to paused, and write the
            # attribution blob the production code emits.
            project_name = next(
                p["name"]
                for p in self.preset["projects"]
                if p["uuid"] == spec["project_uuid"]
            )
            # Stable timestamp tied to seed for reproducibility.
            paused_at = datetime(
                date.today().year,
                date.today().month,
                max(1, min(28, date.today().day - 7)),
                tzinfo=UTC,
            ).isoformat()
            attribution = {
                "policy_class": "ProjectEstimatedCostPolicy",
                "policy_uuid": policy_uuid,
                "action": "request_pausing",
                "scope_name": project_name,
                "timestamp": paused_at,
                "limit_cost": str(limit_cost),
                "actions": "request_pausing",
            }
            for r in resources_by_project.get(spec["project_uuid"], []):
                r["paused"] = True
                attrs = r.setdefault("attributes", {})
                attrs.setdefault("_policy_attribution", {})["paused"] = attribution

        self.preset["project_estimated_cost_policies"] = policies

    # ---- compensations -----------------------------------------------------
    def build_compensations(self) -> None:
        """Emit credit-compensation invoice items + a terminated-and-compensated case.

        Mirrors what ``invoices/compensations.py::MonthlyCompensation`` produces in
        prod: a NEW InvoiceItem per compensated charge with ``unit_price = -X``,
        ``credit_uuid`` linking to the customer credit, ``name`` prefixed
        ``"Credit compensation. ..."``. The original positive item stays put.

        Picks one project from the ``light`` bucket of customer ``Acme`` (which
        has a CustomerCredit), terminates one of its resources, keeps the
        already-emitted positive items, and adds matching negatives so the net
        is zero on that resource. Adjusts CustomerCredit value to reflect the
        drain.
        """
        # Find Acme's customer credit (bullet C2 needs a customer with credit).
        acme_credit = next(
            (
                c
                for c in self.preset.get("customer_credits", [])
                if any(
                    cust["uuid"] == c["customer_uuid"]
                    and cust["name"] == "Acme Research Lab"
                    for cust in self.preset["customers"]
                )
            ),
            None,
        )
        if acme_credit is None:
            return  # nothing to compensate against

        # Pick a project that is BOTH owned by Acme AND has invoiced spend.
        acme_uuid = acme_credit["customer_uuid"]
        acme_projects = [
            p["uuid"]
            for p in self.preset["projects"]
            if p["customer_uuid"] == acme_uuid
        ]
        # Choose the project with the most invoice items so we have meaningful
        # data to compensate against.
        item_counts: dict[str, int] = {}
        for it in self.preset.get("invoice_items", []):
            if it.get("project_uuid") in acme_projects:
                item_counts[it["project_uuid"]] = (
                    item_counts.get(it["project_uuid"], 0) + 1
                )
        if not item_counts:
            return
        target_project_uuid = max(item_counts, key=item_counts.get)
        target_project_name = next(
            p["name"]
            for p in self.preset["projects"]
            if p["uuid"] == target_project_uuid
        )

        # Pick ONE resource in that project to terminate.
        project_resources = [
            r
            for r in self.preset["resources"]
            if r["project_uuid"] == target_project_uuid
        ]
        if not project_resources:
            return
        terminated_resource = project_resources[0]
        terminated_resource["state"] = 6  # TERMINATED — concealed from end users
        terminated_resource["name"] = terminated_resource["name"] + " (terminated)"

        # Find every invoice item for this resource and emit a matching
        # negative compensation row. Track the credit drain so we can reduce
        # the customer credit value to keep the math consistent.
        terminated_uuid = terminated_resource["uuid"]
        compensations_to_add: list[dict] = []
        total_drained = Decimal("0")
        for it in self.preset["invoice_items"]:
            if it.get("resource_uuid") != terminated_uuid:
                continue
            try:
                gross = Decimal(str(it["unit_price"])) * Decimal(str(it["quantity"]))
            except Exception:  # noqa: BLE001 — defensive against malformed rows
                continue
            if gross <= 0:
                continue
            comp_uuid = self._next_uuid("invoice_item", sub="d")
            compensations_to_add.append(
                {
                    "uuid": comp_uuid,
                    "invoice_uuid": it["invoice_uuid"],
                    "resource_uuid": terminated_uuid,
                    "project_uuid": target_project_uuid,
                    "name": f"Credit compensation. {it['name']}",
                    "quantity": 1,
                    "measured_unit": "",
                    "unit_price": str(-gross),
                    "start": it["start"],
                    "end": it["end"],
                    "credit_uuid": acme_credit["uuid"],
                    "details": dict(it.get("details", {})),
                }
            )
            total_drained += gross

        if not compensations_to_add:
            return

        self.preset["invoice_items"].extend(compensations_to_add)

        # Reduce the customer credit value so the JSON is self-consistent
        # (post-compensation snapshot, mirrors prod where credit.value
        # decrements as compensations apply).
        try:
            new_value = max(
                Decimal("0"),
                Decimal(str(acme_credit["value"])) - total_drained,
            )
            acme_credit["value"] = str(new_value)
        except Exception:  # noqa: BLE001
            pass

        # Stash for metadata.
        self._compensations_summary = {
            "terminated_resource": terminated_resource["name"],
            "project": target_project_name,
            "compensations_added": len(compensations_to_add),
            "credit_drained": str(total_drained),
        }

    # ---- finalisation ------------------------------------------------------
    def finalise(self) -> dict:
        self.preset["_metadata"] = {
            "title": "Credit management — realistic shapes",
            "description": (
                "Production-shaped preset for credit-explainer tool "
                "validation. 3 customers (small/mid/large), 30 projects "
                "across all utilisation buckets including overdrawn and no-"
                "credit, 6 months of invoices and component usages, fired "
                "policies on overdrawn projects."
            ),
            "version": "1.0.0",
            "scenarios": [
                "explain_project_credit_balance across utilisation bands",
                "list_overdrawn_projects (3 overdrawn out of 19 credit-bearing)",
                "Project with no credit configured",
                "Customer with no customer credit",
                "Fired policy on overdrawn project (paused resources)",
                "Terminated resource still compensated by customer credit "
                "(concealed from end users via filter, visible to staff)",
            ],
        }
        return self.preset


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", "-o", required=True, help="output JSON path")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--months", type=int, default=6)
    args = p.parse_args()

    gen = CreditPresetGenerator(months=args.months, seed=args.seed)
    gen.build_skeleton()
    gen.build_projects_and_resources()
    gen.build_credits()
    gen.build_billing()
    gen.build_policies()
    gen.build_compensations()
    data = gen.finalise()

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2) + "\n")

    counts = {k: len(v) for k, v in data.items() if isinstance(v, list)}
    print(f"[generator] wrote {out}")
    for k in sorted(counts):
        print(f"  {k:<32} {counts[k]:>5}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
