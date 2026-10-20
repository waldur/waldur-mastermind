from django.test import TestCase
from django.urls import reverse

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
