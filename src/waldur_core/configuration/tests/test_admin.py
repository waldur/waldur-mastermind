from django.test import TestCase
from django.urls import reverse

from waldur_core.configuration import config
from waldur_core.configuration.forms import SettingsForm
from waldur_core.configuration.models import Setting, Source
from waldur_core.structure.tests.factories import UserFactory


class SettingAdminTest(TestCase):
    def setUp(self):
        self.client.force_login(UserFactory(is_staff=True, is_superuser=True))

    def test_settings_page_renders(self):
        response = self.client.get(reverse("admin:configuration_setting_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SITE_NAME")

    def test_rows_cannot_be_edited_one_by_one(self):
        row = Setting.objects.create(key="SITE_NAME", value="Portal", source=Source.API)

        for name, args in (
            ("change", [row.pk]),
            ("add", []),
            ("delete", [row.pk]),
        ):
            url = reverse(f"admin:configuration_setting_{name}", args=args)
            self.assertEqual(self.client.get(url).status_code, 403, name)

    def lock(self, key, value):
        Setting.objects.set(
            key,
            value,
            source=Source.DEPLOYMENT,
            source_detail="whitelabeling.yaml",
            locked=True,
            managed_by="whitelabeling.yaml",
        )

    def posted_page(self, url):
        """What a browser posts from the page: enabled inputs, checked boxes
        as "on"."""
        page = self.client.get(url).context["form"]
        posted = {}
        for name, field in page.fields.items():
            value = page[name].value()
            if field.disabled or value is None or value is False:
                continue
            if value is True:
                posted[name] = "on"
            elif isinstance(value, str | int):
                posted[name] = value
        return posted

    def test_a_locked_setting_is_read_only(self):
        self.lock("SITE_NAME", "Portal")
        url = reverse("admin:configuration_setting_changelist")

        form = self.client.get(url).context["form"]

        self.assertTrue(form.fields["SITE_NAME"].disabled)
        self.assertIn("whitelabeling.yaml", form.fields["SITE_NAME"].help_text)

    def test_locked_settings_of_every_type_leave_the_form_usable(self):
        # Dict, list and image fields cannot clean the stored value as if it
        # were posted; a locked secret is not shown.
        self.lock("SITE_NAME", "Portal")
        self.lock("DOCKER_CLIENT", {"base_url": "unix:///var/run/docker.sock"})
        self.lock("FREEIPA_BLACKLISTED_USERNAMES", ["root"])
        self.lock("COUNTRIES", ["EE"])
        self.lock("POWERED_BY_LOGO", "logo.png")
        self.lock("ATLASSIAN_PASSWORD", "hunter2")
        initial = Setting.objects.values_with_defaults()

        form = SettingsForm(data={"SITE_NAME": "Other"}, initial=initial)
        form.full_clean()
        form.save()

        for key in ("DOCKER_CLIENT", "POWERED_BY_LOGO", "SITE_NAME"):
            self.assertNotIn(key, form.errors)
        self.assertEqual(Setting.objects.get(key="SITE_NAME").value, "Portal")
        self.assertNotIn("hunter2", str(form["ATLASSIAN_PASSWORD"]))

    def test_a_page_loaded_before_a_lock_was_released_keeps_the_value(self):
        # A disabled input is not posted. Were the released setting an
        # ordinary field again on save, its missing input would be stored.
        self.lock("SITE_NAME", "Portal")
        self.lock("WALDUR_SUPPORT_ENABLED", True)
        url = reverse("admin:configuration_setting_changelist")
        posted = self.posted_page(url)
        posted["SITE_DESCRIPTION"] = "Changed"

        Setting.objects.release("SITE_NAME", "WALDUR_SUPPORT_ENABLED")
        self.client.post(url, posted)

        self.assertEqual(config.SITE_NAME, "Portal")
        self.assertIs(config.WALDUR_SUPPORT_ENABLED, True)
        self.assertEqual(config.SITE_DESCRIPTION, "Changed")

    def test_a_change_to_a_setting_locked_after_the_page_was_loaded_is_reported(
        self,
    ):
        Setting.objects.set("WALDUR_SUPPORT_ENABLED", True, source=Source.API)
        url = reverse("admin:configuration_setting_changelist")
        posted = self.posted_page(url)
        posted["SITE_NAME"] = "Changed"
        del posted["WALDUR_SUPPORT_ENABLED"]  # unchecked
        posted["SITE_DESCRIPTION"] = "Changed"

        self.lock("SITE_NAME", "Portal")
        self.lock("WALDUR_SUPPORT_ENABLED", True)
        # Locked too, but left as it was on the page.
        self.lock("SITE_EMAIL", config.SITE_EMAIL)
        response = self.client.post(url, posted, follow=True)

        self.assertEqual(config.SITE_NAME, "Portal")
        self.assertIs(config.WALDUR_SUPPORT_ENABLED, True)
        self.assertEqual(config.SITE_DESCRIPTION, "Changed")
        warnings = [
            str(m) for m in response.context["messages"] if m.level_tag == "warning"
        ]
        self.assertEqual(len(warnings), 1)
        self.assertIn("SITE_NAME, WALDUR_SUPPORT_ENABLED", warnings[0])
        self.assertNotIn("SITE_EMAIL", warnings[0])
