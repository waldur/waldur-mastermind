"""A call's own wording of its mail, and the preview of the mail a call sends.

The wording is rendered in a sandbox with the masked context of the
recipient's mail; a preview renders on a synthetic proposal and sends
nothing.
"""

from unittest import mock

import pytest
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from django.utils.safestring import mark_safe
from rest_framework import status, test

from waldur_core.core.models import Notification, NotificationTemplate
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    call_transfer,
    models,
    notification_overrides,
    tasks,
    workflow_description,
    workflow_versions,
)
from waldur_mastermind.proposal.enums import (
    CallStates,
    NotificationRuleRecipients,
    NotificationRuleTriggers,
)
from waldur_mastermind.proposal.tests import factories, fixtures
from waldur_mastermind.proposal.tests.test_step_event_mail_visibility import REASON
from waldur_mastermind.proposal.tests.test_step_inputs import StepInputsTestMixin
from waldur_mastermind.proposal.tests.test_workflow_dry_run import (
    DryRunMixin,
    row_counts,
)

APPLICANT = notification_overrides.STEP_EVENT_APPLICANT
CALL_TEAM = notification_overrides.STEP_EVENT_CALL_TEAM


def render(definition, body, context=None, html=""):
    return notification_overrides.sandbox_render(
        definition, "Subject", body, html, context or {}
    )


class SandboxTest(test.APITestCase):
    def assertRefused(self, body, field="body_text", html=""):
        with self.assertRaises(notification_overrides.OverrideError) as caught:
            render(CALL_TEAM, body, html=html)
        self.assertIn(field, caught.exception.errors)

    def test_tags_reaching_beyond_the_template_are_refused(self):
        for source in (
            "{% load static %}",
            "{% load i18n %}{% trans 'x' %}",
            '{% include "proposal/workflow_step_event_message.txt" %}',
            '{% extends "proposal/workflow_step_event_message.html" %}',
            '{% ssi "/etc/passwd" %}',
            "{% debug %}",
            '{% url "admin:index" %}',
            "{% csrf_token %}",
            "{% autoescape off %}{{ proposal_name }}{% endautoescape %}",
            "{% lorem 100000 w %}",
            "{% widthratio 1 1 100000000 %}",
        ):
            with self.subTest(source=source):
                self.assertRefused(source)

    def test_private_and_dunder_attributes_are_refused(self):
        for source in (
            "{{ proposal_name.__class__ }}",
            "{{ proposal_name.__class__.__mro__ }}",
            "{{ counts._meta }}",
            "{{ _private }}",
            "{% for x in unlocked_parts.__iter__ %}{% endfor %}",
        ):
            with self.subTest(source=source):
                self.assertRefused(source)

    def test_filters_unmarking_or_dumping_are_refused(self):
        for source in (
            "{{ proposal_name|safe }}",
            "{{ unlocked_parts|safeseq }}",
            "{{ counts|pprint }}",
            '{{ proposal_name|ljust:"100000000" }}',
            '{{ proposal_name|stringformat:"r" }}',
            "{{ counts|json_script }}",
        ):
            with self.subTest(source=source):
                self.assertRefused(source)

    def test_settings_and_undocumented_context_are_unreachable(self):
        context = {
            "proposal_name": "P",
            "settings": {"SECRET_KEY": "s3cr3t"},
            "request": object(),
            "user": structure_factories.UserFactory(),
        }
        _, text, _ = render(
            CALL_TEAM,
            "{{ settings.SECRET_KEY }}|{{ request }}|{{ user.email }}|"
            "{{ proposal_name }}",
            context,
        )
        self.assertEqual(text, "|||P")

    def test_objects_in_the_context_reach_the_template_as_text_only(self):
        user = structure_factories.UserFactory()
        _, text, _ = render(
            CALL_TEAM,
            "{{ proposal_name.email }}{{ proposal_name.password }}",
            {"proposal_name": user},
        )
        self.assertEqual(text, "")

    def test_the_html_body_is_autoescaped(self):
        hostile = '<script>alert("x")</script>'
        _, text, html = render(
            CALL_TEAM,
            "{{ proposal_name }}",
            {"proposal_name": hostile},
            html="<p>{{ proposal_name }}</p>",
        )
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        # The text body is plain text, as the shipped templates render it.
        self.assertEqual(text, hostile)

    def test_a_value_marked_safe_is_escaped_all_the_same(self):
        _, _, html = render(
            CALL_TEAM,
            "x",
            {"proposal_name": mark_safe("<img src=x onerror=alert(1)>")},
            html="{{ proposal_name }}",
        )
        self.assertNotIn("<img", html)

    def test_without_an_html_body_the_text_is_escaped_into_one(self):
        _, _, html = render(
            CALL_TEAM, "{{ proposal_name }}", {"proposal_name": "<b>x</b>"}
        )
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)

    def test_loops_are_nested_two_deep_at_most(self):
        self.assertRefused(
            "{% for a in unlocked_parts %}{% for b in unlocked_parts %}"
            "{% for c in unlocked_parts %}{% endfor %}{% endfor %}{% endfor %}"
        )
        render(
            CALL_TEAM,
            "{% for a in unlocked_parts %}{% for b in unlocked_parts %}"
            "{{ a }}{{ b }}{% endfor %}{% endfor %}",
            {"unlocked_parts": ["x", "y"]},
        )

    def test_sources_are_bounded_in_size(self):
        with self.assertRaises(notification_overrides.OverrideError) as caught:
            notification_overrides.sandbox_render(
                CALL_TEAM,
                "s" * (notification_overrides.MAX_SUBJECT + 1),
                "t" * (notification_overrides.MAX_BODY_TEXT + 1),
                "h" * (notification_overrides.MAX_BODY_HTML + 1),
                {},
            )
        self.assertEqual(
            set(caught.exception.errors), {"subject", "body_text", "body_html"}
        )

    def test_the_applicant_wording_never_reads_the_outcome(self):
        """Even handed a context carrying it, the applicant's wording sees
        only its documented variables, which hold no outcome."""
        _, text, _ = render(
            APPLICANT,
            "[{{ outcome }}][{{ outcome_reason }}][{{ counts.submitted }}]",
            {"outcome": "rejected", "outcome_reason": REASON, "counts": {"a": 1}},
        )
        self.assertEqual(text, "[][][]")


class OverrideApiMixin:
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.manager = self.fixture.call_manager
        self.staff = structure_factories.UserFactory(is_staff=True)

    def url(self, call=None, action="notification_overrides"):
        return factories.CallFactory.get_protected_url(call or self.call, action)

    def detail_url(self, override, call=None):
        return f"{self.url(call)}{override.uuid.hex}/"

    def post(self, data, user=None, call=None):
        self.client.force_authenticate(user or self.manager)
        return self.client.post(self.url(call), data, format="json")

    def wording(self, **extra):
        return {
            "mail_type": "workflow_step_event",
            "variant": "applicant",
            "subject": "News on {{ proposal_name }}",
            "body_text": "Dear applicant, {{ step_name }} is {{ trigger }}.",
            "body_html": "<p>{{ step_name }}</p>",
            **extra,
        }


class OverrideApiTest(OverrideApiMixin, test.APITestCase):
    def test_a_call_manager_words_a_mail_and_rewords_it(self):
        response = self.post(self.wording())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["key"], "workflow_step_event:applicant")
        response = self.post(self.wording(subject="Again {{ proposal_name }}"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        override = models.CallNotificationOverride.objects.get(call=self.call)
        self.assertEqual(override.subject, "Again {{ proposal_name }}")
        self.assertEqual(override.updated_by, self.manager)

    def test_errors_are_given_by_field(self):
        response = self.post(self.wording(subject="{% load static %}"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("subject", response.data)
        response = self.post(self.wording(body_html="{{ x.__class__ }}"))
        self.assertIn("body_html", response.data)
        response = self.post(self.wording(mail_type="password_reset"))
        self.assertIn("mail_type", response.data)
        response = self.post(
            self.wording(body_text="x" * (notification_overrides.MAX_BODY_TEXT + 1))
        )
        self.assertIn("body_text", response.data)
        self.assertFalse(models.CallNotificationOverride.objects.exists())

    def test_edit_and_reset(self):
        override_uuid = self.post(self.wording()).data["uuid"]
        override = models.CallNotificationOverride.objects.get(uuid=override_uuid)
        self.client.force_authenticate(self.manager)
        response = self.client.patch(
            self.detail_url(override), {"enabled": False}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        override.refresh_from_db()
        self.assertFalse(override.enabled)
        response = self.client.patch(
            self.detail_url(override), {"subject": "{% debug %}"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.delete(self.detail_url(override))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.CallNotificationOverride.objects.exists())

    def test_another_calls_manager_neither_reads_nor_writes(self):
        override_uuid = self.post(self.wording()).data["uuid"]
        override = models.CallNotificationOverride.objects.get(uuid=override_uuid)
        other = fixtures.ProposalFixture()
        stranger = other.call_manager
        self.client.force_authenticate(stranger)
        for method, url in (
            ("get", self.url()),
            ("get", self.url(action="notification_mail_types")),
            ("get", self.detail_url(override)),
            ("patch", self.detail_url(override)),
            ("delete", self.detail_url(override)),
            ("post", self.url()),
        ):
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(
                    url, self.wording(), format="json"
                )
                self.assertIn(
                    response.status_code,
                    (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                )
        # Nor through their own call's URL.
        response = self.client.get(self.detail_url(override, call=other.call))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response = self.client.delete(self.detail_url(override, call=other.call))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(models.CallNotificationOverride.objects.exists())
        override.refresh_from_db()
        self.assertEqual(override.call, self.call)

    def test_reviewers_panel_and_applicants_do_not_read_it(self):
        self.post(self.wording())
        for user in (
            self.fixture.panel_member,
            self.fixture.proposal_creator,
            structure_factories.UserFactory(),
        ):
            with self.subTest(user=user):
                self.client.force_authenticate(user)
                self.assertIn(
                    self.client.get(self.url()).status_code,
                    (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                )

    def test_an_archived_call_is_read_only(self):
        models.Call.objects.filter(pk=self.call.pk).update(state=CallStates.ARCHIVED)
        response = self.post(self.wording())
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_mail_types_list_the_variables_and_the_portal_wording(self):
        self.post(self.wording())
        self.client.force_authenticate(self.manager)
        response = self.client.get(self.url(action="notification_mail_types"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        by_key = {row["key"]: row for row in response.data}
        applicant = by_key["workflow_step_event:applicant"]
        names = {v["name"] for v in applicant["variables"]}
        self.assertIn("proposal_name", names)
        self.assertNotIn("outcome_reason", names)
        self.assertIn(
            "outcome_reason",
            {v["name"] for v in by_key["workflow_step_event:call_team"]["variables"]},
        )
        self.assertIn("{{ step_name }}", applicant["portal_wording"]["subject"])
        self.assertIsNotNone(applicant["override"])
        self.assertIsNone(by_key["review_assigned"]["override"])
        self.assertFalse(applicant["portal_overridden"])

    def test_a_staff_wording_of_the_portal_shows(self):
        NotificationTemplate.objects.create(
            name="subject",
            path="proposal/review_assigned_subject.txt",
            content="Portal wording",
        )
        self.client.force_authenticate(self.manager)
        response = self.client.get(self.url(action="notification_mail_types"))
        row = {r["key"]: r for r in response.data}["review_assigned"]
        self.assertTrue(row["portal_overridden"])

    def test_a_write_records_a_workflow_version_of_wording(self):
        workflow_versions.ensure_baseline(self.call)
        with self.captureOnCommitCallbacks(execute=True):
            self.post(self.wording())
        versions = list(workflow_versions.owner_versions(self.call).order_by("number"))
        self.assertEqual(len(versions), 2)
        changes = versions[-1].summary
        self.assertEqual(changes["cosmetic"], 1)
        self.assertEqual(changes["behaviour"], 0)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class SendingTest(StepInputsTestMixin, test.APITestCase):
    """The mail as sent: the call's wording, over the masked context of
    each recipient's mail."""

    def setUp(self):
        super().setUp()
        Notification.objects.update_or_create(
            key="proposal.workflow_step_event", defaults={"enabled": True}
        )
        self.reach_consolidated_step()
        self.step_instance = self.instance(self.consolidated_step)
        self.step_instance.outcome = "done"
        self.step_instance.outcome_reason = REASON
        self.step_instance.feedback_withheld = True
        self.step_instance.completed_at = timezone.now()
        self.step_instance.save()
        self.other_reviewer = structure_factories.UserFactory()
        factories.ReviewFactory(proposal=self.proposal, reviewer=self.other_reviewer)
        models.CallWorkflowStepNotificationRule.objects.filter(
            workflow_step__call=self.call
        ).delete()
        for recipient in (
            NotificationRuleRecipients.CALL_MANAGERS,
            NotificationRuleRecipients.ASSIGNED_REVIEWERS,
        ):
            models.CallWorkflowStepNotificationRule.objects.create(
                workflow_step=self.consolidated_step,
                trigger=NotificationRuleTriggers.STEP_COMPLETED,
                recipient=recipient,
            )

    def word(self, **fields):
        return models.CallNotificationOverride.objects.create(
            call=self.call,
            mail_type="workflow_step_event",
            variant="call_team",
            subject=fields.get("subject", "Wording of {{ proposal_name }}"),
            body_text=fields.get("body_text", "Reason: [{{ outcome_reason }}]"),
            body_html=fields.get("body_html", ""),
            enabled=fields.get("enabled", True),
        )

    def send(self):
        mail.outbox = []
        tasks.notify_workflow_step_event(
            self.step_instance.uuid.hex, NotificationRuleTriggers.STEP_COMPLETED
        )

    def to(self, user):
        [message] = [m for m in mail.outbox if user.email in m.to]
        return message

    def test_the_calls_wording_is_sent_and_masked_as_the_portals(self):
        self.word()
        self.send()
        to_manager = self.to(self.manager)
        self.assertEqual(to_manager.subject, f"Wording of {self.proposal.name}")
        self.assertIn(f"Reason: [{REASON}]", to_manager.body)
        # The reviewer holding nothing of the step reads no reason, in the
        # call's wording as in the portal's.
        self.assertIn("Reason: []", self.to(self.other_reviewer).body)

    def test_a_disabled_wording_is_not_sent(self):
        self.word(enabled=False)
        self.send()
        self.assertIn("has been completed", self.to(self.manager).body)

    def test_a_wording_failing_to_render_falls_back_to_the_portals(self):
        self.word(body_text="{% load static %}")
        self.send()
        self.assertIn("has been completed", self.to(self.manager).body)

    def test_another_calls_wording_is_not_used(self):
        other = fixtures.ProposalFixture().call
        models.CallNotificationOverride.objects.create(
            call=other,
            mail_type="workflow_step_event",
            variant="call_team",
            subject="Other call",
            body_text="Other call",
        )
        self.send()
        self.assertNotIn("Other call", self.to(self.manager).subject)

    def test_the_notification_switch_still_decides(self):
        self.word()
        Notification.objects.filter(key="proposal.workflow_step_event").update(
            enabled=False
        )
        self.send()
        self.assertEqual(mail.outbox, [])


class TransferTest(OverrideApiMixin, test.APITestCase):
    def test_export_import_and_description_carry_the_wording(self):
        self.post(self.wording())
        document, _ = call_transfer.export_call(self.call)
        self.assertEqual(
            document["notification_overrides"][0]["subject"],
            "News on {{ proposal_name }}",
        )
        call, _, warnings = call_transfer.import_call(
            document, self.call.manager, self.staff
        )
        self.assertEqual(warnings, [])
        copy = call.notification_overrides.get()
        self.assertEqual(copy.variant, "applicant")

        description = workflow_description.describe_call(call)
        self.assertEqual(
            description["notifications"]["customised"],
            ["workflow_step_event:applicant"],
        )
        rules = [
            rule for step in description["steps"] for rule in step["notifications"]
        ]
        self.assertTrue(rules)
        for rule in rules:
            self.assertEqual(rule["customised"], rule["variant"] == "applicant")
        change_request = [
            m
            for m in description["notifications"]["always_sent"]
            if m["event"] == "change_request"
        ][0]
        self.assertTrue(change_request["customised"])

    def test_a_call_without_wording_exports_as_before(self):
        document, _ = call_transfer.export_call(self.call)
        self.assertNotIn("notification_overrides", document)

    def test_an_import_leaves_out_wording_it_cannot_render(self):
        document, _ = call_transfer.export_call(self.call)
        document["notification_overrides"] = [
            {
                "mail_type": "workflow_step_event",
                "variant": "applicant",
                "subject": "{% load static %}",
                "body_text": "x",
            },
            {"mail_type": "no_such_mail", "subject": "s", "body_text": "x"},
            {"mail_type": "review_assigned", "subject": "s", "body_text": "x"},
        ]
        call, _, warnings = call_transfer.import_call(
            document, self.call.manager, self.staff
        )
        self.assertEqual(len(warnings), 2)
        self.assertEqual(
            list(call.notification_overrides.values_list("mail_type", flat=True)),
            ["review_assigned"],
        )


class PreviewTest(DryRunMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.call = self.import_template("regular_access")
        self.manager = structure_factories.UserFactory(
            first_name="Realname", last_name="Callmanager"
        )
        self.call.add_user(self.manager, CallRole.MANAGER)
        steps = list(self.call.workflow_steps.order_by("order"))
        self.internal = next(s for s in steps if models.is_internal_step(s))
        self.facing = next(s for s in steps if not models.is_internal_step(s))

    def preview(self, user=None, **params):
        self.client.force_authenticate(user or self.manager)
        return self.client.get(
            factories.CallFactory.get_protected_url(self.call, "notification_preview"),
            params,
        )

    def draft(self, user=None, **data):
        self.client.force_authenticate(user or self.manager)
        return self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, "notification_preview_draft"
            ),
            data,
            format="json",
        )

    def test_a_cell_renders_with_the_real_templates(self):
        response = self.preview(
            step=self.facing.key,
            trigger=NotificationRuleTriggers.STEP_STARTED,
            recipient=NotificationRuleRecipients.APPLICANT,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["key"], "workflow_step_event:applicant")
        self.assertEqual(response.data["source"], "default")
        self.assertIn(self.facing.name, response.data["subject"])
        self.assertIn("Dear applicant", response.data["text"])
        self.assertIn("<html>", response.data["html"])

    def test_the_applicant_is_never_mailed_about_an_internal_step(self):
        response = self.preview(
            step=self.internal.key,
            trigger=NotificationRuleTriggers.STEP_STARTED,
            recipient=NotificationRuleRecipients.APPLICANT,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("recipient", response.data)

    def test_the_call_team_reads_the_example_outcome_the_applicant_does_not(self):
        team = self.preview(
            step=self.facing.key,
            trigger=NotificationRuleTriggers.STEP_COMPLETED,
            recipient=NotificationRuleRecipients.CALL_MANAGERS,
        ).data
        self.assertIn("Example reason", team["text"])
        applicant = self.preview(
            step=self.facing.key,
            trigger=NotificationRuleTriggers.STEP_COMPLETED,
            recipient=NotificationRuleRecipients.APPLICANT,
        ).data
        self.assertNotIn("Example reason", applicant["text"])

    def test_every_mail_type_previews(self):
        for definition in notification_overrides.MAIL_TYPES:
            with self.subTest(key=definition.key):
                response = self.preview(
                    mail_type=definition.mail_type, variant=definition.variant
                )
                self.assertEqual(
                    response.status_code, status.HTTP_200_OK, response.data
                )
                self.assertTrue(response.data["subject"])

    def test_the_calls_wording_shows_and_a_draft_previews(self):
        models.CallNotificationOverride.objects.create(
            call=self.call,
            mail_type="review_assigned",
            subject="Call wording {{ proposal_name }}",
            body_text="Body",
        )
        data = self.preview(mail_type="review_assigned").data
        self.assertEqual(data["source"], "call")
        self.assertEqual(data["subject"], "Call wording Example proposal")
        response = self.draft(
            mail_type="review_assigned",
            subject="Draft {{ call_name }}",
            body_text="<{{ proposal_name }}>",
            body_html="<p>{{ proposal_name }}</p>",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["subject"], f"Draft {self.call.name}")
        response = self.draft(
            mail_type="review_assigned", subject="{% load x %}", body_text="b"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("subject", response.data)

    @pytest.mark.nightly
    def test_a_preview_sends_and_keeps_nothing_and_names_nobody_real(self):
        Notification.objects.update_or_create(
            key="proposal.workflow_step_event", defaults={"enabled": True}
        )
        before = row_counts()
        mail.outbox = []
        with mock.patch("celery.app.task.Task.apply_async") as apply_async:
            with self.captureOnCommitCallbacks() as callbacks:
                responses = [
                    self.preview(
                        step=self.facing.key,
                        trigger=trigger,
                        recipient=NotificationRuleRecipients.CALL_MANAGERS,
                    )
                    for trigger, _ in NotificationRuleTriggers.CHOICES
                ] + [
                    self.preview(mail_type=d.mail_type, variant=d.variant)
                    for d in notification_overrides.MAIL_TYPES
                ]
        for response in responses:
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            rendered = "".join(response.data[f] for f in ("subject", "text", "html"))
            for real in (self.manager, *self.offering_managers):
                self.assertNotIn(real.last_name, rendered)
                self.assertNotIn(real.email, rendered)
        self.assertEqual(row_counts(), before)
        self.assertFalse(
            models.CallNotificationOverride.objects.exclude(call=self.call).exists()
        )
        self.assertEqual(mail.outbox, [])
        self.assertEqual(callbacks, [])
        apply_async.assert_not_called()

    def test_the_description_audience_previews_others_do_not(self):
        support = structure_factories.UserFactory(is_support=True)
        self.assertEqual(
            self.preview(user=support, mail_type="review_assigned").status_code,
            status.HTTP_200_OK,
        )
        other_manager = fixtures.ProposalFixture().call_manager
        for user in (other_manager, structure_factories.UserFactory()):
            with self.subTest(user=user):
                self.assertIn(
                    self.preview(user=user, mail_type="review_assigned").status_code,
                    (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                )
        # A draft is for those who may update the call.
        self.assertIn(
            self.draft(
                user=support, mail_type="review_assigned", subject="s", body_text="b"
            ).status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
