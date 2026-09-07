from constance.test.unittest import override_config
from django.test import TestCase
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support.backend import build_backend_id
from waldur_mastermind.support.backend.basic import BasicBackend
from waldur_mastermind.support.backend.email_backend import EmailSupportBackend
from waldur_mastermind.support.tests import factories

CUSTOM_PREFIX = override_config(WALDUR_SUPPORT_ISSUE_KEY_PREFIX="ACME")


def blank_issue(**kwargs):
    """An issue as the API leaves it before the backend assigns the key."""
    return factories.IssueFactory(backend_id="", key="", status="", **kwargs)


class BuildBackendIdTest(TestCase):
    def test_default_prefix_is_wld(self):
        issue = blank_issue()

        self.assertEqual(
            build_backend_id(issue.uuid), f"WLD-{issue.uuid.hex[:8].upper()}"
        )

    def test_marker_sits_between_prefix_and_suffix(self):
        issue = blank_issue()

        self.assertEqual(
            build_backend_id(issue.uuid, "C"), f"WLD-C-{issue.uuid.hex[:8].upper()}"
        )

    @CUSTOM_PREFIX
    def test_configured_prefix_is_used(self):
        issue = blank_issue()

        self.assertEqual(
            build_backend_id(issue.uuid), f"ACME-{issue.uuid.hex[:8].upper()}"
        )
        self.assertEqual(
            build_backend_id(issue.uuid, "EA"), f"ACME-EA-{issue.uuid.hex[:8].upper()}"
        )

    @override_config(WALDUR_SUPPORT_ISSUE_KEY_PREFIX="")
    def test_blank_setting_falls_back_to_the_default(self):
        issue = blank_issue()

        self.assertEqual(
            build_backend_id(issue.uuid), f"WLD-{issue.uuid.hex[:8].upper()}"
        )

    def test_a_value_that_evaded_validation_is_normalised(self):
        # Written straight into the database, or stored before the setting was
        # validated on write. A space or a newline here would otherwise land in
        # the mail subject and make every support notification raise.
        issue = blank_issue()
        for stored, expected in (
            ("  ", "WLD"),
            (" acme ", "ACME"),
            ("wld", "WLD"),
        ):
            with override_config(WALDUR_SUPPORT_ISSUE_KEY_PREFIX=stored):
                built = build_backend_id(issue.uuid)
                self.assertTrue(built.startswith(expected + "-"), (stored, built))


class BasicBackendKeyTest(TestCase):
    def setUp(self):
        self.backend = BasicBackend()

    def test_default_prefix(self):
        issue = blank_issue()
        self.backend.create_issue(issue)

        self.assertTrue(issue.key.startswith("WLD-"), issue.key)
        self.assertEqual(issue.key, issue.backend_id)

    @CUSTOM_PREFIX
    def test_issue_comment_and_attachment_take_the_configured_prefix(self):
        issue = blank_issue()
        self.backend.create_issue(issue)
        comment = factories.CommentFactory(issue=issue, backend_id="")
        self.backend.create_comment(comment)
        attachment = factories.AttachmentFactory(issue=issue, backend_id="")
        self.backend.create_attachment(attachment)

        self.assertTrue(issue.key.startswith("ACME-"), issue.key)
        self.assertTrue(comment.backend_id.startswith("ACME-C-"), comment.backend_id)
        self.assertTrue(
            attachment.backend_id.startswith("ACME-A-"), attachment.backend_id
        )

    @CUSTOM_PREFIX
    def test_keys_of_existing_tickets_are_not_rewritten(self):
        issue = blank_issue()
        with override_config(WALDUR_SUPPORT_ISSUE_KEY_PREFIX="WLD"):
            self.backend.create_issue(issue)
        original_key = issue.key

        self.backend.update_issue(issue)
        issue.refresh_from_db()

        self.assertEqual(issue.key, original_key)
        self.assertTrue(issue.key.startswith("WLD-"), issue.key)


class EmailBackendKeyTest(TestCase):
    def setUp(self):
        self.backend = EmailSupportBackend()

    @CUSTOM_PREFIX
    def test_email_backend_keeps_its_own_markers(self):
        issue = blank_issue()
        self.backend.create_issue(issue)
        comment = factories.CommentFactory(issue=issue, backend_id="")
        self.backend.create_comment(comment)
        attachment = factories.AttachmentFactory(issue=issue, backend_id="")
        self.backend.create_attachment(attachment)

        self.assertTrue(issue.key.startswith("ACME-E-"), issue.key)
        self.assertTrue(comment.backend_id.startswith("ACME-EC-"), comment.backend_id)
        self.assertTrue(
            attachment.backend_id.startswith("ACME-EA-"), attachment.backend_id
        )


class IssueKeyPrefixAdminFormTest(TestCase):
    """The Django admin builds its form from CONSTANCE_ADDITIONAL_FIELDS and
    never goes through the settings serializer, so the shape has to be enforced
    there as well. A newline is the case that hurts: it reaches the mail subject
    through the ticket key, and `send_mail` then raises a header error that the
    support tasks do not catch."""

    def submit(self, value):
        from constance.forms import ConstanceForm

        form = ConstanceForm(
            initial={}, data={"WALDUR_SUPPORT_ISSUE_KEY_PREFIX": value}
        )
        form.is_valid()
        return form.errors.get("WALDUR_SUPPORT_ISSUE_KEY_PREFIX")

    def test_well_formed_prefixes_are_accepted(self):
        for value in ("WLD", "ACME", "HELLO"):
            self.assertIsNone(self.submit(value), value)

    def test_malformed_prefixes_are_rejected(self):
        for value in ("wld", "AC", "TOOLONG", "AC-1", "AC ME", "A\nB", ""):
            self.assertIsNotNone(self.submit(value), value)


class IssueKeyPrefixSettingTest(test.APITestCase):
    """The setting is operator input, so the API has to police its shape."""

    def setUp(self):
        self.url = "/api/override-settings/"
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))

    def submit(self, value):
        return self.client.post(self.url, {"WALDUR_SUPPORT_ISSUE_KEY_PREFIX": value})

    def test_three_to_five_capital_letters_are_accepted(self):
        for value in ("WLD", "ACME", "HELLO"):
            response = self.submit(value)
            self.assertEqual(
                response.status_code, status.HTTP_200_OK, (value, response.data)
            )

    def test_malformed_prefixes_are_rejected(self):
        for value in ("wld", "AC", "TOOLONG", "AC-1", "AC ME", ""):
            response = self.submit(value)
            self.assertEqual(
                response.status_code,
                status.HTTP_400_BAD_REQUEST,
                (value, response.data),
            )
