import os
import shutil
import tempfile
from io import StringIO

import yaml
from django.core.management import CommandError, call_command
from django.test import TestCase

from waldur_core.configuration import config
from waldur_core.configuration.models import Setting, Source
from waldur_core.media.utils import dummy_image


class SettingsFileMixin:
    def setUp(self):
        """
        Create a temporary directory for the tests
        """
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        """
        Clean up the temporary directory after the tests.
        """
        shutil.rmtree(self.temp_dir)

    def create_settings_file(self, settings_dict=None):
        """
        Create a settings.yaml file in the temporary directory.
        """
        settings_file = os.path.join(self.temp_dir, "settings.yaml")
        with open(settings_file, "w") as f:
            yaml.dump(settings_dict, f)
        return settings_file


class OverrideConstanceSettingsTest(SettingsFileMixin, TestCase):
    def test_empty_settings_file(self):
        """
        Test that the command prints a warning when the settings file is empty.
        """
        settings_file = self.create_settings_file()
        output = StringIO()
        call_command("override_settings", settings_file, stdout=output)
        self.assertIn("Settings file is empty", output.getvalue())

    def test_empty_image_file(self):
        """
        Test that the command prints an error when the image file is empty.
        """
        temp_image = os.path.join(self.temp_dir, "test_logo.png")
        open(temp_image, "wb").close()
        settings = {"LOGIN_LOGO": temp_image}
        settings_file = self.create_settings_file(settings)
        output = StringIO()
        call_command("override_settings", settings_file, stdout=output)
        self.assertIn(
            "Failed to save setting LOGIN_LOGO due to error: ", output.getvalue()
        )

    def test_basic_settings_override(self):
        """
        Test that the command overrides the settings correctly.
        """
        settings = {
            "WALDUR_SUPPORT_ENABLED": True,
            "WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE": "zammad",
            "ZAMMAD_API_URL": "https://zammad.example.com/api/",
        }
        settings_file = self.create_settings_file(settings)

        output = StringIO()
        call_command("override_settings", settings_file, stdout=output)
        for key, value in settings.items():
            self.assertIn(f"{key} has been set to {value}", output.getvalue())

    def test_secret_settings_are_not_printed(self):
        # Deployments print this output to container logs on every start;
        # docker-compose's Matrix init seeds the registration secret with it.
        settings = {
            "MATRIX_USER_REGISTRATION_SECRET": "registration-secret-value",
            "MATRIX_LIVEKIT_SECRET": "livekit-secret-value",
            # Marked secret_field, with nothing secret-sounding in its name.
            "SCIM_API_KEY": "scim-key-value",
            "MATRIX_HOMESERVER_DOMAIN": "chat.example.com",
        }
        settings_file = self.create_settings_file(settings)

        output = StringIO()
        call_command("override_settings", settings_file, stdout=output)

        self.assertNotIn("registration-secret-value", output.getvalue())
        self.assertNotIn("livekit-secret-value", output.getvalue())
        self.assertNotIn("scim-key-value", output.getvalue())
        self.assertIn(
            "MATRIX_USER_REGISTRATION_SECRET has been set to <redacted>",
            output.getvalue(),
        )
        self.assertIn(
            "MATRIX_HOMESERVER_DOMAIN has been set to chat.example.com",
            output.getvalue(),
        )
        self.assertEqual(
            config.MATRIX_USER_REGISTRATION_SECRET, "registration-secret-value"
        )

    def test_basic_support_backend_is_a_valid_choice(self):
        """
        Test that the basic support backend passes choice validation.
        """
        settings = {"WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE": "basic"}
        settings_file = self.create_settings_file(settings)

        output = StringIO()
        call_command("override_settings", settings_file, stdout=output)
        self.assertIn(
            "WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE has been set to basic",
            output.getvalue(),
        )

    def test_password_and_token_redaction(self):
        """
        Test that the command redacts passwords and tokens.
        """
        # Set password and token
        settings = {
            "ZAMMAD_TOKEN": "secret-token",
            "ATLASSIAN_PASSWORD": "secret-password",
        }
        settings_file = self.create_settings_file(settings)

        output = StringIO()
        call_command("override_settings", settings_file, stdout=output)

        self.assertIn("ZAMMAD_TOKEN has been set to <redacted>", output.getvalue())
        self.assertIn(
            "ATLASSIAN_PASSWORD has been set to <redacted>", output.getvalue()
        )

    def test_logo_upload(self):
        """
        Test that the command uploads a logo file correctly.
        """
        image_content = dummy_image()
        # Create a test logo file
        temp_image = os.path.join(self.temp_dir, "test_logo.png")
        with open(temp_image, "wb") as f:
            f.write(image_content.read())

        settings = {"LOGIN_LOGO": temp_image}
        settings_file = self.create_settings_file(settings)

        output = StringIO()
        call_command("override_settings", settings_file, stdout=output)
        self.assertIn("LOGIN_LOGO has been set to test_logo.png.", output.getvalue())

    def test_nonexistent_logo_file(self):
        """
        Test that the command returns an error when the logo file does not exist.
        """
        # Set invalid logo file
        settings = {"SIDEBAR_LOGO": "/not/a/valid/path/to/logo.png"}
        settings_file = self.create_settings_file(settings)

        output = StringIO()
        call_command("override_settings", settings_file, stdout=output)

        self.assertIn("SIDEBAR_LOGO file does not exist", output.getvalue())

    def test_dict_field_validation_success(self):
        """
        Test that the dict field is validated correctly.
        """
        # Set valid dict
        settings = {"DOCKER_RUN_OPTIONS": {"mem_limit": "512m", "cpu_count": 2}}
        settings_file = self.create_settings_file(settings)

        output = StringIO()
        call_command("override_settings", settings_file, stdout=output)
        self.assertIn(
            "DOCKER_RUN_OPTIONS has been set to {'cpu_count': 2, 'mem_limit': '512m'}",
            output.getvalue(),
        )

    def test_dict_field_validation_failure(self):
        """
        Test that the dict field is validated correctly.
        """
        # Set invalid dict
        settings = {"DOCKER_RUN_OPTIONS": "randomstring"}
        settings_file = self.create_settings_file(settings)
        output = StringIO()
        call_command("override_settings", settings_file, stdout=output)
        self.assertIn(
            "Failed to save setting DOCKER_RUN_OPTIONS due to error: ",
            output.getvalue(),
        )

    def test_url_field_validation(self):
        """
        Test that the url field is validated correctly.
        """
        # Set invalid url
        settings = {"ZAMMAD_API_URL": "prandomstring"}
        settings_file = self.create_settings_file(settings)
        output = StringIO()
        call_command("override_settings", settings_file, stdout=output)
        self.assertIn(
            "Failed to save setting ZAMMAD_API_URL due to error: ", output.getvalue()
        )

    def test_color_field_validation(self):
        """
        Test that the color field is validated correctly.
        """
        # Set invalid color
        settings = {"BRAND_COLOR": "randomstring"}
        settings_file = self.create_settings_file(settings)
        output = StringIO()
        call_command("override_settings", settings_file, stdout=output)
        self.assertIn(
            "Failed to save setting BRAND_COLOR due to error: ", output.getvalue()
        )

        # Set valid color
        settings = {"BRAND_COLOR": "#FF0000"}
        settings_file = self.create_settings_file(settings)
        output = StringIO()
        call_command("override_settings", settings_file, stdout=output)
        self.assertIn("BRAND_COLOR has been set to", output.getvalue())

    def test_list_field_validation(self):
        """
        Test that the list field is validated correctly.
        """
        # Set invalid list
        settings = {"FREEIPA_BLACKLISTED_USERNAMES": "randomstring"}
        settings_file = self.create_settings_file(settings)

        output = StringIO()
        call_command("override_settings", settings_file, stdout=output)
        self.assertIn(
            "Failed to save setting FREEIPA_BLACKLISTED_USERNAMES due to error: ",
            output.getvalue(),
        )

        # Set valid list
        settings = {"FREEIPA_BLACKLISTED_USERNAMES": ["root", "admin"]}
        settings_file = self.create_settings_file(settings)
        output = StringIO()
        call_command("override_settings", settings_file, stdout=output)
        self.assertIn(
            "FREEIPA_BLACKLISTED_USERNAMES has been set to", output.getvalue()
        )


class SeedOnlyIfUnsetTest(SettingsFileMixin, TestCase):
    """`--if-unset` is what the boot-time init jobs use for operator settings.

    Those jobs run on every container start, so an unconditional override
    silently reverted anything an administrator had changed in the UI.
    """

    def seed(self, settings):
        output = StringIO()
        call_command(
            "override_settings",
            self.create_settings_file(settings),
            "--if-unset",
            stdout=output,
        )
        return output.getvalue()

    def test_an_unset_setting_is_seeded(self):
        output = self.seed({"LANGUAGE_CHOICES": "en,et"})

        self.assertIn("LANGUAGE_CHOICES has been set to en,et", output)
        self.assertEqual(config.LANGUAGE_CHOICES, "en,et")

    def test_a_setting_an_administrator_changed_is_left_alone(self):
        self.seed({"LANGUAGE_CHOICES": "en,et"})

        # The administrator narrows the list in the UI, which stores a row.
        config.LANGUAGE_CHOICES = "en"

        output = self.seed({"LANGUAGE_CHOICES": "en,et,lt,lv,ru"})

        self.assertIn("LANGUAGE_CHOICES already has a stored value", output)
        self.assertEqual(config.LANGUAGE_CHOICES, "en")

    def test_without_the_flag_the_stored_value_is_still_overridden(self):
        config.LANGUAGE_CHOICES = "en"
        settings_file = self.create_settings_file({"LANGUAGE_CHOICES": "en,et"})

        call_command("override_settings", settings_file, stdout=StringIO())

        self.assertEqual(config.LANGUAGE_CHOICES, "en,et")

    def test_an_unchanged_value_is_recorded_as_the_deployments(self):
        Setting.objects.set("SITE_NAME", "Portal", source=Source.MIGRATION)
        settings_file = self.create_settings_file({"SITE_NAME": "Portal"})

        call_command("override_settings", settings_file, stdout=StringIO())

        row = Setting.objects.get(key="SITE_NAME")
        self.assertEqual(row.value, "Portal")
        self.assertEqual(row.source, Source.DEPLOYMENT)
        self.assertEqual(row.source_detail, "settings.yaml")

    def test_settings_are_considered_one_by_one(self):
        self.seed({"LANGUAGE_CHOICES": "en,et"})

        output = self.seed({"LANGUAGE_CHOICES": "en,et,lt", "BRAND_COLOR": "#FF0000"})

        self.assertIn("LANGUAGE_CHOICES already has a stored value", output)
        self.assertIn("BRAND_COLOR has been set to", output)


class DeploymentLockTest(SettingsFileMixin, TestCase):
    """`--lock` is what init-whitelabeling passes for the files it re-applies
    on every start."""

    def apply(self, settings, name="settings.yaml", *args, lock=True):
        path = os.path.join(self.temp_dir, name)
        with open(path, "w") as f:
            yaml.dump(settings, f)
        output = StringIO()
        if lock:
            args = (*args, "--lock")
        call_command("override_settings", path, *args, stdout=output)
        return output.getvalue()

    def unlock(self, *keys):
        output = StringIO()
        args = [arg for key in keys for arg in ("--unlock", key)]
        call_command("override_settings", *args, stdout=output)
        return output.getvalue()

    def locked(self):
        return dict(
            Setting.objects.filter(locked=True).values_list("key", "managed_by")
        )

    def managed(self):
        return dict(
            Setting.objects.exclude(managed_by="").values_list("key", "managed_by")
        )

    def test_settings_of_the_file_are_locked(self):
        self.apply({"SITE_NAME": "Portal", "BRAND_COLOR": "#FF0000"})

        self.assertEqual(
            self.locked(),
            {"SITE_NAME": "settings.yaml", "BRAND_COLOR": "settings.yaml"},
        )

    def test_the_former_command_name_still_works(self):
        # Deployments with their own init scripts still call it by this name.
        path = os.path.join(self.temp_dir, "settings.yaml")
        with open(path, "w") as f:
            yaml.dump({"SITE_NAME": "Portal"}, f)

        call_command(
            "override_constance_settings",
            constance_settings_file=path,
            lock=True,
            stdout=StringIO(),
        )

        self.assertEqual(self.locked(), {"SITE_NAME": "settings.yaml"})

    def test_a_one_off_run_locks_nothing(self):
        # Such as a hotfix file, or input piped through /dev/stdin: nothing
        # would apply it again to release the lock.
        self.apply({"SITE_NAME": "Portal"}, "hotfix.yaml", lock=False)

        self.assertEqual(config.SITE_NAME, "Portal")
        self.assertEqual(self.managed(), {})

    def test_a_one_off_run_leaves_the_deployment_locks_alone(self):
        self.apply({"SITE_NAME": "Portal", "BRAND_COLOR": "#FF0000"})

        self.apply({"SITE_NAME": "Hotfix"}, lock=False)

        self.assertEqual(
            self.locked(),
            {"SITE_NAME": "settings.yaml", "BRAND_COLOR": "settings.yaml"},
        )

    def test_an_unchanged_value_stored_before_locking_is_locked(self):
        # A deployment upgraded from a release without locks.
        Setting.objects.set(
            "SITE_NAME",
            "Portal",
            source=Source.DEPLOYMENT,
            source_detail="settings.yaml",
        )

        self.apply({"SITE_NAME": "Portal"})

        self.assertEqual(self.locked(), {"SITE_NAME": "settings.yaml"})

    def test_a_setting_taken_out_of_the_file_is_handed_back(self):
        self.apply({"SITE_NAME": "Portal", "BRAND_COLOR": "#FF0000"})

        output = self.apply({"BRAND_COLOR": "#FF0000"})

        self.assertEqual(self.managed(), {"BRAND_COLOR": "settings.yaml"})
        self.assertEqual(config.SITE_NAME, "Portal")
        self.assertIn(
            "SITE_NAME no longer managed by settings.yaml, handed back", output
        )

    def test_a_setting_an_administrator_changed_after_unlock_is_still_handed_back(
        self,
    ):
        # The row then records the administrator as the last writer, not the
        # file; what manages it is kept apart.
        self.apply({"SITE_NAME": "Portal"})
        self.unlock("SITE_NAME")
        Setting.objects.set("SITE_NAME", "Renamed", source=Source.API)

        self.apply({"BRAND_COLOR": "#FF0000"})

        self.assertEqual(self.managed(), {"BRAND_COLOR": "settings.yaml"})
        self.assertEqual(config.SITE_NAME, "Renamed")

    def test_an_emptied_file_hands_its_settings_back(self):
        self.apply({"SITE_NAME": "Portal"})

        self.apply(None)

        self.assertEqual(self.managed(), {})

    def test_another_file_leaves_the_settings_of_this_one_locked(self):
        self.apply({"SITE_NAME": "Portal"}, "whitelabeling.yaml")

        self.apply({"BRAND_COLOR": "#FF0000"}, "support.yaml")

        self.assertEqual(
            self.locked(),
            {"SITE_NAME": "whitelabeling.yaml", "BRAND_COLOR": "support.yaml"},
        )

    def test_a_setting_in_two_files_belongs_to_the_last_one(self):
        self.apply({"SITE_NAME": "Portal"}, "a.yaml")
        self.apply({"SITE_NAME": "Portal B"}, "b.yaml")

        # a.yaml no longer manages it, so leaving it out releases nothing.
        self.apply({"BRAND_COLOR": "#FF0000"}, "a.yaml")

        self.assertEqual(
            self.locked(), {"SITE_NAME": "b.yaml", "BRAND_COLOR": "a.yaml"}
        )
        self.assertEqual(config.SITE_NAME, "Portal B")

    def test_a_rejected_value_leaves_the_setting_locked(self):
        self.apply({"BRAND_COLOR": "#FF0000"})

        self.apply({"BRAND_COLOR": "not a colour"})

        self.assertEqual(self.locked(), {"BRAND_COLOR": "settings.yaml"})
        self.assertEqual(config.BRAND_COLOR, "#FF0000")

    def test_seeded_settings_are_not_managed(self):
        self.apply(
            {"LANGUAGE_CHOICES": "en,et"}, "settings.yaml", "--if-unset", lock=False
        )

        self.assertEqual(self.managed(), {})

    def test_a_file_switched_to_seeding_hands_its_settings_back(self):
        self.apply({"SITE_NAME": "Portal"})

        self.apply({"SITE_NAME": "Portal"}, "settings.yaml", "--if-unset", lock=False)

        self.assertEqual(self.managed(), {})

    def test_lock_and_if_unset_exclude_each_other(self):
        with self.assertRaisesMessage(CommandError, "exclude each other"):
            self.apply({"SITE_NAME": "Portal"}, "settings.yaml", "--if-unset")

    def test_settings_waldur_changes_itself_are_only_seeded(self):
        config.TELEMETRY_DEPLOYMENT_ID = "running-id"

        output = self.apply(
            {"TELEMETRY_DEPLOYMENT_ID": "file-id", "REMOTE_EDUTEAMS_REFRESH_TOKEN": "t"}
        )

        self.assertEqual(config.TELEMETRY_DEPLOYMENT_ID, "running-id")
        self.assertIn("TELEMETRY_DEPLOYMENT_ID already has a stored value", output)
        self.assertEqual(config.REMOTE_EDUTEAMS_REFRESH_TOKEN, "t")
        self.assertEqual(self.managed(), {})

    def test_unknown_settings_are_reported_not_claimed(self):
        output = self.apply({"SITE_NAME": "Portal", "SUPPORT_EMAIL": "a@b.c"})

        self.assertIn("SUPPORT_EMAIL is not a setting Waldur knows, skipped.", output)
        self.assertNotIn("SUPPORT_EMAIL has been set", output)
        self.assertEqual(self.locked(), {"SITE_NAME": "settings.yaml"})

    def test_unlock_lasts_until_the_next_deploy(self):
        self.apply({"SITE_NAME": "Portal"})

        output = self.unlock("site_name")

        self.assertEqual(self.locked(), {})
        self.assertEqual(self.managed(), {"SITE_NAME": "settings.yaml"})
        self.assertEqual(config.SITE_NAME, "Portal")
        self.assertIn("SITE_NAME has been unlocked.", output)
        self.assertIn("The next deploy puts back the value of SITE_NAME", output)

        self.apply({"SITE_NAME": "Portal"})
        self.assertEqual(self.locked(), {"SITE_NAME": "settings.yaml"})

    def test_unlock_of_a_setting_that_is_not_locked(self):
        output = self.unlock("SITE_NAME")

        self.assertIn("SITE_NAME is not locked.", output)
        self.assertNotIn("has been unlocked", output)

    def test_unlock_rejects_unknown_settings(self):
        with self.assertRaisesMessage(CommandError, "NO_SUCH_SETTING"):
            self.unlock("NO_SUCH_SETTING")

    def test_a_file_or_unlock_is_required(self):
        with self.assertRaises(CommandError):
            call_command("override_settings")

    def test_dump_managed_only_names_each_file(self):
        self.apply({"SITE_NAME": "Portal"}, "whitelabeling.yaml")
        self.apply({"BRAND_COLOR": "#FF0000"}, "support.yaml")
        config.SHORT_PAGE_TITLE = "Not managed"
        path = os.path.join(self.temp_dir, "dump.yaml")

        call_command("dump_settings", path, "--managed-only", stdout=StringIO())

        with open(path) as f:
            text = f.read()
        self.assertEqual(
            yaml.safe_load(text), {"SITE_NAME": "Portal", "BRAND_COLOR": "#FF0000"}
        )
        self.assertIn("# support.yaml\nBRAND_COLOR:", text)
        self.assertIn("# whitelabeling.yaml\nSITE_NAME:", text)
