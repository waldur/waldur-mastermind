#!/usr/bin/env python3
"""Generate the ``discounts_and_affiliates`` demo preset.

Builds a small marketplace ecosystem that demonstrates:

- volume-discount scenarios configured by the service provider on plan
  components: none / threshold / graduated tiers / continuous formula, and
  organization-level aggregation across two resources;
- the affiliate program: links with different fees, per-invoice fee accruals,
  and a credit ledger (earned fees, a manual withdrawable adjustment, a payout
  and a non-withdrawable promotional grant).

The discount line items are baked directly (the finalization pass is not run at
preset load time), so the amounts here mirror what the pass would produce.

Run: ``python scripts/generate_discounts_affiliates_preset.py`` — writes the
preset JSON next to the other presets. Re-run to regenerate.
"""

import json
import os
from decimal import ROUND_HALF_UP, Decimal

OUT = os.path.join(
    os.path.dirname(__file__),
    "..",
    "src",
    "waldur_mastermind",
    "marketplace",
    "demo_presets",
    "presets",
    "discounts_and_affiliates.json",
)


def money(value) -> str:
    return str(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def u(prefix: str, n: int) -> str:
    """32-char hex uuid with a 2-hex prefix (per preset UUID rules)."""
    return f"{prefix}{n:030x}"[-32:]


# --- identities -----------------------------------------------------------
C_PROVIDER = u("a3", 1)  # GPU Cloud Provider (service provider)
C_RESELLER = u("a3", 2)  # Cloud Reseller (affiliate — earns fees)
C_ACME = u("a3", 3)  # Acme Corp (referred, discounted)
C_BETA = u("a3", 4)  # Beta Labs (referred, discounted)

USERS = [
    ("staff", "Staff", "Admin", C_PROVIDER, True),
    ("provider", "Pat", "Provider", C_PROVIDER, False),
    ("reseller", "Riley", "Reseller", C_RESELLER, False),
    ("acme", "Alex", "Acme", C_ACME, False),
    ("beta", "Bo", "Beta", C_BETA, False),
]
USER_UUID = {name: u("00", i + 1) for i, (name, *_rest) in enumerate(USERS)}

CUSTOMERS = [
    (C_PROVIDER, "GPU Cloud Provider", "GCP"),
    (C_RESELLER, "Cloud Reseller", "RES"),
    (C_ACME, "Acme Corp", "ACME"),
    (C_BETA, "Beta Labs", "BETA"),
]

# --- catalogue ------------------------------------------------------------
CATEGORY_GROUP = u("cc", 1)
CATEGORY = u("e3", 1)
OFFERING = u("f3", 1)
PLAN = u("b3", 1)

# component type -> (name, measured_unit, unit_price, discount_formula, scenario)
COMPONENTS = [
    ("cpu", "CPU cores", "cores", "2.00", "", "no discount"),
    ("ram", "RAM", "GB", "1.00", "10 if usage >= 100 else 0", "threshold 10%"),
    (
        "gpu",
        "GPU",
        "GPUs",
        "500.00",
        "15 if usage >= 8 else 10 if usage >= 4 else 0",
        "graduated tiers",
    ),
    (
        "storage",
        "Storage",
        "GB",
        "0.10",
        "MIN(20, usage / 100)",
        "continuous formula, capped 20%",
    ),
]
COMP_UUID = {c[0]: u("ac", i + 1) for i, c in enumerate(COMPONENTS)}
COMP = {c[0]: c for c in COMPONENTS}

# Volume-discount aggregation scope per component. "customer" sums usage across
# all of the customer's resources of the offering; "resource" discounts each
# resource on its own usage. Storage is per-resource to demonstrate both scopes.
DISCOUNT_AGGREGATION = {
    "cpu": "customer",
    "ram": "customer",
    "gpu": "customer",
    "storage": "resource",
}


def discount_percent(ctype: str, usage) -> Decimal:
    """Evaluate the component's formula for an aggregated usage (mirrors the
    backend get_discount_percent, clamped to 0-100)."""
    usage = Decimal(usage)
    if ctype == "cpu":
        return Decimal(0)
    if ctype == "ram":
        return Decimal(10) if usage >= 100 else Decimal(0)
    if ctype == "gpu":
        if usage >= 8:
            return Decimal(15)
        if usage >= 4:
            return Decimal(10)
        return Decimal(0)
    if ctype == "storage":
        return min(Decimal(20), usage / 100)
    return Decimal(0)


# --- consumption per (customer, project, resource) ------------------------
# Acme spreads GPU usage across two projects: 3 + 3 = 6 GPUs aggregated, which
# reaches the 4+ tier (10%) even though neither resource reaches it alone.
PROJECTS = [
    (u("c3", 1), C_ACME, "Acme — Research"),
    (u("c3", 2), C_ACME, "Acme — Production"),
    (u("c3", 3), C_BETA, "Beta — Lab"),
]

# resource uuid, project uuid, customer, name, {component: usage}
RESOURCES = [
    (
        u("d3", 1),
        u("c3", 1),
        C_ACME,
        "Acme GPU Node A",
        {"cpu": 32, "ram": 128, "gpu": 3, "storage": 500},
    ),
    (
        u("d3", 2),
        u("c3", 2),
        C_ACME,
        "Acme GPU Node B",
        {"cpu": 32, "ram": 64, "gpu": 3, "storage": 1500},
    ),
    (
        u("d3", 3),
        u("c3", 3),
        C_BETA,
        "Beta GPU Node",
        {"cpu": 16, "ram": 200, "gpu": 10, "storage": 1200},
    ),
]


def aggregated_usage(customer: str, ctype: str) -> Decimal:
    return sum(Decimal(r[4].get(ctype, 0)) for r in RESOURCES if r[2] == customer)


# --- invoices -------------------------------------------------------------
# Acme is billed for two months (to populate monthly earnings); Beta for one.
INVOICE_PLAN = [
    (u("13", 1), C_ACME, 2026, 5),
    (u("13", 2), C_ACME, 2026, 6),
    (u("13", 3), C_BETA, 2026, 6),
]


def build():
    preset = {
        "_metadata": {
            "name": "discounts_and_affiliates",
            "title": "Volume discounts & affiliate program",
            "description": (
                "Demonstrates service-provider volume-discount scenarios "
                "(threshold, graduated tiers, continuous formula, "
                "organization-level aggregation) and the affiliate program "
                "(links, fee accruals and the credit ledger)."
            ),
            "version": "1.0.0",
            "scenarios": [
                "No discount, threshold, graduated-tier and continuous-formula "
                "volume discounts",
                "Organization-level aggregation across two resources",
                "Affiliate links with different fees and an inactive link",
                "Affiliate fee accruals per invoice",
                "Credit ledger: earned fees, promotional grant, withdrawable "
                "adjustment and payout",
            ],
        },
        "constance_settings": {
            "SITE_NAME": "Waldur discounts & affiliates demo",
            "SITE_DESCRIPTION": "Volume discounts and affiliate program showcase",
            "CURRENCY_NAME": "EUR",
            # Affiliate program is opt-in — enable the backend enforcement.
            "AFFILIATES_ENABLED": True,
        },
        # ...and the homeport menu/page visibility feature.
        "features": [{"key": "reseller.affiliates", "value": True}],
        "users": [],
        "customers": [],
        "service_providers": [],
        "projects": [],
        "category_groups": [
            {
                "uuid": CATEGORY_GROUP,
                "title": "Infrastructure",
                "description": "Core infrastructure services",
            }
        ],
        "categories": [
            {
                "uuid": CATEGORY,
                "title": "Compute",
                "description": "Compute offerings",
                "group_uuid": CATEGORY_GROUP,
            }
        ],
        "offerings": [],
        "offering_components": [],
        "plans": [],
        "plan_components": [],
        "resources": [],
        "user_roles": [],
        "invoices": [],
        "invoice_items": [],
        "customer_credits": [],
        "customer_affiliates": [],
        "credit_transactions": [],
        "affiliate_fee_accruals": [],
    }

    org_of = {c[0]: c[1] for c in CUSTOMERS}
    # users
    for name, first, last, org, is_staff in USERS:
        preset["users"].append(
            {
                "uuid": USER_UUID[name],
                "username": name,
                "first_name": first,
                "last_name": last,
                "email": f"{name}@example.com",
                "is_active": True,
                "is_staff": is_staff,
                "password": "demo",
                "organization": org_of[org],
                "agreement_date": "2025-01-01T00:00:00",
            }
        )
    # customers
    for uuid, name, abbr in CUSTOMERS:
        preset["customers"].append(
            {
                "uuid": uuid,
                "name": name,
                "abbreviation": abbr,
                "email": f"info@{abbr.lower()}.example.com",
                "country": "EE",
                "description": "",
            }
        )
    # service provider
    preset["service_providers"].append(
        {
            "uuid": u("19", 1),
            "customer_uuid": C_PROVIDER,
            "description": "GPU Cloud Provider",
        }
    )
    # owners
    role_n = 0
    for name, first, last, org, is_staff in USERS:
        if is_staff:
            continue
        role_n += 1
        preset["user_roles"].append(
            {
                "uuid": u("1a", role_n),
                "user_uuid": USER_UUID[name],
                "user_username": name,
                "role_name": "CUSTOMER.OWNER",
                "scope_type": "structure.customer",
                "scope_uuid": org,
                "is_active": True,
            }
        )
    # projects
    for puuid, cust, pname in PROJECTS:
        preset["projects"].append(
            {
                "uuid": puuid,
                "customer_uuid": cust,
                "name": pname,
                "description": "",
            }
        )
    # offering
    preset["offerings"].append(
        {
            "uuid": OFFERING,
            "name": "GPU Cloud",
            "customer_uuid": C_PROVIDER,
            "category_uuid": CATEGORY,
            "type": "Marketplace.Basic",
            "state": 2,
            "shared": True,
            "billable": True,
            "description": "GPU cloud with organization-aggregated volume discounts",
        }
    )
    # offering + plan components
    preset["plans"].append(
        {
            "uuid": PLAN,
            "offering_uuid": OFFERING,
            "name": "Standard",
            "unit": "month",
        }
    )
    for ctype, cname, unit, price, formula, _scenario in COMPONENTS:
        preset["offering_components"].append(
            {
                "uuid": COMP_UUID[ctype],
                "offering_uuid": OFFERING,
                "type": ctype,
                "name": cname,
                "measured_unit": unit,
                "billing_type": "usage",
                "description": "",
            }
        )
        pc = {
            "plan_uuid": PLAN,
            "component_uuid": COMP_UUID[ctype],
            "price": price,
            "amount": 0,
        }
        if formula:
            pc["discount_formula"] = formula
            pc["discount_aggregation"] = DISCOUNT_AGGREGATION[ctype]
        preset["plan_components"].append(pc)
    # resources
    for ruuid, puuid, cust, rname, usage in RESOURCES:
        preset["resources"].append(
            {
                "uuid": ruuid,
                "offering_uuid": OFFERING,
                "plan_uuid": PLAN,
                "project_uuid": puuid,
                "name": rname,
                "state": 2,
                "limits": {k: int(v) for k, v in usage.items()},
                "attributes": {"name": rname},
                "created": "2025-01-01T00:00:00",
            }
        )

    # invoices + baked main/discount items
    item_seq = 0
    for inv_uuid, cust, year, month in INVOICE_PLAN:
        start = f"{year}-{month:02d}-01T00:00:00"
        end = f"{year}-{month:02d}-28T23:59:59"
        cust_resources = [r for r in RESOURCES if r[2] == cust]
        net = Decimal(0)
        for ruuid, puuid, rc, rname, usage in cust_resources:
            for ctype in ("cpu", "ram", "gpu", "storage"):
                qty = Decimal(usage.get(ctype, 0))
                if qty == 0:
                    continue
                _t, cname, unit, price, _f, _s = COMP[ctype]
                unit_price = Decimal(price)
                line = unit_price * qty
                net += line
                item_seq += 1
                main_uuid = u("14", item_seq)
                preset["invoice_items"].append(
                    {
                        "uuid": main_uuid,
                        "invoice_uuid": inv_uuid,
                        "resource_uuid": ruuid,
                        "project_uuid": puuid,
                        "name": f"{rname} / {cname}",
                        "quantity": str(qty),
                        "unit_price": money(unit_price),
                        "measured_unit": unit,
                        "start": start,
                        "end": end,
                        "details": {
                            "offering_component_type": ctype,
                            "offering_component_name": cname,
                            "discount_usage": float(qty),
                        },
                    }
                )
                # Per-resource scope feeds the formula with this resource's own
                # usage; per-customer scope feeds the aggregated usage.
                if DISCOUNT_AGGREGATION[ctype] == "resource":
                    agg = qty
                else:
                    agg = aggregated_usage(cust, ctype)
                pct = discount_percent(ctype, agg)
                if pct <= 0:
                    continue
                discount = (line * pct / 100).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                if discount <= 0:
                    continue
                net -= discount
                item_seq += 1
                preset["invoice_items"].append(
                    {
                        "uuid": u("14", item_seq),
                        "invoice_uuid": inv_uuid,
                        "resource_uuid": ruuid,
                        "project_uuid": puuid,
                        "name": f"{rname} / {cname} / Volume discount ({pct}%)",
                        "quantity": "1",
                        "unit_price": money(-discount),
                        "measured_unit": "",
                        "start": start,
                        "end": end,
                        "details": {
                            "is_discount": True,
                            "discount_type": "org_volume_discount",
                            "offering_component_type": ctype,
                            "offering_component_name": cname,
                            "discount_percent": float(pct),
                            "aggregated_usage": float(agg),
                            "discount_of_item": main_uuid,
                        },
                    }
                )
        preset["invoices"].append(
            {
                "uuid": inv_uuid,
                "customer_uuid": cust,
                "year": year,
                "month": month,
                "state": "created",
                "tax_percent": "0.00",
                "invoice_date": f"{year}-{month:02d}-01",
                "total_price": money(net),
                "total_cost": money(net),
                "created": start,
            }
        )
        # remember net for the affiliate fee
        inv_net[inv_uuid] = net

    # affiliate links (different scenarios)
    LINK_ACME = u("16", 1)
    LINK_BETA = u("16", 2)
    LINK_INACTIVE = u("16", 3)
    preset["customer_affiliates"] = [
        {
            "uuid": LINK_ACME,
            "customer_uuid": C_ACME,
            "affiliate_uuid": C_RESELLER,
            "fee_percent": "10.00000",
            "is_active": True,
            "start_date": "2026-01-01",
        },
        {
            "uuid": LINK_BETA,
            "customer_uuid": C_BETA,
            "affiliate_uuid": C_RESELLER,
            "fee_percent": "5.00000",
            "is_active": True,
            "start_date": "2026-06-01",
        },
        {
            # Dormant link (feature disabled window / paused partnership).
            "uuid": LINK_INACTIVE,
            "customer_uuid": C_PROVIDER,
            "affiliate_uuid": C_RESELLER,
            "fee_percent": "8.00000",
            "is_active": False,
        },
    ]
    link_of = {C_ACME: LINK_ACME, C_BETA: LINK_BETA}
    fee_pct = {C_ACME: Decimal(10), C_BETA: Decimal(5)}

    # fee accruals + affiliate_fee transactions, one per referred invoice
    reseller_credit = u("15", 1)
    tx_seq = 0
    txns = []
    total_fees = Decimal(0)
    for inv_uuid, cust, year, month in INVOICE_PLAN:
        if cust not in link_of:
            continue
        fee = (inv_net[inv_uuid] * fee_pct[cust] / 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        total_fees += fee
        preset["affiliate_fee_accruals"].append(
            {
                "uuid": u("17", len(preset["affiliate_fee_accruals"]) + 1),
                "affiliate_link_uuid": link_of[cust],
                "invoice_uuid": inv_uuid,
                "amount": money(fee),
            }
        )
        tx_seq += 1
        txns.append(
            {
                "uuid": u("18", tx_seq),
                "credit_uuid": reseller_credit,
                "amount": money(fee),
                "transaction_type": "affiliate_fee",
                "comment": "",
                "created": f"{year}-{month:02d}-05T09:00:00",
            }
        )

    # a non-withdrawable promotional grant, a manual withdrawable adjustment
    # and a payout — to show the withdrawable-vs-total distinction.
    grant = Decimal("200.00")
    adjustment = Decimal("100.00")
    payout = Decimal("-50.00")
    for extra in [
        ("staff_grant", grant, "Promotional onboarding credit", "2026-01-10T10:00:00"),
        (
            "withdrawable_adjustment",
            adjustment,
            "Goodwill top-up agreed with partner",
            "2026-06-20T14:00:00",
        ),
        ("payout", payout, "Quarterly partner payout", "2026-06-30T16:00:00"),
    ]:
        tx_seq += 1
        ttype, amount, comment, created = extra
        txns.append(
            {
                "uuid": u("18", tx_seq),
                "credit_uuid": reseller_credit,
                "amount": money(amount),
                "transaction_type": ttype,
                "comment": comment,
                "created": created,
            }
        )
    preset["credit_transactions"] = txns

    # Value starts at 0; the imported credit transactions apply their signed
    # amounts to build it up to grant + fees + adjustment + payout.
    preset["customer_credits"].append(
        {
            "uuid": reseller_credit,
            "customer_uuid": C_RESELLER,
            "value": "0",
            "expected_consumption": "0",
            "grace_coefficient": "0",
            "minimal_consumption_logic": "fixed",
            "apply_as_minimal_consumption": True,
            "offering_uuids": [],
            "created": "2026-01-01T00:00:00",
        }
    )

    return preset


inv_net: dict = {}

if __name__ == "__main__":
    data = build()
    with open(OUT, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(f"Wrote {os.path.abspath(OUT)}")
    print(
        f"  invoices={len(data['invoices'])} items={len(data['invoice_items'])} "
        f"affiliates={len(data['customer_affiliates'])} "
        f"accruals={len(data['affiliate_fee_accruals'])} "
        f"transactions={len(data['credit_transactions'])}"
    )
