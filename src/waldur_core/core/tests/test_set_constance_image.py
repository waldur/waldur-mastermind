import os
import shutil
import tempfile
from io import StringIO

from constance import config
from constance.models import Constance
from django.core.management import call_command
from django.test import TestCase

from waldur_core.media.utils import dummy_image


class SetConstanceImageCommandTest(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.image_path = os.path.join(self.temp_dir, "sidebar_logo.png")
        with open(self.image_path, "wb") as f:
            f.write(dummy_image().read())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_stored_value_is_readable_through_config(self):
        call_command(
            "set_constance_image", "SIDEBAR_LOGO", self.image_path, stdout=StringIO()
        )

        self.assertTrue(config.SIDEBAR_LOGO.startswith("sidebar_logo"))
        self.assertTrue(config.SIDEBAR_LOGO.endswith(".png"))

    def test_unknown_key_is_rejected(self):
        output = StringIO()
        call_command(
            "set_constance_image", "NO_SUCH_KEY", self.image_path, stdout=output
        )

        self.assertIn("is not a valid Constance setting", output.getvalue())

    def test_value_stored_unencoded_by_earlier_version_is_replaced(self):
        Constance.objects.create(key="SIDEBAR_LOGO", value="old_logo.png")

        call_command(
            "set_constance_image", "SIDEBAR_LOGO", self.image_path, stdout=StringIO()
        )

        self.assertTrue(config.SIDEBAR_LOGO.startswith("sidebar_logo"))

    def test_non_image_key_is_rejected(self):
        output = StringIO()
        call_command(
            "set_constance_image",
            "WALDUR_SUPPORT_ENABLED",
            self.image_path,
            stdout=output,
        )

        self.assertIn("is not an image setting", output.getvalue())
        self.assertIsInstance(config.WALDUR_SUPPORT_ENABLED, bool)
