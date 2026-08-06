"""Tests for the 0258 per-resource -> (customer, offering) collapse planner.

The migration itself cannot be replayed once ``ResourceAccessSubnet`` is dropped
from the app registry, so the planning logic is exercised directly. What matters
here is that the two side effects of the union are reported: a resource that
gains addresses it did not have, and a resource that had no list and starts
inheriting one.
"""

from django.test import SimpleTestCase

from waldur_mastermind.marketplace.migrations._collapse_resource_subnets import (
    plan_collapse,
)


def _row(resource, inet, customer=1, offering=1, description=""):
    return {
        "customer_id": customer,
        "customer_name": f"Customer {customer}",
        "offering_id": offering,
        "offering_name": f"Offering {offering}",
        "resource_name": resource,
        "inet": inet,
        "description": description,
    }


class PlanCollapseTest(SimpleTestCase):
    def test_uniform_lists_collapse_without_widening(self):
        plan = plan_collapse(
            [
                _row("a", "10.0.0.1/32"),
                _row("b", "10.0.0.1/32"),
            ]
        )
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["union"], ["10.0.0.1/32"])
        self.assertEqual(plan[0]["widened"], [])
        self.assertEqual(plan[0]["newly_restricted"], [])

    def test_divergent_lists_report_what_each_resource_gains(self):
        plan = plan_collapse(
            [
                _row("narrow", "10.0.0.1/32"),
                _row("wide", "10.0.0.1/32"),
                _row("wide", "10.0.0.2/32"),
            ]
        )
        self.assertEqual(plan[0]["union"], ["10.0.0.1/32", "10.0.0.2/32"])
        self.assertEqual(
            plan[0]["widened"],
            [{"resource_name": "narrow", "gained": ["10.0.0.2/32"]}],
        )

    def test_resource_without_subnets_is_reported_as_newly_restricted(self):
        plan = plan_collapse(
            [
                _row("has-list", "10.0.0.1/32"),
                _row("no-list", None),
            ]
        )
        self.assertEqual(plan[0]["newly_restricted"], ["no-list"])
        # It gains nothing it can be "widened" to — it simply had no list.
        self.assertEqual(plan[0]["widened"], [])

    def test_pairs_are_kept_separate_per_customer(self):
        plan = plan_collapse(
            [
                _row("a", "10.0.0.1/32", customer=1),
                _row("b", "10.0.0.2/32", customer=2),
            ]
        )
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[0]["union"], ["10.0.0.1/32"])
        self.assertEqual(plan[1]["union"], ["10.0.0.2/32"])
        # No cross-contamination: neither pair sees the other's address.
        self.assertEqual(plan[0]["widened"], [])
        self.assertEqual(plan[1]["widened"], [])

    def test_pairs_are_kept_separate_per_offering(self):
        plan = plan_collapse(
            [
                _row("a", "10.0.0.1/32", offering=1),
                _row("b", "10.0.0.2/32", offering=2),
            ]
        )
        self.assertEqual(len(plan), 2)
        self.assertEqual(
            [pair["union"] for pair in plan], [["10.0.0.1/32"], ["10.0.0.2/32"]]
        )

    def test_first_description_wins_for_a_duplicated_address(self):
        plan = plan_collapse(
            [
                _row("a", "10.0.0.1/32", description="office"),
                _row("b", "10.0.0.1/32", description="vpn"),
            ]
        )
        self.assertEqual(plan[0]["inets"], {"10.0.0.1/32": "office"})

    def test_pair_with_only_subnetless_resources_restricts_nobody(self):
        # Fail-open: an empty union must not be reported as newly restricting.
        plan = plan_collapse([_row("a", None), _row("b", None)])
        self.assertEqual(plan[0]["union"], [])
        self.assertEqual(plan[0]["newly_restricted"], [])
        self.assertEqual(plan[0]["inets"], {})
