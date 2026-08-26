from django.apps import apps
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured
from django.db import models as django_models
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework import status, test

from waldur_core.checklist.models import CHECKLIST_FILE_PREFIX
from waldur_core.media import access
from waldur_core.media import models as media_models
from waldur_core.media.utils import MARKDOWN_IMAGE_PREFIX, get_image_hash
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories

PNG = b"\x89PNG\r\n"


def make_file(name):
    return media_models.File.objects.create(
        name=name,
        content=PNG,
        size=len(PNG),
        mime_type="image/png",
        hash=get_image_hash(PNG),
    )


class RegistryTest(SimpleTestCase):
    def setUp(self):
        override = access.override_rules()
        override.__enter__()
        self.addCleanup(override.__exit__, None, None, None)

    def test_unregistered_prefix_is_denied(self):
        file = media_models.File(name="nobody_declared_this/x.pdf")
        self.assertFalse(access.user_can_access_file(file, AnonymousUser()))

    def test_file_at_storage_root_is_denied(self):
        """Constance logos have no directory; they are served via /api/icons/."""
        self.assertFalse(
            access.user_can_access_file(
                media_models.File(name="logo.png"), AnonymousUser()
            )
        )

    def test_public_prefix_allows_anonymous(self):
        access.register_public("public_stuff/")
        file = media_models.File(name="public_stuff/x.png")
        self.assertTrue(access.user_can_access_file(file, AnonymousUser()))

    def test_authenticated_prefix_denies_anonymous(self):
        access.register_authenticated("members_only/")
        file = media_models.File(name="members_only/x.png")
        self.assertFalse(access.user_can_access_file(file, AnonymousUser()))

    def test_longest_prefix_wins(self):
        access.register_public("exports/")
        access.register("exports/private/", lambda file, user: False)

        self.assertTrue(
            access.user_can_access_file(
                media_models.File(name="exports/summary.csv"), AnonymousUser()
            )
        )
        self.assertFalse(
            access.user_can_access_file(
                media_models.File(name="exports/private/secret.csv"), AnonymousUser()
            )
        )

    def test_prefix_must_end_with_slash(self):
        with self.assertRaises(ValueError):
            access.register_public("no_trailing_slash")

    def test_duplicate_prefix_is_rejected(self):
        """Two fields sharing a prefix must share one rule, not silently race."""
        access.register_public("shared/")
        with self.assertRaises(ImproperlyConfigured):
            access.register_authenticated("shared/")

    def test_duplicate_prefix_can_be_replaced_deliberately(self):
        access.register_public("shared/")
        access.register("shared/", lambda file, user: False, override=True)
        self.assertFalse(
            access.user_can_access_file(
                media_models.File(name="shared/x.png"), AnonymousUser()
            )
        )

    def test_override_restores_the_registry_after_an_exception(self):
        before = access.get_rules()
        with self.assertRaises(RuntimeError):
            with access.override_rules():
                access.register_public("temporary/")
                raise RuntimeError
        self.assertEqual(sorted(access.get_rules()), sorted(before))


class PrefixHelperTest(SimpleTestCase):
    def test_upload_prefix_derives_from_upload_to(self):
        from waldur_mastermind.marketplace.models import Order

        self.assertEqual(
            access.upload_prefix(Order, "attachment"),
            "marketplace_order_attachments/",
        )

    def test_upload_prefix_truncates_strftime_tokens(self):
        from waldur_mastermind.matrix_chat.models import MatrixHistoryExport

        self.assertEqual(
            access.upload_prefix(MatrixHistoryExport, "export_file"),
            "matrix_exports/",
        )

    def test_upload_prefix_rejects_callable_upload_to(self):
        with self.assertRaises(ValueError):
            access.upload_prefix(Customer, "image")

    def test_upload_prefix_rejects_empty_upload_to(self):
        """Such files land at the storage root and have no prefix to key on."""
        from waldur_mastermind.marketplace.models import Order

        field = Order._meta.get_field("attachment")
        original = field.upload_to
        field.upload_to = ""
        try:
            with self.assertRaises(ValueError):
                access.upload_prefix(Order, "attachment")
        finally:
            field.upload_to = original

    def test_image_prefix_is_the_model_name(self):
        self.assertEqual(access.image_prefix(Customer), "customer/")


class CoverageTest(SimpleTestCase):
    """Every stored file must have an owner that declared who may read it.

    This is the guard that keeps the endpoint closed. Adding a FileField
    without a rule in the owning app's ``media_access`` module fails here
    rather than silently publishing the files.
    """

    def test_every_file_field_prefix_has_an_access_rule(self):
        missing = []
        for model in apps.get_models():
            if model._meta.proxy:
                continue
            for field in model._meta.get_fields():
                if not isinstance(field, django_models.FileField):
                    continue
                if isinstance(field.upload_to, str) and field.upload_to:
                    prefix = access.upload_prefix(model, field.name)
                else:
                    prefix = access.image_prefix(model)
                if not access.has_rule(prefix):
                    missing.append(f"{model._meta.label}.{field.name} -> {prefix}")

        self.assertEqual(
            missing,
            [],
            "File fields with no media access rule. Declare one in the owning "
            "app's media_access module; see waldur_core.media.access.",
        )

    def test_model_names_used_as_prefixes_are_unique(self):
        """``get_upload_path`` keys on the bare model name, with no app label."""
        seen = {}
        for model in apps.get_models():
            if model._meta.proxy:
                continue
            for field in model._meta.get_fields():
                if not isinstance(field, django_models.FileField):
                    continue
                if isinstance(field.upload_to, str) and field.upload_to:
                    continue
                prefix = access.image_prefix(model)
                owner = seen.setdefault(prefix, model._meta.label)
                self.assertEqual(
                    owner,
                    model._meta.label,
                    f"{prefix} is written by both {owner} and {model._meta.label}; "
                    "they would share a single access rule.",
                )


class MediaViewTest(test.APITestCase):
    def test_public_prefix_is_served_to_anonymous(self):
        file = make_file(f"{MARKDOWN_IMAGE_PREFIX}public.png")
        response = self.client.get(reverse("media", kwargs={"uuid": file.uuid}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_unregistered_prefix_is_denied_to_staff(self):
        file = make_file("nobody_declared_this/x.png")
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        response = self.client.get(reverse("media", kwargs={"uuid": file.uuid}))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_project_image_stays_anonymous(self):
        """Rendered on the public group-invitation landing page."""
        file = make_file("project/logo.png")
        response = self.client.get(reverse("media", kwargs={"uuid": file.uuid}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_authenticated_prefix_denies_anonymous(self):
        file = make_file("user/avatar.png")
        response = self.client.get(reverse("media", kwargs={"uuid": file.uuid}))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_authenticated_prefix_allows_any_logged_in_user(self):
        file = make_file("user/avatar.png")
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.get(reverse("media", kwargs={"uuid": file.uuid}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_certificates_are_staff_only(self):
        file = make_file("certs/some.pem")

        self.client.force_authenticate(structure_factories.UserFactory())
        self.assertEqual(
            self.client.get(reverse("media", kwargs={"uuid": file.uuid})).status_code,
            status.HTTP_404_NOT_FOUND,
        )

        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        self.assertEqual(
            self.client.get(reverse("media", kwargs={"uuid": file.uuid})).status_code,
            status.HTTP_200_OK,
        )


class ChecklistPrefixTest(TestCase):
    def test_checklist_prefix_is_registered(self):
        self.assertTrue(access.has_rule(CHECKLIST_FILE_PREFIX))
