"""The mail a call would send, rendered for those who configure it.

A preview is built on the synthetic proposal of a dry run of the call
(``dry_run.with_sample_proposal``): a copy of the call -- its steps, rules
and own wording of its mail -- a synthetic applicant and stand-ins for every
holder of a call role, all inside a transaction rolled back once the mail is
rendered, with the dry run's tripwires armed. Nothing is sent or kept, and
no real person's name reaches the mail.

The mail's context is the one its builder in ``tasks.py`` makes for the
audience asked about -- the applicant's carries no outcome, as theirs never
does -- rendered with the call's own wording, the portal's, or the shipped
template, as the mail would be sent; or with a draft of the call's wording
while it is being edited.
"""

from datetime import timedelta

from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_mastermind.proposal import (
    dry_run,
    models,
    notification_overrides,
    notification_rules,
    tasks,
)
from waldur_mastermind.proposal.enums import (
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    ProposalStates,
    ResponsibleRoles,
)


class PreviewError(ValueError):
    """A preview the call cannot give: ``errors`` by field."""

    def __init__(self, errors):
        super().__init__(errors)
        self.errors = errors


EXAMPLE_REASON = "Example reason given with the outcome."
EXAMPLE_MESSAGE = "Example message of the request."


def resolve(call, params):
    """``(mail_type_def, step, trigger, recipient)`` the request names: a
    notification rule's cell -- a step, a trigger and a recipient -- or a
    mail type and its variant."""
    step_key = params.get("step")
    if step_key:
        call_step = models.CallWorkflowStep.objects.filter(
            call=call, key=step_key
        ).first()
        if call_step is None:
            raise PreviewError({"step": ["The call has no such step."]})
        trigger = params.get("trigger") or NotificationRuleTriggers.STEP_STARTED
        recipient = params.get("recipient") or NotificationRuleRecipients.CALL_MANAGERS
        is_applicant = notification_rules.is_applicant_recipient(recipient, call_step)
        if is_applicant and notification_rules.is_internal_step(call_step):
            raise PreviewError(
                {"recipient": ["The applicant is never mailed about an internal step."]}
            )
        definition = notification_overrides.get_type(
            "workflow_step_event",
            notification_overrides.step_event_variant(is_applicant),
        )
        return definition, call_step, trigger, recipient
    mail_type = params.get("mail_type")
    definition = notification_overrides.get_type(mail_type, params.get("variant"))
    if definition is None:
        raise PreviewError({"mail_type": ["Not a mail a call sends."]})
    return definition, None, None, None


def _step_instance(run, step_key):
    """The synthetic proposal's instance of the step; one made for the
    preview where the proposal does not reach it on submission (a step
    after the award)."""
    instance = (
        run.proposal.workflow_step_instances.select_related("call_step")
        .filter(step=step_key)
        .order_by("-id")
        .first()
    )
    if instance is not None:
        if instance.deadline is None:
            # Not started yet on the example proposal: due as it would be.
            instance.deadline = timezone.now() + timedelta(
                days=instance.call_step.duration_in_days or 14
            )
        return instance
    call_step = run.call.workflow_steps.get(key=step_key)
    return models.ProposalWorkflowStepInstance(
        proposal=run.proposal,
        call_step=call_step,
        step=step_key,
        deadline=timezone.now() + timedelta(days=call_step.duration_in_days or 14),
    )


def _example_outcome(call_step, trigger):
    if trigger == NotificationRuleTriggers.STEP_REJECTED:
        return "rejected"
    if trigger != NotificationRuleTriggers.STEP_COMPLETED:
        return None
    outcomes = call_step.allowed_outcomes
    return outcomes[0] if outcomes else "done"


def _step_event_mail(run, call_step, trigger, recipient, days_before):
    instance = _step_instance(run, call_step.key)
    copy_step = instance.call_step
    is_applicant = notification_rules.is_applicant_recipient(recipient, copy_step)
    outcome = _example_outcome(copy_step, trigger)
    if trigger == NotificationRuleTriggers.DEADLINE_APPROACHING and days_before is None:
        days_before = 3
    evaluators = recipient in NotificationRuleRecipients.NAMING_PROPOSAL_ROLE or (
        recipient == NotificationRuleRecipients.RESPONSIBLE_ROLE
        and copy_step.completer == ResponsibleRoles.PROPOSAL_ROLE
    )
    context = tasks._step_event_context(
        instance,
        trigger,
        is_applicant,
        days_before=days_before
        if trigger == NotificationRuleTriggers.DEADLINE_APPROACHING
        else None,
        evaluators=evaluators,
        outcome=outcome,
        outcome_reason=EXAMPLE_REASON if outcome else "",
        offering_teams=recipient == NotificationRuleRecipients.OFFERING_TEAMS,
    )
    if trigger in (
        NotificationRuleTriggers.CHANGES_REQUESTED,
        NotificationRuleTriggers.CONTRIBUTION_REOPENED,
    ):
        context["message"] = EXAMPLE_MESSAGE
    if trigger == NotificationRuleTriggers.CHANGES_REQUESTED:
        context["unlocked_parts"] = ["description"]
        context["due_at"] = timezone.now() + timedelta(days=14)
    if trigger == NotificationRuleTriggers.CHANGES_SUBMITTED and not is_applicant:
        context["submitted_requests"] = [
            {"step_name": context["step_name"], "unlocked_parts": ["description"]}
        ]
    if trigger == NotificationRuleTriggers.CONTRIBUTION_SUBMITTED and not is_applicant:
        context["counts"] = {"submitted": 1, "expected": 2}
    return tasks.Mail(
        "workflow_step_event",
        context,
        [],
        audience="applicant" if is_applicant else recipient,
        call=run.call,
    )


def _mail_of_type(run, definition):
    if definition.mail_type == "workflow_step_event":
        instance = (
            run.proposal.workflow_step_instances.select_related("call_step")
            .order_by("order", "id")
            .first()
        )
        if instance is None:
            raise PreviewError({"step": ["The call has no step to word a mail of."]})
        trigger = NotificationRuleTriggers.STEP_STARTED
        recipient = (
            NotificationRuleRecipients.APPLICANT
            if definition.variant == "applicant"
            else NotificationRuleRecipients.CALL_MANAGERS
        )
        if definition.variant == "applicant":
            # An internal step is never told to the applicant: the
            # changes requested of them are.
            trigger = NotificationRuleTriggers.CHANGES_REQUESTED
        return _step_event_mail(run, instance.call_step, trigger, recipient, None)
    if definition.mail_type == "proposal_state_changed":
        return tasks.proposal_state_update_mails(
            run.proposal, ProposalStates.IN_REVIEW, ProposalStates.ACCEPTED
        )[0]
    if definition.mail_type == "new_proposal_submitted":
        return tasks.new_submission_mails(run.proposal)[0]
    # The other mails are about a review, a role, a report: their example
    # context, named after the synthetic proposal and the call.
    context = notification_overrides.example_context(definition)
    proposal = run.proposal
    known = {
        "proposal_name": proposal.name,
        "call_name": run.call.name,
        "round_name": proposal.round.name,
        "proposal_url": core_utils.format_homeport_link(
            "proposals/{proposal_uuid}/", proposal_uuid=proposal.uuid.hex
        ),
    }
    context.update({k: v for k, v in known.items() if k in context})
    return tasks.Mail(
        definition.mail_type, context, [], audience=definition.audience, call=run.call
    )


def preview(call, params, draft=None):
    """The mail ``params`` names, as it would be sent on ``call``; with
    ``draft`` -- ``subject``, ``body_text``, ``body_html`` -- in that
    wording instead."""
    definition, call_step, trigger, recipient = resolve(call, params)
    if draft is not None:
        try:
            notification_overrides.validate(
                definition,
                draft.get("subject"),
                draft.get("body_text"),
                draft.get("body_html"),
            )
        except notification_overrides.OverrideError as error:
            raise PreviewError(error.errors)

    def build(run):
        if call_step is not None:
            mail = _step_event_mail(
                run, call_step, trigger, recipient, params.get("days_before")
            )
        else:
            mail = _mail_of_type(run, definition)
        with dry_run._templates_cached():
            if draft is not None:
                subject, text, html = notification_overrides.sandbox_render(
                    definition,
                    draft.get("subject"),
                    draft.get("body_text"),
                    draft.get("body_html"),
                    mail.context,
                )
                source = notification_overrides.SOURCE_CALL
            else:
                subject, text, html, source = notification_overrides.render(mail)
            enabled = core_utils.notification_enabled(mail.app, mail.event_type)
        return {
            "key": definition.key,
            "mail_type": definition.mail_type,
            "variant": definition.variant,
            "label": definition.label,
            "audience": definition.audience,
            "notification": f"{mail.app}.{mail.event_type}",
            "enabled": bool(enabled),
            "source": source,
            "step": call_step.key if call_step is not None else None,
            "trigger": trigger,
            "recipient": recipient,
            "subject": subject,
            "text": text,
            "html": html,
        }

    try:
        return dry_run.with_sample_proposal(call, build)
    except dry_run.DryRunError as error:
        raise PreviewError({"detail": [error.message]})
