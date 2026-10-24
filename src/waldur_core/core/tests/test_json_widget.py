import json
from decimal import Decimal

from django import forms
from django.contrib import admin
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Value
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape

from waldur_core.core import fields as core_fields
from waldur_core.core.admin import format_json_field
from waldur_core.core.admin_widgets import (
    AdminJsonField,
    JsonWidget,
    admin_json_formfield_kwargs,
)
from waldur_core.structure import models as structure_models
from waldur_core.structure.registry import SupportedServices
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.admin import OfferingComponentInline
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

VALUE = {"name": "Ülo's cluster", "limits": {"cores": 4}, "tags": ["a", "b"]}
PRETTY = json.dumps(VALUE, indent=2, ensure_ascii=False)


def object_field(**kwargs):
    """A field as the admin builds it for a NOT NULL JSONField(default=dict)."""
    return AdminJsonField(
        holds_object=True, allow_null=False, empty=dict, required=False, **kwargs
    )


def superuser_request():
    request = RequestFactory().get("/")
    request.user = structure_factories.UserFactory(is_staff=True, is_superuser=True)
    return request


def admin_formfield(model, name):
    model_admin = admin.site._registry[model]
    return model_admin.formfield_for_dbfield(
        model._meta.get_field(name), superuser_request()
    )


class JsonWidgetTest(TestCase):
    def test_value_is_pretty_printed(self):
        self.assertEqual(JsonWidget().format_value(VALUE), PRETTY)

    def test_text_is_shown_as_it_is(self):
        # The field lays the value out; the widget never guesses whether a
        # string is encoded JSON, so "123" stays "123".
        self.assertEqual(JsonWidget().format_value("123"), "123")
        typed = forms.fields.InvalidJSONInput('{"cores": 4,')
        self.assertEqual(JsonWidget().format_value(typed), '{"cores": 4,')

    def test_markup_in_the_value_is_escaped(self):
        html = JsonWidget().render(
            "attributes", {"note": "</textarea><script>x</script>"}, {"id": "a"}
        )

        self.assertNotIn("<script>", html)
        self.assertIn("&lt;/textarea&gt;&lt;script&gt;", html)

    def test_textarea_grows_with_the_content_up_to_a_limit(self):
        short = JsonWidget().get_context("a", {}, {"id": "a"})
        long = JsonWidget().get_context(
            "a", {str(i): i for i in range(100)}, {"id": "a"}
        )

        self.assertEqual(short["widget"]["attrs"]["rows"], JsonWidget.MIN_ROWS)
        self.assertEqual(long["widget"]["attrs"]["rows"], JsonWidget.MAX_ROWS)

    def test_long_lines_count_the_rows_they_wrap_to(self):
        context = JsonWidget().get_context("a", {"note": "x" * 400}, {"id": "a"})

        # The 412-character line wraps to 6 rows of 80 columns, plus the
        # braces and a spare row.
        self.assertEqual(context["widget"]["attrs"]["cols"], JsonWidget.COLUMNS)
        self.assertEqual(context["widget"]["attrs"]["rows"], 9)

    def test_caller_attrs_add_to_the_defaults(self):
        widget = JsonWidget(attrs={"class": "wide", "style": "color: red", "rows": 3})
        attrs = widget.get_context("a", VALUE, {"id": "a"})["widget"]["attrs"]

        self.assertEqual(attrs["class"], "vLargeTextField wide")
        self.assertIn("font-family: monospace", attrs["style"])
        self.assertIn("color: red", attrs["style"])
        self.assertEqual(attrs["rows"], 3)

    def test_copy_button_targets_the_textarea(self):
        html = JsonWidget().render("attributes", VALUE, {"id": "id_attributes"})

        self.assertIn('id="id_attributes"', html)
        self.assertIn('data-target-id="id_attributes"', html)
        self.assertIn("copy-button", html)

    def test_without_an_id_there_is_no_copy_button(self):
        html = JsonWidget().render("attributes", VALUE)

        self.assertIn("<textarea", html)
        self.assertNotIn("copy-button", html)

    def test_copy_script_loads_after_the_admins_own_jquery(self):
        for debug, jquery in (
            (True, "admin/js/vendor/jquery/jquery.js"),
            (False, "admin/js/vendor/jquery/jquery.min.js"),
        ):
            with self.subTest(debug=debug), override_settings(DEBUG=debug):
                scripts = JsonWidget().media._js

                self.assertEqual(scripts[0], jquery)
                self.assertEqual(scripts[1], "admin/js/jquery.init.js")
                self.assertTrue(scripts[2].endswith("landing/js/copy2clipboard.js"))


class AdminJsonFieldTest(TestCase):
    def test_value_is_laid_out_for_editing(self):
        self.assertEqual(object_field().prepare_value(VALUE), PRETTY)

    def test_string_value_stays_a_string(self):
        self.assertEqual(AdminJsonField().prepare_value("123"), '"123"')

    def test_the_fields_encoder_is_used(self):
        field = AdminJsonField(encoder=DjangoJSONEncoder)

        self.assertEqual(
            field.prepare_value({"price": Decimal("1.50")}), '{\n  "price": "1.50"\n}'
        )

    def test_only_a_lone_surrogate_is_escaped(self):
        text = object_field().prepare_value({"name": "Ülo \ud800"})

        text.encode("utf-8")
        self.assertIn("Ülo \\ud800", text)

    def test_cleared_text_stores_the_default(self):
        self.assertEqual(object_field().clean(""), {})

    def test_anything_but_an_object_is_refused(self):
        for text in ("null", "[]", '"x"', "1", "true"):
            with self.subTest(text=text):
                with self.assertRaisesMessage(
                    forms.ValidationError, "Enter a JSON object"
                ):
                    object_field().clean(text)

    def test_json_postgresql_or_the_api_cannot_take_is_refused(self):
        for text in (
            '{"a": NaN}',
            '{"a": Infinity}',
            '{"a": -Infinity}',
            '{"a": 1e999}',
            '{"a": -1e400}',
            '{"a": 1, "a": 2}',
            "[" * 100_000 + "]" * 100_000,
        ):
            with self.subTest(text=text[:20]):
                with self.assertRaisesMessage(
                    forms.ValidationError, "Enter a valid JSON"
                ):
                    AdminJsonField(required=False).clean(text)

    def test_text_postgresql_cannot_store_is_refused(self):
        for text in (
            '{"a": "nul \\u0000 here"}',
            '{"a": "half \\ud800 a pair"}',
            '{"a": {"deep": ["\\udfff"]}}',
            '{"key \\u0000": 1}',
        ):
            with self.subTest(text=text):
                with self.assertRaisesMessage(
                    forms.ValidationError, "may not contain NUL"
                ):
                    object_field().clean(text)

    def test_a_complete_surrogate_pair_is_accepted(self):
        self.assertEqual(object_field().clean('{"a": "\\ud83d\\ude00"}'), {"a": "😀"})


class EmptyValueFromModelFieldTest(TestCase):
    """What cleared text and null mean comes from the model field."""

    def field_for(self, model_field):
        model_field.set_attributes_from_name("data")
        return model_field.formfield(**admin_json_formfield_kwargs(model_field))

    def test_list_default_not_null_clears_to_an_empty_list(self):
        field = self.field_for(models.JSONField(default=list, blank=True))

        self.assertEqual(field.clean(""), [])
        with self.assertRaisesMessage(forms.ValidationError, "cannot be null"):
            field.clean("null")

    def test_no_default_not_null_cannot_be_cleared(self):
        field = self.field_for(models.JSONField(blank=True))

        with self.assertRaisesMessage(forms.ValidationError, "cannot be null"):
            field.clean("")

    def test_nullable_field_without_a_default_clears_to_null(self):
        field = self.field_for(models.JSONField(blank=True, null=True))

        self.assertIsNone(field.clean(""))
        self.assertIsNone(field.clean("null"))
        self.assertEqual(field.clean("[1, 2]"), [1, 2])

    def test_legacy_text_field_keeps_its_encoder(self):
        field = self.field_for(core_fields.JSONField(default=dict, blank=True))

        self.assertIs(field.encoder, DjangoJSONEncoder)
        self.assertTrue(field.holds_object)

    def test_admin_fields_from_the_audit(self):
        cases = [
            (structure_models.ServiceSettings, "options", {}),
            (marketplace_models.Offering, "attributes", {}),
            (marketplace_models.OfferingComponent, "spending_limit_periods", {}),
        ]
        user_model = structure_factories.UserFactory._meta.model
        cases.append((user_model, "managed_isds", []))
        for model, name, empty in cases:
            with self.subTest(field=f"{model.__name__}.{name}"):
                if model not in admin.site._registry:
                    continue
                field = admin_formfield(model, name)
                self.assertIsInstance(field, AdminJsonField)
                self.assertEqual(field.clean(""), empty)


class JsonWidgetAdminPagesTest(TestCase):
    """The admin pages that edit JSON open with it pretty-printed."""

    def setUp(self):
        self.client.force_login(
            structure_factories.UserFactory(is_staff=True, is_superuser=True)
        )

    def assert_pretty(self, response, name, value):
        self.assertEqual(response.status_code, 200)
        field = response.context["adminform"].form.fields[name]
        self.assertIsInstance(field, AdminJsonField)
        self.assertIsInstance(field.widget, JsonWidget)
        pretty = json.dumps(value, indent=2, ensure_ascii=False)
        self.assertContains(response, escape(pretty))

    def test_offering_fields_open_pretty_printed(self):
        offering = marketplace_factories.OfferingFactory(
            attributes=VALUE,
            options={"order": ["cores"]},
            plugin_options={"auto_approve": True},
        )
        # jsonb keeps keys in its own order, which is the order shown.
        offering.refresh_from_db()

        response = self.client.get(
            reverse("admin:marketplace_offering_change", args=[offering.pk])
        )

        for name in ("attributes", "options", "plugin_options"):
            self.assert_pretty(response, name, getattr(offering, name))

    def test_service_settings_options_open_pretty_printed(self):
        settings = structure_factories.ServiceSettingsFactory(
            shared=False, options={"tenant_name": "admin", "verify_ssl": False}
        )
        settings.refresh_from_db()

        response = self.client.get(
            reverse("admin:structure_privateservicesettings_change", args=[settings.pk])
        )

        self.assert_pretty(response, "options", settings.options)

    def test_payment_profile_attributes_open_pretty_printed(self):
        profile = invoices_factories.PaymentProfileFactory(attributes=VALUE)
        profile.refresh_from_db()

        response = self.client.get(
            reverse("admin:invoices_paymentprofile_change", args=[profile.pk])
        )

        self.assert_pretty(response, "attributes", profile.attributes)


class JsonWidgetRoundTripTest(TestCase):
    """Edited text saves through the admin's own forms; invalid JSON does not."""

    def admin_form(self, instance, data, fields, model=None):
        model_admin = admin.site._registry[model or type(instance)]
        Form = model_admin.get_form(
            superuser_request(), instance, change=True, fields=fields
        )
        return Form(data, instance=instance)

    def test_edited_json_is_saved(self):
        offering = marketplace_factories.OfferingFactory(attributes=VALUE)
        edited = PRETTY.replace('"cores": 4', '"cores": 8')

        form = self.admin_form(offering, {"attributes": edited}, ["attributes"])

        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        offering.refresh_from_db()
        self.assertEqual(offering.attributes["limits"], {"cores": 8})

    def test_cleared_text_saves_an_empty_object(self):
        offering = marketplace_factories.OfferingFactory(attributes=VALUE)

        form = self.admin_form(offering, {"attributes": ""}, ["attributes"])

        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        offering.refresh_from_db()
        self.assertEqual(offering.attributes, {})

    def test_invalid_input_is_refused_and_nothing_is_saved(self):
        offering = marketplace_factories.OfferingFactory(attributes=VALUE)
        offering.refresh_from_db()
        stored = offering.attributes

        for text in ('{"cores": 8,', "null", "[]", '{"a": NaN}'):
            with self.subTest(text=text):
                form = self.admin_form(offering, {"attributes": text}, ["attributes"])

                self.assertFalse(form.is_valid())
                self.assertIn("attributes", form.errors)
                offering.refresh_from_db()
                self.assertEqual(offering.attributes, stored)

    def test_rejected_text_comes_back_as_typed(self):
        offering = marketplace_factories.OfferingFactory(attributes=VALUE)

        form = self.admin_form(offering, {"attributes": '{"cores": 8,'}, ["attributes"])

        self.assertFalse(form.is_valid())
        self.assertIn(escape('{"cores": 8,'), str(form["attributes"]))

    def test_stored_null_shows_and_saves_as_an_empty_object(self):
        offering = marketplace_factories.OfferingFactory(name="Old")
        marketplace_models.Offering.objects.filter(pk=offering.pk).update(
            attributes=Value(None, models.JSONField())
        )
        offering.refresh_from_db()
        self.assertIsNone(offering.attributes)

        form = self.admin_form(offering, None, ["name", "attributes"])
        self.assertEqual(form["attributes"].value(), "{}")

        form = self.admin_form(
            offering, {"name": "New", "attributes": "{}"}, ["name", "attributes"]
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn("attributes", form.changed_data)
        form.save()
        offering.refresh_from_db()
        self.assertEqual((offering.name, offering.attributes), ("New", {}))

    def test_stored_non_object_stays_until_someone_edits_it(self):
        offering = marketplace_factories.OfferingFactory(name="Old")
        marketplace_models.Offering.objects.filter(pk=offering.pk).update(
            attributes=["legacy"]
        )
        offering.refresh_from_db()
        shown = self.admin_form(offering, None, ["name", "attributes"])[
            "attributes"
        ].value()

        form = self.admin_form(
            offering,
            {"name": "New", "attributes": shown},
            ["name", "attributes"],
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        offering.refresh_from_db()
        self.assertEqual((offering.name, offering.attributes), ("New", ["legacy"]))

        form = self.admin_form(
            offering,
            {"name": "New", "attributes": '["edited"]'},
            ["name", "attributes"],
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Enter a JSON object", str(form.errors["attributes"]))

    def test_payment_profile_attributes_round_trip(self):
        profile = invoices_factories.PaymentProfileFactory(attributes={"a": 1})

        form = self.admin_form(
            profile, {"attributes": '{\n  "a": 2\n}'}, ["attributes"]
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        profile.refresh_from_db()
        self.assertEqual(profile.attributes, {"a": 2})

        form = self.admin_form(profile, {"attributes": ""}, ["attributes"])
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        profile.refresh_from_db()
        self.assertEqual(profile.attributes, {})

    def test_service_settings_options_must_be_an_object(self):
        settings = structure_factories.ServiceSettingsFactory(shared=False)
        private = structure_models.PrivateServiceSettings.objects.get(pk=settings.pk)
        service_type = SupportedServices.get_choices()[0][0]

        for text in ("null", "[]", '"x"', "1"):
            with self.subTest(text=text):
                form = self.admin_form(
                    private,
                    {"type": service_type, "name": "S", "options": text},
                    ["type", "name", "options", "backend_url"],
                )

                self.assertFalse(form.is_valid())
                self.assertIn("Enter a JSON object", str(form.errors["options"]))


class InlineJsonFieldTest(TestCase):
    """OfferingComponent.spending_limit_periods, inline on the offering page."""

    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory()
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering
        )
        inline = OfferingComponentInline(marketplace_models.Offering, admin.site)
        self.field = inline.get_formset(
            superuser_request(), self.offering
        ).form.base_fields["spending_limit_periods"]

    def bound(self, data, initial):
        class Form(forms.Form):
            spending_limit_periods = self.field

        return Form(
            {"spending_limit_periods": data},
            initial={"spending_limit_periods": initial},
        )

    def test_inline_field_is_cleared_to_an_empty_object(self):
        self.assertIsInstance(self.field, AdminJsonField)
        self.assertEqual(self.field.clean(""), {})
        with self.assertRaisesMessage(forms.ValidationError, "Enter a JSON object"):
            self.field.clean("null")

    def test_untouched_inline_row_with_stored_null_stays_valid(self):
        # A collapsed row submits what it was shown, {} for a stored null.
        form = self.bound(self.field.prepare_value(None), None)

        self.assertEqual(form.data["spending_limit_periods"], "{}")
        self.assertFalse(form.has_changed())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["spending_limit_periods"], {})


class FormatJsonFieldTest(TestCase):
    def test_json_shown_read_only_is_escaped(self):
        html = format_json_field({"name": "</pre><script>alert(1)</script>"})

        self.assertNotIn("<script>", html)
        self.assertIn("&lt;/pre&gt;&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertTrue(html.startswith('<div><pre style="overflow: hidden">'))


class AdminJsonFieldCoverageTest(TestCase):
    """Every admin and inline edits every JSON model field with AdminJsonField."""

    def test_every_admin_json_field_follows_its_model_field(self):
        request = superuser_request()
        checked = []
        for model, model_admin in admin.site._registry.items():
            owners = [(model_admin, model)] + [
                (inline(model, admin.site), inline.model)
                for inline in model_admin.inlines
            ]
            for owner, owner_model in owners:
                for db_field in owner_model._meta.get_fields():
                    if not isinstance(
                        db_field, (models.JSONField, core_fields.JSONField)
                    ):
                        continue
                    if db_field.choices or not db_field.editable:
                        continue
                    form_field = owner.formfield_for_dbfield(db_field, request)
                    label = f"{type(owner).__name__}.{db_field.name}"
                    with self.subTest(field=label):
                        self.assertIsInstance(form_field, AdminJsonField)
                        self.assertEqual(
                            form_field.holds_object,
                            db_field.default is dict
                            or isinstance(db_field.default, dict),
                        )
                        self.assertEqual(form_field.allow_null, db_field.null)
                    checked.append(label)

        self.assertIn("OfferingComponentInline.spending_limit_periods", checked)
        self.assertIn("OfferingAdmin.plugin_options", checked)
        self.assertIn("UserAdmin.managed_isds", checked)
