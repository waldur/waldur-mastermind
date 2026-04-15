#!/usr/bin/env python3
"""
Generate billing and reporting demo data for Waldur preset files.

This script generates realistic billing data including:
- Invoices (12 months of history)
- Invoice items (per resource)
- Customer credits
- Project credits
- Component usages (12 months with growth)
- Component user usages (per-user breakdown)

Usage:
    python scripts/generate_preset_billing_data.py <preset_path> [--months N] [--output PATH]

Examples:
    python scripts/generate_preset_billing_data.py src/waldur_mastermind/marketplace/demo_presets/presets/ai_factory.json
    python scripts/generate_preset_billing_data.py my_preset.json --months 6 --output my_preset_with_billing.json
"""

import argparse
import json
import random
from datetime import date
from pathlib import Path


class PresetBillingGenerator:
    """Generator for billing and reporting demo data."""

    def __init__(self, preset_data: dict, months: int = 12):
        self.data = preset_data
        self.months = months

        # Build lookup tables
        self.customers = {c["uuid"]: c for c in self.data.get("customers", [])}
        self.projects = {p["uuid"]: p for p in self.data.get("projects", [])}
        self.resources = {r["uuid"]: r for r in self.data.get("resources", [])}
        self.offerings = {o["uuid"]: o for o in self.data.get("offerings", [])}

        # Build component lookup by offering
        self.components_by_offering = {}
        for c in self.data.get("offering_components", []):
            if c["offering_uuid"] not in self.components_by_offering:
                self.components_by_offering[c["offering_uuid"]] = []
            self.components_by_offering[c["offering_uuid"]].append(c)

        # Build project-customer mapping
        self.customer_projects = {}
        for p in self.data.get("projects", []):
            cust_uuid = p["customer_uuid"]
            if cust_uuid not in self.customer_projects:
                self.customer_projects[cust_uuid] = []
            self.customer_projects[cust_uuid].append(p)

        # Build resource-project mapping
        self.project_resources = {}
        for r in self.data.get("resources", []):
            proj_uuid = r["project_uuid"]
            if proj_uuid not in self.project_resources:
                self.project_resources[proj_uuid] = []
            self.project_resources[proj_uuid].append(r)

        # UUID counters
        self.invoice_counter = 1
        self.item_counter = 1
        self.credit_counter = 1
        self.proj_credit_counter = 1
        self.usage_counter = 1
        self.user_usage_counter = 1

        # Growth factors for realistic data
        self.growth_factors = self._generate_growth_factors()

    def _generate_growth_factors(self) -> list:
        """Generate growth factors showing gradual business growth."""
        factors = []
        for i in range(self.months):
            # Start at 0.85 and grow to 1.18 over the period
            factor = 0.85 + (0.33 * i / max(self.months - 1, 1))
            factors.append(factor)
        return factors

    def _get_billing_periods(self) -> list:
        """Generate billing periods (year, month) tuples."""
        today = date.today()
        periods = []

        for i in range(self.months - 1, -1, -1):
            year = today.year
            month = today.month - i
            while month <= 0:
                month += 12
                year -= 1
            periods.append((year, month))

        return periods

    def _make_uuid(self, prefix: str, num: int) -> str:
        """Generate a UUID with given prefix and number."""
        return f"{prefix}{num:06d}"

    def _identify_consuming_customers(self) -> list:
        """Identify customers that consume services (have resources)."""
        consuming = []

        for cust_uuid, customer in self.customers.items():
            # Check if customer has projects with resources
            projects = self.customer_projects.get(cust_uuid, [])
            has_resources = False

            for proj in projects:
                if self.project_resources.get(proj["uuid"]):
                    has_resources = True
                    break

            if has_resources:
                # Estimate cost range based on number of resources
                resource_count = sum(
                    len(self.project_resources.get(p["uuid"], [])) for p in projects
                )

                # Base cost estimation: 500-2000 per resource
                base_min = max(500, resource_count * 500)
                base_max = max(1500, resource_count * 2000)

                consuming.append((cust_uuid, customer["name"], base_min, base_max))

        return consuming

    def _get_org_users(self, customer_uuid: str) -> list:
        """Get users associated with an organization."""
        users = []
        for role in self.data.get("user_roles", []):
            scope_uuid = role.get("scope_uuid", "")
            # Check if role is for this customer or its projects
            if scope_uuid == customer_uuid:
                users.append(role["user_uuid"])
            else:
                # Check if it's a project belonging to this customer
                for proj in self.customer_projects.get(customer_uuid, []):
                    if scope_uuid == proj["uuid"]:
                        users.append(role["user_uuid"])

        # Get usernames from user UUIDs
        user_lookup = {u["uuid"]: u["username"] for u in self.data.get("users", [])}
        return list(set(user_lookup.get(uuid, f"user_{uuid[:8]}") for uuid in users))

    def generate_invoices(self) -> list:
        """Generate invoices for all consuming customers."""
        invoices = []
        periods = self._get_billing_periods()
        consuming = self._identify_consuming_customers()

        for cust_uuid, cust_name, min_cost, max_cost in consuming:
            for idx, (year, month) in enumerate(periods):
                growth = self.growth_factors[idx]

                # Base cost with seasonal variation and growth
                seasonal_factor = 1.0 + 0.1 * (abs(month - 7) / 6)
                base_cost = (
                    random.uniform(min_cost, max_cost) * growth * seasonal_factor
                )

                # Add compensations randomly for older months
                compensations = (
                    random.choice([0, 0, 0, -50, -100, -200]) if idx > 2 else 0
                )

                total_cost = round(base_cost + compensations, 2)
                total_price = round(total_cost * 1.1, 2)

                # Determine state
                if idx < self.months - 2:
                    state = "paid"
                elif idx == self.months - 2:
                    state = "created"
                else:
                    state = "pending"

                invoices.append(
                    {
                        "uuid": self._make_uuid(
                            "afcf0000000000000000000000", self.invoice_counter
                        ),
                        "customer_uuid": cust_uuid,
                        "year": year,
                        "month": month,
                        "state": state,
                        "total_cost": str(total_cost),
                        "total_price": str(total_price),
                        "tax_percent": "20.00",
                        "created": f"{year}-{month:02d}-01",
                        "invoice_date": f"{year}-{month:02d}-15"
                        if state != "pending"
                        else None,
                    }
                )
                self.invoice_counter += 1

        return invoices

    def generate_invoice_items(self, invoices: list) -> list:
        """Generate invoice items for each invoice."""
        items = []

        for inv in invoices:
            cust_uuid = inv["customer_uuid"]
            total_cost = float(inv["total_cost"])

            cust_projects = self.customer_projects.get(cust_uuid, [])
            if not cust_projects:
                continue

            remaining_cost = total_cost

            for i, proj in enumerate(cust_projects):
                proj_resources = self.project_resources.get(proj["uuid"], [])
                if not proj_resources:
                    continue

                # Allocate cost proportionally
                if i == len(cust_projects) - 1:
                    proj_cost = remaining_cost
                else:
                    proj_cost = round(remaining_cost * random.uniform(0.3, 0.6), 2)
                    remaining_cost -= proj_cost

                for res in proj_resources:
                    res_cost = round(proj_cost / len(proj_resources), 2)
                    offering = self.offerings.get(res["offering_uuid"], {})

                    items.append(
                        {
                            "uuid": self._make_uuid(
                                "afcf1000000000000000000000", self.item_counter
                            ),
                            "invoice_uuid": inv["uuid"],
                            "resource_uuid": res["uuid"],
                            "project_uuid": proj["uuid"],
                            "name": f"{res['name']} ({offering.get('name', 'Service')[:30]})",
                            "quantity": random.randint(100, 1000),
                            "measured_unit": "hours",
                            "unit_price": str(round(res_cost / 500, 4)),
                            "start": f"{inv['year']}-{inv['month']:02d}-01T00:00:00",
                            "end": f"{inv['year']}-{inv['month']:02d}-28T23:59:59",
                        }
                    )
                    self.item_counter += 1

        return items

    def generate_customer_credits(self) -> list:
        """Generate customer credits for consuming customers."""
        credits = []
        consuming = self._identify_consuming_customers()

        # Calculate end date as first day of month, 1 year from now
        today = date.today()
        end_year = today.year + 1
        end_date = f"{end_year}-{today.month:02d}-01"

        for cust_uuid, cust_name, min_cost, max_cost in consuming:
            # Credit value based on ~12 months of average consumption
            avg_cost = (min_cost + max_cost) / 2
            credit_value = int(avg_cost * 12 * 0.8)  # 80% of annual cost

            credits.append(
                {
                    "uuid": self._make_uuid(
                        "afcf2000000000000000000000", self.credit_counter
                    ),
                    "customer_uuid": cust_uuid,
                    "value": str(credit_value),
                    "expected_consumption": str(int(credit_value * 0.8)),
                    "minimal_consumption_logic": "fixed",
                    "grace_coefficient": "0.1",
                    "apply_as_minimal_consumption": True,
                    "end_date": end_date,
                    "created": f"{today.year}-01-15T00:00:00",
                    "offering_uuids": [],
                }
            )
            self.credit_counter += 1

        return credits

    def generate_project_credits(self, customer_credits: list) -> list:
        """Generate project credits for projects with resources."""
        credits = []

        # Get customers that have credits
        credited_customers = {cc["customer_uuid"] for cc in customer_credits}

        for cust_uuid in credited_customers:
            projects = self.customer_projects.get(cust_uuid, [])

            for proj in projects:
                # Only add credits to projects with resources
                if not self.project_resources.get(proj["uuid"]):
                    continue

                # Calculate credit based on number of resources
                resource_count = len(self.project_resources.get(proj["uuid"], []))
                credit_value = resource_count * random.randint(3000, 8000)

                credits.append(
                    {
                        "uuid": self._make_uuid(
                            "afcf3000000000000000000000", self.proj_credit_counter
                        ),
                        "project_uuid": proj["uuid"],
                        "value": str(credit_value),
                        "created": f"{date.today().year}-01-20T00:00:00",
                    }
                )
                self.proj_credit_counter += 1

        return credits

    def generate_component_usages(self) -> list:
        """Generate component usages for all resources."""
        usages = []
        periods = self._get_billing_periods()

        for res_uuid, resource in self.resources.items():
            offering_uuid = resource["offering_uuid"]
            components = self.components_by_offering.get(offering_uuid, [])

            if not components:
                continue

            for idx, (year, month) in enumerate(periods):
                growth = self.growth_factors[idx]

                for comp in components:
                    # Base usage based on component type
                    comp_type = comp.get("type", "unknown")

                    # Estimate base usage
                    if comp_type in ["gpu", "gpu_hours"]:
                        base_usage = random.randint(100, 1000)
                    elif comp_type in ["cpu", "cpu_hours"]:
                        base_usage = random.randint(5000, 50000)
                    elif comp_type in ["ram", "memory"]:
                        base_usage = random.randint(50000, 500000)
                    elif comp_type in ["storage", "disk"]:
                        base_usage = random.randint(100000, 2000000)
                    elif comp_type in ["base_fee", "monthly"]:
                        base_usage = 1
                    elif comp_type in ["users", "seats"]:
                        base_usage = random.randint(5, 30)
                    elif comp_type in ["documents", "requests"]:
                        base_usage = random.randint(500, 5000)
                    else:
                        base_usage = random.randint(100, 10000)

                    # Apply growth and variance
                    variance = random.uniform(0.85, 1.15)
                    usage = int(base_usage * growth * variance)

                    if usage > 0:
                        usages.append(
                            {
                                "uuid": self._make_uuid(
                                    "afcfa00000000000000000000000", self.usage_counter
                                ),
                                "resource_uuid": res_uuid,
                                "component_uuid": comp["uuid"],
                                "usage": str(usage),
                                "date": f"{year}-{month:02d}-15T12:00:00",
                                "billing_period": f"{year}-{month:02d}-01",
                                "recurring": False,
                                "description": f"{comp_type} usage for {year}-{month:02d}",
                            }
                        )
                        self.usage_counter += 1

        return usages

    def generate_component_user_usages(self, component_usages: list) -> list:
        """Generate per-user breakdown of component usages."""
        user_usages = []

        # Map resources to customers
        resource_to_customer = {}
        for res_uuid, resource in self.resources.items():
            proj = self.projects.get(resource["project_uuid"])
            if proj:
                resource_to_customer[res_uuid] = proj["customer_uuid"]

        for usage in component_usages:
            # Only break down GPU and CPU usages
            if (
                "gpu" not in usage["description"].lower()
                and "cpu" not in usage["description"].lower()
            ):
                continue

            total_usage = int(usage["usage"])
            if total_usage < 100:
                continue

            cust_uuid = resource_to_customer.get(usage["resource_uuid"])
            if not cust_uuid:
                continue

            users = self._get_org_users(cust_uuid)
            if not users:
                continue

            remaining = total_usage
            for i, username in enumerate(users[:5]):  # Max 5 users
                if i == len(users) - 1 or i == 4:
                    user_usage = remaining
                else:
                    share = 0.5 if i == 0 else 0.5 / min(len(users) - 1, 4)
                    user_usage = int(total_usage * share * random.uniform(0.9, 1.1))
                    remaining -= user_usage

                if user_usage > 0:
                    user_usages.append(
                        {
                            "uuid": self._make_uuid(
                                "afcfb00000000000000000000000", self.user_usage_counter
                            ),
                            "component_usage_uuid": usage["uuid"],
                            "username": username,
                            "usage": user_usage,
                            "description": f"{username}'s usage",
                        }
                    )
                    self.user_usage_counter += 1

        return user_usages

    def generate_all(self) -> dict:
        """Generate all billing data and update preset."""
        print("Generating invoices...")
        invoices = self.generate_invoices()
        print(f"  Generated {len(invoices)} invoices")

        print("Generating invoice items...")
        invoice_items = self.generate_invoice_items(invoices)
        print(f"  Generated {len(invoice_items)} invoice items")

        print("Generating customer credits...")
        customer_credits = self.generate_customer_credits()
        print(f"  Generated {len(customer_credits)} customer credits")

        print("Generating project credits...")
        project_credits = self.generate_project_credits(customer_credits)
        print(f"  Generated {len(project_credits)} project credits")

        print("Generating component usages...")
        component_usages = self.generate_component_usages()
        print(f"  Generated {len(component_usages)} component usages")

        print("Generating component user usages...")
        component_user_usages = self.generate_component_user_usages(component_usages)
        print(f"  Generated {len(component_user_usages)} component user usages")

        # Update preset data
        self.data["invoices"] = invoices
        self.data["invoice_items"] = invoice_items
        self.data["customer_credits"] = customer_credits
        self.data["project_credits"] = project_credits
        self.data["component_usages"] = component_usages
        self.data["component_user_usages"] = component_user_usages

        return self.data


def main():
    parser = argparse.ArgumentParser(
        description="Generate billing and reporting demo data for Waldur preset files."
    )
    parser.add_argument("preset_path", type=str, help="Path to the preset JSON file")
    parser.add_argument(
        "--months",
        type=int,
        default=12,
        help="Number of months of historical data to generate (default: 12)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        help="Output file path (default: overwrite input file)",
    )
    parser.add_argument("--seed", type=int, help="Random seed for reproducible output")

    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    preset_path = Path(args.preset_path)
    if not preset_path.exists():
        print(f"Error: Preset file not found: {preset_path}")
        return 1

    print(f"Loading preset from: {preset_path}")
    with open(preset_path) as f:
        preset_data = json.load(f)

    generator = PresetBillingGenerator(preset_data, months=args.months)
    updated_data = generator.generate_all()

    output_path = Path(args.output) if args.output else preset_path
    print(f"\nSaving to: {output_path}")
    with open(output_path, "w") as f:
        json.dump(updated_data, f, indent=2)

    print("\n=== Summary ===")
    print(f"Invoices: {len(updated_data.get('invoices', []))}")
    print(f"Invoice Items: {len(updated_data.get('invoice_items', []))}")
    print(f"Customer Credits: {len(updated_data.get('customer_credits', []))}")
    print(f"Project Credits: {len(updated_data.get('project_credits', []))}")
    print(f"Component Usages: {len(updated_data.get('component_usages', []))}")
    print(
        f"Component User Usages: {len(updated_data.get('component_user_usages', []))}"
    )

    return 0


if __name__ == "__main__":
    exit(main())
