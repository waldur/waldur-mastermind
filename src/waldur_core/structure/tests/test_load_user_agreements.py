import os
import tempfile
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from waldur_core.structure.models import UserAgreement


class LoadUserAgreementsTest(TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def write(self, name, content):
        path = os.path.join(self.tmpdir.name, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def call(self, *args):
        stdout, stderr = StringIO(), StringIO()
        call_command("load_user_agreements", *args, stdout=stdout, stderr=stderr)
        return stdout.getvalue(), stderr.getvalue()

    def test_missing_file_is_reported_and_other_file_still_loads(self):
        missing = os.path.join(self.tmpdir.name, "terms-of-service.html")
        pp = self.write("privacy-policy.html", "<p>Privacy</p>")

        _, stderr = self.call("--tos", missing, "--pp", pp)

        self.assertIn(missing, stderr)
        self.assertFalse(
            UserAgreement.objects.filter(
                agreement_type=UserAgreement.UserAgreements.TOS
            ).exists()
        )
        self.assertEqual(
            UserAgreement.objects.get(
                agreement_type=UserAgreement.UserAgreements.PP
            ).content,
            "<p>Privacy</p>",
        )

    def test_existing_agreement_is_kept_without_force(self):
        tos = self.write("tos.html", "first")
        self.call("--tos", tos)
        tos = self.write("tos.html", "second")

        self.call("--tos", tos)

        self.assertEqual(
            UserAgreement.objects.get(
                agreement_type=UserAgreement.UserAgreements.TOS
            ).content,
            "first",
        )

    def test_force_replaces_existing_agreement(self):
        tos = self.write("tos.html", "first")
        self.call("--tos", tos)
        tos = self.write("tos.html", "second")

        self.call("--tos", tos, "--force")

        self.assertEqual(
            UserAgreement.objects.get(
                agreement_type=UserAgreement.UserAgreements.TOS
            ).content,
            "second",
        )
