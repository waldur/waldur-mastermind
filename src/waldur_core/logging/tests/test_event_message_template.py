from ddt import data, ddt, unpack
from django.test import SimpleTestCase
from rest_framework import test

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.logging.middleware import reset_event_context, set_event_context
from waldur_core.logging.mixins import LoggableMixin
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories


@ddt
class RenderMessageTest(SimpleTestCase):
    context = {"name": "Alpha", "user_agent": "Browser/1.0", "count": 3}
    placeholders = {"name", "user_agent", "count"}

    def render(self, template):
        return event_logger.render_message(template, self.context, self.placeholders)

    @data(
        ("Customer {name} has {count} projects.", "Customer Alpha has 3 projects."),
        ("{{name}} is {name}", "{name} is Alpha"),
        ("{{{name}}}", "{Alpha}"),
    )
    @unpack
    def test_placeholders_and_doubled_braces_render_as_with_format(
        self, template, expected
    ):
        self.assertEqual(self.render(template), expected)
        self.assertEqual(template.format(**self.context), expected)

    @data(
        "{user_agent.__class__.__mro__}",
        "{user_agent.__class__.__init__.__globals__}",
        "{user_agent[0]}",
        "{user_agent!r}",
        "{user_agent:>200000}",
        "{0}",
        "{}",
        "{unknown}",
        "price {",
        "}",
        "{ name }",
    )
    def test_anything_but_a_known_bare_name_is_kept_as_written(self, text):
        self.assertEqual(self.render(f"Description: {text}"), f"Description: {text}")

    def test_a_name_not_offered_as_a_placeholder_is_kept(self):
        self.assertEqual(
            event_logger.render_message("{name}", self.context, set()), "{name}"
        )

    def test_literal_text_renders_exactly_as_written(self):
        text = "AA{{BB}}CC {name} {user_agent.__class__} {"
        self.assertEqual(
            self.render(f"Note: {event_logger.literal(text)}"), f"Note: {text}"
        )


class EmitTest(test.APITestCase):
    def tearDown(self):
        reset_event_context()

    def emit(self, template, **event_context):
        event_logger.emit(
            template, EventType.CUSTOMER_UPDATE_SUCCEEDED, event_context, scopes=[]
        )
        return Event.objects.latest("created").message

    def test_malformed_text_in_a_template_does_not_lose_the_event(self):
        self.assertEqual(self.emit("Reason: price {"), "Reason: price {")
        self.assertEqual(self.emit("Reason: {nosuchkey}"), "Reason: {nosuchkey}")

    def test_request_details_of_the_actor_are_not_placeholders(self):
        user = factories.UserFactory()
        set_event_context(
            {
                "ip_address": "203.0.113.7",
                "user_agent": "StaffBrowser/1.0",
                "user_impersonator_username": "staff",
                **user._get_log_context("user"),
            }
        )

        message = self.emit(
            "Renamed to {ip_address} {user_agent} {user_impersonator_username} "
            "by {user_username}."
        )

        self.assertEqual(
            message,
            "Renamed to {ip_address} {user_agent} {user_impersonator_username} "
            f"by {user.username}.",
        )
        # They stay in the context, where readers are filtered.
        event = Event.objects.latest("created")
        self.assertEqual(event.context["ip_address"], "203.0.113.7")

    def test_request_details_carried_by_an_entity_are_not_placeholders(self):
        class Actor(LoggableMixin):
            impersonator_username = "staff"

            def get_log_fields(self):
                return ("impersonator_username",)

        message = self.emit("Renamed to {actor_impersonator_username}.", actor=Actor())

        self.assertEqual(message, "Renamed to {actor_impersonator_username}.")

    def test_a_request_detail_the_emitter_passes_itself_is_a_placeholder(self):
        set_event_context({"ip_address": "198.51.100.1"})

        message = self.emit(
            "Blocked {username} from {ip_address}.",
            username="alice",
            ip_address="203.0.113.7",
        )

        self.assertEqual(message, "Blocked alice from 203.0.113.7.")
