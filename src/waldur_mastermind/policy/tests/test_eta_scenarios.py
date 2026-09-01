"""Scenario matrix for the cost-policy ETA.

Builds a project from scratch per scenario — credit shape, offering coverage,
invoice items, tax, period — and asserts the invariants that must hold in every
one of them, the load-bearing being that `eta_days == 0` and `is_triggered()`
agree. A projection that says "limit reached" on a policy the backend reports
as untriggered is the class of contradiction this field exists to stop clients
inventing, and it is only visible across a spread of credit shapes.

The table of every intermediate figure is printed on failure, which is what
makes a broken scenario diagnosable without re-deriving it by hand.
"""

import datetime
from decimal import Decimal

from rest_framework import test

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.models import PeriodMixin
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.tests import factories as mp_factories
from waldur_mastermind.policy.tests import factories as policy_factories

P = PeriodMixin.Periods
ROWS = []


class ScenarioMatrixTest(test.APITestCase):
    maxDiff = None

    def build(
        self,
        *,
        label,
        limit_cost,
        items,  # list of (offering_key, unit_price) ; offering_key None = no resource
        credit_offerings=None,  # None = unrestricted, [] = covers nothing, ['a'] = only a
        customer_credit=500000,
        project_credit=None,
        tax=0,
        period=P.TOTAL,
        expected_consumption=0,
        apply_minimal=False,
        use_credit=True,
    ):
        customer = structure_factories.CustomerFactory()
        project = structure_factories.ProjectFactory(customer=customer)
        offerings = {
            k: mp_factories.OfferingFactory(customer=customer, name=f"off-{k}")
            for k in ("a", "b")
        }
        resources = {
            k: mp_factories.ResourceFactory(project=project, offering=o)
            for k, o in offerings.items()
        }
        month_start = core_utils.month_start(datetime.date.today())
        invoice = invoices_factories.InvoiceFactory(
            customer=customer,
            year=month_start.year,
            month=month_start.month,
            tax_percent=tax,
        )
        for offering_key, price in items:
            invoices_factories.InvoiceItemFactory(
                invoice=invoice,
                project=project,
                resource=resources[offering_key] if offering_key else None,
                unit_price=price,
                quantity=1,
            )
        cc = invoices_factories.CustomerCreditFactory(
            customer=customer,
            value=customer_credit,
            expected_consumption=expected_consumption,
            apply_as_minimal_consumption=apply_minimal,
        )
        if credit_offerings is None:
            cc.offerings.add(*offerings.values())
        else:
            for k in credit_offerings:
                cc.offerings.add(offerings[k])
        if project_credit is not None:
            invoices_factories.ProjectCreditFactory(
                project=project,
                value=project_credit,
                expected_consumption=expected_consumption,
                apply_as_minimal_consumption=apply_minimal,
            )
        policy = policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=project,
            limit_cost=limit_cost,
            actions="request_pausing",
            period=period,
            use_credit=use_credit,
        )
        self.record(label, policy, limit_cost)
        return policy

    def record(self, label, policy, limit_cost):
        from waldur_mastermind.policy import eta as policy_eta

        items, deduction = policy._cost_inputs()
        today = datetime.date.today()
        gross = policy._gross_cost_this_month(items)
        gross_per_day = gross / Decimal(today.day)
        credit = policy._projection_credit()
        row = dict(
            label=label,
            limit=limit_cost,
            gross=round(float(gross), 2),
            deduction=round(float(deduction), 2),
            current=round(float(policy._evaluated_cost(items, deduction)), 2),
            uncomp=round(
                float(policy._uncompensated_cost_this_month(items, deduction)), 2
            ),
            credit_days=policy_eta.credit_days_remaining(credit, gross_per_day),
            gate=policy_eta.credit_days_to_limit(credit, limit_cost, gross_per_day),
            eta=policy.get_eta_days(),
            fired=policy.is_triggered(),
        )
        for k in ("credit_days", "gate"):
            if row[k] is not None:
                row[k] = round(float(row[k]), 1)
        ROWS.append(row)

        # Invariants that must hold in every scenario.
        self.assertEqual(
            row["eta"] == 0,
            row["fired"],
            f"{label}: eta==0 must mean is_triggered(); got eta={row['eta']} "
            f"fired={row['fired']}",
        )
        if row["eta"] is not None:
            self.assertGreaterEqual(row["eta"], 0, label)
            self.assertLessEqual(row["eta"], 365, label)

    # ---------------- scenarios ----------------

    def test_matrix(self):
        d = datetime.date.today().day

        self.build(
            label="1 no credit, room to spare",
            limit_cost=20000,
            items=[("a", 1000)],
            customer_credit=0,
            use_credit=False,
        )

        self.build(
            label="2 credit covers all, huge balance",
            limit_cost=5000,
            items=[("a", 1000)],
            project_credit=500000,
        )

        self.build(
            label="3 credit covers all, balance near limit",
            limit_cost=5000,
            items=[("a", 1000)],
            customer_credit=6000,
            project_credit=6000,
        )

        self.build(
            label="4 credit covers offering a only, b uncovered",
            limit_cost=5000,
            items=[("a", 1000), ("b", 400)],
            credit_offerings=["a"],
            project_credit=500000,
        )

        self.build(
            label="5 item with no resource (never covered)",
            limit_cost=5000,
            items=[("a", 1000), (None, 300)],
            project_credit=500000,
        )

        self.build(
            label="6 over limit, balance still above it",
            limit_cost=100,
            items=[("a", 1000)],
            project_credit=500000,
        )

        self.build(
            label="7 over limit, balance below it",
            limit_cost=100,
            items=[("a", 1000)],
            customer_credit=50,
            project_credit=50,
        )

        self.build(
            label="8 tax 20%, credit covers all",
            limit_cost=5000,
            items=[("a", 1000)],
            tax=20,
            project_credit=500000,
        )

        self.build(
            label="9 MONTH_1, no credit",
            limit_cost=20000,
            items=[("a", 1000)],
            period=P.MONTH_1,
            customer_credit=0,
            use_credit=False,
        )

        self.build(
            label="10 minimal consumption drains credit faster",
            limit_cost=5000,
            items=[("a", 1000)],
            customer_credit=60000,
            project_credit=60000,
            expected_consumption=30000,
            apply_minimal=True,
        )

        self.build(
            label="11 credit covers nothing (empty-but-set list)",
            limit_cost=5000,
            items=[("a", 1000)],
            credit_offerings=[],
            project_credit=500000,
        )

        self.build(
            label="12 no spend at all", limit_cost=5000, items=[], project_credit=500000
        )

        self.build(
            label="13 use_credit=False but a credit exists",
            limit_cost=5000,
            items=[("a", 1000)],
            project_credit=500000,
            use_credit=False,
        )

        self.build(
            label="14 MONTH_1, projection past month end",
            limit_cost=20000,
            items=[("a", 300)],
            period=P.MONTH_1,
            customer_credit=0,
            use_credit=False,
        )

        self.build(
            label="15 already over, no credit at all",
            limit_cost=500,
            items=[("a", 1000)],
            customer_credit=0,
            use_credit=False,
        )

        hdr = f"{'scenario':44} {'limit':>7} {'gross':>8} {'deduct':>8} {'current':>8} {'uncomp':>7} {'credit_d':>9} {'gate_d':>8} {'eta':>5} {'fires':>6}"
        print("\n" + "=" * len(hdr))
        print(f"today is day {d} of the month")
        print(hdr)
        print("-" * len(hdr))
        for r in ROWS:
            print(
                f"{r['label']:44} {r['limit']:>7} {r['gross']:>8} {r['deduction']:>8} "
                f"{r['current']:>8} {r['uncomp']:>7} {str(r['credit_days']):>9} "
                f"{str(r['gate']):>8} {str(r['eta']):>5} {str(r['fired']):>6}"
            )
        print("=" * len(hdr))
