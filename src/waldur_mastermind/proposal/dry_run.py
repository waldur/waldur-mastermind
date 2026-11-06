"""Dry run of a call's workflow on a synthetic proposal.

The call team picks a path -- which offerings are requested, and one action
after another: an outcome, contributions, a lapse, a request for changes --
and sees what the real engine makes of it: the state of every step, the
log, and every mail it would send, rendered with the real templates. Nothing
it does survives.

How it holds:

- It is stateless: each request replays its whole list of actions from
  scratch, and rewinding is sending a shorter list.
- Everything runs in one transaction that is always rolled back: the run ends
  by raising a private exception, so no code path can commit it.
- The synthetic proposal, its round, its applicant and every person acting on
  it are created inside that transaction; no real proposal or person takes
  part. Real offerings are requested, as the call accepts them; their teams
  appear as a count only.
- The engine is driven through its own functions, as the endpoints drive it,
  with side effects switched off: mail is handed to a collector that renders
  it (``dry_run_context``), allocation is recorded instead of provisioned,
  and ``skip_side_effects`` keeps the signal handlers quiet.
- Tripwires (``tripwires.py``) make any other side effect fail the run:
  sending mail, queueing or running a Celery task, publishing to the broker,
  registering an ``on_commit`` callback, writing to the cache, opening a
  network connection to anything but the database and the cache, writing a
  file to storage, creating a marketplace order and starting a thread raise
  ``DryRunSideEffect`` while the run is armed. What a library does in C --
  LDAP, say -- is out of their reach; no step of the engine calls one.
- One run per call at a time (a transaction-level advisory lock; busy is
  ``DryRunBusy``), within a wall-clock budget -- checked before each action
  and as each synthetic person is made -- and the database's statement,
  idle and transaction timeouts.
- A step asks for at most ``models.MAX_STEP_PEOPLE`` contributors or holders,
  and a call asking for more cannot be copied for a run; a run makes no more
  than ``MAX_PEOPLE_PER_STEP`` people for one step or call role, and says so
  in its log if the call asks for more.

``view_as`` previews the proposal as a role of the call's workflow
description would see it: a synthetic user is given that role inside the
same transaction and the real read endpoints answer as them.

Steps run once per round are one action on the synthetic proposal: opened for
its round, their round reports written by synthetic authors, and completed.
After the award, extension requests and the final report run on the accepted
proposal with its allocation still simulated: a synthetic project holds the
end date an extension moves, and nothing is provisioned. The synthetic round
falls under the budgets of the call's latest round; a run may take them as
spent to see an award refused or deferred.

Features add their actions to ``ACTION_HANDLERS`` and their step fields to
``STEP_STATE_EXTENSIONS``.
"""

import contextlib
import json
import logging
import math
import time
import uuid
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import InternalError, OperationalError, connection, transaction
from django.db.models import Q
from django.http import HttpRequest, QueryDict
from django.urls import resolve, reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import ValidationError

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.enums import QuestionTypes
from waldur_core.core import middleware as core_middleware
from waldur_core.core import utils as core_utils
from waldur_core.core.models import User
from waldur_core.logging import middleware as logging_middleware
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event, Feed
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.models import Role, UserRole
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.proposal import (
    budgets,
    call_transfer,
    change_requests,
    contributions,
    dry_run_context,
    models,
    notification_overrides,
    notification_rules,
    proposal_roles,
    round_reports,
    round_steps,
    runs,
    step_inputs,
    tasks,
    workflow_description,
    workflow_service,
)
from waldur_mastermind.proposal import tripwires as tripwires_module
from waldur_mastermind.proposal.enums import (
    REVIEW_SCORE_MAX,
    REVIEW_SCORE_MIN,
    SCORE_SPREAD_ABOVE,
    ContributionOutcomes,
    ContributorSources,
    DelegationTargets,
    EvaluationStart,
    NotificationRuleTriggers,
    OutcomeSets,
    ProposalRoleEligibility,
    ProposalStates,
    RequestedOfferingStates,
    ResponsibleRoles,
    StepContributionStatuses,
    StepKinds,
    WorkflowPhases,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
    parse_contributor_source,
)

logger = logging.getLogger(__name__)

MAX_ACTIONS = 200
MAX_OFFERINGS = 10
# The most synthetic people a run makes for one step or one call role: what a
# step may ask for. A call is validated as it is copied, so only a bound
# lowered since -- or a role's holders on the real call -- reach it.
MAX_PEOPLE_PER_STEP = models.MAX_STEP_PEOPLE
TIMEOUT_SECONDS = 15
# Waiting on a row someone else holds is bounded well below the budget.
LOCK_TIMEOUT_MS = 3000
# PostgreSQL bounds a whole transaction from version 17 on.
TRANSACTION_TIMEOUT_SINCE = 170000
# How the database says it stopped the run. The idle and transaction timeouts
# end the session, which arrives as an InternalError, not an OperationalError.
TIMEOUT_REASONS = (
    "statement timeout",
    "lock timeout",
    "transaction timeout",
    "idle-in-transaction timeout",
)
DATABASE_STOPS = (OperationalError, InternalError)
# The namespace of the advisory lock taken on a call for its dry run.
ADVISORY_LOCK_NAMESPACE = 575_718

EXAMPLE_DOMAIN = "dry-run.invalid"
PLACEHOLDER_TEXT = "Placeholder text of the dry run."


class DryRunError(ValueError):
    """An action the proposal's state does not allow, or a request the call
    cannot run. Answered 400, naming the action and its step."""

    def __init__(self, message, action=None, step=None):
        super().__init__(message)
        self.message = message
        self.action = action
        self.step = step


class DryRunBusy(Exception):
    """Another dry run of the call is in progress."""


class DryRunTimeout(Exception):
    """The run took longer than its budget."""


class _Rollback(Exception):
    """Ends every run: raised inside its transaction so nothing commits."""


# =============================================================================
# Tripwires
# =============================================================================

# The run arms them (``tripwires.py``); named here for those who read the run.
DryRunSideEffect = tripwires_module.DryRunSideEffect
tripwires = tripwires_module.tripwires
_armed = tripwires_module._armed


def _templates_cached():
    """Let the template loader cache what it looked up while a mail is
    rendered: the deployment's own templates, or that it has none, read from
    a table no dry run writes -- nothing of the run's own."""
    return tripwires_module.disarmed()


@contextlib.contextmanager
def _acting_as(user):
    """Record whatever the engine logs as done by ``user``: the request's
    own user and its address never reach the synthetic proposal's events."""
    previous_user = core_middleware.get_current_user()
    previous_context = logging_middleware.get_event_context()
    core_middleware.set_current_user(user)
    logging_middleware.set_event_context(user._get_log_context("user"))
    try:
        yield
    finally:
        core_middleware.set_current_user(previous_user)
        if previous_context is None:
            logging_middleware.reset_event_context()
        else:
            logging_middleware.set_event_context(previous_context)


# =============================================================================
# Collecting mail
# =============================================================================

# What a mail's audience reads as, by the recipient it was built for.
AUDIENCE_ACTORS = {
    "applicant": workflow_description.APPLICANT,
    "call_managers": workflow_description.CALL_MANAGER,
    "assigned_reviewers": workflow_description.REVIEWER,
    "reviewers": workflow_description.REVIEWER,
    "all_pool_reviewers": workflow_description.REVIEWER,
    "panel_chair": workflow_description.PANEL_CHAIR,
    "offering_teams": workflow_description.OFFERING_TEAM,
}
AUDIENCE_LABELS = {
    "all_pool_reviewers": "Every reviewer in the call's pool",
    "round_report_author": "Each author of a round report",
    "contributor": "Whoever revises the reopened work",
    "delegation": "The person the work is handed to, or who handed it on",
}


class Collector:
    """Where the engine's mail goes in a dry run.

    The engine hands over what it would queue; the run renders it once the
    action is done, as the real task would render it after the request
    commits, so the mail reads the state the action left.
    """

    def __init__(self, run):
        self.run = run
        self.pending = []

    # Hooks the engine calls (``dry_run_context``).

    def step_event(self, instance, trigger):
        self.pending.append(("step", instance.pk, trigger))

    def change_request(self, change_request, event):
        self.pending.append(("change", change_request.pk, event))

    def changes_submitted(self, closed):
        self.pending.append(
            ("submitted", [change_request.pk for change_request in closed])
        )

    def allocation(self, proposal):
        # Logged once the action's steps are, after the decision taking it.
        self.run.allocated = True

    # The run's own mail: what the endpoints send around the engine.

    def mails(self, mails, trigger, instance=None):
        self.pending.append(("mails", mails, trigger, instance))

    def flush(self):
        pending, self.pending = self.pending, []
        for item in pending:
            kind = item[0]
            if kind == "step":
                instance = models.ProposalWorkflowStepInstance.objects.select_related(
                    "call_step", "proposal__round__call__manager__customer"
                ).get(pk=item[1])
                rules = list(notification_rules.enabled_rules(instance, item[2]))
                self.render(
                    tasks.step_event_mails(instance, item[2], rules), item[2], instance
                )
            elif kind == "change":
                change_request = models.ProposalChangeRequest.objects.select_related(
                    "step_instance__call_step",
                    "proposal__round__call__manager__customer",
                    "contribution__requested_offering",
                ).get(pk=item[1])
                self.render(
                    tasks.change_request_mails(change_request, item[2]),
                    item[2],
                    change_request.step_instance,
                )
            elif kind == "submitted":
                closed = list(
                    models.ProposalChangeRequest.objects.select_related(
                        "step_instance__call_step",
                        "proposal__round__call__manager__customer",
                    )
                    .filter(pk__in=item[1])
                    .order_by("created", "id")
                )
                self.render(
                    tasks.changes_submitted_mails(closed),
                    NotificationRuleTriggers.CHANGES_SUBMITTED,
                    closed[0].step_instance if closed else None,
                )
            else:
                self.render(item[1], item[2], item[3])

    def render(self, mails, trigger, instance=None):
        for mail in mails:
            if not mail.recipients:
                continue
            self.run.message(mail, trigger, instance)


# =============================================================================
# The run
# =============================================================================


def _day(value):
    return None if value is None else int(value)


class _Run:
    """One replay of a path on a fresh synthetic proposal."""

    def __init__(self, call, data, deadline):
        # The real call is only read: the run works on a copy of it.
        self.real_call = call
        self.call = None
        self.data = data
        self.deadline = deadline
        self.collector = Collector(self)
        self.log = []
        self.messages = []
        self.people = {}
        self.offering_members = {}
        self.counters = {}
        self.clock = 0
        self.days = {}
        self.reminded = set()
        self.allocated = False
        self.tried = {}
        self.contribution_tried = {}
        self.exercised = []
        self.description = workflow_description.describe_call(call)
        self.real_requested = []
        self.actor_labels = {
            actor["key"]: actor["label"] for actor in self.description["actors"]
        }

    # -- people ---------------------------------------------------------------

    def person(self, label, single=False):
        """A synthetic person, "Example <label> <n>": no password, no token,
        an address nobody reads; gone with the transaction.

        Every loop of the run makes its people here, so the run's budget is
        checked here as well as between actions."""
        if time.monotonic() > self.deadline:
            raise DryRunTimeout()
        self.counters[label] = self.counters.get(label, 0) + 1
        number = self.counters[label]
        username = f"dry-run-{uuid.uuid4().hex}"
        user = User.objects.create(
            username=username,
            email=f"{username}@{EXAMPLE_DOMAIN}",
            first_name="Example",
            last_name=label if single and number == 1 else f"{label} {number}",
            is_active=True,
            notifications_enabled=True,
        )
        user.set_unusable_password()
        user.save(update_fields=["password"])
        # Nor a token: one is made for every new user.
        Token.objects.filter(user=user).delete()
        self.people[user.pk] = user.full_name
        return user

    def capped(self, needed, instance):
        """``needed`` people, but no more than a step may ask for: a call
        configured before the bound, or written around it, asks for more,
        and the run says how many it made instead."""
        if needed <= MAX_PEOPLE_PER_STEP:
            return needed
        self.log_entry(
            "capped",
            instance,
            text=(
                f"{self.label(instance)} asks for {needed} people; the dry run "
                f"makes {MAX_PEOPLE_PER_STEP} of them."
            ),
        )
        return MAX_PEOPLE_PER_STEP

    # -- log ------------------------------------------------------------------

    def log_entry(self, kind, instance=None, text="", **extra):
        entry = {
            "index": len(self.log),
            "action": self.action_index,
            "day": self.clock,
            "kind": kind,
            "step": instance.step if instance is not None else None,
            "step_name": (instance.call_step.name or instance.step)
            if instance is not None
            else None,
            "text": text,
        }
        entry.update(extra)
        self.log.append(entry)
        return entry

    def audience(self, mail):
        audience, detail = mail.audience, mail.audience_detail
        actor = AUDIENCE_ACTORS.get(audience)
        if audience in (
            "proposal_role",
            "proposal_role_lead",
        ):
            prefix = (
                workflow_description.ROLE_LEAD_PREFIX
                if audience == "proposal_role_lead"
                else workflow_description.ROLE_PREFIX
            )
            actor = f"{prefix}{detail}"
        elif audience == "responsible_role":
            if detail.startswith("proposal_role:"):
                actor = f"{workflow_description.ROLE_PREFIX}{detail.split(':', 1)[1]}"
            else:
                actor = workflow_description.COMPLETER_ACTORS.get(detail)
        label = (
            AUDIENCE_LABELS.get(audience)
            or self.actor_labels.get(actor)
            or (audience.replace("_", " ").capitalize())
        )
        return {
            "recipient": audience,
            "actor": actor,
            "label": label,
            "count": len(set(mail.recipients)),
            # Copied on the applicant's mail (``Call.applicant_cc_roles``).
            "cc_count": len(set(mail.cc)),
        }

    def message(self, mail, trigger, instance=None):
        enabled = core_utils.notification_enabled(mail.app, mail.event_type)
        with _templates_cached():
            # In the call's own wording where it has one: the copy carries it.
            subject, text, html, _source = notification_overrides.render(mail)
        message = {
            "index": len(self.messages),
            "action": self.action_index,
            "day": self.clock,
            "trigger": trigger or "",
            "notification": f"{mail.app}.{mail.event_type}",
            "enabled": bool(enabled),
            "step": instance.step if instance is not None else None,
            "step_name": (instance.call_step.name or instance.step)
            if instance is not None
            else None,
            "audience": self.audience(mail),
            "single_message": mail.single_message,
            "subject": subject,
            "text": text,
            "html": html,
        }
        self.messages.append(message)
        self.log_entry(
            "notification",
            instance,
            text=f"{message['audience']['label']} would be told: {subject}",
            message=message["index"],
        )

    # -- setting up ---------------------------------------------------------------

    def requested_offerings(self):
        accepted = models.RequestedOffering.objects.filter(
            call=self.real_call, state=RequestedOfferingStates.ACCEPTED
        ).select_related("offering")
        wanted = self.data.get("requested_offerings")
        if wanted is None:
            chosen = list(accepted.order_by("created", "id")[:MAX_OFFERINGS])
        else:
            by_uuid = {ro.uuid.hex: ro for ro in accepted.filter(uuid__in=wanted)}
            missing = [str(u) for u in wanted if uuid.UUID(str(u)).hex not in by_uuid]
            if missing:
                raise DryRunError(
                    "These are not offerings the call has accepted: "
                    f"{', '.join(missing)}."
                )
            chosen = [by_uuid[uuid.UUID(str(u)).hex] for u in wanted]
        return chosen

    def copy_call(self):
        """A copy of the real call made inside the transaction, as an import
        of its export makes one: its settings, evaluator roles,
        steps, rules and forms. The run never writes, nor locks, a row of the
        real call or of its rounds.

        Each person holding a call role holds it on the copy through a
        synthetic stand-in -- the panel's chair and vice-chair too -- so that
        mail to them is counted as on the real call, and no real person is
        a recipient of the run. A role held by more people than a step may
        ask for is held by that many stand-ins."""
        real = self.real_call
        document, _ = call_transfer.export_call(
            real, {"documents": False, "rounds": False, "offerings": False}
        )
        try:
            call, _, _ = call_transfer.import_call(
                document, real.manager, self.manager, name=real.name
            )
        except ValidationError as error:
            raise DryRunError(
                f"The call's configuration cannot be replayed: {error.detail}"
            )
        holders = UserRole.objects.filter(
            content_type=ContentType.objects.get_for_model(models.Call),
            object_id=real.pk,
            is_active=True,
        ).select_related("role")
        stand_ins = {}
        held = {}
        rows = []
        for holder in holders.order_by("role_id", "id"):
            if holder.user_id in stand_ins:
                user = stand_ins[holder.user_id]
            else:
                count = held.get(holder.role_id, 0)
                if count >= MAX_PEOPLE_PER_STEP:
                    continue
                held[holder.role_id] = count + 1
                user = self.person(
                    holder.role.name.split(".")[-1].replace("_", " ").lower()
                )
                stand_ins[holder.user_id] = user
            rows.append(
                UserRole(
                    user=user,
                    role_id=holder.role_id,
                    content_type_id=holder.content_type_id,
                    object_id=call.pk,
                    is_active=True,
                    expiration_time=holder.expiration_time,
                )
            )
        UserRole.objects.bulk_create(rows)

        def stand_in(user_id):
            if user_id is None:
                return None
            if user_id not in stand_ins:
                stand_ins[user_id] = self.person("panel member")
            return stand_ins[user_id].pk

        models.Call.objects.filter(pk=call.pk).update(
            state=real.state,
            panel_chair_id=stand_in(real.panel_chair_id),
            panel_vice_chair_id=stand_in(real.panel_vice_chair_id),
        )
        call.refresh_from_db()
        return call

    def copy_requested(self, real):
        """The copy's request of ``real``'s offering, accepted as there."""
        copy = models.RequestedOffering.objects.get(pk=real.pk)
        copy.pk = None
        copy.id = None
        copy.uuid = uuid.uuid4().hex
        copy.call = self.call
        copy._state.adding = True
        copy.save()
        return models.RequestedOffering.objects.select_related("offering").get(
            pk=copy.pk
        )

    def copy_budgets(self, latest):
        """The component rules of the requested offerings, on the copy, with
        the pools the real call's latest round draws on: the call-wide one
        stays call-wide, the round's covers the synthetic round. Each offers
        what remains of it: what the real call awarded counts as spent."""
        for real, copy in zip(self.real_requested, self.requested):
            for rule in real.component_rules.order_by("id"):
                new_rule = models.CallOfferingComponent.objects.create(
                    requested_offering=copy,
                    component_type=rule.component_type,
                    min_per_proposal=rule.min_per_proposal,
                    max_per_proposal=rule.max_per_proposal,
                    default_amount=rule.default_amount,
                    is_fixed=rule.is_fixed,
                )
                pools = rule.pools.select_related(
                    "offering_component__requested_offering", "round"
                )
                if latest is not None:
                    pools = pools.filter(Q(round__isnull=True) | Q(round=latest))
                else:
                    pools = pools.filter(round__isnull=True)
                for pool in pools.order_by("id"):
                    spent = budgets.awarded_so_far(pool)
                    models.CallResourcePool.objects.create(
                        offering_component=new_rule,
                        round=None if pool.round_id is None else self.round,
                        offered_total=max(pool.offered_total - spent, 0),
                    )

    def setup(self):
        self.manager = self.person("call manager", single=True)
        # From here on, whatever the engine records is done by them.
        core_middleware.set_current_user(self.manager)
        logging_middleware.set_event_context(self.manager._get_log_context("user"))
        self.real_requested = self.requested_offerings()
        self.call = call = self.copy_call()
        now = timezone.now()
        latest = self.real_call.round_set.order_by("-start_time", "-id").first()
        self.round = models.Round.objects.create(
            call=call,
            start_time=now - timedelta(days=1),
            # The round closes as the proposal is submitted: evaluation
            # starts at once, whichever start the call has.
            cutoff_time=now,
            review_duration_in_days=latest.review_duration_in_days if latest else None,
            undecided_at_round_completion=latest.undecided_at_round_completion
            if latest
            else "",
        )
        self.applicant = self.person("applicant", single=True)
        self.proposal = models.Proposal.objects.create(
            round=self.round,
            name="Example proposal",
            description=PLACEHOLDER_TEXT,
            project_summary=PLACEHOLDER_TEXT,
            created_by=self.applicant,
        )
        self.proposal.add_user(
            self.applicant, RoleEnum.PROPOSAL_MANAGER, created_by=self.applicant
        )
        self.requested = [self.copy_requested(ro) for ro in self.real_requested]
        self.copy_budgets(latest)
        self.offering_index = {}
        for index, requested_offering in enumerate(self.requested):
            self.offering_index[requested_offering.pk] = index
            models.RequestedResource.objects.create(
                proposal=self.proposal,
                requested_offering=requested_offering,
                limits=self.example_limits(requested_offering.offering),
                created_by=self.applicant,
            )
        self.action_index = None
        self.submit()

    @staticmethod
    def example_limits(offering):
        limits = {}
        for component in offering.components.all():
            if component.billing_type == BillingTypes.LIMIT or (
                component.billing_type == BillingTypes.ONE_TIME and component.is_prepaid
            ):
                limits[component.type] = max(int(component.min_value or 0), 1)
        return limits

    def submit(self):
        """Submit the proposal as the submit endpoint does."""
        proposal = self.proposal
        proposal.submitted_at = timezone.now()
        proposal.save(update_fields=["submitted_at"])
        workflow_service.create_step_instances(proposal)
        self.snapshot = self.statuses()
        self.log_entry("submitted", text="The proposal is submitted.")
        if self.call.evaluation_start != EvaluationStart.ON_SUBMISSION:
            self.log_entry(
                "cutoff",
                text="Evaluation starts once the round's cut-off has passed.",
            )
        started = workflow_service.activate_first_step(proposal)
        if started is None:
            proposal.state = ProposalStates.SUBMITTED
            proposal.save(update_fields=["state"])
        proposal.refresh_from_db()
        self.collector.mails(
            tasks.proposal_state_update_mails(
                proposal, ProposalStates.DRAFT, proposal.state
            ),
            "proposal_submitted",
        )
        self.collector.mails(tasks.new_submission_mails(proposal), "proposal_submitted")
        self.settle()

    # -- state ------------------------------------------------------------------

    def instances(self):
        return list(
            self.proposal.workflow_step_instances.select_related("call_step")
            .prefetch_related("call_step__assigns_roles")
            .order_by("order", "id")
        )

    def statuses(self):
        return {
            i.pk: (i.status, i.outcome)
            for i in self.proposal.workflow_step_instances.all()
        }

    def instance(self, key, action):
        if not key:
            raise DryRunError("Name the step the action is taken on.", action)
        # The latest: a step after the award runs again in each run.
        instance = (
            self.proposal.workflow_step_instances.select_related("call_step")
            .filter(step=key)
            .order_by("-id")
            .first()
        )
        if instance is None:
            raise DryRunError(f"The call has no step '{key}'.", action, key)
        return instance

    def active(self, key, action):
        instance = self.instance(key, action)
        if instance.status != WorkflowStepInstanceStatuses.ACTIVE:
            raise DryRunError(
                f"The step {self.label(instance)} is {instance.status}: the action "
                "needs it active.",
                action,
                key,
            )
        return instance

    @staticmethod
    def label(instance):
        return instance.call_step.name or instance.step

    def settle(self):
        """After an action: record what it changed, in the order of the
        proposal's path, then render the mail it would send."""
        before, after = self.snapshot, self.statuses()
        self.proposal.refresh_from_db()
        for instance in self.instances():
            status, outcome = after[instance.pk]
            old = before.get(instance.pk)
            if old == (status, outcome):
                continue
            ran = old is not None and old[0] == WorkflowStepInstanceStatuses.PENDING
            if status == WorkflowStepInstanceStatuses.ACTIVE or (
                ran and status != WorkflowStepInstanceStatuses.SKIPPED
            ):
                # Started -- and, decided at once, maybe ended too.
                self.days[instance.pk] = {"started": self.clock, "ended": None}
                if instance.step not in self.exercised:
                    self.exercised.append(instance.step)
                self.log_entry(
                    "started", instance, text=f"{self.label(instance)} starts."
                )
                self.log_contributions(instance)
            if status == WorkflowStepInstanceStatuses.SKIPPED:
                self.log_entry(
                    "skipped",
                    instance,
                    text=f"{self.label(instance)} is skipped: "
                    + self.skip_reason(instance),
                )
            elif status not in (
                WorkflowStepInstanceStatuses.ACTIVE,
                WorkflowStepInstanceStatuses.PENDING,
            ):
                self.days.setdefault(
                    instance.pk, {"started": self.clock, "ended": None}
                )["ended"] = self.clock
                self.log_end(instance)
        self.snapshot = after
        feeds = Feed.objects.filter(
            content_type=ContentType.objects.get_for_model(models.Proposal),
            object_id=self.proposal.pk,
        ).values("event_id")
        for event in Event.objects.filter(
            event_type=EventType.PROPOSAL_AUTOMATIC_DECISION_DEFERRED,
            created__gte=self.action_started,
            id__in=feeds,
        ).order_by("created", "id"):
            self.log_entry(
                "automatic_decision_deferred", text=event.message.replace("\n", " ")
            )
        if self.allocated:
            self.allocated = False
            self.log_entry(
                "allocation",
                text=(
                    "The award would be provisioned: a project for the applicant "
                    f"and a resource for each of {len(self.requested)} "
                    "requested offerings."
                ),
            )
        self.collector.flush()

    def log_end(self, instance):
        status = instance.status
        label = self.label(instance)
        if status == WorkflowStepInstanceStatuses.COMPLETED:
            automatic = (
                instance.is_allocation_decision
                and instance.call_step.decides_automatically
                and instance.completed_by_id is not None
                and instance.completed_by.username == "system_robot"
            )
            self.log_entry(
                "automatic_decision" if automatic else "completed",
                instance,
                text=(
                    f"{label} is decided automatically: {instance.outcome}."
                    if automatic
                    else f"{label} is completed: {instance.outcome}."
                ),
                outcome=instance.outcome,
            )
            self.tried.setdefault(instance.step, set()).add(instance.outcome)
        elif status == WorkflowStepInstanceStatuses.EXPIRED:
            self.log_entry("expired", instance, text=f"{label} lapses.")
        elif status == WorkflowStepInstanceStatuses.WITHDRAWN:
            self.log_entry(
                "withdrawn",
                instance,
                text=f"{label} is withdrawn: a step beside it ended the workflow.",
            )

    def skip_reason(self, instance):
        condition = instance.run_condition or {}
        if not condition:
            return "the step is disabled."
        if SCORE_SPREAD_ABOVE in condition:
            spread = None
            source = self.proposal.workflow_step_instances.filter(
                step=condition.get("step")
            ).first()
            if source is not None:
                spread = contributions.score_spread(source)
            found = (
                "fewer than two scores were given"
                if spread is None
                else f"they lie {_number(spread, 0)} apart"
            )
            return (
                f"it runs only when the scores of {condition.get('step')} lie "
                f"more than {_number(condition[SCORE_SPREAD_ABOVE], 0)} points "
                f"apart; {found}."
            )
        return (
            f"it runs only when {condition.get('step')} ended "
            f"{' or '.join(condition.get('outcomes') or [])}."
        )

    def log_contributions(self, instance):
        for contribution in self.slots(instance):
            self.log_entry(
                "contribution_opened",
                instance,
                text=f"Work opens for {self.slot_label(contribution)}.",
            )

    def slots(self, instance, **filters):
        """The step's contributions in a fixed order: by requested offering,
        then by who holds them. Actions name a slot by its place here."""
        return sorted(
            instance.contributions.filter(**filters).select_related("contributor"),
            key=lambda c: (
                self.offering_index.get(c.requested_offering_id, -1),
                self.slot_label(c),
                c.pk,
            ),
        )

    def slot_label(self, contribution):
        if contribution.requested_offering_id:
            index = self.offering_index.get(contribution.requested_offering_id, 0)
            return f"offering {index + 1} ({self.requested[index].offering.name})"
        if contribution.contributor_id in self.people:
            return self.people[contribution.contributor_id]
        return "a contributor"

    # -- timing -----------------------------------------------------------------

    def started_day(self, instance):
        return self.days.get(instance.pk, {}).get("started", self.clock)

    def deadline_day(self, instance):
        duration = instance.call_step.duration_in_days
        return None if not duration else self.started_day(instance) + duration

    def at_step_end(self, instance):
        """An action ending ``instance`` comes at its deadline, the latest it
        could; without one, as soon as it started. Deadline reminders due
        before then are sent first."""
        end = self.deadline_day(instance)
        when = end if end is not None else self.started_day(instance)
        self.remind(instance, when)
        self.clock = max(self.clock, when)

    def remind(self, instance, until):
        deadline = self.deadline_day(instance)
        if deadline is None:
            return
        for rule in notification_rules.enabled_rules(
            instance, NotificationRuleTriggers.DEADLINE_APPROACHING
        ):
            day = deadline - (rule.days_before or 0)
            key = (instance.pk, rule.pk)
            if key in self.reminded or day < self.started_day(instance) or day > until:
                continue
            self.reminded.add(key)
            clock, self.clock = self.clock, max(self.clock, day)
            self.collector.render(
                tasks.step_event_mails(
                    instance,
                    NotificationRuleTriggers.DEADLINE_APPROACHING,
                    [rule],
                    days_before=rule.days_before,
                ),
                NotificationRuleTriggers.DEADLINE_APPROACHING,
                instance,
            )
            self.clock = max(clock, self.clock)

    # -- applying actions ------------------------------------------------------

    def execute(self, at=None):
        self.action_started = timezone.now()
        self.action_index = None
        self.setup()
        actions = self.data.get("actions") or []
        if at is not None:
            actions = actions[:at]
        for index, action in enumerate(actions):
            if time.monotonic() > self.deadline:
                raise DryRunTimeout()
            self.action_index = index
            self.action_started = timezone.now()
            kind = action_kind(action)
            handler = ACTION_HANDLERS[kind]
            previous_state = self.proposal.state
            try:
                handler(self, action, index)
            except DryRunError as error:
                error.action = index
                raise
            except (
                ValueError,
                contributions.ContributionError,
                workflow_service.ChangeRequestError,
                step_inputs.ReleaseError,
            ) as error:
                raise DryRunError(str(error), index, action.get("step"))
            self.settle()
            self.announce_decision(kind, previous_state)
            self.collector.flush()
        self.action_index = None

    def announce_decision(self, kind, previous_state):
        """The decision mail the endpoints send once the workflow ended,
        through every path but a rejection at a step, which its own rules
        tell."""
        proposal = self.proposal
        if (
            previous_state == ProposalStates.IN_REVIEW
            and proposal.state != ProposalStates.IN_REVIEW
            and not proposal.decision_held
            and kind != "reject"
        ):
            self.collector.mails(
                tasks.proposal_state_update_mails(
                    proposal, ProposalStates.IN_REVIEW, proposal.state
                ),
                "proposal_decided",
            )
            self.collector.mails(
                tasks.reviewer_decision_mails(proposal), "proposal_decided"
            )
            self.log_entry(
                "decided",
                text=f"The proposal is {proposal.get_state_display().lower()}.",
            )

    def locked(self, instance):
        models.Proposal.objects.select_for_update().get(pk=self.proposal.pk)
        return models.ProposalWorkflowStepInstance.objects.select_related(
            "call_step"
        ).get(pk=instance.pk)

    # Work needed before a step can be completed --------------------------------

    def ensure_holders(self, instance):
        """Assign the roles an assignment step assigns, enough of each for
        every step their holders contribute to, with a lead where the role
        has one."""
        call_step = instance.call_step
        for role in call_step.assigns_roles.all():
            needed = max(role.min_holders or 0, 1)
            for step in models.CallWorkflowStep.objects.filter(
                call=self.call, is_enabled=True
            ):
                named = proposal_roles.resolve_role(self.call, step.contributor_source)
                if named is not None and named.pk == role.pk:
                    needed = max(needed, step.min_contributions or 1)
            if role.max_holders is not None:
                needed = min(needed, role.max_holders)
            needed = self.capped(needed, instance)
            held = proposal_roles.active_assignments(self.proposal, role).count()
            for number in range(held, needed):
                self.assign(role, instance, is_lead=role.allows_lead and number == 0)

    def assign(self, role, instance, is_lead=False):
        user = self.person(role.name.lower())
        assignment = models.ProposalRoleAssignment.objects.create(
            proposal=self.proposal,
            role=role,
            user=user,
            is_lead=is_lead,
            assigned_by=self.manager,
            assigned_in=instance,
            coi_confirmation_required=False,
            coi_declaration_only=True,
        )
        contributions.sync_lead_slots(self.proposal, role)
        workflow_service.settle_evaluator_slots(self.proposal, user, self.manager)
        self.log_entry(
            "evaluator_assigned",
            instance,
            text=f"{user.full_name} is assigned as {role.name}"
            + (" (lead)." if is_lead else "."),
        )
        self.collector.mails(
            tasks.proposal_role_mails(assignment, True), "evaluator_assigned", instance
        )
        return assignment

    def ensure_reviews(self, instance, outcome, scores=None):
        """The step's reviews, written and submitted: with ``scores``, one
        review per score, in order; else enough for the step's minimum, each
        scoring the proposal high enough to pass its threshold."""
        call_step = instance.call_step
        if call_step.contributor_source_kind != ContributorSources.ASSIGNED_REVIEWERS:
            if scores:
                raise ValueError(
                    f"{self.label(instance)} has no reviews to score: its "
                    "reviewers are not assigned to it."
                )
            return
        scores = list(scores or [])
        if (
            call_step.max_contributions is not None
            and len(scores) > call_step.max_contributions
        ):
            raise ValueError(
                f"{self.label(instance)} takes at most "
                f"{call_step.max_contributions} reviews; {len(scores)} scores "
                "were given."
            )
        needed = self.capped(
            max(call_step.min_contributions or 0, 1, len(scores)), instance
        )
        have = instance.contributions.filter(review__isnull=False).count()
        score = max(
            REVIEW_SCORE_MAX,
            math.ceil(call_step.min_score_threshold or 0),
        )
        criteria = list(call_step.criteria.all())
        for _ in range(have, needed):
            reviewer = self.person("reviewer")
            review = models.Review.objects.create(
                proposal=self.proposal,
                reviewer=reviewer,
                state=models.Review.States.IN_REVIEW,
            )
            contributions.attach_review(review)
            self.collector.mails(
                tasks.review_assigned_mails(review), "review_assigned", instance
            )
        for position, review in enumerate(
            models.Review.objects.filter(
                proposal=self.proposal, state=models.Review.States.IN_REVIEW
            ).order_by("id")
        ):
            review.state = models.Review.States.SUBMITTED
            review.submitted_at = timezone.now()
            review.summary_score = scores[position] if position < len(scores) else score
            # Every criterion of the step gets the review's score, which is
            # then also what the criteria give it.
            if REVIEW_SCORE_MIN <= review.summary_score <= REVIEW_SCORE_MAX:
                review.criteria_scores = {
                    contributions.criterion_key(criterion): review.summary_score
                    for criterion in criteria
                }
            review.summary_public_comment = PLACEHOLDER_TEXT
            review.save()
            contributions.attach_review(review)
            self.log_entry(
                "contribution_submitted",
                instance,
                text=f"{review.reviewer.full_name} submits a review.",
            )
        self.collector.mails(
            tasks.reviews_completed_mails(self.proposal), "reviews_completed", instance
        )

    def ensure_role_contributors(self, instance):
        """A step its evaluator role's holders contribute to, with fewer of
        them than it needs, gets them assigned by the call manager, as no
        assignment step does it; each holder opens their slot."""
        call_step = instance.call_step
        if call_step.contributor_source_kind != ContributorSources.PROPOSAL_ROLE:
            return
        role = proposal_roles.resolve_role(self.call, call_step.contributor_source)
        if role is None:
            return
        needed = max(call_step.min_contributions or 0, role.min_holders or 0, 1)
        if role.max_holders is not None:
            needed = min(needed, role.max_holders)
        needed = self.capped(needed, instance)
        held = proposal_roles.active_assignments(self.proposal, role).count()
        for number in range(held, needed):
            self.assign(
                role,
                instance,
                is_lead=role.allows_lead
                and number == 0
                and not proposal_roles.active_assignments(self.proposal, role)
                .filter(is_lead=True)
                .exists(),
            )
        for user in contributions.step_role_contributors(self.proposal, call_step):
            models.StepContribution.objects.get_or_create(
                proposal_step=instance,
                contributor=user,
                requested_offering=None,
                review=None,
                defaults={"due_at": instance.deadline},
            )

    def ensure_person_slots(self, instance):
        """People contributing through a call role -- the panel, say -- open
        their slots themselves: enough of them for the step's minimum."""
        call_step = instance.call_step
        if call_step.contributor_source_kind != ContributorSources.CALL_ROLE:
            return
        needed = self.capped(max(call_step.min_contributions or 0, 1), instance)
        have = instance.contributions.exclude(
            status__in=(StepContributionStatuses.WITHDRAWN,)
        ).count()
        _, parameter = parse_contributor_source(call_step.contributor_source)
        for _ in range(have, needed):
            user = self.person(parameter.replace("_", " "))
            models.StepContribution.objects.create(
                proposal_step=instance,
                contributor=user,
                due_at=instance.deadline,
            )

    def answer_form(self, contribution, user):
        checklist = contribution.proposal_step.call_step.checklist
        if checklist is None:
            return
        completion = contribution.ensure_checklist_completion(checklist)
        answered = set(
            checklist_models.Answer.objects.filter(completion=completion).values_list(
                "question_id", flat=True
            )
        )
        for question in checklist.questions.filter(required=True).exclude(
            id__in=answered
        ):
            checklist_models.Answer.objects.create(
                completion=completion,
                question=question,
                user=user,
                answer_data=example_answer(question),
            )

    def contribution_outcome(self, call_step, step_outcome, position):
        allowed = call_step.contribution_outcomes
        if not allowed:
            return ""
        if step_outcome in WorkflowStepOutcomes.NEGATIVE_OUTCOMES and position == 0:
            return ContributionOutcomes.REJECTED
        return ContributionOutcomes.ACCEPTED

    def submitter(self, contribution):
        delegate_id = contributions.effective_delegate_id(contribution)
        if delegate_id is not None:
            return User.objects.get(pk=delegate_id)
        if contribution.contributor_id is not None:
            return contribution.contributor
        if contribution.requested_offering_id:
            index = self.offering_index.get(contribution.requested_offering_id, 0)
            return self.offering_member(index)
        return self.manager

    def offering_member(self, index):
        user = self.offering_members.get(index)
        if user is None:
            user = self.person(f"offering {index + 1} team member", single=True)
            self.offering_members[index] = user
        return user

    def submit_contribution(self, instance, contribution, outcome, conditions=""):
        contribution = models.StepContribution.objects.select_for_update().get(
            pk=contribution.pk
        )
        user = self.submitter(contribution)
        self.answer_form(contribution, user)
        if (
            outcome == ContributionOutcomes.CONDITIONALLY_ACCEPTED
            and not (conditions or "").strip()
        ):
            conditions = "Example conditions."
        contributions.submit(contribution, user, outcome=outcome, conditions=conditions)
        if outcome:
            self.contribution_tried.setdefault(instance.step, set()).add(outcome)
        self.log_entry(
            "contribution_submitted",
            instance,
            text=f"Work of {self.slot_label(contribution)} is submitted"
            + (f": {outcome}." if outcome else "."),
            outcome=outcome or None,
        )
        return contribution

    def slot(self, instance, ref, action):
        """The contribution an action names: ``offering`` by the index of the
        requested offering, ``slot`` by its place on the step."""
        slots = self.slots(instance)
        if "offering" in ref and ref["offering"] is not None:
            index = ref["offering"]
            if index < 0 or index >= len(self.requested):
                raise DryRunError(
                    f"There is no requested offering {index}.", action, instance.step
                )
            found = [
                c for c in slots if c.requested_offering_id == self.requested[index].pk
            ]
            if not found:
                raise DryRunError(
                    f"{self.label(instance)} has no work for offering {index + 1}.",
                    action,
                    instance.step,
                )
            return found[0]
        index = ref.get("slot")
        if index is None or index < 0 or index >= len(slots):
            raise DryRunError(
                f"{self.label(instance)} has no work number {index}.",
                action,
                instance.step,
            )
        return slots[index]

    def submit_named(self, instance, items, action):
        for item in items:
            contribution = self.slot(instance, item, action)
            if contribution.status not in StepContributionStatuses.OPEN:
                raise DryRunError(
                    f"The work of {self.slot_label(contribution)} on "
                    f"{self.label(instance)} is {contribution.status}.",
                    action,
                    instance.step,
                )
            self.submit_contribution(
                instance,
                contribution,
                item.get("outcome") or "",
                item.get("conditions"),
            )

    def fill_open(self, instance, outcome):
        """Submit every open contribution of the step, with an outcome that
        adds up to ``outcome``."""
        call_step = instance.call_step
        for position, contribution in enumerate(
            self.slots(
                instance, status__in=StepContributionStatuses.OPEN, review__isnull=True
            )
        ):
            self.submit_contribution(
                instance,
                contribution,
                self.contribution_outcome(call_step, outcome, position),
            )

    def prepare_completion(self, instance, outcome, completer, scores=None):
        """What the step's people would have done before its completer
        decides: holders assigned, reviews and contributions in, its form
        answered."""
        call_step = instance.call_step
        if outcome == WorkflowStepOutcomes.DECLINED:
            return
        if call_step.kind == StepKinds.ASSIGNMENT:
            self.ensure_holders(instance)
        self.ensure_role_contributors(instance)
        self.ensure_reviews(instance, outcome, scores)
        self.ensure_person_slots(instance)
        self.fill_open(instance, outcome)
        if call_step.checklist_id and not call_step.collects_contributions:
            # The completer's own form is the step's output.
            slot, _ = models.StepContribution.objects.get_or_create(
                proposal_step=instance,
                contributor=completer,
                requested_offering=None,
                review=None,
                defaults={"due_at": instance.deadline},
            )
            self.answer_form(slot, completer)

    def run_numbers(self):
        """Each post-award run of the proposal by its number, from 1."""
        return {
            pk: number
            for number, pk in enumerate(
                self.proposal.runs.order_by("id").values_list("pk", flat=True), 1
            )
        }

    def ensure_project(self):
        """The accepted proposal's project, which the simulated allocation
        did not create: a synthetic one, so that an extension has an end
        date to move. Nothing is provisioned in it."""
        proposal = self.proposal
        if proposal.project_id is not None:
            return proposal.project
        customer = structure_models.Customer.objects.create(name="Example organisation")
        offset = max(
            runs.offset_days(proposal, WorkflowPhases.EXTENSION) or 0,
            runs.offset_days(proposal, WorkflowPhases.CLOSING) or 0,
        )
        project = structure_models.Project.objects.create(
            customer=customer,
            name="Example project",
            end_date=timezone.localdate() + timedelta(days=offset + 90),
        )
        proposal.project = project
        proposal.save(update_fields=["project"])
        self.log_entry(
            "project",
            text=(
                "The award's project stands in for the allocation, ending in "
                f"{offset + 90} days; nothing is provisioned in it."
            ),
        )
        return project

    def completer(self, instance):
        call_step = instance.call_step
        if call_step.completer == ResponsibleRoles.APPLICANT:
            return self.applicant
        if call_step.completer == ResponsibleRoles.PROPOSAL_ROLE:
            holders = list(proposal_roles.step_completers(self.proposal, call_step))
            if holders:
                return holders[0]
        return self.manager


def _number(value, default):
    if value is None:
        return default
    value = float(value)
    return int(value) if value.is_integer() else value


def example_answer(question):
    """A placeholder answer of the question's type: the highest a rating
    allows, so a score gate reads it as a pass."""
    kind = question.question_type
    if kind == QuestionTypes.BOOLEAN:
        return True
    if kind in (QuestionTypes.NUMBER, QuestionTypes.YEAR):
        return _number(question.max_value, 1)
    if kind in (QuestionTypes.RATING, QuestionTypes.LIKERT):
        return _number(question.max_value, 5)
    if kind in (QuestionTypes.SINGLE_SELECT, QuestionTypes.MULTI_SELECT):
        option = question.question_options.order_by("order", "id").first()
        value = option.uuid.hex if option is not None else ""
        return [value] if kind == QuestionTypes.MULTI_SELECT else value
    if kind == QuestionTypes.DATE:
        return timezone.localdate().isoformat()
    if kind == QuestionTypes.DATETIME:
        return timezone.now().isoformat()
    if kind in (QuestionTypes.FILE, QuestionTypes.MULTIPLE_FILES):
        return []
    if kind == QuestionTypes.EMAIL:
        return f"example@{EXAMPLE_DOMAIN}"
    if kind == QuestionTypes.URL:
        return f"https://{EXAMPLE_DOMAIN}/"
    return PLACEHOLDER_TEXT


# =============================================================================
# Actions
# =============================================================================

ACTION_KINDS = (
    "complete",
    "contributions",
    "round_step",
    "extend",
    "final_report",
    "exhaust_budget",
    "reject",
    "expire",
    "request_changes",
    "submit_changes",
    "cancel_changes",
    "advance",
    "publish",
    "release",
    "delegate",
    "reopen",
)


def action_kind(action):
    """Which action a request item is: the one flag or field it sets."""
    if action.get("round_step"):
        return "round_step"
    if action.get("outcome"):
        return "complete"
    for kind in ACTION_KINDS[2:]:
        if action.get(kind):
            return kind
    if action.get("contributions"):
        return "contributions"
    raise DryRunError("The action does not say what to do.")


def _complete(run, action, index):
    instance = run.active(action.get("step"), index)
    call_step = instance.call_step
    if call_step.round_scoped:
        raise DryRunError(
            f"{run.label(instance)} runs once per round: it is opened and "
            "completed for the round (round_step), not on one proposal.",
            index,
            instance.step,
        )
    outcome = action["outcome"]
    if outcome not in call_step.allowed_outcomes:
        raise DryRunError(
            f"'{outcome}' is not an outcome of {run.label(instance)}; use one of "
            f"{', '.join(call_step.allowed_outcomes)}.",
            index,
            instance.step,
        )
    run.at_step_end(instance)
    instance = run.locked(instance)
    completer = run.completer(instance)
    if action.get("contributions"):
        run.submit_named(instance, action["contributions"], index)
    run.prepare_completion(instance, outcome, completer, action.get("scores"))
    instance.refresh_from_db()
    if call_step.derives_outcome:
        completed, _ = workflow_service.complete_from_contributions(
            run.proposal, instance, completer
        )
        if completed:
            instance.refresh_from_db()
            if instance.outcome != outcome:
                raise DryRunError(
                    f"The contributions of {run.label(instance)} add up to "
                    f"{instance.outcome}, not {outcome}.",
                    index,
                    instance.step,
                )
            return
    workflow_service.complete_step(
        proposal=run.proposal,
        current_instance=instance,
        outcome=outcome,
        outcome_reason=action.get("reason") or "",
        completed_by=completer,
        budget_override_reason=action.get("override_budget") or "",
    )


def _round_step(run, action, index):
    """A step run once per round, as one action: opened for the synthetic
    round, a round report asked of each of its authors -- synthetic people,
    as many as the step names -- and written, and the step completed: by
    the reports for an assessment, with ``outcome`` for a decision."""
    instance = run.active(action.get("step"), index)
    call_step = instance.call_step
    if not call_step.round_scoped:
        raise DryRunError(
            f"{run.label(instance)} runs on each proposal, not once per round.",
            index,
            instance.step,
        )
    by_reports = round_steps.completes_by_reports(call_step)
    outcome = action.get("outcome") or (WorkflowStepOutcomes.DONE if by_reports else "")
    if outcome not in call_step.allowed_outcomes:
        raise DryRunError(
            f"Complete {run.label(instance)} for the round with one of "
            f"{', '.join(call_step.allowed_outcomes)}.",
            index,
            instance.step,
        )
    run.at_step_end(instance)
    instance = run.locked(instance)
    round_step, _ = models.RoundStep.objects.get_or_create(
        round=run.round, call_step=call_step
    )
    if round_step.opened_at is None:
        round_step.opened_at = timezone.now()
        round_step.opened_by = run.manager
        round_step.save(update_fields=["opened_at", "opened_by", "modified"])
    run.log_entry(
        "round_step_opened",
        instance,
        text=f"{run.label(instance)} is opened for the round's proposals together.",
    )
    reports = []
    if call_step.contributor_source_kind != ContributorSources.NONE:
        # Who writes counts, never who they are: the round's real people
        # stay out of the run.
        needed = max(len(round_reports.report_authors(instance)), 1)
        for _ in range(needed):
            author = run.person(f"{run.label(instance)} report author")
            report = models.RoundMeetingNote.objects.create(
                round=run.round,
                call_step=call_step,
                author=author,
                title=round_reports.report_title(call_step, author),
            )
            report.proposals.set([run.proposal])
            reports.append(report)
    run.collector.mails(
        tasks.round_report_mails(reports), "round_step_opened", instance
    )
    run.collector.mails(
        tasks.round_step_opened_mails(round_step), "round_step_opened", instance
    )
    for report in reports:
        report.submitted_at = timezone.now()
        report.save(update_fields=["submitted_at", "modified"])
        run.log_entry(
            "round_report_submitted",
            instance,
            text=f"{report.author.full_name} submits a round report.",
        )
    run.tried.setdefault(instance.step, set()).add(outcome)
    workflow_service.complete_step(
        proposal=run.proposal,
        current_instance=instance,
        outcome=outcome,
        outcome_reason=action.get("reason") or "",
        completed_by=reports[-1].author if reports and by_reports else run.manager,
    )


def _open_run(run, index, phase):
    """Open an extension request (by the applicant team) or the final
    report (by the call's managers) on the accepted proposal."""
    proposal = run.proposal
    if proposal.state != ProposalStates.ACCEPTED:
        raise DryRunError(
            "Extension requests and the final report come after the award.", index
        )
    project = run.ensure_project()
    requested_end_date = None
    if phase == WorkflowPhases.EXTENSION:
        requested_end_date = project.end_date + timedelta(days=90)
    workflow_service.open_run(
        proposal,
        phase,
        run.applicant if phase == WorkflowPhases.EXTENSION else run.manager,
        requested_end_date=requested_end_date,
    )
    run.proposal.refresh_from_db()
    run.log_entry(
        "run_opened",
        text=(
            "The applicant team asks to extend the allocation by 90 days."
            if phase == WorkflowPhases.EXTENSION
            else "The final report is opened."
        ),
    )


def _extend(run, action, index):
    _open_run(run, index, WorkflowPhases.EXTENSION)


def _final_report(run, action, index):
    _open_run(run, index, WorkflowPhases.CLOSING)


def _exhaust_budget(run, action, index):
    """Take the pools the synthetic proposal draws on as spent: what the
    other proposals were awarded is all they offer."""
    pools = list(budgets.pools_for_round(run.round))
    if not pools:
        raise DryRunError(
            "The call has no resource pool for the proposal's round.", index
        )
    for pool in pools:
        spent = budgets.awarded_so_far(pool, exclude_proposal=run.proposal)
        models.CallResourcePool.objects.filter(pk=pool.pk).update(offered_total=spent)
    run.log_entry(
        "budget_exhausted",
        text=f"The {len(pools)} resource pool(s) of the round are taken as spent.",
    )


def _contributions(run, action, index):
    instance = run.active(action.get("step"), index)
    instance = run.locked(instance)
    run.submit_named(instance, action["contributions"], index)
    if not instance.contributions.filter(
        status__in=StepContributionStatuses.OPEN
    ).exists():
        run.at_step_end(instance)
    workflow_service.complete_from_contributions(
        run.proposal, instance, run.completer(instance)
    )


def _reject(run, action, index):
    instance = run.active(action.get("step"), index)
    if workflow_service.open_change_request(instance) is not None:
        raise DryRunError(
            "Changes have been requested of the applicant at this step: cancel "
            "the request before rejecting.",
            index,
            instance.step,
        )
    run.at_step_end(instance)
    instance = run.locked(instance)
    workflow_service.reject_at_step(
        run.proposal,
        instance,
        action.get("reason") or "Rejected in the dry run.",
        run.completer(instance),
    )


def _expire(run, action, index):
    instance = run.active(action.get("step"), index)
    if not instance.call_step.duration_in_days:
        raise DryRunError(
            f"{run.label(instance)} has no duration: it does not lapse.",
            index,
            instance.step,
        )
    if workflow_service.open_change_request(instance) is not None:
        raise DryRunError(
            f"{run.label(instance)} waits on the applicant's changes: it does not "
            "lapse meanwhile.",
            index,
            instance.step,
        )
    run.at_step_end(instance)
    instance = run.locked(instance)
    workflow_service.expire_step(instance)


def _request_changes(run, action, index):
    instance = run.active(action.get("step"), index)
    instance = run.locked(instance)
    parts = action.get("parts") or change_requests.unlockable_parts(run.proposal)
    change_request = workflow_service.request_changes(
        run.proposal,
        instance,
        run.manager,
        action.get("message") or "Please revise the proposal.",
        parts,
    )
    run.log_entry(
        "changes_requested",
        instance,
        text=f"Changes are asked of the applicant: {', '.join(change_request.unlocked_parts)}.",
    )


def _submit_changes(run, action, index):
    models.Proposal.objects.select_for_update().get(pk=run.proposal.pk)
    closed = workflow_service.submit_changes(run.proposal, run.applicant)
    # One submission answers every step that asked.
    for change_request in closed:
        run.log_entry(
            "changes_submitted",
            change_request.step_instance,
            text="The applicant submits the changes.",
        )


def _cancel_changes(run, action, index):
    instance = run.instance(action.get("step"), index)
    change_request = workflow_service.open_change_request(instance)
    if change_request is None:
        raise DryRunError(
            f"No changes are requested at {run.label(instance)}.", index, instance.step
        )
    models.Proposal.objects.select_for_update().get(pk=run.proposal.pk)
    workflow_service.cancel_change_request(run.proposal, change_request, run.manager)
    run.log_entry(
        "changes_cancelled", instance, text="The request for changes is called off."
    )


def _advance(run, action, index):
    proposal = models.Proposal.objects.select_for_update().get(pk=run.proposal.pk)
    workflow_service.advance_step(proposal, acting_user=run.manager)
    run.log_entry("advanced", text="The call manager moves the proposal on.")


def _publish(run, action, index):
    proposal = models.Proposal.objects.select_for_update().get(pk=run.proposal.pk)
    if not proposal.decision_held:
        raise DryRunError("No decision is held for the round's publication.", index)
    workflow_service.release_held_decision(proposal)
    run.log_entry("published", text="The round publishes its results.")


def _release(run, action, index):
    instance = run.instance(action.get("step"), index)
    instance = run.locked(instance)
    step_inputs.release(instance, run.manager, action.get("message") or "")
    run.log_entry(
        "released", instance, text=f"The report of {run.label(instance)} is released."
    )


def _delegate(run, action, index):
    instance = run.active(action.get("step"), index)
    instance = run.locked(instance)
    contribution = run.slot(instance, action["delegate"], index)
    contribution = models.StepContribution.objects.select_for_update().get(
        pk=contribution.pk
    )
    target, parameter = contributions.delegation_target(instance.call_step)
    delegate = run.person("delegate")
    if contribution.requested_offering_id:
        # Anyone with a role on the slot's own offering may take it over.
        offering = contribution.requested_offering.offering
        offering.add_user(delegate, RoleEnum.OFFERING_MANAGER)
    elif parameter:
        role = proposal_roles.resolve_role(
            run.call, f"{ContributorSources.PROPOSAL_ROLE}:{parameter}"
        )
        if role is not None:
            models.ProposalRoleAssignment.objects.create(
                proposal=run.proposal,
                role=role,
                user=delegate,
                assigned_by=run.manager,
                coi_confirmation_required=False,
                coi_declaration_only=True,
            )
    handed_by = run.submitter(contribution)
    contributions.delegate(contribution, handed_by, delegate)
    run.collector.mails(
        tasks.contribution_delegated_mails(
            contribution, tasks.DelegationEvents.DELEGATED, delegate, handed_by
        ),
        "contribution_delegated",
        instance,
    )
    contributions.accept_delegation(contribution, delegate, confirms_no_conflict=True)
    run.collector.mails(
        tasks.contribution_delegated_mails(
            contribution, tasks.DelegationEvents.ACCEPTED, handed_by, delegate
        ),
        "contribution_delegated",
        instance,
    )
    run.log_entry(
        "delegated",
        instance,
        text=f"The work of {run.slot_label(contribution)} is handed to "
        f"{delegate.full_name}, who takes it over.",
    )


def _reopen(run, action, index):
    instance = run.instance(action.get("step"), index)
    instance = run.locked(instance)
    contribution = run.slot(instance, action["reopen"], index)
    contribution = models.StepContribution.objects.select_for_update().get(
        pk=contribution.pk
    )
    workflow_service.reopen_contribution(
        run.proposal,
        instance,
        contribution,
        run.manager,
        action.get("message") or "Please revise your contribution.",
    )
    run.log_entry(
        "contribution_reopened",
        instance,
        text=f"The work of {run.slot_label(contribution)} is reopened.",
    )


ACTION_HANDLERS = {
    "complete": _complete,
    "contributions": _contributions,
    "reject": _reject,
    "expire": _expire,
    "request_changes": _request_changes,
    "submit_changes": _submit_changes,
    "cancel_changes": _cancel_changes,
    "advance": _advance,
    "publish": _publish,
    "release": _release,
    "delegate": _delegate,
    "reopen": _reopen,
    "round_step": _round_step,
    "extend": _extend,
    "final_report": _final_report,
    "exhaust_budget": _exhaust_budget,
}


def _round_state(run, instance):
    """A step run once per round: whether it was opened for the round and
    its round reports."""
    if not instance.call_step.round_scoped:
        return {}
    round_step = round_reports.round_step_of(run.round, instance.call_step)
    reports = round_reports.reports_of(run.round, instance.call_step)
    return {
        "round_step": {
            "opened": bool(round_step and round_step.opened_at),
            "reports": reports.count(),
            "reports_submitted": reports.filter(submitted_at__isnull=False).count(),
        }
    }


def _amount(value):
    return None if value is None else budgets.json_number(value)


def _budget_state(run, instance):
    """At the allocation decision: each budget the award draws on, what
    remains of it and whether the award fits."""
    if not instance.is_allocation_decision:
        return {}
    entries = budgets.proposal_budget(run.proposal)
    if not entries:
        return {}
    return {
        "budget": [
            {
                "offering": run.offering_index.get(
                    entry["pool"].offering_component.requested_offering_id
                ),
                "component_type": entry["component_type"],
                "scope": "call" if entry["round"] is None else "round",
                "offered_total": _amount(entry["offered_total"]),
                "awarded_so_far": _amount(entry["awarded_so_far"]),
                "remaining": _amount(entry["remaining"]),
                "award": _amount(entry["award"]),
                "fits": entry["fits"],
            }
            for entry in entries
        ]
    }


# Functions ``(run, instance) -> dict`` adding fields of later features to a
# step's state.
STEP_STATE_EXTENSIONS: list = [_round_state, _budget_state]


# =============================================================================
# Describing the outcome
# =============================================================================


def _step_state(run, instance):
    call_step = instance.call_step
    days = run.days.get(instance.pk, {})
    started = days.get("started")
    duration = call_step.duration_in_days
    contribution_rows = []
    for position, contribution in enumerate(run.slots(instance)):
        contribution_rows.append(
            {
                "slot": position,
                "offering": run.offering_index.get(contribution.requested_offering_id),
                "label": run.slot_label(contribution),
                "status": contribution.status,
                "outcome": contribution.outcome or None,
                "delegated": contribution.delegate_id is not None,
            }
        )
    condition = instance.run_condition or {}
    skipped_reason = None
    if instance.status == WorkflowStepInstanceStatuses.SKIPPED:
        skipped_reason = "condition" if condition else "disabled"
    state = {
        "key": instance.step,
        "name": run.label(instance),
        "order": instance.order,
        "stage": instance.stage_key,
        "phase": call_step.phase,
        "run": run.run_numbers().get(instance.run_id),
        "round_scoped": call_step.round_scoped,
        "status": instance.status,
        "outcome": instance.outcome or None,
        "skipped_reason": skipped_reason,
        "condition": condition or None,
        "started_day": started,
        "deadline_day": started + duration
        if started is not None and duration
        else None,
        "ended_day": days.get("ended"),
        "is_allocation_decision": instance.is_allocation_decision,
        "decides_automatically": bool(call_step.decides_automatically),
        "automatic": instance.is_allocation_decision
        and call_step.decides_automatically
        and instance.status == WorkflowStepInstanceStatuses.COMPLETED
        and instance.completed_by_id is not None
        and instance.completed_by.username == "system_robot",
        "released": instance.released_at is not None,
        "changes_requested": workflow_service.open_change_request(instance) is not None,
        "outcomes": list(call_step.allowed_outcomes),
        "contribution_outcomes": list(call_step.contribution_outcomes),
        "contributions": contribution_rows,
        "extra": {},
    }
    for extension in STEP_STATE_EXTENSIONS:
        state["extra"].update(extension(run, instance) or {})
    return state


def _next_actions(run, states):
    proposal = run.proposal
    nexts = []
    in_review = proposal.state == ProposalStates.IN_REVIEW
    pending_request = change_requests.open_applicant_requests(proposal).exists()
    open_run = runs.open_run_of(proposal)
    for instance in run.instances():
        call_step = instance.call_step
        # In review, or at a step of the accepted proposal's open run.
        running = in_review or (open_run is not None and instance.run_id == open_run.pk)
        if instance.status == WorkflowStepInstanceStatuses.ACTIVE and running:
            waits = workflow_service.open_change_request(instance) is not None
            open_slots = [
                row
                for row in states[instance.pk]["contributions"]
                if row["status"] in StepContributionStatuses.OPEN
            ]
            submitted = [
                row
                for row in states[instance.pk]["contributions"]
                if row["status"] == StepContributionStatuses.SUBMITTED
            ]
            target, _ = contributions.delegation_target(call_step)
            nexts.append(
                {
                    "step": instance.step,
                    "name": run.label(instance),
                    "outcomes": [] if waits else list(call_step.allowed_outcomes),
                    "contribution_outcomes": list(call_step.contribution_outcomes),
                    "open_slots": [row["slot"] for row in open_slots],
                    "can_reject": not waits,
                    "can_expire": bool(call_step.duration_in_days) and not waits,
                    "can_request_changes": bool(call_step.allows_change_requests)
                    and not instance.is_allocation_decision
                    and not waits,
                    "can_cancel_changes": waits,
                    "can_delegate": target != DelegationTargets.NONE
                    and bool(open_slots)
                    and bool(call_step.delegable_to),
                    "can_reopen": [row["slot"] for row in submitted]
                    if call_step.collects_contributions
                    and call_step.kind != StepKinds.APPLICANT_ACTION
                    else [],
                    "can_release": False,
                    "round_step": call_step.round_scoped,
                }
            )
        elif (
            instance.status == WorkflowStepInstanceStatuses.COMPLETED
            and call_step.release_to_applicant
            and call_step.kind == StepKinds.ASSESSMENT
            and instance.released_at is None
        ):
            nexts.append(
                {
                    "step": instance.step,
                    "name": run.label(instance),
                    "outcomes": [],
                    "contribution_outcomes": [],
                    "open_slots": [],
                    "can_reject": False,
                    "can_expire": False,
                    "can_request_changes": False,
                    "can_cancel_changes": False,
                    "can_delegate": False,
                    "can_reopen": [],
                    "can_release": True,
                    "round_step": False,
                }
            )
    return {
        "steps": nexts,
        "can_submit_changes": pending_request,
        "can_advance": in_review
        and workflow_service.is_awaiting_manual_advance(proposal),
        "can_publish": bool(proposal.decision_held),
        "can_extend": _may_open(proposal, open_run, WorkflowPhases.EXTENSION),
        "can_open_final_report": _may_open(proposal, open_run, WorkflowPhases.CLOSING),
        "can_exhaust_budget": in_review and budgets.pools_for_round(run.round).exists(),
    }


def _may_open(proposal, open_run, phase):
    """Whether a run of ``phase`` may be opened on the accepted proposal; its
    project, which the allocation would create, is the run's to stand in."""
    return (
        proposal.state == ProposalStates.ACCEPTED
        and open_run is None
        and proposal.completed_at is None
        and models.CallWorkflowStep.phase_steps(proposal.round.call_id, phase).exists()
    )


def _coverage(run, states):
    enabled = [s for s in states.values() if s["skipped_reason"] != "disabled"]
    return {
        "steps": list(dict.fromkeys(s["key"] for s in enabled)),
        "exercised": list(run.exercised),
        "outcomes": {
            s["key"]: {
                "available": s["outcomes"],
                "tried": sorted(run.tried.get(s["key"], ())),
            }
            for s in enabled
        },
        "contribution_outcomes": {
            s["key"]: {
                "available": s["contribution_outcomes"],
                "tried": sorted(run.contribution_tried.get(s["key"], ())),
            }
            for s in enabled
            if s["contribution_outcomes"]
        },
    }


def _result(run):
    instances = run.instances()
    # By instance: a step after the award runs again in every run.
    states = {instance.pk: _step_state(run, instance) for instance in instances}
    return {
        "proposal_state": run.proposal.state,
        "decision_held": bool(run.proposal.decision_held),
        "day": run.clock,
        "requested_offerings": [
            {
                "index": index,
                "uuid": requested_offering.uuid.hex,
                "name": requested_offering.offering.name,
            }
            for index, requested_offering in enumerate(run.real_requested)
        ],
        "steps": [states[instance.pk] for instance in instances],
        "next": _next_actions(run, states),
        "log": run.log,
        "messages": run.messages,
        "coverage": _coverage(run, states),
        "view_as": None,
    }


# =============================================================================
# Viewing as a role
# =============================================================================

VIEW_AS_KINDS = (
    "applicant",
    "reviewer",
    "offering_team",
    "proposal_role",
    "proposal_role_lead",
    "panel_member",
    "panel_chair",
    "call_manager",
)


def viewable_actors(description):
    """The actor keys of the call's description a preview may be taken as:
    every actor but the system, which reads nothing."""
    return [
        actor["key"]
        for actor in description["actors"]
        if actor["kind"] in VIEW_AS_KINDS
    ]


def _viewer(run, view_as):
    """A synthetic user holding ``view_as``'s role on the synthetic proposal:
    the applicant, or a person given the role here. Never a real user."""
    key = view_as["role"]
    actors = {a["key"]: a for a in run.description["actors"]}
    actor = actors.get(key)
    if actor is None or key not in viewable_actors(run.description):
        raise DryRunError(f"'{key}' is not a role of the call's workflow.")
    kind = actor["kind"]
    proposal = run.proposal
    call = run.call
    if kind == "applicant":
        return run.applicant, "the applicant"
    # On a real proposal its people are real: the preview is taken as a
    # synthetic person given their role, never as one of them.
    synthetic_only = getattr(run, "synthetic_viewers", False)
    if kind == "reviewer":
        review = (
            None
            if synthetic_only
            else models.Review.objects.filter(proposal=proposal)
            .exclude(state=models.Review.States.REJECTED)
            .select_related("reviewer")
            .order_by("id")
            .first()
        )
        if review is not None:
            return review.reviewer, "an assigned reviewer"
        reviewer = run.person("reviewer")
        models.Review.objects.create(
            proposal=proposal, reviewer=reviewer, state=models.Review.States.IN_REVIEW
        )
        return reviewer, "an assigned reviewer"
    if kind == "offering_team":
        if not run.requested:
            raise DryRunError("The proposal requests no offering to view as its team.")
        index = view_as.get("offering") or 0
        if index < 0 or index >= len(run.requested):
            raise DryRunError(f"There is no requested offering {index}.")
        user = run.person(f"offering {index + 1} manager", single=True)
        run.requested[index].offering.add_user(user, RoleEnum.OFFERING_MANAGER)
        return user, f"a manager of offering {index + 1}"
    if kind in ("proposal_role", "proposal_role_lead"):
        lead = kind == "proposal_role_lead"
        role = models.CallProposalRole.objects.filter(
            call=call, key=actor["role_key"]
        ).first()
        if role is None:
            raise DryRunError(f"'{key}' is not a role of the call's workflow.")
        held = proposal_roles.active_assignments(proposal, role).select_related("user")
        held = held.filter(is_lead=True) if lead else held.order_by("-is_lead")
        assignment = (
            None
            if synthetic_only
            else held.order_by("id").first()
            if not lead
            else held.first()
        )
        if assignment is None:
            if proposal.state in (ProposalStates.DRAFT,):
                raise DryRunError(
                    "Evaluators are assigned once the proposal is submitted."
                )
            user = run.person(role.name.lower())
            if lead:
                proposal_roles.active_assignments(proposal, role).update(is_lead=False)
            assignment = models.ProposalRoleAssignment.objects.create(
                proposal=proposal,
                role=role,
                user=user,
                is_lead=lead,
                assigned_by=run.manager,
                coi_confirmation_required=False,
                coi_declaration_only=True,
            )
        if role.eligible_from in ProposalRoleEligibility.PANEL_BASED:
            _seat_on_panel(run, assignment.user, role)
        return assignment.user, (f"the lead {role.name}" if lead else f"a {role.name}")
    if kind == "panel_member":
        user = run.person("panel member")
        call.add_user(user, RoleEnum.CALL_PANEL_MEMBER)
        return user, "a panel member"
    if kind == "panel_chair":
        user = run.person("panel chair", single=True)
        call.add_user(user, RoleEnum.CALL_PANEL_MEMBER)
        models.Call.objects.filter(pk=call.pk).update(panel_chair=user)
        return user, "the panel chair"
    if kind == "call_manager":
        call.add_user(run.manager, RoleEnum.CALL_MANAGER)
        return run.manager, "a call manager"
    raise DryRunError(f"'{key}' is not a role a preview can be taken as.")


def _seat_on_panel(run, user, role):
    """A role held through the proposal's domain panel: the viewer sits on a
    synthetic panel of the call -- its chair for a chair's role -- which the
    proposal is routed to. The call's real panels and their people stay out."""
    proposal = run.proposal
    panel = proposal.panel
    if panel is None or not panel.key.startswith("dry-run-"):
        panel = models.CallPanel.objects.create(
            call=run.call,
            key=f"dry-run-{uuid.uuid4().hex[:12]}",
            name="Example panel",
        )
        models.Proposal.objects.filter(pk=proposal.pk).update(panel=panel)
        proposal.panel = panel
    run.call.add_user(user, RoleEnum.CALL_PANEL_MEMBER)
    panel.members.add(user)
    if role.eligible_from == ProposalRoleEligibility.PANEL_CHAIR:
        panel.chair = user
        panel.save(update_fields=["chair"])


def _read(path, user, origin, query=None):
    """``GET path`` through the real URL routing, view, permissions and
    serializers, as ``user``. Returns ``{status, data}``."""
    request = HttpRequest()
    request.method = "GET"
    request.path = request.path_info = path
    request.META = {
        "SERVER_NAME": origin.get("SERVER_NAME", "localhost"),
        "SERVER_PORT": origin.get("SERVER_PORT", "80"),
        "HTTP_HOST": origin.get("HTTP_HOST") or origin.get("SERVER_NAME", "localhost"),
        "wsgi.url_scheme": origin.get("wsgi.url_scheme", "http"),
        "REQUEST_METHOD": "GET",
        "QUERY_STRING": "",
    }
    request.GET = QueryDict(mutable=True)
    for name, value in (query or {}).items():
        request.GET[name] = value
    request.user = user
    request._force_auth_user = user
    match = resolve(path)
    response = match.func(request, *match.args, **match.kwargs)
    # As the client would read it: rendered by the API's own renderer.
    response.render()
    data = json.loads(response.content) if response.content else None
    if response.status_code >= 400:
        detail = data.get("detail") if isinstance(data, dict) else None
        return {"status": response.status_code, "data": {"detail": str(detail or "")}}
    return {"status": response.status_code, "data": data}


# The statuses of a step whose inputs the proposal page shows.
_STARTED = (
    WorkflowStepInstanceStatuses.ACTIVE,
    WorkflowStepInstanceStatuses.COMPLETED,
    WorkflowStepInstanceStatuses.EXPIRED,
)


def _step_reads(run, uuid_hex):
    """The reads the proposal page makes per step, for the latest run of
    each step, as the page decides them from the workflow states:

    - ``step_inputs``: what earlier steps produced, on a started step that
      reads them or that the applicant responds through;
    - ``step_answers``: the contributions with their form answers, on a step
      with a form that has begun;
    - ``step_checklist``: the applicant's own form on a response step.

    Fixed by the step's configuration and status: nothing in the request
    chooses what is read."""
    latest = {}
    for instance in run.instances():
        latest[instance.call_step.uuid.hex] = instance
    reads = {}
    for step_uuid, instance in latest.items():
        call_step = instance.call_step
        responds = (
            call_step.kind == StepKinds.APPLICANT_ACTION
            and call_step.outcome_set != OutcomeSets.ACCEPTANCE
        )
        started = instance.status in _STARTED
        query = {"step_uuid": step_uuid}
        if started and (responds or instance.input_steps.exists()):
            reads[f"step_inputs:{step_uuid}"] = (
                reverse("proposal-proposal-step-inputs", kwargs={"uuid": uuid_hex}),
                query,
            )
        if not call_step.checklist_id:
            continue
        if instance.status not in (
            WorkflowStepInstanceStatuses.PENDING,
            WorkflowStepInstanceStatuses.SKIPPED,
        ):
            reads[f"step_answers:{step_uuid}"] = (
                reverse(
                    "proposal-proposal-step-checklist-responses",
                    kwargs={"uuid": uuid_hex},
                ),
                query,
            )
        if started and responds:
            reads[f"step_checklist:{step_uuid}"] = (
                reverse("proposal-proposal-step-checklist", kwargs={"uuid": uuid_hex}),
                {**query, "include_all": "true"},
            )
    return reads


def _page_reads(run, user, uuid_hex):
    """The rest of what the proposal page reads, for a preview of the whole
    page: the call's settings it lays the page out by, the compliance
    answers, the team, and the evaluator roles -- the call's, read by those
    who assign them, and the viewer's own."""
    call_hex = run.call.uuid.hex
    return {
        "call": (
            reverse("proposal-public-call-detail", kwargs={"uuid": call_hex}),
            None,
        ),
        "compliance": (
            reverse("proposal-proposal-checklist", kwargs={"uuid": uuid_hex}),
            {"include_all": "true"},
        ),
        "team": (
            reverse("proposal-proposal-list-users", kwargs={"uuid": uuid_hex}),
            {"page_size": 200},
        ),
        "call_proposal_roles": (
            reverse(
                "proposal-protected-call-proposal-roles", kwargs={"uuid": call_hex}
            ),
            None,
        ),
        "call_workflow_steps": (
            reverse(
                "proposal-protected-call-workflow-steps", kwargs={"uuid": call_hex}
            ),
            None,
        ),
        "my_reviewer_suggestions": (
            reverse(
                "proposal-proposal-my-reviewer-suggestions", kwargs={"uuid": uuid_hex}
            ),
            None,
        ),
        "suggested_reviewers": (
            reverse("proposal-proposal-suggested-reviewers", kwargs={"uuid": uuid_hex}),
            None,
        ),
        "my_role_assignments": (
            reverse("proposal-role-assignment-list"),
            {
                "proposal_uuid": uuid_hex,
                "user_uuid": user.uuid.hex,
                "is_active": "true",
            },
        ),
    }


def _view_as(run, view_as, origin):
    user, as_what = _viewer(run, view_as)
    proposal = run.proposal
    uuid_hex = proposal.uuid.hex
    reads = {
        # Who the viewer is, with the roles they hold, as the portal reads it.
        "me": (reverse("user-me"), None),
        "proposal": (
            reverse("proposal-proposal-detail", kwargs={"uuid": uuid_hex}),
            None,
        ),
        "workflow_states": (
            reverse("proposal-proposal-workflow-states", kwargs={"uuid": uuid_hex}),
            None,
        ),
        "contributions": (
            reverse("proposal-proposal-contributions", kwargs={"uuid": uuid_hex}),
            None,
        ),
        "offering_assessments": (
            reverse(
                "proposal-proposal-offering-assessments", kwargs={"uuid": uuid_hex}
            ),
            None,
        ),
        "change_requests": (
            reverse("proposal-proposal-change-requests", kwargs={"uuid": uuid_hex}),
            None,
        ),
        "released_reports": (
            reverse("proposal-proposal-released-reports", kwargs={"uuid": uuid_hex}),
            None,
        ),
        # What the proposal requests: the offering teams' contribution
        # cards show what was asked of their offering.
        "resources": (
            reverse("proposal-proposal-resources", kwargs={"uuid": uuid_hex}),
            {"page_size": 200},
        ),
        "role_assignments": (
            reverse("proposal-proposal-role-assignments", kwargs={"uuid": uuid_hex}),
            None,
        ),
        "reviews": (reverse("proposal-review-list"), {"proposal_uuid": uuid_hex}),
        "events": (
            reverse("event-list"),
            {
                "scope": reverse("proposal-proposal-detail", kwargs={"uuid": uuid_hex}),
                # Every event, for the page's Activity card to page through.
                "page_size": 200,
            },
        ),
    }
    reads.update(_step_reads(run, uuid_hex))
    if getattr(run, "reads_page", False):
        reads.update(_page_reads(run, user, uuid_hex))
    run.viewer = user
    run.reads = reads
    responses = {}
    with _acting_as(user):
        for key, (path, query) in reads.items():
            if key == "events":
                query = {**query, "scope": _absolute(origin, query["scope"])}
            responses[key] = _read(path, user, origin, query)
    me = responses["me"].get("data")
    if isinstance(me, dict):
        # The portal hands a signed-in user their API token; the viewer is
        # nobody to sign in as, and the preview carries none.
        for name in [name for name in me if name.startswith("token")]:
            me.pop(name)
    return {
        "role": view_as["role"],
        "offering": view_as.get("offering"),
        "at": view_as.get("at"),
        "viewer": {
            "uuid": user.uuid.hex,
            "username": user.username,
            "full_name": user.full_name,
            "is_staff": False,
            "is_support": False,
            "permissions": [],
        },
        "described_as": as_what,
        "proposal_uuid": uuid_hex,
        "responses": responses,
    }


def _absolute(origin, path):
    scheme = origin.get("wsgi.url_scheme", "http")
    host = origin.get("HTTP_HOST") or origin.get("SERVER_NAME", "localhost")
    return f"{scheme}://{host}{path}"


# =============================================================================
# Entry point
# =============================================================================


def _lock(call):
    """Bound the run's statements, its waits and the transaction itself, and
    take the call's dry-run lock: busy means another run of the call is in
    progress.

    The wall-clock budget is the run's own; the database bounds back it up
    should the run stop checking it: a statement, an idle spell inside the
    transaction, and -- where the server knows the setting -- the whole
    transaction, at twice the budget."""
    if connection.vendor != "postgresql":
        return
    budget_ms = max(TIMEOUT_SECONDS, 1) * 1000
    with connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL statement_timeout = {budget_ms}")
        cursor.execute(f"SET LOCAL lock_timeout = {LOCK_TIMEOUT_MS}")
        cursor.execute(f"SET LOCAL idle_in_transaction_session_timeout = {budget_ms}")
        if connection.pg_version >= TRANSACTION_TIMEOUT_SINCE:
            cursor.execute(f"SET LOCAL transaction_timeout = {2 * budget_ms}")
        cursor.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)",
            [ADVISORY_LOCK_NAMESPACE, call.pk],
        )
        acquired = cursor.fetchone()[0]
    if not acquired:
        raise DryRunBusy()


def run(call, data, origin=None):
    """Replay ``data``'s path on a synthetic proposal of ``call`` and return
    what happened. Nothing survives the call: the transaction is always
    rolled back.

    ``data`` is the validated request: ``requested_offerings`` (requested
    offering uuids, all the call's accepted ones when absent), ``actions``
    and ``view_as``. ``origin`` is the request's META, for the
    links the read endpoints build in a preview.
    """
    deadline = time.monotonic() + TIMEOUT_SECONDS
    result = {}
    cached_roles = set(Role.objects._cache)
    try:
        with transaction.atomic():
            _lock(call)
            _, result = replay(call, data, origin or {}, deadline)
            raise _Rollback()
    except _Rollback:
        pass
    except DATABASE_STOPS as error:
        if any(reason in str(error) for reason in TIMEOUT_REASONS):
            raise DryRunTimeout() from error
        raise
    finally:
        forget_rolled_back_roles(cached_roles)
    return result


def with_sample_proposal(call, build):
    """``build(run)`` called on the synthetic proposal of ``call`` just
    submitted, as a run with no action leaves it, and its result returned.
    Rolled back, armed and locked as a run is: nothing ``build`` does
    survives, and no side effect is reached."""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    result = None
    cached_roles = set(Role.objects._cache)
    try:
        with transaction.atomic():
            _lock(call)
            the_run = _Run(call, {"actions": []}, deadline)
            with (
                core_middleware.skip_side_effects(),
                dry_run_context.collecting(the_run.collector),
                tripwires(),
                _acting_as_nobody(),
            ):
                the_run.execute()
                result = build(the_run)
            raise _Rollback()
    except _Rollback:
        pass
    except DATABASE_STOPS as error:
        if any(reason in str(error) for reason in TIMEOUT_REASONS):
            raise DryRunTimeout() from error
        raise
    finally:
        forget_rolled_back_roles(cached_roles)
    return result


def _requested_offerings(proposal):
    """The call's requests of the offerings ``proposal`` asks for, in a
    stable order: an offering's team is named by its place in it."""
    ids = models.RequestedResource.objects.filter(proposal=proposal).values(
        "requested_offering_id"
    )
    return list(
        models.RequestedOffering.objects.filter(id__in=ids)
        .select_related("offering")
        .order_by("id")[:MAX_OFFERINGS]
    )


class _ProposalPreview(_Run):
    """The proposal page of a real proposal as a role of its call sees it,
    as it stands: nothing is replayed. Its viewer is a synthetic person
    given the role on the real proposal, call or offering inside the run's
    transaction, which is rolled back."""

    synthetic_viewers = True
    # The whole page is previewed, not only the workflow's parts of it.
    reads_page = True

    def __init__(self, proposal, deadline):
        call = proposal.round.call
        super().__init__(call, {}, deadline)
        self.call = call
        self.proposal = proposal
        self.manager = self.person("call manager", single=True)
        self.applicant = self.person("applicant", single=True)
        proposal.add_user(
            self.applicant, RoleEnum.PROPOSAL_MANAGER, created_by=self.manager
        )
        self.requested = _requested_offerings(proposal)


def preview_roles(proposal) -> list[dict]:
    """The roles of the proposal's call it may be previewed as, each as
    ``{role, offering, label}``: one per requested offering for an
    offering's team."""
    description = workflow_description.describe_call(proposal.round.call)
    viewable = set(viewable_actors(description))
    requested = _requested_offerings(proposal)
    roles = []
    for actor in description["actors"]:
        if actor["key"] not in viewable:
            continue
        if actor["kind"] == "offering_team":
            for index, requested_offering in enumerate(requested):
                roles.append(
                    {
                        "role": actor["key"],
                        "offering": index,
                        "label": f"{requested_offering.offering.name} team",
                    }
                )
            continue
        roles.append({"role": actor["key"], "offering": None, "label": actor["label"]})
    return roles


def preview(proposal, view_as, origin=None):
    """The proposal page of ``proposal`` as ``view_as`` (``{role,
    offering}``) sees it now: the real read endpoints, answering a
    synthetic person given the role. Rolled back, armed and locked as a run
    is: nothing survives and no side effect is reached."""
    call = proposal.round.call
    deadline = time.monotonic() + TIMEOUT_SECONDS
    result = None
    cached_roles = set(Role.objects._cache)
    try:
        with transaction.atomic():
            _lock(call)
            with (
                core_middleware.skip_side_effects(),
                tripwires(),
                _acting_as_nobody(),
            ):
                the_run = _ProposalPreview(proposal, deadline)
                with dry_run_context.collecting(the_run.collector):
                    result = _view_as(the_run, view_as, origin or {})
            raise _Rollback()
    except _Rollback:
        pass
    except DATABASE_STOPS as error:
        if any(reason in str(error) for reason in TIMEOUT_REASONS):
            raise DryRunTimeout() from error
        raise
    finally:
        forget_rolled_back_roles(cached_roles)
    return result


def forget_rolled_back_roles(before):
    """A system role first created inside a rolled-back run is gone with
    it: the process must not keep it cached (``Role.objects`` caches the
    system roles by name)."""
    cache = Role.objects._cache
    for name in set(cache) - before:
        if not Role.objects.filter(pk=cache[name].pk).exists():
            cache.pop(name, None)


def replay(call, data, origin, deadline):
    """Replay ``data``'s path with every side effect switched off, and
    return the run and what it says. Commits nothing by itself: ``run``
    calls it inside the transaction it rolls back; a test may call it to
    keep what it made and read it through the API."""
    view_as = data.get("view_as")
    at = view_as.get("at") if view_as else None
    the_run = _Run(call, data, deadline)
    with (
        core_middleware.skip_side_effects(),
        dry_run_context.collecting(the_run.collector),
        tripwires(),
        _acting_as_nobody(),
    ):
        the_run.execute(at)
        result = _result(the_run)
        if view_as:
            result["view_as"] = _view_as(the_run, view_as, origin)
    return the_run, result


@contextlib.contextmanager
def _acting_as_nobody():
    """Nobody real acts in a dry run: until the synthetic call manager
    exists, events name nobody, and the request's own user and address are
    put back once the run is over."""
    previous_user = core_middleware.get_current_user()
    previous_context = logging_middleware.get_event_context()
    core_middleware.set_current_user(None)
    logging_middleware.set_event_context({})
    try:
        yield
    finally:
        core_middleware.set_current_user(previous_user)
        if previous_context is None:
            logging_middleware.reset_event_context()
        else:
            logging_middleware.set_event_context(previous_context)
