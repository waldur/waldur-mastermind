import datetime
import json

from django.core.management.base import BaseCommand, CommandError

from waldur_core.structure.models import Customer, Project
from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.marketplace import billing_simulation
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.billing_simulation import Change

END_DATE = "end_date"


def _end_date(pairs):
    """The END_DATE=YYYY-MM-DD among ``pairs``, and the other pairs."""
    end_date, rest = None, []
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if sep and key == END_DATE:
            try:
                end_date = datetime.date.fromisoformat(value)
            except ValueError:
                raise CommandError(f"{END_DATE} must be YYYY-MM-DD.")
        else:
            rest.append(pair)
    return end_date, rest


def _limits(pairs):
    limits = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise CommandError(f"Expected TYPE=VALUE, got {pair!r}.")
        try:
            limits[key] = int(value)
        except ValueError:
            raise CommandError(f"Limit {key} must be a whole number.")
    return limits


def _get(model, uuid, label):
    try:
        return model.objects.get(uuid=uuid)
    except (model.DoesNotExist, ValueError):
        raise CommandError(f"{label} {uuid} not found.")


def resource(uuid):
    return _get(marketplace_models.Resource, uuid, "Resource")


def plan(uuid):
    return _get(marketplace_models.Plan, uuid, "Plan")


class Command(BaseCommand):
    help = (
        "Estimate what a change to a customer's resources would cost per month, "
        "by billing the next month with and without the change and rolling it "
        "back. Nothing is saved. The monthly, yearly and one-off figures are "
        "gross: before credit and excluding VAT. The month's credit, its total "
        "net of credit and its VAT are given next to them."
    )

    def add_arguments(self, parser):
        parser.add_argument("customer_uuid")
        parser.add_argument(
            "--month",
            help="Reference month as YYYY-MM; it must not be invoiced yet. "
            "Defaults to next month.",
        )
        parser.add_argument(
            "--resize",
            nargs="+",
            action="append",
            default=[],
            metavar=("RESOURCE_UUID", "TYPE=VALUE"),
            help="New limits for a resource, in the component's unit.",
        )
        parser.add_argument(
            "--switch",
            nargs=2,
            action="append",
            default=[],
            metavar=("RESOURCE_UUID", "PLAN_UUID"),
        )
        parser.add_argument(
            "--remove", action="append", default=[], metavar="RESOURCE_UUID"
        )
        parser.add_argument(
            "--add",
            nargs="+",
            action="append",
            default=[],
            metavar=("PLAN_UUID", "PROJECT_UUID"),
            help="A new resource: plan, project, then optional TYPE=VALUE "
            f"limits and {END_DATE}=YYYY-MM-DD, when it ends; a prepaid "
            "component is estimated only with an end date.",
        )
        parser.add_argument("--json", action="store_true", default=False)

    def handle(self, customer_uuid, month, resize, switch, remove, add, **options):
        customer = _get(Customer, customer_uuid, "Customer")
        reference = None
        if month:
            try:
                reference = datetime.datetime.strptime(month, "%Y-%m").date()
            except ValueError:
                raise CommandError("--month must be YYYY-MM.")

        changes = []
        try:
            for args in resize:
                changes.append(
                    Change(
                        "resize", resource=resource(args[0]), limits=_limits(args[1:])
                    )
                )
            for resource_uuid, plan_uuid in switch:
                changes.append(
                    Change(
                        "switch", resource=resource(resource_uuid), plan=plan(plan_uuid)
                    )
                )
            for resource_uuid in remove:
                changes.append(Change("remove", resource=resource(resource_uuid)))
            for args in add:
                if len(args) < 2:
                    raise CommandError("--add needs a plan and a project.")
                end_date, pairs = _end_date(args[2:])
                changes.append(
                    Change(
                        "add",
                        plan=plan(args[0]),
                        project=_get(Project, args[1], "Project"),
                        limits=_limits(pairs),
                        end_date=end_date,
                    )
                )
            result = billing_simulation.estimate(customer, changes, reference)
        except billing_simulation.SimulationError as e:
            raise CommandError(str(e))

        if options["json"]:
            self.stdout.write(json.dumps(_as_dict(result), indent=2, default=str))
            return
        self._print(result)

    def _print(self, result):
        current, changed = result.current, result.changed
        self.stdout.write(f"Reference month: {current.year}-{current.month:02d}")
        self.stdout.write(
            f"{'Resource':30} {'Component':16} {'Current':>12} {'Changed':>12} {'Difference':>12}"
        )
        for row in result.differences():
            component = row["component_type"] + (
                " discount" if row["is_discount"] else ""
            )
            self.stdout.write(
                f"{row['resource_name'][:30]:30} {component[:16]:16} "
                f"{row['current']:>12} {row['changed']:>12} {row['difference']:>12}"
            )
        self.stdout.write("Before credit, excluding VAT:")
        self.stdout.write(
            f"Monthly: {current.monthly_total} -> {changed.monthly_total} "
            f"({result.monthly_difference:+})"
        )
        self.stdout.write(
            f"Invoice: {current.invoice_total} -> {changed.invoice_total} "
            f"({result.invoice_difference:+}), the reference month's own invoice"
        )
        self.stdout.write(
            f"Yearly:  {current.yearly_total} -> {changed.yearly_total} "
            f"({result.yearly_difference:+})"
        )
        self.stdout.write(f"One-off: {result.one_off_total}")
        self.stdout.write(
            f"Billed this month by the orders: {result.current_month_total}"
        )
        self.stdout.write("After credit, excluding VAT, the reference month only:")
        self.stdout.write(
            f"Credit:  {current.credit_compensation} -> {changed.credit_compensation}"
            f" (drawn {current.credit_drawn} -> {changed.credit_drawn})"
        )
        self.stdout.write(
            f"Monthly: {current.monthly_total_net} -> {changed.monthly_total_net} "
            f"({result.monthly_net_difference:+})"
        )
        self.stdout.write(
            f"VAT ({changed.tax_percent}%): {quantize_price(current.monthly_tax)} "
            f"-> {quantize_price(changed.monthly_tax)}"
        )
        for note in changed.notes:
            self.stdout.write(f"Note: {note}")
        for row in changed.not_estimated:
            self.stdout.write(
                f"Not estimated: {row['resource_name']} / {row['component_type']}: "
                f"{row['reason']}"
            )


def _as_dict(result):
    return {
        "year": result.current.year,
        "month": result.current.month,
        "monthly": {
            "current": result.current.monthly_total,
            "changed": result.changed.monthly_total,
            "difference": result.monthly_difference,
        },
        "yearly": {
            "current": result.current.yearly_total,
            "changed": result.changed.yearly_total,
            "difference": result.yearly_difference,
        },
        "invoice": {
            "current": result.current.invoice_total,
            "changed": result.changed.invoice_total,
            "difference": result.invoice_difference,
        },
        "one_off": result.one_off_total,
        "current_month": result.current_month_total,
        # The figures above are before credit and excluding VAT; these are
        # for the reference month only.
        "credit": {
            "current": result.current.credit_compensation,
            "changed": result.changed.credit_compensation,
        },
        "credit_drawn": {
            "current": result.current.credit_drawn,
            "changed": result.changed.credit_drawn,
        },
        "monthly_net": {
            "current": result.current.monthly_total_net,
            "changed": result.changed.monthly_total_net,
            "difference": result.monthly_net_difference,
        },
        "tax_percent": result.changed.tax_percent,
        "monthly_tax": {
            "current": quantize_price(result.current.monthly_tax),
            "changed": quantize_price(result.changed.monthly_tax),
        },
        "lines": result.differences(),
        "not_estimated": result.changed.not_estimated,
        "notes": result.changed.notes,
    }
