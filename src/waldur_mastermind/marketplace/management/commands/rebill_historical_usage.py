import datetime
import decimal
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from waldur_mastermind.common.enums import Units
from waldur_mastermind.invoices import ledger
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.marketplace import billing_discount
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.billing_usage import BillingUsageProcessor
from waldur_mastermind.marketplace.billing_utils import convert_quantity
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    DiscountAggregations,
    ResourceStates,
)
from waldur_mastermind.marketplace.models import (
    ComponentUsage,
    Offering,
    PlanComponent,
    Resource,
)
from waldur_mastermind.policy import models as policy_models
from waldur_mastermind.policy.policy_actions import (
    POLICY_ACTIONS,
    _filter_resources_by_scope,
    _resources_locked_by_other_policies,
)

logger = logging.getLogger(__name__)


class _DryRunRollback(Exception):
    """Raised inside the outer atomic block in handle() to discard an entire
    simulated dry run (every resource-period in the run) in one go."""


class Command(BaseCommand):
    # Cost Policy actions that flip a per-resource state flag (and therefore
    # can be previewed as "these resources would change"). block_creation_*,
    # block_modification_*, and notify_* are deliberately excluded: they
    # don't touch existing resources the same way, so listing resources for
    # them would be misleading. filter_kwargs/exclude_states mirror each
    # action's own queryset in policy_actions.py exactly -- they differ per
    # action (e.g. request_pausing excludes only TERMINATED, request_downscaling
    # excludes TERMINATED and TERMINATING too), so this can't be one shared
    # queryset. field_name is the boolean flag _apply_generic_action compares
    # against the target value to decide whether a resource is a no-op for
    # this action; None for terminate_resources, which isn't a flag flip.
    _POLICY_RESOURCE_ACTIONS = {
        "request_pausing": {
            "field_name": "paused",
            "filter_kwargs": {"offering__plugin_options__supports_pausing": True},
            "exclude_states": (ResourceStates.TERMINATED,),
        },
        "request_downscaling": {
            "field_name": "downscaled",
            "filter_kwargs": {"offering__plugin_options__supports_downscaling": True},
            "exclude_states": (ResourceStates.TERMINATED, ResourceStates.TERMINATING),
        },
        "restrict_members": {
            "field_name": "restrict_member_access",
            "filter_kwargs": {
                "offering__plugin_options__service_provider_can_create_offering_user": True
            },
            "exclude_states": (ResourceStates.TERMINATED,),
        },
        "terminate_resources": {
            "field_name": None,
            "filter_kwargs": {},
            "exclude_states": (ResourceStates.TERMINATED, ResourceStates.TERMINATING),
        },
    }

    help = (
        "Re-bill ComponentUsage records whose invoice item is missing or stale "
        "because their invoice was already finalized when the usage was "
        "reported or corrected (e.g. via waldur_site_load_historical_usage in "
        "waldur-site-agent). Staff-only, one-off correction tool. Never run "
        "automatically or on a schedule. Pass -v 2 (or -v 3) for debug-level "
        "logging of every decision this command makes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Actually apply the correction. Without this flag the command "
            "always runs as a dry run -- it computes and prints the exact same "
            "plan (inside one transaction spanning every resource-period in "
            "the run, deliberately rolled back at the very end) but writes "
            "nothing to the database. Cost Policy previews therefore see "
            "earlier periods' corrections in the same dry run too, matching "
            "what --execute would actually produce. This default is "
            "deliberate: review the printed plan first, then re-run with "
            "--execute once it looks right.",
        )
        parser.add_argument(
            "--offering",
            dest="offering_uuid",
            help="Only process resources belonging to the offering with this UUID.",
        )
        parser.add_argument(
            "--resource",
            dest="resource_uuid",
            help="Only process the resource with this UUID.",
        )
        parser.add_argument(
            "--start-date",
            dest="start_date",
            help="Only billing periods on or after this date (format: YYYY-MM-DD).",
        )
        parser.add_argument(
            "--end-date",
            dest="end_date",
            help="Only billing periods on or before this date (format: YYYY-MM-DD).",
        )
        parser.add_argument(
            "--allow-aggregated-discount-recompute",
            action="store_true",
            help="Allow recomputing volume discounts for offering components that "
            "use the aggregated (non-per-resource) discount scope. This also "
            "rewrites discount amounts for OTHER resources sharing that offering "
            "component on the same invoice. Review the printed sibling-impact "
            "report (from a plain dry-run invocation) before using this.",
        )

    def _parse_date(self, value, label):
        try:
            return datetime.datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            raise CommandError(
                f"Invalid {label} {value!r}: expected format YYYY-MM-DD."
            )

    def _find_main_item(self, invoice, resource, offering_component):
        """Find the main (cost) invoice item for a resource/component.

        Compensation and discount items copy the main item's `details`
        wholesale (see MonthlyCompensation.calculate_current_compensations
        and billing_discount._create_discount_item), so they also carry
        `offering_component_type` and can match this lookup by accident.
        Disambiguate with `unit_price__gte=0`: a real cost item's price is
        never negative, while compensation items (only created `if
        credit_compensation:`, i.e. nonzero) and discount items (only
        created `if discount_amount > 0`) are always strictly negative --
        a hard domain invariant. Do NOT rely on JSON detail keys like
        `is_compensation`/`compensation_of_item` for this: production data
        can predate whenever that tagging convention was added and simply
        not carry them at all.
        """
        return invoice.items.filter(
            resource=resource,
            details__offering_component_type=offering_component.type,
            unit_price__gte=0,
        ).first()

    def handle(self, *args, **options):
        # `-v 2`/`-v 3` (Django's built-in --verbosity, added automatically by
        # BaseCommand) raises just this command's own logger to DEBUG. The
        # root logger stays at INFO, so this doesn't turn on debug output
        # for the rest of the app -- only for this command's own log calls.
        # The DatabaseLogHandler only accepts INFO+ regardless, so debug
        # detail only ever shows up on the console, never in persisted logs.
        if options["verbosity"] >= 2:
            logger.setLevel(logging.DEBUG)

        # Dry run is the default; --execute is the one thing that opts out of
        # it, so a bare invocation can never write to the database.
        dry_run = not options["execute"]
        self.allow_aggregated = options["allow_aggregated_discount_recompute"]
        logger.debug(
            "rebill_historical_usage starting: dry_run=%s offering=%s resource=%s "
            "start_date=%s end_date=%s allow_aggregated_discount_recompute=%s",
            dry_run,
            options["offering_uuid"],
            options["resource_uuid"],
            options["start_date"],
            options["end_date"],
            self.allow_aggregated,
        )
        start_date = (
            self._parse_date(options["start_date"], "--start-date")
            if options["start_date"]
            else None
        )
        end_date = (
            self._parse_date(options["end_date"], "--end-date")
            if options["end_date"]
            else None
        )
        if start_date and end_date and start_date > end_date:
            raise CommandError("--start-date must not be after --end-date.")

        offering = None
        if options["offering_uuid"]:
            try:
                offering = Offering.objects.get(uuid=options["offering_uuid"])
            except (Offering.DoesNotExist, ValueError):
                raise CommandError(
                    f"Offering with UUID {options['offering_uuid']!r} does not exist."
                )

        resource = None
        if options["resource_uuid"]:
            try:
                resource = Resource.objects.get(uuid=options["resource_uuid"])
            except (Resource.DoesNotExist, ValueError):
                raise CommandError(
                    f"Resource with UUID {options['resource_uuid']!r} does not exist."
                )

        usages = ComponentUsage.objects.filter(
            component__billing_type=BillingTypes.USAGE,
            component__is_prepaid=False,
        ).select_related(
            "resource",
            "resource__project",
            "resource__project__customer",
            "resource__offering",
            "component",
            "plan_period",
        )
        if offering:
            usages = usages.filter(resource__offering=offering)
        if resource:
            usages = usages.filter(resource=resource)
        if start_date:
            usages = usages.filter(billing_period__gte=start_date)
        if end_date:
            usages = usages.filter(billing_period__lte=end_date)

        prefix = "[DRY RUN] " if dry_run else ""
        corrected = 0
        unaffected = 0
        candidate_count = usages.count()
        logger.debug(
            "rebill_historical_usage: %s candidate usage(s) to consider",
            candidate_count,
        )

        def _process_all():
            nonlocal corrected, unaffected
            for usage in usages.order_by("resource_id", "billing_period"):
                label = (
                    f"{usage.resource.name} ({usage.resource.uuid.hex}) / "
                    f"{usage.component.type} / {usage.billing_period}"
                )
                logger.debug(
                    "Processing %s (ComponentUsage id=%s, usage=%s)",
                    label,
                    usage.pk,
                    usage.usage,
                )
                try:
                    if self._process_usage(usage, dry_run):
                        corrected += 1
                    else:
                        unaffected += 1
                except Exception:
                    # One broken resource-period must not abort the whole run --
                    # every prior correction already committed to its own
                    # savepoint (or, in --execute, its own transaction) and
                    # would otherwise be stranded with no final summary to
                    # show for it.
                    logger.exception("Unable to process usage for %s", label)
                    self.stdout.write(
                        self.style.ERROR(
                            f"{prefix}{label}: unexpected error, skipped. See "
                            f"logs for details."
                        )
                    )
                    unaffected += 1

        if dry_run:
            # Every resource-period used to run in its own transaction.atomic()
            # that rolled back immediately after that period's own preview was
            # printed (see _process_usage). That meant each period's Cost
            # Policy preview was evaluated as if it were the ONLY correction
            # that would ever be applied -- a dry run of periods 1..N-1 was
            # invisible to period N's preview, even though --execute commits
            # them in order and period N's real evaluation DOES see them.
            # Wrapping the whole batch in one outer transaction (rolled back
            # here, at the very end) makes each period's nested
            # transaction.atomic() in _process_usage a savepoint instead of an
            # independent transaction, so later periods' previews see earlier
            # periods' not-yet-discarded corrections too -- matching what
            # --execute would actually produce. The trade-off: any row locks
            # taken along the way (e.g. select_for_update() on credit rows in
            # _apply_credit_correction) are held for the whole dry run instead
            # of being released after each period, unlike --execute. Acceptable
            # for this staff-only, one-off tool; avoid dry-running huge batches
            # against a live system for long stretches.
            try:
                with transaction.atomic():
                    _process_all()
                    raise _DryRunRollback()
            except _DryRunRollback:
                pass
        else:
            _process_all()

        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Done: {corrected} resource-periods corrected, "
                f"{unaffected} unaffected/skipped."
            )
        )

    def _process_usage(self, usage: ComponentUsage, dry_run: bool) -> bool:
        resource = usage.resource
        offering_component = usage.component
        customer = resource.project.customer
        billing_period = usage.billing_period
        label = f"{resource.name} ({resource.uuid.hex}) / {offering_component.type} / {billing_period}"

        invoice = invoice_models.Invoice.objects.filter(
            customer=customer,
            year=billing_period.year,
            month=billing_period.month,
        ).first()
        if invoice is None:
            # No invoice yet for this period; ordinary billing will create it
            # (and bill it correctly) the next time usage is reported.
            logger.debug(
                "%s: no invoice for %s-%s yet, skipping",
                label,
                billing_period.year,
                billing_period.month,
            )
            return False
        logger.debug(
            "%s: invoice %s-%s state=%s",
            label,
            invoice.year,
            invoice.month,
            invoice.state,
        )
        if invoice.state in invoice_models.Invoice.States.MUTABLE_STATES:
            # Ordinary billing already handles mutable invoices on ComponentUsage
            # save; nothing for this tool to do.
            logger.debug("%s: invoice is mutable, ordinary billing handles it", label)
            return False
        if invoice.state != invoice_models.Invoice.States.CREATED:
            # PAID: money already collected against the stale price -- a
            # silent price change needs a manual reconciliation, not an
            # automatic one. CANCELED: reopening/re-finalizing a canceled
            # invoice would silently resurrect it into a billable state.
            self.stdout.write(
                self.style.WARNING(
                    f"  {label}: invoice is {invoice.state!r}, not 'created' -- "
                    f"skipping (this tool only corrects finalized-but-unpaid "
                    f"invoices; needs manual review)."
                )
            )
            return False

        had_plan_period = usage.plan_period is not None
        plan_period = (
            usage.plan_period
            or marketplace_utils.get_or_create_plan_period_for_historical_backfill(
                resource, billing_period
            )
        )
        if plan_period is None:
            logger.debug("%s: no plan period could be resolved or created", label)
            self.stdout.write(
                self.style.WARNING(
                    f"  {label}: no plan period available (resource not "
                    f"active/no plan); skipping."
                )
            )
            return False
        logger.debug(
            "%s: resolved plan_period id=%s (had_plan_period=%s)",
            label,
            plan_period.pk,
            had_plan_period,
        )
        if not had_plan_period:
            # The plan actually active during this historical month is
            # unknowable if no ResourcePlanPeriod ever covered it -- pricing
            # falls back to the resource's CURRENT plan, which may differ
            # from what was active back then.
            self.stdout.write(
                self.style.WARNING(
                    f"  {label}: no plan period covered this billing period; "
                    f"created one using the resource's current plan "
                    f"({resource.plan}). Verify pricing manually if the "
                    f"resource has changed plans since {billing_period:%Y-%m}."
                )
            )
            # Persist it onto the ComponentUsage row itself -- otherwise
            # this billing period would still read plan_period=None
            # afterwards, and every subsequent run would keep treating it
            # as unresolved.
            ComponentUsage.objects.filter(pk=usage.pk).update(plan_period=plan_period)
            usage.plan_period = plan_period

        old_item = self._find_main_item(invoice, resource, offering_component)

        # Cheap pre-check so unaffected resource-periods never touch the
        # invoice at all (minimizes blast radius on invoices with many items).
        # Skipped when --allow-aggregated-discount-recompute is set: that flag
        # is itself a request to force the discount pass for this resource's
        # offering component, even if the item's own quantity is already
        # correct (e.g. a follow-up run after reviewing a --dry-run warning).
        expected_quantity = convert_quantity(
            usage.usage,
            resource.offering.type,
            offering_component.type,
            billing_type=offering_component.billing_type,
        )
        logger.debug(
            "%s: quantity %s -> %s (billed item exists: %s)",
            label,
            old_item.quantity if old_item else None,
            expected_quantity,
            old_item is not None,
        )
        if (
            old_item is not None
            and old_item.quantity == expected_quantity
            and not self.allow_aggregated
        ):
            logger.debug("%s: already matches expected quantity, skipping", label)
            return False

        logger.debug("%s: applying correction (dry_run=%s)", label, dry_run)
        # Nested inside the outer dry-run transaction (see handle()) this is a
        # savepoint, not an independent transaction -- it isolates just this
        # resource-period from an exception in another one, but does NOT
        # discard this period's corrections on its own. The whole batch is
        # rolled back together, once, at the end of handle() when dry_run.
        with transaction.atomic():
            policies_before = self._snapshot_cost_policies(resource)
            self._apply_correction(
                usage, resource, offering_component, invoice, plan_period, dry_run
            )
            # Still inside the transaction the correction just ran in, so this
            # sees the corrected numbers -- for dry-run, including every prior
            # resource-period's corrections in this same run, before the
            # outer rollback in handle() discards all of them together.
            # is_triggered() itself has no side effects (it neither writes
            # has_fired nor fires any action), so evaluating it here is safe
            # in both modes.
            self._report_policy_impact(resource, policies_before, dry_run)
        return True

    def _snapshot_cost_policies(self, resource):
        """Every Cost Policy in scope for this resource's project, customer,
        or offering, with its currently-persisted `has_fired` -- captured
        before the correction runs, so the post-correction preview can
        report exactly what would change.

        OfferingEstimatedCostPolicy is included alongside the project/customer
        ones: it's an EstimatedCostPolicyMixin too, with trigger_class =
        InvoiceItem wired to post_save (policy/apps.py), so the invoice item
        this command rewrites triggers it exactly like the other two. It has
        no resource-state actions in its available_actions, so it never
        produces a resource listing below -- it can still fire and block new
        orders or notify owners, which is worth previewing.

        SLURM Periodic Usage Policy is deliberately not included: it only
        reacts to ComponentUsage.usage, which this command never touches --
        the plan_period backfill above uses .update(), which fires no
        signal, and never writes to usage itself -- so it can never be
        affected by anything this command does.
        """
        project = resource.project
        policies = (
            list(policy_models.ProjectEstimatedCostPolicy.objects.filter(scope=project))
            + list(
                policy_models.CustomerEstimatedCostPolicy.objects.filter(
                    scope=project.customer
                )
            )
            + list(
                policy_models.OfferingEstimatedCostPolicy.objects.filter(
                    scope=resource.offering
                )
            )
        )
        return [(policy, policy.has_fired) for policy in policies]

    _POLICY_SCOPE_LABELS = {
        policy_models.ProjectEstimatedCostPolicy: "Project",
        policy_models.CustomerEstimatedCostPolicy: "Customer",
        policy_models.OfferingEstimatedCostPolicy: "Offering",
    }

    def _report_policy_impact(self, resource, policies_before, dry_run) -> None:
        """Log every in-scope Cost Policy's gate state after the correction.

        Calls each policy's own `_cost_inputs()` / `_evaluated_cost()` --
        the exact methods `is_triggered()` itself calls -- instead of
        re-deriving the cost sum and credit deduction by hand. An earlier
        version did the latter and silently went stale when
        fix/cost-policy-double-credit-deduction changed how the deduction is
        computed (`_pending_compensation`, not the raw MonthlyCompensation
        projection): reusing the policy's own methods means this can't
        happen again short of `is_triggered()` itself changing, in which
        case this preview changes with it for free.
        """
        prefix = "[DRY RUN] " if dry_run else ""
        label = f"{resource.name} ({resource.uuid.hex})"

        if not policies_before:
            self.stdout.write(
                f"{prefix}{label}: no Cost Policy configured for this project, "
                f"customer, or offering -- nothing to evaluate. (SLURM Periodic "
                f"Usage Policy is never affected by this command -- it only "
                f"reacts to ComponentUsage changes, which this command never "
                f"makes.)"
            )
            return

        for policy, was_fired in policies_before:
            policy.refresh_from_db()
            scope_label = self._POLICY_SCOPE_LABELS[type(policy)]

            invoice_items, deduction = policy._cost_inputs()
            cost_total = policy._scoped_cost(invoice_items)
            net_cost = policy._evaluated_cost(invoice_items, deduction)
            # Match _is_triggered's strict `>`: cost exactly on limit_cost is
            # not triggered, so gate 1 must read closed there too.
            gate1 = "open" if net_cost > policy.limit_cost else "closed"

            credit_balance = None
            if isinstance(policy, policy_models.ProjectEstimatedCostPolicy):
                if policy.use_credit:
                    project_credit = invoice_models.ProjectCredit.objects.filter(
                        project=policy.scope
                    ).first()
                    if project_credit:
                        credit_balance = project_credit.value
                    else:
                        customer_credit = invoice_models.CustomerCredit.objects.filter(
                            customer=policy.scope.customer
                        ).first()
                        credit_balance = (
                            customer_credit.value if customer_credit else None
                        )
            elif isinstance(policy, policy_models.CustomerEstimatedCostPolicy):
                customer_credit = invoice_models.CustomerCredit.objects.filter(
                    customer=policy.scope
                ).first()
                credit_balance = customer_credit.value if customer_credit else None
            # OfferingEstimatedCostPolicy: no single customer's credit applies
            # (_cost_inputs always returns deduction=0), so credit_balance
            # stays None -- there is no gate 2 for this policy type.

            credit_note = ""
            if credit_balance is not None:
                gate2 = "open" if credit_balance <= policy.limit_cost else "closed"
                credit_note = f" credit_balance={credit_balance} (gate 2: {gate2})"

            now_triggered = policy.is_triggered()
            changed = was_fired != now_triggered
            verdict = ""
            if changed:
                verdict = (
                    " *** WOULD FIRE ***" if now_triggered else " *** WOULD RESET ***"
                )

            self.stdout.write(
                f"{prefix}{label}: [{scope_label} Cost Policy {policy.uuid.hex}] "
                f"limit_cost={policy.limit_cost} cost_this_window={net_cost} "
                f"(gate 1: {gate1}){credit_note} fired: {was_fired} -> "
                f"{now_triggered}{verdict}"
            )
            logger.debug(
                "%s: policy=%s scope=%s cost_total=%s deduction=%s net_cost=%s "
                "credit_balance=%s fired_before=%s fired_after=%s",
                label,
                policy.uuid.hex,
                scope_label,
                cost_total,
                deduction,
                net_cost,
                credit_balance,
                was_fired,
                now_triggered,
            )

            if not changed:
                continue

            action_names = [a for a in policy.actions.split(",") if a]
            resource_actions = [
                a for a in action_names if a in self._POLICY_RESOURCE_ACTIONS
            ]
            other_actions = [a for a in action_names if a not in resource_actions]
            if other_actions:
                self.stdout.write(
                    f"{prefix}{label}:   also configured: "
                    f"{', '.join(other_actions)} (not a resource pause / "
                    f"downscale / terminate action -- not previewed here)"
                )
            for action_name in resource_actions:
                meta = self._POLICY_RESOURCE_ACTIONS[action_name]

                if (
                    not now_triggered
                    and POLICY_ACTIONS[action_name].reset_method is None
                ):
                    # e.g. terminate_resources: nothing runs on a fired -> clear
                    # transition, so a resource list here would read as if
                    # terminated resources get restored.
                    self.stdout.write(
                        f"{prefix}{label}:   {action_name} has no reset -- "
                        f"nothing would run for it on this reset"
                    )
                    continue

                candidates = Resource.objects.filter(**meta["filter_kwargs"]).exclude(
                    state__in=meta["exclude_states"]
                )
                affected = _filter_resources_by_scope(candidates, policy)
                if affected is None:
                    continue

                field_name = meta["field_name"]
                if field_name is not None:
                    # Mirror _apply_generic_action exactly: a resource already
                    # at the target value is a no-op the real action skips,
                    # and (only when clearing) one another still-firing policy
                    # wants kept set is left alone too.
                    affected = affected.exclude(**{field_name: now_triggered})
                    if not now_triggered:
                        locked = _resources_locked_by_other_policies(policy, field_name)
                        if locked:
                            affected = affected.exclude(pk__in=locked)

                names = [f"{r.name} ({r.uuid.hex})" for r in affected]
                direction = "would apply to" if now_triggered else "would reset on"
                self.stdout.write(
                    f"{prefix}{label}:   {action_name} {direction} "
                    f"{len(names)} resource(s): {', '.join(names) if names else '(none)'}"
                )

    def _apply_correction(
        self, usage, resource, offering_component, invoice, plan_period, dry_run
    ) -> None:
        prefix = "[DRY RUN] " if dry_run else ""
        label = f"{resource.name} ({resource.uuid.hex}) / {offering_component.type} / {usage.billing_period}"

        old_item = self._find_main_item(invoice, resource, offering_component)
        old_price = old_item.price if old_item else decimal.Decimal(0)
        logger.debug(
            "%s: reopening invoice (state %s -> pending)", label, invoice.state
        )

        invoice.state = invoice_models.Invoice.States.PENDING
        invoice.save(update_fields=["state"])

        BillingUsageProcessor._create_or_update_usage_invoice_item(
            resource=resource,
            offering_component=offering_component,
            usage_to_bill=usage.usage,
            date=usage.billing_period,
            plan_period=plan_period,
        )

        new_item = self._find_main_item(invoice, resource, offering_component)
        if new_item is None:
            logger.debug(
                "%s: no invoice item after billing pass (no plan component / zero price?)",
                label,
            )
            self.stdout.write(
                self.style.WARNING(
                    f"{prefix}{label}: invoice item was not created (no plan "
                    f"component / zero price?); re-finalizing invoice unchanged."
                )
            )
            invoice.set_created()
            return

        new_price = new_item.price
        logger.debug(
            "%s: item id=%s quantity %s -> %s, price %s -> %s",
            label,
            new_item.pk,
            old_item.quantity if old_item else None,
            new_item.quantity,
            old_price,
            new_price,
        )
        self.stdout.write(
            f"{prefix}{label}: invoice item price {old_price} -> {new_price}"
        )

        self._apply_discount_recompute(
            resource, offering_component, invoice, plan_period, dry_run
        )

        invoice.set_created()
        logger.debug("%s: invoice re-finalized (state -> created)", label)

        if invoice_models.AffiliateFeeAccrual.objects.filter(invoice=invoice).exists():
            # accrue_affiliate_fee() is idempotent per (link, invoice) --
            # invoice.set_created() re-fires the invoice_created signal, but
            # the already-existing accrual makes it a no-op, so the fee
            # stays based on the pre-correction price.
            self.stdout.write(
                self.style.WARNING(
                    f"{prefix}{label}: an affiliate fee was already accrued "
                    f"for this invoice; it is NOT recomputed against the "
                    f"corrected price. Review manually if affiliate fees "
                    f"apply here."
                )
            )

        self._apply_credit_correction(
            resource, offering_component, invoice, new_item, dry_run
        )

    def _apply_discount_recompute(
        self, resource, offering_component, invoice, plan_period, dry_run
    ) -> None:
        prefix = "[DRY RUN] " if dry_run else ""
        label = f"{resource.name} ({resource.uuid.hex}) / {offering_component.type}"

        try:
            plan_component = plan_period.plan.components.get(
                component=offering_component
            )
        except PlanComponent.DoesNotExist:
            logger.debug(
                "%s: no matching plan component, skipping discount recompute", label
            )
            return

        if not (plan_component.discount_formula or "").strip():
            logger.debug(
                "%s: no discount_formula configured, skipping discount recompute", label
            )
            return

        logger.debug(
            "%s: discount_formula=%r aggregation=%s",
            label,
            plan_component.discount_formula,
            plan_component.discount_aggregation,
        )
        if plan_component.discount_aggregation == DiscountAggregations.PER_RESOURCE:
            # Only this resource's own discount bucket is affected.
            billing_discount.apply_aggregated_volume_discounts(invoice)
            return

        siblings = invoice.items.exclude(resource=resource).filter(
            plan_component__component=offering_component,
            plan_component__isnull=False,
        )
        sibling_resources = sorted(
            {
                f"{item.resource.name} ({item.resource.uuid.hex})"
                for item in siblings
                if item.resource_id
            }
        )
        if not sibling_resources:
            # No other resource on this invoice shares the component; safe.
            logger.debug(
                "%s: aggregated scope but no sibling resources, safe to recompute",
                label,
            )
            billing_discount.apply_aggregated_volume_discounts(invoice)
            return

        self.stdout.write(
            self.style.WARNING(
                f"{prefix}{label}: offering component uses aggregated (non-"
                f"per-resource) discount scope; recomputing would also rewrite "
                f"discount amounts for: {', '.join(sibling_resources)}."
            )
        )
        if self.allow_aggregated:
            self.stdout.write(
                f"{prefix}{label}: --allow-aggregated-discount-recompute set; "
                f"recomputing discounts for the whole invoice."
            )
            billing_discount.apply_aggregated_volume_discounts(invoice)
        else:
            self.stdout.write(
                f"{prefix}{label}: skipping discount recompute (pass "
                f"--allow-aggregated-discount-recompute to proceed after review)."
            )

    def _apply_credit_correction(
        self, resource, offering_component, invoice, new_item, dry_run
    ) -> None:
        prefix = "[DRY RUN] " if dry_run else ""
        label = (
            f"{resource.name} ({resource.uuid.hex}) / {offering_component.type} / "
            f"{invoice.year}-{invoice.month:02d}"
        )
        customer = resource.project.customer

        # Lock the credit rows before reading/mutating them, matching
        # invoices/tasks.py:process_invoice_credits (see WAL-9806): without
        # this, a concurrent invoice finalization or another correction run
        # touching the same customer's credit could race with our
        # read-modify-write on `.value` and silently lose an update.
        customer_credit = (
            invoice_models.CustomerCredit.objects.select_for_update()
            .filter(customer=customer)
            .first()
        )
        if customer_credit is None:
            logger.debug("%s: no CustomerCredit configured, nothing to correct", label)
            return
        project_credit = (
            invoice_models.ProjectCredit.objects.select_for_update()
            .filter(project=resource.project)
            .first()
        )
        logger.debug(
            "%s: customer_credit id=%s value=%s project_credit id=%s value=%s",
            label,
            customer_credit.pk,
            customer_credit.value,
            project_credit.pk if project_credit else None,
            project_credit.value if project_credit else None,
        )

        # Matched structurally (resource + component + credit + negative
        # price), not via the `compensation_of_item` detail key: production
        # compensation rows can predate whenever that tagging convention was
        # added and simply not carry it (or `is_compensation`) at all.
        old_compensation = decimal.Decimal(0)
        existing_compensation = invoice.items.filter(
            resource=resource,
            credit=customer_credit,
            details__offering_component_type=offering_component.type,
            unit_price__lt=0,
        ).first()
        if existing_compensation is not None:
            old_compensation = existing_compensation.unit_price * -1
        logger.debug(
            "%s: existing_compensation id=%s old_compensation=%s",
            label,
            existing_compensation.pk if existing_compensation else None,
            old_compensation,
        )

        # Net out any paired volume-discount item, mirroring
        # MonthlyCompensation.calculate_current_compensations's
        # discount_by_item handling -- otherwise credit would be drawn
        # against the pre-discount gross price.
        discount_price = decimal.Decimal(0)
        for discount_item in invoice.items.filter(
            details__is_discount=True,
            details__discount_of_item=new_item.uuid.hex,
        ):
            discount_price += discount_item.price
        new_compensation = new_item.price + discount_price
        if new_compensation < 0:
            new_compensation = decimal.Decimal(0)
        logger.debug(
            "%s: new_item.price=%s discount_price=%s new_compensation=%s",
            label,
            new_item.price,
            discount_price,
            new_compensation,
        )

        # Simplifying assumption: the corrected cost is drawn 1:1 against
        # credit, same as a fresh MonthlyCompensation pass would for a single
        # item — safe here because the correction is tiny relative to typical
        # credit balances. The available-balance check below is the guard
        # against that assumption not holding.
        delta_draw = new_compensation - old_compensation
        logger.debug("%s: delta_draw=%s", label, delta_draw)
        if delta_draw == 0:
            self.stdout.write(
                f"{prefix}{label}: credit compensation already correct "
                f"({-old_compensation}); no change."
            )
            return

        sibling_compensations = invoice.items.filter(
            credit=customer_credit, unit_price__lt=0
        ).exclude(resource=resource)
        if sibling_compensations.exists():
            sibling_resources = sorted(
                {
                    f"{item.resource.name} ({item.resource.uuid.hex})"
                    for item in sibling_compensations
                    if item.resource_id
                }
            )
            self.stdout.write(
                self.style.WARNING(
                    f"{prefix}{label}: this credit is also drawn by other "
                    f"resources on this invoice ({', '.join(sibling_resources)}). "
                    f"This correction only adjusts {resource.name}'s own "
                    f"compensation by its own price delta -- it does NOT "
                    f"re-run cheapest-first credit allocation across those "
                    f"resources, so their amounts may no longer match what a "
                    f"full recompute would produce if the credit is scarce."
                )
            )
        if customer_credit.minimal_consumption:
            self.stdout.write(
                self.style.WARNING(
                    f"{prefix}{label}: customer credit uses minimal-consumption "
                    f"logic; its minimal-consumption tail / expected_consumption "
                    f"were computed before this correction and are NOT "
                    f"recomputed here. Review manually if this correction is "
                    f"large relative to typical spend."
                )
            )

        available = customer_credit.value
        if project_credit:
            available = min(available, project_credit.value)
        logger.debug(
            "%s: available=%s (min of customer_credit=%s%s)",
            label,
            available,
            customer_credit.value,
            f", project_credit={project_credit.value}" if project_credit else "",
        )
        if delta_draw > 0 and delta_draw > available:
            # Used to abort the whole credit correction here, leaving this
            # period's ENTIRE incurred cost uncompensated instead of just the
            # part credit can't cover. That's wrong in two ways: it silently
            # diverges from what a live MonthlyCompensation pass would do
            # once credit runs out (draw what's left, don't refuse the
            # whole thing), and it makes a Cost Policy's cost_this_window
            # jump by the full period cost the moment credit is exhausted --
            # rather than by just the uncovered overage -- so the policy
            # fires far earlier than the customer's actual credit exhaustion
            # would warrant. Draw whatever credit remains instead, and leave
            # only the genuine shortfall as real incurred cost.
            applied_draw = max(decimal.Decimal(0), available)
            uncovered = delta_draw - applied_draw
            logger.debug(
                "%s: delta_draw %s exceeds available %s; drawing only %s, "
                "leaving %s of this period's cost uncompensated",
                label,
                delta_draw,
                available,
                applied_draw,
                uncovered,
            )
            self.stdout.write(
                self.style.WARNING(
                    f"{prefix}{label}: correction would draw {delta_draw} "
                    f"more credit than available ({available}); drawing only "
                    f"the remaining {applied_draw} of credit and leaving "
                    f"{uncovered} of this period's cost as real, uncompensated "
                    f"incurred cost (matches what a live MonthlyCompensation "
                    f"pass does once credit runs out)."
                )
            )
            new_compensation = old_compensation + applied_draw
            delta_draw = applied_draw
            if delta_draw == 0:
                # No credit left at all for this resource -- nothing to draw,
                # and any existing compensation item is already correct at
                # its current value, so there's nothing to write.
                return

        self.stdout.write(
            f"{prefix}{label}: credit compensation {-old_compensation} -> "
            f"{-new_compensation} (credit balance delta: {-delta_draw:+})"
        )

        if existing_compensation is not None:
            logger.debug(
                "%s: updating existing compensation item id=%s, unit_price %s -> %s",
                label,
                existing_compensation.pk,
                existing_compensation.unit_price,
                -new_compensation,
            )
            existing_compensation.unit_price = -new_compensation
            existing_compensation.details = dict(existing_compensation.details or {})
            existing_compensation.details["compensation_of_item"] = new_item.uuid.hex
            existing_compensation.save(update_fields=["unit_price", "details"])
        else:
            logger.debug("%s: creating new compensation item", label)
            invoice_models.InvoiceItem.objects.create(
                invoice=invoice,
                unit_price=-new_compensation,
                quantity=1,
                unit=Units.QUANTITY,
                credit=customer_credit,
                name=f"Credit compensation (retroactive correction). {new_item}",
                resource=resource,
                project=resource.project,
                # Match the cost item's own start/end (already correctly
                # derived from the billing period, not today's date) --
                # without this, InvoiceItem.start/end silently default to
                # get_current_month_start()/end(), dating the compensation
                # to whenever this command happened to run instead of the
                # invoice's actual billing period.
                start=new_item.start,
                end=new_item.end,
                details={
                    "is_compensation": True,
                    "compensation_of_item": new_item.uuid.hex,
                    "retroactive_correction": True,
                    # Without this, _find_main_item's own structural lookup
                    # (which matches on this exact key) would never find
                    # this row on a later run, creating a duplicate
                    # compensation item every time instead of updating it.
                    "offering_component_type": offering_component.type,
                },
            )

        with ledger.credit_transaction_type(
            invoice_models.CreditTransaction.Types.ADJUSTMENT,
            reference=invoice,
            comment=(
                f"Retroactive correction: usage for resource {resource.uuid.hex} "
                f"({offering_component.type}, {invoice.year}-{invoice.month:02d}) "
                f"was corrected after the invoice had already been finalized."
            ),
        ):
            old_customer_credit_value = customer_credit.value
            old_project_credit_value = project_credit.value if project_credit else None
            customer_credit.value -= delta_draw
            customer_credit.save(update_fields=["value"])
            if project_credit:
                project_credit.value -= delta_draw
                project_credit.save(update_fields=["value"])
            logger.debug(
                "%s: customer_credit %s -> %s, project_credit %s -> %s",
                label,
                old_customer_credit_value,
                customer_credit.value,
                old_project_credit_value,
                project_credit.value if project_credit else None,
            )
