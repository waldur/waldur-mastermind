"""Emit the credit_scenarios demo preset.

Authored as a script so the twelve scenarios stay readable as data: each row
below is one dashboard state, and the JSON is derived from it rather than
hand-maintained.
"""

import decimal
import json
from pathlib import Path

OUT = (
    Path(__file__).resolve().parents[1]
    / "src/waldur_mastermind/marketplace/demo_presets/presets/credit_scenarios.json"
)

CUSTOMER_MAIN = "a3c00000000000000000000000000001"
CUSTOMER_CAPPED = "a3c00000000000000000000000000002"
CUSTOMER_EMPTY = "a3c00000000000000000000000000003"

CATEGORY_GROUP = "eb000000000000000000000000000001"
CATEGORY = "e3c00000000000000000000000000001"
PROVIDER = "fa000000000000000000000000000001"

# Offerings: one single-component (so it shows in the project dashboard's
# "Active limit-based resources" table, which filters on component_count=1) and
# two multi-component ones, which appear only in the credit views.
OFFERINGS = [
    {
        "uuid": "f3c00000000000000000000000000001",
        "name": "Research Data Storage",
        "components": [("storage", "Storage", "TB")],
        "plan": "b3c00000000000000000000000000001",
    },
    {
        "uuid": "f3c00000000000000000000000000002",
        "name": "SLURM HPC Allocation",
        "components": [
            ("cpu", "CPU hours", "hours"),
            ("ram", "Memory", "GB-hours"),
            ("gpu", "GPU hours", "hours"),
        ],
        "plan": "b3c00000000000000000000000000002",
    },
    {
        "uuid": "f3c00000000000000000000000000003",
        "name": "Cloud VM Package",
        "components": [("vcpu", "vCPU", "cores"), ("ram", "Memory", "GB")],
        "plan": "b3c00000000000000000000000000003",
    },
]

STORAGE, SLURM, CLOUD = (o["uuid"] for o in OFFERINGS)

# months: consumption per past month as a fraction of expected, oldest first.
# current: the month in progress, billed pro rata for the days elapsed.
SCENARIOS = [
    {
        "key": "01",
        "name": "01 On pace",
        "note": "Healthy: usage tracks the linear ideal, nothing lost, no deadline in sight.",
        "credit": dict(remaining="60000", expected="10000", grace="20"),
        "months": [1.0],
        "current": 1.0,
    },
    {
        "key": "02",
        "name": "02 Behind pace - credit being lost",
        "note": "Draw far below the minimum floor: the shortfall is taken from the balance anyway and shows as Lost.",
        "credit": dict(remaining="45000", expected="10000", grace="20"),
        "months": [0.2, 0.15, 0.25, 0.2, 0.1, 0.2],
        "current": 0.15,
    },
    {
        "key": "03",
        "name": "03 Ahead of pace - over expected",
        "note": "Drawing faster than the plan; the pace bar clamps at 100% and flags the overage.",
        "credit": dict(remaining="30000", expected="8000", grace="20"),
        "months": [1.1, 1.2, 1.05, 1.15, 1.1, 1.2],
        "current": 1.58,
    },
    {
        "key": "04",
        "name": "04 Final month - grace waived",
        "note": "The credit ends this month, so the minimum draw jumps to the full expected consumption and the grace coefficient does not apply.",
        "credit": dict(
            remaining="22000",
            expected="9000",
            grace="40",
            end_date="relative:+0months@month_start",
        ),
        "months": [0.6, 0.7, 0.5, 0.65, 0.6, 0.55],
        "current": 0.33,
    },
    {
        "key": "05",
        "name": "05 Credit expires in two months",
        "note": "A balance that will be forfeited at expiry unless the draw increases.",
        "credit": dict(
            remaining="38000",
            expected="9000",
            grace="25",
            end_date="relative:+2months@month_start",
        ),
        "months": [0.8, 0.75, 0.8, 0.7, 0.75, 0.8],
        "current": 0.5,
    },
    {
        "key": "06",
        "name": "06 Balance runs out first",
        "note": "Days of runway at the current burn: exhaustion is the binding constraint.",
        "credit": dict(remaining="2500", expected="8000", grace="20"),
        "months": [0.95, 1.0, 0.9, 1.0, 0.95, 1.0],
        "current": 0.9,
    },
    {
        "key": "07",
        "name": "07 Project ends soon",
        "note": "Project end date in twelve days with a grace period: resources are paused first, terminated later.",
        "credit": dict(remaining="50000", expected="8000", grace="20"),
        "months": [1.0],
        "current": 0.95,
        "project_end": "relative:+12days",
    },
    {
        "key": "08",
        "name": "08 Resources end before the credit",
        "note": "Staggered resource end dates: the work stops long before the money does.",
        "credit": dict(remaining="50000", expected="8000", grace="20"),
        "months": [0.9],
        "current": 0.85,
        "resource_ends": ["relative:+40days", "relative:+95days", "relative:+160days"],
    },
    {
        "key": "09",
        "name": "09 Linear ramp-up",
        "note": "LINEAR minimal-consumption logic: the expected draw rises each month so the balance lands at zero on the end date.",
        "credit": dict(
            remaining="30000",
            expected="6000",
            grace="0",
            logic="linear",
            end_date="relative:+4months@month_start",
        ),
        "months": [0.8],
        "current": 0.7,
    },
    {
        "key": "10",
        "name": "10 Paused by policy",
        "note": "A resource paused by a cost policy, with the attribution shown in the expandable row.",
        "credit": dict(remaining="26000", expected="8000", grace="20"),
        "months": [1.05],
        "current": 1.1,
        "paused": True,
        "policy": {"uuid": "93c00000000000000000000000000001", "limit_cost": 9000},
    },
    {
        "key": "11",
        "name": "11 Organization credit lower than allocation",
        "note": "The allocation stands but the organization balance behind it is smaller, so only part of it can be drawn.",
        "customer": CUSTOMER_CAPPED,
        "credit": dict(remaining="20000", expected="8000", grace="20"),
        "months": [0.75],
        "current": 0.8,
    },
    {
        "key": "12",
        "name": "12 Organization credit exhausted",
        "note": "The organization balance is zero, so none of the allocation can be drawn.",
        "customer": CUSTOMER_EMPTY,
        "credit": dict(remaining="15000", expected="8000", grace="20"),
        "months": [0.6],
        "current": 0.6,
    },
]


HISTORY_MONTHS = 6


def starting_grant(scenario) -> str:
    """The grant that leaves the scenario at its intended balance.

    History is generated by the real compensation flow, which draws each month
    down by the greater of the consumption and the minimum floor — so a preset
    cannot state the balance it wants directly. Work backwards from it instead:
    grant = intended balance + what the generated months will draw.
    """
    credit = scenario["credit"]
    if scenario.get("customer"):
        # Compensation is capped by the organization credit, and these two
        # organizations have almost none — so history draws next to nothing and
        # the grant is the balance.
        return credit["remaining"]
    expected = decimal.Decimal(credit["expected"])
    floor = (decimal.Decimal("100") - decimal.Decimal(credit["grace"])) / 100
    if credit.get("end_date") == "relative:+0months@month_start":
        # `BaseCredit.minimal_consumption` waives the grace once the credit is
        # in its final month, and it reads the calendar now — so every month the
        # generator bills draws the full expected consumption, not just the
        # last one.
        floor = decimal.Decimal("1")
    if not credit.get("apply_floor", True):
        floor = decimal.Decimal("0")
    shape = [decimal.Decimal(str(f)) for f in scenario["months"]]
    drawn = (
        sum(max(shape[index % len(shape)], floor) for index in range(HISTORY_MONTHS))
        * expected
    )
    return str(int(decimal.Decimal(credit["remaining"]) + drawn))


def build():
    users = [
        {
            "uuid": "00000000000000000000000000000001",
            "username": "staff",
            "email": "staff@demo.waldur.com",
            "first_name": "Staff",
            "last_name": "User",
            "is_staff": True,
            "is_active": True,
            "password": "demo",
            "agreement_date": "2025-01-01T00:00:00Z",
        },
        {
            "uuid": "00000000000000000000000000000002",
            "username": "owner",
            "email": "owner@demo.waldur.com",
            "first_name": "Olivia",
            "last_name": "Owner",
            "is_active": True,
            "password": "demo",
            "agreement_date": "2025-01-01T00:00:00Z",
        },
        {
            "uuid": "00000000000000000000000000000003",
            "username": "manager",
            "email": "manager@demo.waldur.com",
            "first_name": "Mark",
            "last_name": "Manager",
            "is_active": True,
            "password": "demo",
            "agreement_date": "2025-01-01T00:00:00Z",
        },
    ]

    customers = [
        {
            "uuid": CUSTOMER_MAIN,
            "name": "Credit scenarios",
            "abbreviation": "CS",
            "description": "Each project demonstrates one credit dashboard state.",
            "email": "credits@demo.waldur.com",
            "country": "EE",
            # Scenario 07 relies on the project grace period: resources are
            # paused at the end date and terminated once this window closes.
            "grace_period_days": 30,
        },
        {
            "uuid": CUSTOMER_CAPPED,
            "name": "Credit scenarios (org-capped)",
            "abbreviation": "CSC",
            "description": "Organization credit is smaller than what its project was allocated.",
            "email": "credits@demo.waldur.com",
            "country": "EE",
        },
        {
            "uuid": CUSTOMER_EMPTY,
            "name": "Credit scenarios (org-exhausted)",
            "abbreviation": "CSE",
            "description": "Organization credit is spent, so allocations cannot be drawn.",
            "email": "credits@demo.waldur.com",
            "country": "EE",
        },
    ]

    offerings, components, plans, plan_components = [], [], [], []
    for index, offering in enumerate(OFFERINGS, start=1):
        offerings.append(
            {
                "uuid": offering["uuid"],
                "name": offering["name"],
                "type": "Marketplace.Basic",
                "state": 2,
                "shared": True,
                "billable": True,
                "category_uuid": CATEGORY,
                "customer_uuid": CUSTOMER_MAIN,
                "description": f"{offering['name']} for the credit scenarios preset.",
            }
        )
        plans.append(
            {
                "uuid": offering["plan"],
                "name": f"{offering['name']} plan",
                "offering_uuid": offering["uuid"],
                "unit": "month",
            }
        )
        for position, (ctype, cname, unit) in enumerate(offering["components"], 1):
            component_uuid = f"fc{index}0000000000000000000000000000{position}"[:32]
            components.append(
                {
                    "uuid": component_uuid,
                    "offering_uuid": offering["uuid"],
                    "type": ctype,
                    "name": cname,
                    "billing_type": "limit",
                    "measured_unit": unit,
                }
            )
            # Priced at zero on purpose: the month in progress is billed from
            # the scenario's own consumption shape, and a plan price on top of
            # it would double-count the draw.
            plan_components.append(
                {
                    "plan_uuid": offering["plan"],
                    "component_uuid": component_uuid,
                    "price": "0.00",
                    "amount": 0,
                }
            )

    projects, project_credits, resources, policies, patterns = [], [], [], [], {}

    for scenario in SCENARIOS:
        key = scenario["key"]
        project_uuid = f"c3c0000000000000000000000000000{key[-1]}"
        if key in ("10", "11", "12"):
            project_uuid = f"c3c000000000000000000000000000{key}"
        customer_uuid = scenario.get("customer", CUSTOMER_MAIN)

        project = {
            "uuid": project_uuid,
            "name": scenario["name"],
            "customer_uuid": customer_uuid,
            "description": scenario["note"],
        }
        if scenario.get("project_end"):
            project["end_date"] = scenario["project_end"]
        projects.append(project)

        credit = scenario["credit"]
        project_credit = {
            "uuid": f"cb{project_uuid[2:]}",
            "project_uuid": project_uuid,
            "value": starting_grant(scenario),
            "expected_consumption": credit["expected"],
            "grace_coefficient": credit["grace"],
            "minimal_consumption_logic": credit.get("logic", "fixed"),
            "apply_as_minimal_consumption": True,
        }
        if credit.get("end_date"):
            project_credit["end_date"] = credit["end_date"]
        project_credits.append(project_credit)

        patterns[project_uuid] = {
            "months": scenario["months"],
            "current": scenario["current"],
        }

        ends = scenario.get("resource_ends") or []

        # 28 zeros + scenario key + position keeps every resource distinct and
        # readable: d3c0…{scenario}{nn}.
        def resource_uuid(position):
            return f"d3c0000000000000000000000000{key}{position:02d}"

        storage = {
            "uuid": resource_uuid(1),
            "name": f"{key} Research Data Storage",
            "offering_uuid": STORAGE,
            "project_uuid": project_uuid,
            "plan_uuid": OFFERINGS[0]["plan"],
            "state": 2,
            "limits": {"storage": 20},
            "attributes": {},
        }
        if ends:
            storage["end_date"] = ends[0]
        if scenario.get("paused"):
            storage["state"] = 2
            storage["paused"] = True
            storage["attributes"] = {
                "_policy_attribution": {
                    "paused": {
                        "policy_class": "ProjectEstimatedCostPolicy",
                        "scope_name": scenario["name"],
                    }
                }
            }
        resources.append(storage)

        slurm = {
            "uuid": resource_uuid(2),
            "name": f"{key} SLURM HPC Allocation",
            "offering_uuid": SLURM,
            "project_uuid": project_uuid,
            "plan_uuid": OFFERINGS[1]["plan"],
            "state": 2,
            "limits": {"cpu": 300000, "ram": 900000, "gpu": 12000},
            "attributes": {},
        }
        if len(ends) > 1:
            slurm["end_date"] = ends[1]
        resources.append(slurm)

        if len(ends) > 2:
            resources.append(
                {
                    "uuid": resource_uuid(3),
                    "name": f"{key} Cloud VM Package",
                    "offering_uuid": CLOUD,
                    "project_uuid": project_uuid,
                    "plan_uuid": OFFERINGS[2]["plan"],
                    "state": 2,
                    "limits": {"vcpu": 40, "ram": 160},
                    "attributes": {},
                    "end_date": ends[2],
                }
            )

        if scenario.get("policy"):
            policies.append(
                {
                    "uuid": scenario["policy"]["uuid"],
                    "project_uuid": project_uuid,
                    "limit_cost": scenario["policy"]["limit_cost"],
                    "actions": "request_pausing,notify_project_team",
                    "period": 2,
                    "has_fired": True,
                }
            )

    customer_credits = [
        {
            "uuid": "ca000000000000000000000000000001",
            "customer_uuid": CUSTOMER_MAIN,
            "value": "1000000",
            "expected_consumption": "90000",
            "minimal_consumption_logic": "fixed",
            "grace_coefficient": "20",
            "apply_as_minimal_consumption": False,
        },
        {
            # Allocated at full value, then drawn down to 500 — below the 20k
            # its project holds. The model refuses to create that state
            # directly, and it is the one the dashboard warns about.
            "uuid": "ca000000000000000000000000000002",
            "customer_uuid": CUSTOMER_CAPPED,
            "value": "50000",
            "value_after_allocations": "500",
            "expected_consumption": "8000",
            "minimal_consumption_logic": "fixed",
            "grace_coefficient": "20",
            "apply_as_minimal_consumption": False,
        },
        {
            "uuid": "ca000000000000000000000000000003",
            "customer_uuid": CUSTOMER_EMPTY,
            "value": "50000",
            "value_after_allocations": "0",
            "expected_consumption": "8000",
            "minimal_consumption_logic": "fixed",
            "grace_coefficient": "20",
            "apply_as_minimal_consumption": False,
        },
    ]

    customer_policies = [
        {
            "uuid": "93c00000000000000000000000000002",
            "customer_uuid": CUSTOMER_CAPPED,
            "limit_cost": 9000,
            "actions": "notify_organization_owners",
            "period": 2,
            "has_fired": False,
        }
    ]

    user_roles = [
        {
            "uuid": "99c00000000000000000000000000001",
            "user_uuid": "00000000000000000000000000000002",
            "user_username": "owner",
            "role_name": "CUSTOMER.OWNER",
            "scope_type": "structure.customer",
            "scope_uuid": CUSTOMER_MAIN,
            "is_active": True,
        },
        {
            "uuid": "99c00000000000000000000000000002",
            "user_uuid": "00000000000000000000000000000003",
            "user_username": "manager",
            "role_name": "PROJECT.MANAGER",
            "scope_type": "structure.project",
            "scope_uuid": projects[0]["uuid"],
            "is_active": True,
        },
    ]

    return {
        "users": users,
        "customers": customers,
        "service_providers": [
            {
                "uuid": PROVIDER,
                "customer_uuid": CUSTOMER_MAIN,
                "description": "Credit scenarios service provider",
            }
        ],
        "category_groups": [
            {
                "uuid": CATEGORY_GROUP,
                "title": "Research infrastructure",
                "description": "Services used by the credit scenarios",
            }
        ],
        "categories": [
            {
                "uuid": CATEGORY,
                "title": "Compute and storage",
                "description": "Limit-based offerings",
                "group_uuid": CATEGORY_GROUP,
            }
        ],
        "offerings": offerings,
        "offering_components": components,
        "plans": plans,
        "plan_components": plan_components,
        "projects": projects,
        "resources": resources,
        "user_roles": user_roles,
        "customer_credits": customer_credits,
        "project_credits": project_credits,
        "project_estimated_cost_policies": policies,
        "customer_estimated_cost_policies": customer_policies,
        "_metadata": {
            "title": "Credit scenarios",
            "description": (
                "Twelve projects, each holding the project dashboard's credit "
                "views in one state: on/behind/ahead of pace, credit expiring "
                "this month and in two months, balance exhaustion, project and "
                "resource end dates, linear ramp-up, a policy-paused resource, "
                "and organizations whose credit is capped or spent."
            ),
            "version": "1.0.0",
            "scenarios": [s["name"] for s in SCENARIOS],
            "credit_history": patterns,
        },
    }


data = build()
with open(OUT, "w") as handle:
    json.dump(data, handle, indent=2)
    handle.write("\n")

print(f"wrote {OUT}")
for key, value in data.items():
    if isinstance(value, list):
        print(f"  {key}: {len(value)}")
