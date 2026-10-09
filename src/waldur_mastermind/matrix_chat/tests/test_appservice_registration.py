import hashlib
import hmac
import json
import os
import re
from io import StringIO
from unittest import mock

import httpx
import respx
import yaml
from constance import config
from constance.test import override_config
from cryptography.fernet import Fernet
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.test.utils import override_settings

from waldur_core.logging.tests.test_sentry_scrubbing import _send
from waldur_mastermind.matrix_chat import appservice_registration, models

HOMESERVER = "https://matrix.example.com"
DOMAIN = "example.com"

MATRIX_CONFIG = dict(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL=HOMESERVER,
    MATRIX_HOMESERVER_DOMAIN=DOMAIN,
    MATRIX_APPSERVICE_AS_TOKEN="as-token",
    MATRIX_APPSERVICE_HS_TOKEN="hs-token",
    MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
    MATRIX_USER_REGISTRATION_SECRET="reg-secret",
)

BOOTSTRAP_USER = f"@{appservice_registration.BOOTSTRAP_LOCALPART}:{DOMAIN}"
BOT_USER = f"@waldur-bot:{DOMAIN}"
ADMIN_ROOM = "!admin:example.com"
SENT_EVENT_ID = "$sent"

# respx has no "endswith" lookup, so the paginated /messages endpoint
# (the room id sits in the middle of the path) is matched by regex.
MESSAGES_URL = rf"{HOMESERVER}/_matrix/client/v3/rooms/.+/messages"

PING_URL = f"{HOMESERVER}/_matrix/client/v1/appservice/waldur/ping"
LOGIN_URL = f"{HOMESERVER}/_matrix/client/v3/login"
LOGOUT_URL = f"{HOMESERVER}/_matrix/client/v3/logout"
SHARED_SECRET_REGISTER_URL = f"{HOMESERVER}/_synapse/admin/v1/register"
BOT_ADMIN_URL = f"{HOMESERVER}/_synapse/admin/v2/users/"
LOST_KEY = Fernet.generate_key().decode()


def _store_under_lost_key(setting, value):
    """Store a secret under a key the deployment no longer has."""
    with override_settings(
        FIELD_ENCRYPTION_KEY=LOST_KEY, FIELD_ENCRYPTION_KEY_FALLBACKS=[]
    ):
        setattr(config, setting, value)


NOT_ADMIN = httpx.Response(
    403, json={"errcode": "M_FORBIDDEN", "error": "You are not a server admin."}
)
NONCE = "ILmMmY29kYp5noYTkL8yRrPacipz6BIk"

# Login flows as Tuwunel 1.9.0 lists them with login_with_password on and off.
PASSWORD_FLOWS = {
    "flows": [
        {"type": "m.login.application_service"},
        {"type": "m.login.token", "get_login_token": True},
        {"type": "m.login.password"},
    ]
}
NO_PASSWORD_FLOWS = {"flows": PASSWORD_FLOWS["flows"][:2]}
USER_IN_USE = httpx.Response(
    400,
    json={
        "errcode": "M_USER_IN_USE",
        "error": "M_USER_IN_USE: User ID is not available",
    },
)

# Replies as Tuwunel v1.9.0 words them.
REGISTERED_REPLY = "Appservice registered with ID: waldur"
DUPLICATE_ID_REPLY = (
    "Command failed with error:\n```\n"
    "Failed to register appservice: Duplicate id: waldur\n```"
)
MAKE_BOT_ADMIN_COMMAND = f"!admin users make-user-admin {BOT_USER}"


def _expected_registration():
    """What the command registers for MATRIX_CONFIG and its default --url."""
    return appservice_registration.build_registration(
        url="https://waldur.example.com",
        as_token="as-token",
        hs_token="hs-token",
        sender_localpart="waldur-bot",
        homeserver_domain=DOMAIN,
    )


def _show_config_reply(**changes):
    """Tuwunel 1.9.0's show-config reply: empty lists dropped, its own keys added."""
    shown = {**_expected_registration(), **changes}
    shown["namespaces"] = {
        kind: entries for kind, entries in shown["namespaces"].items() if entries
    }
    shown.update(
        {
            "receive_ephemeral": False,
            "io.element.msc4190": False,
            "org.matrix.msc3202": False,
        }
    )
    return (
        "Config for waldur:\n\n```yaml\n"
        f"{yaml.dump(shown, default_flow_style=False, sort_keys=False)}\n```"
    )


def _bad_status_ping(status, reason):
    """Tuwunel 1.9.0's answer when the appservice answers its ping with `status`."""
    return httpx.Response(
        502,
        json={
            "errcode": "M_BAD_STATUS",
            "status": status,
            "body": "",
            "error": f"M_BAD_STATUS: Appservice returned status {status} {reason}",
        },
    )


def _message(sender, body, event_id="", in_reply_to=""):
    content = {"body": body}
    if in_reply_to:
        # How Tuwunel 1.9.0's admin bot ties its reply to the command.
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": in_reply_to}}
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "content": content,
    }


def _sent_command_event():
    """The command Waldur itself sent — the anchor the reply scan looks for."""
    return _message(
        BOOTSTRAP_USER, "!admin appservices register", event_id=SENT_EVENT_ID
    )


def _reply_page(reply):
    """A /messages page holding the admin bot's reply to our command."""
    return httpx.Response(
        200,
        json={"chunk": [_message(f"@conduit:{DOMAIN}", reply), _sent_command_event()]},
    )


@override_config(**MATRIX_CONFIG)
class BuildRegistrationTest(TestCase):
    def test_descriptor_scopes_the_exclusive_namespace_to_the_bot(self):
        registration = appservice_registration.build_registration_from_config(
            url="https://waldur.example.com/"
        )

        self.assertEqual(registration["id"], "waldur")
        # Trailing slash stripped — the homeserver appends the transaction path.
        self.assertEqual(registration["url"], "https://waldur.example.com")
        exclusive = [
            ns for ns in registration["namespaces"]["users"] if ns["exclusive"]
        ]
        self.assertEqual(len(exclusive), 1)
        self.assertEqual(exclusive[0]["regex"], f"@waldur-bot:{DOMAIN}")

    def test_missing_tokens_are_reported_together(self):
        with override_config(
            MATRIX_APPSERVICE_AS_TOKEN="", MATRIX_APPSERVICE_HS_TOKEN=""
        ):
            with self.assertRaises(
                appservice_registration.AppserviceRegistrationError
            ) as ctx:
                appservice_registration.build_registration_from_config(url="https://x")

        # Both commands take the tokens as arguments too, so say so.
        self.assertIn(
            "pass --as-token or set MATRIX_APPSERVICE_AS_TOKEN", str(ctx.exception)
        )
        self.assertIn(
            "pass --hs-token or set MATRIX_APPSERVICE_HS_TOKEN", str(ctx.exception)
        )

    def test_undecryptable_token_is_refused(self):
        # A wrong FIELD_ENCRYPTION_KEY makes the token read as a random
        # stand-in; registering that would replace a live appservice.
        _store_under_lost_key("MATRIX_APPSERVICE_HS_TOKEN", "hs-token")

        with self.assertRaises(
            appservice_registration.AppserviceRegistrationError
        ) as ctx:
            appservice_registration.build_registration_from_config(url="https://x")

        self.assertIn(
            "MATRIX_APPSERVICE_HS_TOKEN cannot be decrypted", str(ctx.exception)
        )
        self.assertNotIn("MATRIX_APPSERVICE_AS_TOKEN", str(ctx.exception))

    def test_namespace_claiming_localpart_is_rejected(self):
        with override_config(MATRIX_APPSERVICE_SENDER_LOCALPART=".*"):
            with self.assertRaises(appservice_registration.AppserviceRegistrationError):
                appservice_registration.build_registration_from_config(url="https://x")


@override_config(**MATRIX_CONFIG)
@mock.patch.object(appservice_registration, "REPLY_POLL_INTERVAL", 0)
# Deployments set it, and without it no bootstrap admin is ever created.
@mock.patch.dict(os.environ, {"MATRIX_BOOTSTRAP_PASSWORD": "kept-password"})
class RegisterCommandTest(TestCase):
    def _call(self, *args):
        out = StringIO()
        call_command(
            "register_matrix_appservice",
            "--url",
            "https://waldur.example.com",
            *args,
            stdout=out,
        )
        return out.getvalue()

    @respx.mock
    def test_undecryptable_registration_secret_is_refused_before_any_call(self):
        _store_under_lost_key("MATRIX_USER_REGISTRATION_SECRET", "reg-secret")

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn(
            "MATRIX_USER_REGISTRATION_SECRET cannot be decrypted", str(ctx.exception)
        )
        self.assertEqual(respx.calls.call_count, 0)

    @respx.mock
    def test_undecryptable_appservice_token_is_refused_before_any_call(self):
        _store_under_lost_key("MATRIX_APPSERVICE_AS_TOKEN", "as-token")

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn(
            "MATRIX_APPSERVICE_AS_TOKEN cannot be decrypted", str(ctx.exception)
        )
        self.assertEqual(respx.calls.call_count, 0)

    def test_tokens_given_as_arguments_are_warned_about(self):
        err = StringIO()
        # An undecryptable registration secret stops the run before any
        # homeserver call; the warnings come first.
        _store_under_lost_key("MATRIX_USER_REGISTRATION_SECRET", "reg-secret")

        with self.assertRaises(CommandError):
            call_command(
                "register_matrix_appservice",
                "--url",
                "https://waldur.example.com",
                "--as-token",
                "as-token",
                "--hs-token",
                "hs-token",
                stdout=StringIO(),
                stderr=err,
            )

        self.assertIn("--as-token shows up in process listings", err.getvalue())
        self.assertIn("--hs-token shows up in process listings", err.getvalue())
        self.assertIn("MATRIX_APPSERVICE_AS_TOKEN in Constance", err.getvalue())

    @respx.mock
    def test_registration_token_given_as_argument_is_warned_about(self):
        err = StringIO()
        # The argument replaces the registration secret, so an undecryptable
        # as_token is what stops the run before any homeserver call.
        _store_under_lost_key("MATRIX_APPSERVICE_AS_TOKEN", "as-token")

        with self.assertRaises(CommandError):
            call_command(
                "register_matrix_appservice",
                "--url",
                "https://waldur.example.com",
                "--registration-token",
                "reg-secret",
                stdout=StringIO(),
                stderr=err,
            )

        self.assertIn(
            "--registration-token shows up in process listings", err.getvalue()
        )
        self.assertIn("MATRIX_USER_REGISTRATION_SECRET in Constance", err.getvalue())
        self.assertEqual(respx.calls.call_count, 0)

    def _whoami(self, request):
        if "user_id" not in request.url.params:
            # Identifying the admin behind --admin-token.
            return httpx.Response(200, json={"user_id": f"@admin:{DOMAIN}"})
        # The appservice probe. Like a real homeserver, the as_token starts
        # working once the register command has been sent.
        if (
            self.registration_takes_effect
            and self.send_route.call_count >= self.effective_after_sends
        ):
            return httpx.Response(200, json={"user_id": request.url.params["user_id"]})
        return self.unregistered_probe_response

    def _mock_homeserver(self, reply):
        # The homeserver has never seen this appservice: the idempotence probe
        # comes back M_UNKNOWN_TOKEN and enrolment proceeds.
        self.registration_takes_effect = True
        self.effective_after_sends = 1
        self.unregistered_probe_response = httpx.Response(
            401, json={"errcode": "M_UNKNOWN_TOKEN"}
        )
        respx.get(f"{HOMESERVER}/_matrix/client/v3/account/whoami").mock(
            side_effect=self._whoami
        )
        self.flows_route = respx.get(LOGIN_URL).mock(
            return_value=httpx.Response(200, json=PASSWORD_FLOWS)
        )
        self.login_route = respx.post(LOGIN_URL).mock(
            return_value=httpx.Response(403, json={"errcode": "M_FORBIDDEN"})
        )
        self.nonce_route = respx.get(SHARED_SECRET_REGISTER_URL).mock(
            return_value=httpx.Response(200, json={"nonce": NONCE})
        )
        # As Tuwunel 1.9.0 answers it: a token on a device of its choosing.
        self.register_route = respx.post(SHARED_SECRET_REGISTER_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "user_id": BOOTSTRAP_USER,
                    "home_server": DOMAIN,
                    "access_token": "bootstrap-token",
                    "device_id": "af6I0ZsmDr",
                },
            )
        )
        self.logout_route = respx.post(LOGOUT_URL).mock(
            return_value=httpx.Response(200, json={})
        )
        respx.get(
            url__startswith=f"{HOMESERVER}/_matrix/client/v3/directory/room/"
        ).mock(return_value=httpx.Response(404, json={"errcode": "M_NOT_FOUND"}))
        respx.get(f"{HOMESERVER}/_matrix/client/v3/joined_rooms").mock(
            return_value=httpx.Response(200, json={"joined_rooms": [ADMIN_ROOM]})
        )
        self.send_route = respx.put(
            url__startswith=f"{HOMESERVER}/_matrix/client/v3/rooms/"
        ).mock(return_value=httpx.Response(200, json={"event_id": SENT_EVENT_ID}))
        self._mock_messages(
            _message(f"@conduit:{DOMAIN}", reply), _sent_command_event()
        )
        self.ping_route = respx.post(PING_URL).mock(
            return_value=httpx.Response(200, json={"duration_ms": 3})
        )
        # The bot is a homeserver admin already unless a test says otherwise.
        self.bot_admin_route = respx.get(url__startswith=BOT_ADMIN_URL).mock(
            return_value=httpx.Response(200, json={"name": BOT_USER, "admin": True})
        )
        # As Tuwunel 1.9.0 answers the grant: the account as it now stands.
        self.grant_route = respx.put(url__startswith=BOT_ADMIN_URL).mock(
            return_value=httpx.Response(200, json={"name": BOT_USER, "admin": True})
        )

    def _replies(self, *replies):
        """One admin bot reply per command sent, in order."""
        respx.get(url__regex=MESSAGES_URL).mock(
            side_effect=[_reply_page(reply) for reply in replies]
        )

    def _sent_commands(self):
        return [
            json.loads(call.request.content)["body"].split("\n", 1)[0]
            for call in self.send_route.calls
        ]

    def _mock_messages(self, *chunk):
        """Mock /messages with a dir=b (newest first) chunk."""
        return respx.get(url__regex=MESSAGES_URL).mock(
            return_value=httpx.Response(200, json={"chunk": list(chunk)})
        )

    def _capture_unexpected_error(self, scope):
        try:
            self._call()
        except ValueError:
            scope.capture_exception()

    def _sentry_event_of_unexpected_error(self):
        events = _send(self._capture_unexpected_error)
        for value in events[0]["exception"]["values"]:
            for frame in value["stacktrace"]["frames"]:
                # Source lines, which quote this module's test secrets.
                for key in ("pre_context", "context_line", "post_context"):
                    frame.pop(key, None)
        return json.dumps(events)

    def test_an_unexpected_error_sends_no_secret_to_sentry(self):
        # Only AppserviceRegistrationError becomes a CommandError. Anything
        # else escapes the command, and Sentry sends the locals of every frame
        # on its way, the registration descriptor and the admin room included.
        tokens = dict(
            MATRIX_APPSERVICE_AS_TOKEN="as-token-4f9c2b7e1d",
            MATRIX_APPSERVICE_HS_TOKEN="hs-token-8d1a6e3f0c",
        )
        secret_values = (
            *tokens.values(),
            "reg-secret",
            "kept-password",
            "bootstrap-token",
            "homeserver-admin-token",
        )
        for env in ({}, {"MATRIX_ADMIN_TOKEN": "homeserver-admin-token"}):
            with (
                self.subTest(env=env),
                respx.mock,
                override_config(**tokens),
                mock.patch.dict(os.environ, env),
            ):
                self._mock_homeserver(REGISTERED_REPLY)
                self.send_route.mock(side_effect=ValueError("unexpected"))

                sent = self._sentry_event_of_unexpected_error()

                self.assertIn("unexpected", sent)
                for secret in secret_values:
                    self.assertNotIn(secret, sent)

    @respx.mock
    def test_successful_registration_reports_success(self):
        self._mock_homeserver("Appservice registered with ID: waldur")

        output = self._call()

        self.assertIn("registered on the homeserver", output)
        sent_body = self.send_route.calls.last.request.content.decode()
        self.assertIn("!admin appservices register", sent_body)
        self.assertIn("as_token", sent_body)

    @respx.mock
    def test_messages_older_than_our_command_are_never_read_as_the_reply(self):
        # A fresh admin room already holds the homeserver's welcome message.
        # Reading it while the real reply is still in flight would report a
        # bogus failure, so the scan is anchored on our own command event.
        self._mock_homeserver("Appservice registered with ID: waldur")
        welcome = _message(f"@conduit:{DOMAIN}", "Welcome to the admin room.")
        respx.get(url__regex=MESSAGES_URL).mock(
            side_effect=[
                # First poll: the reply has not landed yet.
                httpx.Response(200, json={"chunk": [_sent_command_event(), welcome]}),
                httpx.Response(
                    200,
                    json={
                        "chunk": [
                            _message(
                                f"@conduit:{DOMAIN}",
                                "Appservice registered with ID: waldur",
                            ),
                            _sent_command_event(),
                            welcome,
                        ]
                    },
                ),
            ]
        )

        self.assertIn("registered on the homeserver", self._call())

    @respx.mock
    def test_a_reply_to_another_command_is_not_taken_for_ours(self):
        # Two runs at once, as when Helm retries a Job while the first attempt
        # is still waiting: the other run's reply can land between our command
        # and our reply. The admin bot marks which command it answers.
        self._mock_homeserver(REGISTERED_REPLY)
        bot = f"@conduit:{DOMAIN}"
        theirs = _message(
            bot,
            "Command failed with error:\n```\nsomething else\n```",
            in_reply_to="$theirs",
        )
        ours = _message(bot, REGISTERED_REPLY, in_reply_to=SENT_EVENT_ID)
        respx.get(url__regex=MESSAGES_URL).mock(
            side_effect=[
                httpx.Response(200, json={"chunk": [theirs, _sent_command_event()]}),
                httpx.Response(
                    200, json={"chunk": [ours, theirs, _sent_command_event()]}
                ),
            ]
        )

        self.assertIn("registered on the homeserver", self._call())

    @respx.mock
    def test_our_own_command_echo_is_not_mistaken_for_the_reply(self):
        # Only events from a sender other than us may be read as the answer.
        self._mock_homeserver("Appservice registered with ID: waldur")
        respx.get(url__regex=MESSAGES_URL).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "chunk": [
                            _message(BOOTSTRAP_USER, "a follow-up we typed"),
                            _sent_command_event(),
                        ]
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "chunk": [
                            _message(
                                f"@conduit:{DOMAIN}",
                                "Appservice registered with ID: waldur",
                            ),
                            _message(BOOTSTRAP_USER, "a follow-up we typed"),
                            _sent_command_event(),
                        ]
                    },
                ),
            ]
        )

        self.assertIn("registered on the homeserver", self._call())

    @respx.mock
    def test_rerun_against_an_already_registered_appservice_is_not_an_error(self):
        # Another run registered these very tokens in the meantime: the token
        # works by the time the reply arrives, so nothing is replaced.
        self._mock_homeserver(DUPLICATE_ID_REPLY)

        output = self._call()

        self.assertIn("already registered", output)
        self.assertEqual(len(self.send_route.calls), 1)

    @respx.mock
    def test_a_registration_under_other_tokens_is_replaced(self):
        # Tuwunel keeps the old tokens when the id is registered again, so a
        # rotation only takes effect after an unregister. The deployment owns
        # the tokens, so its copy wins.
        self._mock_homeserver("unused")
        self.effective_after_sends = 3
        self._replies(DUPLICATE_ID_REPLY, "Appservice unregistered.", REGISTERED_REPLY)

        output = self._call()

        self.assertIn("replaced", output)
        self.assertEqual(
            self._sent_commands(),
            [
                "!admin appservices register",
                "!admin appservices unregister waldur",
                "!admin appservices register",
            ],
        )

    @respx.mock
    def test_a_replacement_the_homeserver_still_rejects_fails_loudly(self):
        self._mock_homeserver("unused")
        self.registration_takes_effect = False
        self._replies(DUPLICATE_ID_REPLY, "Appservice unregistered.", REGISTERED_REPLY)

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn("rejects the as_token", str(ctx.exception))

    def test_a_lost_connection_is_reported_with_the_step_it_broke(self):
        refused = httpx.ConnectError("Connection refused")
        whoami = f"{HOMESERVER}/_matrix/client/v3/account/whoami"

        def admin_whoami_fails(request):
            if "user_id" in request.url.params:
                return self._whoami(request)
            raise refused

        cases = [
            (
                "Could not create the bootstrap admin",
                lambda: self.nonce_route.mock(side_effect=refused),
                (),
                {},
            ),
            (
                "Could not create the bootstrap admin",
                lambda: self.register_route.mock(side_effect=refused),
                (),
                {},
            ),
            (
                "Bootstrap user login failed",
                lambda: (
                    self.register_route.mock(return_value=USER_IN_USE),
                    self.login_route.mock(side_effect=refused),
                ),
                (),
                {},
            ),
            (
                "Could not identify the admin user",
                lambda: respx.get(whoami).mock(side_effect=admin_whoami_fails),
                ("--admin-token", "admin-token"),
                {},
            ),
            (
                "Could not resolve the admin room",
                lambda: respx.get(
                    url__startswith=f"{HOMESERVER}/_matrix/client/v3/directory/room/"
                ).mock(side_effect=refused),
                (),
                {},
            ),
            (
                "Could not list joined rooms",
                lambda: respx.get(f"{HOMESERVER}/_matrix/client/v3/joined_rooms").mock(
                    side_effect=refused
                ),
                (),
                {},
            ),
            (
                "Could not send the admin-room command",
                lambda: self.send_route.mock(side_effect=refused),
                (),
                {},
            ),
            (
                # The command reached the homeserver; only the reply is unknown.
                "may or may not have taken effect",
                lambda: respx.get(url__regex=MESSAGES_URL).mock(side_effect=refused),
                (),
                {},
            ),
        ]
        for expected, break_step, args, env in cases:
            with (
                self.subTest(expected),
                respx.mock,
                mock.patch.dict(os.environ, env),
            ):
                self._mock_homeserver(REGISTERED_REPLY)
                break_step()

                with self.assertRaises(CommandError) as ctx:
                    self._call(*args)

                self.assertIn(expected, str(ctx.exception))
                self.assertIn("Connection refused", str(ctx.exception))

    @respx.mock
    def test_a_lost_connection_during_unregister_says_it_may_be_gone(self):
        self._mock_homeserver("unused")
        self.registration_takes_effect = False
        respx.get(url__regex=MESSAGES_URL).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "chunk": [
                            _message(f"@conduit:{DOMAIN}", DUPLICATE_ID_REPLY),
                            _sent_command_event(),
                        ]
                    },
                ),
                httpx.ReadTimeout("timed out"),
            ]
        )

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn("may already be removed", str(ctx.exception))
        self.assertIn("re-running the command registers", str(ctx.exception).lower())

    @respx.mock
    def test_a_lost_connection_after_unregister_says_the_registration_is_gone(self):
        # Between the unregister and the new registration the homeserver holds
        # no registration at all, so the operator must know a re-run is due.
        self._mock_homeserver("unused")
        self.registration_takes_effect = False
        self._replies(DUPLICATE_ID_REPLY, "Appservice unregistered.")
        self.send_route.mock(
            side_effect=[
                httpx.Response(200, json={"event_id": SENT_EVENT_ID}),
                httpx.Response(200, json={"event_id": SENT_EVENT_ID}),
                httpx.ConnectError("Connection refused"),
            ]
        )

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn("old registration was removed", str(ctx.exception))
        self.assertIn("re-running the command registers", str(ctx.exception).lower())

    @respx.mock
    def test_a_rejected_registration_after_unregister_says_the_registration_is_gone(
        self,
    ):
        self._mock_homeserver("unused")
        self.registration_takes_effect = False
        self._replies(
            DUPLICATE_ID_REPLY,
            "Appservice unregistered.",
            "Command failed with error:\n```\n"
            "Failed to register appservice: invalid url\n```",
        )

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn("old registration was removed", str(ctx.exception))
        self.assertIn("invalid url", str(ctx.exception))

    @respx.mock
    def test_a_refused_unregister_fails_loudly(self):
        self._mock_homeserver("unused")
        self.registration_takes_effect = False
        self._replies(DUPLICATE_ID_REPLY, "You do not have permission to do that.")

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn("permission", str(ctx.exception))
        self.assertEqual(len(self.send_route.calls), 2)

    @respx.mock
    def test_a_fresh_homeserver_gets_its_bootstrap_admin_from_the_shared_secret_api(
        self,
    ):
        # Nothing relies on the first account being made admin: the shared-
        # secret API creates an admin on a homeserver that already has users,
        # and is not subject to forbidden_usernames.
        self._mock_homeserver(REGISTERED_REPLY)

        self.assertIn("registered on the homeserver", self._call())

        body = json.loads(self.register_route.calls.last.request.content)
        user = appservice_registration.BOOTSTRAP_LOCALPART
        self.assertEqual(body["nonce"], NONCE)
        self.assertEqual(body["username"], user)
        self.assertEqual(body["password"], "kept-password")
        self.assertIs(body["admin"], True)
        # Keyed with the registration token, which the packagers also set as
        # the homeserver's registration_shared_secret.
        expected_mac = hmac.new(
            b"reg-secret",
            b"\0".join(x.encode() for x in (NONCE, user, "kept-password", "admin")),
            hashlib.sha1,
        ).hexdigest()
        self.assertEqual(body["mac"], expected_mac)
        self.assertEqual(
            self.send_route.calls.last.request.headers["Authorization"],
            "Bearer bootstrap-token",
        )
        self.assertFalse(self.login_route.called)

    @respx.mock
    def test_a_fresh_install_does_not_need_password_login(self):
        # The registration answers with a token, so a homeserver with
        # login_with_password = false can be set up from scratch.
        self._mock_homeserver(REGISTERED_REPLY)
        self.flows_route.mock(return_value=httpx.Response(200, json=NO_PASSWORD_FLOWS))

        self.assertIn("registered on the homeserver", self._call())
        self.assertFalse(self.login_route.called)

    @respx.mock
    def test_an_existing_bootstrap_admin_is_signed_in_with_the_kept_password(self):
        # After the first install, and when a concurrent first install got
        # there first, the bootstrap admin exists and the registration fails.
        self._mock_homeserver(REGISTERED_REPLY)
        self.register_route.mock(return_value=USER_IN_USE)
        self.login_route.mock(
            return_value=httpx.Response(
                200, json={"access_token": "login-token", "user_id": BOOTSTRAP_USER}
            )
        )

        self.assertIn("registered on the homeserver", self._call())
        self.assertEqual(
            json.loads(self.login_route.calls.last.request.content)["password"],
            "kept-password",
        )
        self.assertEqual(
            self.send_route.calls.last.request.headers["Authorization"],
            "Bearer login-token",
        )

    @respx.mock
    def test_every_bootstrap_login_gets_a_device_of_its_own(self):
        # Signing out removes the device and every token on it, so runs that
        # shared one would cut each other off.
        self._mock_homeserver(REGISTERED_REPLY)
        self.login_route.mock(
            return_value=httpx.Response(
                200, json={"access_token": "login-token", "user_id": BOOTSTRAP_USER}
            )
        )

        with httpx.Client(base_url=HOMESERVER) as client:
            for _ in range(2):
                appservice_registration.login_bootstrap_user(
                    client, "waldur-bootstrap", "kept-password"
                )

        devices = [
            json.loads(call.request.content)["device_id"]
            for call in self.login_route.calls
        ]
        self.assertEqual(len(set(devices)), 2)
        for device in devices:
            self.assertRegex(device, r"^WALDUR-BOOTSTRAP-[0-9A-F]{8}$")

    @respx.mock
    @mock.patch.dict(os.environ, {"MATRIX_BOOTSTRAP_PASSWORD": "wrong-password"})
    def test_a_rejected_bootstrap_password_fails_loudly(self):
        self._mock_homeserver(REGISTERED_REPLY)
        self.register_route.mock(return_value=USER_IN_USE)

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn("rejects MATRIX_BOOTSTRAP_PASSWORD", str(ctx.exception))
        self.assertIn("MATRIX_ADMIN_TOKEN", str(ctx.exception))
        self.assertFalse(self.send_route.called)

    @respx.mock
    def test_a_disabled_password_login_asks_for_an_admin_token(self):
        # login_with_password = false, the setting recommended with single
        # sign-on. Blaming the password would send the operator the wrong way.
        self._mock_homeserver(REGISTERED_REPLY)
        self.register_route.mock(return_value=USER_IN_USE)
        self.flows_route.mock(return_value=httpx.Response(200, json=NO_PASSWORD_FLOWS))

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn(
            "Password login is disabled on the homeserver", str(ctx.exception)
        )
        self.assertIn("MATRIX_ADMIN_TOKEN", str(ctx.exception))
        self.assertNotIn("MATRIX_BOOTSTRAP_PASSWORD", str(ctx.exception))
        # Tuwunel 1.9.0 echoes a refused login's body, password included.
        self.assertFalse(self.login_route.called)

    @respx.mock
    def test_a_refused_login_is_reported_without_the_homeservers_text(self):
        self._mock_homeserver(REGISTERED_REPLY)
        self.register_route.mock(return_value=USER_IN_USE)
        # What Tuwunel 1.9.0 answers a password login it does not accept.
        self.login_route.mock(
            return_value=httpx.Response(
                400,
                json={
                    "errcode": "M_UNKNOWN",
                    "error": "M_UNKNOWN: Invalid or unsupported login type "
                    'body.json_body=Some(Object({"password": String("kept-password")}))',
                },
            )
        )

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn("HTTP 400 M_UNKNOWN", str(ctx.exception))
        self.assertNotIn("kept-password", str(ctx.exception))

    @respx.mock
    def test_a_homeserver_without_shared_secret_registration_says_what_to_set(self):
        self._mock_homeserver(REGISTERED_REPLY)
        self.nonce_route.mock(
            return_value=httpx.Response(
                400,
                json={
                    "errcode": "M_UNKNOWN",
                    "error": "M_UNKNOWN: Shared-secret registration is not enabled",
                },
            )
        )

        with self.assertRaises(CommandError) as ctx:
            self._call()

        message = str(ctx.exception)
        self.assertIn("Shared-secret registration is not enabled", message)
        self.assertIn("registration_shared_secret", message)
        self.assertIn("MATRIX_USER_REGISTRATION_SECRET", message)
        self.assertFalse(self.register_route.called)
        self.assertFalse(self.send_route.called)

    @respx.mock
    def test_a_shared_secret_that_differs_says_what_to_set(self):
        self._mock_homeserver(REGISTERED_REPLY)
        self.register_route.mock(
            return_value=httpx.Response(
                403,
                json={
                    "errcode": "M_FORBIDDEN",
                    "error": "M_FORBIDDEN: HMAC check failed",
                },
            )
        )

        with self.assertRaises(CommandError) as ctx:
            self._call()

        message = str(ctx.exception)
        self.assertIn("HTTP 403 M_FORBIDDEN", message)
        self.assertIn("registration_shared_secret", message)
        self.assertIn("MATRIX_USER_REGISTRATION_SECRET", message)
        self.assertFalse(self.login_route.called)

    @respx.mock
    def test_the_bootstrap_session_is_signed_out(self):
        # Its token never expires and holds homeserver admin rights.
        self._mock_homeserver(REGISTERED_REPLY)

        self._call()

        self.assertEqual(self.logout_route.call_count, 1)
        self.assertEqual(
            self.logout_route.calls.last.request.headers["Authorization"],
            "Bearer bootstrap-token",
        )

    @respx.mock
    def test_the_bootstrap_session_is_signed_out_when_registering_fails(self):
        self._mock_homeserver("Command failed with error: something else")

        with self.assertRaises(CommandError):
            self._call()

        self.assertEqual(self.logout_route.call_count, 1)

    @respx.mock
    def test_a_signed_in_bootstrap_session_is_signed_out_too(self):
        self._mock_homeserver(REGISTERED_REPLY)
        self.register_route.mock(return_value=USER_IN_USE)
        self.login_route.mock(
            return_value=httpx.Response(
                200, json={"access_token": "login-token", "user_id": BOOTSTRAP_USER}
            )
        )

        self._call()

        self.assertEqual(
            self.logout_route.calls.last.request.headers["Authorization"],
            "Bearer login-token",
        )

    @respx.mock
    def test_an_admin_token_is_never_signed_out(self):
        # The caller owns it, and may still need it.
        self._mock_homeserver(REGISTERED_REPLY)

        self._call("--admin-token", "admin-token")

        self.assertFalse(self.logout_route.called)

    @respx.mock
    def test_a_failed_sign_out_is_a_warning_not_an_error(self):
        self._mock_homeserver(REGISTERED_REPLY)
        self.logout_route.mock(side_effect=httpx.ConnectError("Connection refused"))

        with self.assertLogs(appservice_registration.logger, "WARNING") as logs:
            output = self._call()

        self.assertIn("registered on the homeserver", output)
        self.assertIn("could not sign the bootstrap admin out", "\n".join(logs.output))

    @respx.mock
    def test_a_session_the_homeserver_already_ended_is_not_reported(self):
        # Every login reuses one device, so a second login in the same run
        # ends the first session before it is signed out.
        self._mock_homeserver(REGISTERED_REPLY)
        self.logout_route.mock(
            return_value=httpx.Response(401, json={"errcode": "M_UNKNOWN_TOKEN"})
        )

        with self.assertNoLogs(appservice_registration.logger, "WARNING"):
            self._call()

    @respx.mock
    def test_a_registration_that_does_not_take_effect_fails_loudly(self):
        self._mock_homeserver("Appservice registered with ID: waldur")
        self.registration_takes_effect = False

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn("rejects the as_token", str(ctx.exception))

    @respx.mock
    def test_the_sent_registration_claims_the_room_alias_namespace(self):
        # The descriptor moved into this module from the Setup endpoint, whose
        # copy had to be fixed to claim aliases. Without the claim every room
        # is created with no alias and no "Open in Matrix client" link.
        self._mock_homeserver("Appservice registered with ID: waldur")

        self._call()

        body = json.loads(self.send_route.calls.last.request.content)["body"]
        descriptor = yaml.safe_load(body.split("```yaml\n", 1)[1].rsplit("```", 1)[0])
        self.assertEqual(
            descriptor["namespaces"]["aliases"],
            [
                {
                    "exclusive": True,
                    "regex": f"#{models.ROOM_ALIAS_PREFIX}[^:]+:{re.escape(DOMAIN)}",
                }
            ],
        )

    @respx.mock
    def test_registration_ends_with_a_ping(self):
        self._mock_homeserver(REGISTERED_REPLY)

        output = self._call()

        self.assertTrue(self.ping_route.called)
        self.assertNotIn("could not reach Waldur", output)

    @respx.mock
    def test_a_failed_ping_is_a_warning_not_an_error(self):
        # On a fresh install the API may still be starting when the
        # registration runs, so an unanswered ping cannot fail it.
        self._mock_homeserver(REGISTERED_REPLY)
        self.ping_route.mock(
            return_value=httpx.Response(
                502, json={"errcode": "M_CONNECTION_FAILED", "error": "refused"}
            )
        )

        output = self._call()

        self.assertIn("registered on the homeserver", output)
        self.assertIn("could not reach Waldur", output)
        self.assertIn("M_CONNECTION_FAILED", output)

    @respx.mock
    def test_a_timed_out_ping_is_a_warning_that_names_the_likely_cause(self):
        self._mock_homeserver(REGISTERED_REPLY)
        self.ping_route.mock(side_effect=httpx.ReadTimeout("timed out"))

        output = self._call()

        self.assertIn("registered on the homeserver", output)
        self.assertIn("may still be trying to reach Waldur", output)

    @respx.mock
    def test_tokens_echoed_in_a_rejection_are_not_repeated(self):
        self._mock_homeserver(
            "Invalid appservice registration:\nas_token: echoed-as\nhs_token: echoed-hs"
        )

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertNotIn("echoed-as", str(ctx.exception))
        self.assertNotIn("echoed-hs", str(ctx.exception))

    def test_success_is_read_only_for_waldurs_own_id(self):
        cases = [
            ("Appservice registered with ID: waldur", True),
            ("Appservice registered with ID: waldur.", True),
            ("Appservice registered with ID: `waldur`", True),
            ("Appservice registered with ID: waldur-staging", False),
            ("Appservice registered with ID: waldur_old", False),
            ("Appservice registered with ID: waldur.example", False),
        ]
        for reply, success in cases:
            with self.subTest(reply), respx.mock:
                self._mock_homeserver(reply)

                if success:
                    self.assertIn("registered on the homeserver", self._call())
                else:
                    with self.assertRaises(CommandError):
                        self._call()

    @respx.mock
    def test_unrecognised_reply_fails_loudly(self):
        # An HTTP 200 on the send says nothing — the admin bot reports failure
        # as an ordinary message, so the reply must be classified.
        self._mock_homeserver("Invalid appservice registration: bad yaml")

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn("bad yaml", str(ctx.exception))

    @respx.mock
    @mock.patch.dict(os.environ, {"MATRIX_BOOTSTRAP_PASSWORD": ""})
    def test_a_live_appservice_short_circuits_the_whole_enrolment(self):
        # Both deployment paths re-run on every upgrade, possibly without any
        # admin credentials. If the as_token already works and Waldur answers
        # the ping, nothing needs admin rights.
        respx.get(f"{HOMESERVER}/_matrix/client/v3/account/whoami").mock(
            return_value=httpx.Response(200, json={"user_id": f"@waldur-bot:{DOMAIN}"})
        )
        register_route = respx.post(SHARED_SECRET_REGISTER_URL)
        respx.get(url__startswith=BOT_ADMIN_URL).mock(
            return_value=httpx.Response(200, json={"name": BOT_USER, "admin": True})
        )
        # Every deploy still pings, which checks the hs_token and the URL.
        ping_route = respx.post(PING_URL).mock(
            return_value=httpx.Response(200, json={"duration_ms": 2})
        )

        output = self._call()

        self.assertIn("already registered", output)
        self.assertFalse(register_route.called)
        self.assertEqual(ping_route.call_count, 1)

    def _mock_live_homeserver(self, *replies):
        """A homeserver that accepts the as_token, with the bootstrap admin able to log in."""
        self._mock_homeserver("unused")
        self.effective_after_sends = 0
        self.login_route = respx.post(f"{HOMESERVER}/_matrix/client/v3/login").mock(
            return_value=httpx.Response(
                200, json={"access_token": "bootstrap-token", "user_id": BOOTSTRAP_USER}
            )
        )
        self._replies(*replies)

    @respx.mock
    def test_a_live_registration_that_matches_is_left_alone(self):
        self._mock_live_homeserver(_show_config_reply())

        output = self._call()

        self.assertIn("already registered", output)
        self.assertNotIn("could not compare", output)
        self.assertEqual(
            self._sent_commands(), ["!admin appservices show-config waldur"]
        )
        self.assertFalse(self.grant_route.called)

    def test_a_live_registration_that_differs_is_replaced(self):
        # The as_token alone says nothing about the rest: a registration
        # pasted by hand before the alias namespace existed, or one made for
        # another URL, works for the bot but not for what Waldur needs now.
        namespaces = _expected_registration()["namespaces"]
        cases = {
            "namespaces.aliases": {"namespaces": {"users": namespaces["users"]}},
            "url": {"url": "https://old-waldur.example.com"},
            "hs_token": {"hs_token": "old-hs-token"},
        }
        for field, changes in cases.items():
            with self.subTest(field), respx.mock:
                self._mock_live_homeserver(
                    _show_config_reply(**changes),
                    "Appservice unregistered.",
                    REGISTERED_REPLY,
                )

                output = self._call()

                self.assertIn("replaced", output)
                self.assertEqual(
                    self._sent_commands(),
                    [
                        "!admin appservices show-config waldur",
                        "!admin appservices unregister waldur",
                        "!admin appservices register",
                    ],
                )

    @respx.mock
    def test_a_matching_registration_waldur_turns_away_is_not_replaced(self):
        # Registering the very same descriptor again cannot fix it, so the
        # command stops instead of dropping events for nothing.
        self._mock_live_homeserver(_show_config_reply())
        self.ping_route.mock(return_value=_bad_status_ping(403, "Forbidden"))

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn("matches", str(ctx.exception))
        self.assertIn("https://waldur.example.com", str(ctx.exception))
        self.assertEqual(
            self._sent_commands(), ["!admin appservices show-config waldur"]
        )

    def test_a_live_registration_that_cannot_be_compared_is_left_alone(self):
        # The registration works for the bot; not being able to check the rest
        # is worth a warning, not a failed deploy, and no bootstrap admin is
        # created just to look.
        def not_in_admin_room():
            respx.get(
                url__startswith=f"{HOMESERVER}/_matrix/client/v3/directory/room/"
            ).mock(return_value=httpx.Response(200, json={"room_id": ADMIN_ROOM}))
            respx.get(f"{HOMESERVER}/_matrix/client/v3/joined_rooms").mock(
                return_value=httpx.Response(200, json={"joined_rooms": []})
            )

        def login_fails():
            self.login_route.mock(
                return_value=httpx.Response(403, json={"errcode": "M_FORBIDDEN"})
            )

        def password_login_disabled():
            self.flows_route.mock(
                return_value=httpx.Response(200, json=NO_PASSWORD_FLOWS)
            )

        cases = [
            ("MATRIX_ADMIN_TOKEN", {"MATRIX_BOOTSTRAP_PASSWORD": ""}, lambda: None),
            ("could not log in", {}, login_fails),
            ("Password login is disabled", {}, password_login_disabled),
            ("is not in the admin room", {}, not_in_admin_room),
            ("Appservice does not exist", {}, lambda: None),
        ]
        for reason, env, arrange in cases:
            with (
                self.subTest(reason),
                respx.mock,
                mock.patch.dict(os.environ, env),
            ):
                self._mock_live_homeserver(
                    "Command failed with error:\n```\nAppservice does not exist.\n```"
                )
                arrange()

                output = self._call()

                self.assertIn("already registered", output)
                self.assertIn("could not compare it with the expected", output)
                self.assertIn(reason, output)
                self.assertFalse(self.register_route.called)
                self.assertNotIn(
                    "!admin appservices unregister waldur", self._sent_commands()
                )

    @respx.mock
    def test_a_fresh_registration_makes_the_bot_a_homeserver_admin(self):
        # Locking the accounts of deactivated users and generating passwords
        # go through the admin API, which answers the bot only once it is an
        # admin, and this run is the one holding an admin session.
        self._mock_homeserver(REGISTERED_REPLY)
        self.bot_admin_route.mock(return_value=NOT_ADMIN)

        output = self._call()

        self.assertIn(f"Made {BOT_USER} a homeserver admin", output)
        self.assertNotIn("is not a homeserver admin", output)
        self.assertEqual(self._sent_commands(), ["!admin appservices register"])
        self.assertEqual(
            self.bot_admin_route.calls.last.request.headers["Authorization"],
            "Bearer as-token",
        )
        grant = self.grant_route.calls.last.request
        self.assertEqual(
            grant.url.raw_path.decode(),
            f"/_synapse/admin/v2/users/%40waldur-bot%3A{DOMAIN}",
        )
        # The bot cannot make itself an admin; the admin of this run can.
        self.assertEqual(grant.headers["Authorization"], "Bearer bootstrap-token")
        self.assertEqual(json.loads(grant.content), {"admin": True})
        # While that admin's token still works.
        calls = [(call.request.method, call.request.url.path) for call in respx.calls]
        self.assertLess(
            calls.index(("PUT", f"/_synapse/admin/v2/users/{BOT_USER}")),
            calls.index(("POST", "/_matrix/client/v3/logout")),
        )

    def test_a_probe_that_says_nothing_does_not_stop_the_grant(self):
        # A proxy that blocks the admin API, or answers it with its own page,
        # says nothing about the bot. Only Tuwunel's own answers do.
        cases = {
            "no admin API": httpx.Response(404, text="<html>Not Found</html>"),
            "a proxy's page": httpx.Response(200, text="<html>OK</html>"),
            "not an admin": httpx.Response(
                200, json={"name": BOT_USER, "admin": False}
            ),
            "unreachable": httpx.ConnectError("connection refused"),
        }
        for case, answer in cases.items():
            with self.subTest(case), respx.mock:
                self._mock_homeserver(REGISTERED_REPLY)
                self.bot_admin_route.mock(side_effect=[answer])

                output = self._call()

                self.assertEqual(self.grant_route.call_count, 1)
                self.assertIn(f"Made {BOT_USER} a homeserver admin", output)

    def test_an_already_registered_appservice_gets_its_bot_made_admin(self):
        # Sites registered before the command did this, and runs whose grant
        # failed, get it on the next deploy.
        cases = {
            "identical": _show_config_reply(),
            "could not compare": (
                "Command failed with error:\n```\nAppservice does not exist.\n```"
            ),
        }
        for case, show_config in cases.items():
            with self.subTest(case), respx.mock:
                self._mock_live_homeserver(show_config)
                self.bot_admin_route.mock(return_value=NOT_ADMIN)

                output = self._call()

                self.assertIn("already registered", output)
                self.assertIn(f"Made {BOT_USER} a homeserver admin", output)
                self.assertEqual(self.grant_route.call_count, 1)

    def test_a_failed_grant_is_a_warning_naming_the_manual_command(self):
        # Chat works without it, so the deploy goes on, but the operator has
        # to finish the job by hand.
        cases = {
            "refused": (
                NOT_ADMIN,
                f"{BOOTSTRAP_USER} could not make it one: "
                "HTTP 403 M_FORBIDDEN You are not a server admin",
            ),
            "not granted": (
                httpx.Response(200, json={"name": BOT_USER, "admin": False}),
                "could not make it one: HTTP 200",
            ),
            "no admin API": (
                httpx.Response(404, text="<html>Not Found</html>"),
                "HTTP 404",
            ),
            "a proxy's page": (httpx.Response(200, text="<html>OK</html>"), "HTTP 200"),
            "unreachable": (
                httpx.ConnectError("connection refused"),
                "could not reach the homeserver",
            ),
        }
        for case, (answer, detail) in cases.items():
            with self.subTest(case), respx.mock:
                self._mock_homeserver(REGISTERED_REPLY)
                self.bot_admin_route.mock(return_value=NOT_ADMIN)
                self.grant_route.mock(side_effect=[answer])

                output = self._call()

                self.assertIn("registered on the homeserver", output)
                self.assertIn(f"{BOT_USER} is not a homeserver admin", output)
                self.assertIn(detail, output)
                self.assertIn(MAKE_BOT_ADMIN_COMMAND, output)
                self.assertNotIn("..", output)

    @respx.mock
    @mock.patch.dict(os.environ, {"MATRIX_BOOTSTRAP_PASSWORD": ""})
    def test_a_bot_that_is_not_admin_is_reported_when_no_admin_can_sign_in(self):
        self._mock_live_homeserver(_show_config_reply())
        self.bot_admin_route.mock(return_value=NOT_ADMIN)

        output = self._call()

        self.assertIn("already registered", output)
        self.assertFalse(self.grant_route.called)
        self.assertIn(f"{BOT_USER} is not a homeserver admin", output)
        self.assertIn(MAKE_BOT_ADMIN_COMMAND, output)

    def test_a_live_token_whose_calls_waldur_turns_away_is_replaced(self):
        # The as_token works, so nothing else would notice that the homeserver
        # holds an old hs_token, or a URL that does not lead to Waldur: its
        # calls are refused and every event is lost.
        for status, reason in ((403, "Forbidden"), (404, "Not Found")):
            with self.subTest(status=status), respx.mock:
                self._mock_homeserver("unused")
                self.effective_after_sends = 0
                self.ping_route.mock(
                    side_effect=[
                        _bad_status_ping(status, reason),
                        httpx.Response(200, json={"duration_ms": 2}),
                    ]
                )
                self._replies("Appservice unregistered.", REGISTERED_REPLY)

                output = self._call()

                self.assertIn("replaced", output)
                self.assertEqual(
                    self._sent_commands(),
                    [
                        "!admin appservices unregister waldur",
                        "!admin appservices register",
                    ],
                )
                self.assertEqual(self.ping_route.call_count, 2)
                self.assertNotIn("could not reach Waldur", output)

    def test_a_live_token_is_left_alone_when_waldur_cannot_answer(self):
        # A 5xx is Waldur or its ingress being down, as while a deploy rolls
        # the API pods; a new registration cannot fix that and would drop the
        # events sent while it is replaced.
        for answer in (
            _bad_status_ping(503, "Service Unavailable"),
            httpx.Response(
                502,
                json={
                    "errcode": "M_CONNECTION_FAILED",
                    "error": "M_CONNECTION_FAILED: Could not send request to "
                    "appservice",
                },
            ),
            httpx.Response(
                504,
                json={
                    "errcode": "M_CONNECTION_TIMEOUT",
                    "error": "M_CONNECTION_TIMEOUT: Connection to appservice timed out",
                },
            ),
            httpx.ReadTimeout("timed out"),
        ):
            with self.subTest(answer=answer), respx.mock:
                self._mock_homeserver("unused")
                self.effective_after_sends = 0
                self.ping_route.mock(side_effect=[answer])

                output = self._call()

                self.assertIn("already registered", output)
                self.assertIn("could not reach Waldur", output)
                self.assertFalse(self.send_route.called)

    @respx.mock
    @mock.patch.dict(os.environ, {"MATRIX_BOOTSTRAP_PASSWORD": ""})
    def test_a_drift_without_admin_rights_says_why_they_are_needed(self):
        # Re-runs against a live appservice never needed admin rights before,
        # so a deployment may well have none to offer.
        self._mock_homeserver("unused")
        self.effective_after_sends = 0
        self.ping_route.mock(return_value=_bad_status_ping(403, "Forbidden"))

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn("403 Forbidden", str(ctx.exception))
        self.assertIn("MATRIX_ADMIN_TOKEN", str(ctx.exception))

    @respx.mock
    def test_a_replacement_that_waldur_still_turns_away_fails_loudly(self):
        # Re-registering changed nothing, so the URL Waldur was given does not
        # lead to it, or the hs_token it registered is not the one it checks.
        # Another run would only replace the registration again.
        self._mock_homeserver("unused")
        self.effective_after_sends = 0
        self.ping_route.mock(return_value=_bad_status_ping(403, "Forbidden"))
        self._replies("Appservice unregistered.", REGISTERED_REPLY)

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn("still", str(ctx.exception))
        self.assertIn("https://waldur.example.com", str(ctx.exception))
        self.assertIn("hs_token", str(ctx.exception))

    @respx.mock
    def test_a_token_the_homeserver_rejects_does_not_count_as_live(self):
        # A 200 for somebody *else* means the token is not this bot's.
        self._mock_homeserver("Appservice registered with ID: waldur")
        self.unregistered_probe_response = httpx.Response(
            200, json={"user_id": f"@someone:{DOMAIN}"}
        )

        self.assertIn("registered on the homeserver", self._call())

    @respx.mock
    def test_a_token_outside_the_bots_namespace_is_replaced(self):
        # Tuwunel 1.9.0 answers M_EXCLUSIVE when the token belongs to an
        # appservice whose namespace does not cover the bot, as after a
        # sender_localpart change. That is a definite no, so the stale
        # registration is replaced.
        self._mock_homeserver("unused")
        self.effective_after_sends = 3
        self.unregistered_probe_response = httpx.Response(
            403,
            json={
                "errcode": "M_EXCLUSIVE",
                "error": "M_EXCLUSIVE: User is not in namespace.",
            },
        )
        self._replies(DUPLICATE_ID_REPLY, "Appservice unregistered.", REGISTERED_REPLY)

        self.assertIn("replaced", self._call())

    def test_an_inconclusive_probe_changes_nothing(self):
        # Only a definite rejection may lead to an unregister: a proxy error,
        # rate limiting or a network blip says nothing about the registration.
        for answer in (
            httpx.Response(503, text="Service Unavailable"),
            httpx.Response(429, json={"errcode": "M_LIMIT_EXCEEDED"}),
            httpx.Response(200, text="<html>not json</html>"),
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("timed out"),
        ):
            with self.subTest(answer=answer), respx.mock:
                self._mock_homeserver(REGISTERED_REPLY)
                respx.get(f"{HOMESERVER}/_matrix/client/v3/account/whoami").mock(
                    side_effect=[answer]
                )

                with self.assertRaises(CommandError) as ctx:
                    self._call()

                self.assertIn("could not determine", str(ctx.exception))
                self.assertIn("nothing was changed", str(ctx.exception))
                self.assertFalse(self.register_route.called)
                self.assertFalse(self.send_route.called)

    @respx.mock
    def test_an_inconclusive_probe_after_duplicate_id_does_not_unregister(self):
        self._mock_homeserver("unused")
        respx.get(f"{HOMESERVER}/_matrix/client/v3/account/whoami").mock(
            side_effect=[
                httpx.Response(401, json={"errcode": "M_UNKNOWN_TOKEN"}),
                httpx.Response(502, text="Bad Gateway"),
            ]
        )
        self._replies(DUPLICATE_ID_REPLY)

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn("nothing was changed", str(ctx.exception))
        self.assertEqual(self._sent_commands(), ["!admin appservices register"])

    @respx.mock
    def test_a_registration_that_cannot_be_confirmed_says_so(self):
        self._mock_homeserver(REGISTERED_REPLY)
        respx.get(f"{HOMESERVER}/_matrix/client/v3/account/whoami").mock(
            side_effect=[
                httpx.Response(401, json={"errcode": "M_UNKNOWN_TOKEN"}),
                httpx.ConnectError("refused"),
            ]
        )

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn("could not confirm", str(ctx.exception))

    def test_no_bootstrap_admin_is_created_without_a_known_password(self):
        # A bootstrap user with a random, discarded password blocks every later
        # run, and on a homeserver that already has users it is not even the
        # admin. Compose passes unset variables through as empty strings.
        for blank in ("", "   "):
            with (
                self.subTest(blank=blank),
                respx.mock,
                mock.patch.dict(
                    os.environ,
                    {"MATRIX_BOOTSTRAP_PASSWORD": blank, "MATRIX_ADMIN_TOKEN": blank},
                ),
            ):
                self._mock_homeserver(REGISTERED_REPLY)
                login = respx.post(f"{HOMESERVER}/_matrix/client/v3/login")

                with self.assertRaises(CommandError) as ctx:
                    self._call()

                self.assertIn("MATRIX_BOOTSTRAP_PASSWORD", str(ctx.exception))
                self.assertIn("MATRIX_ADMIN_TOKEN", str(ctx.exception))
                self.assertFalse(self.register_route.called)
                self.assertFalse(login.called)
                self.assertFalse(self.send_route.called)

    @respx.mock
    @mock.patch.dict(os.environ, {"MATRIX_ADMIN_TOKEN": "   "})
    def test_a_blank_admin_token_counts_as_unset(self):
        self._mock_homeserver(REGISTERED_REPLY)

        self.assertIn("registered on the homeserver", self._call())
        self.assertEqual(
            self.send_route.calls.last.request.headers["Authorization"],
            "Bearer bootstrap-token",
        )

    @respx.mock
    def test_no_reply_within_the_timeout_is_reported_as_indeterminate(self):
        self._mock_homeserver("ignored")
        respx.get(url__regex=MESSAGES_URL).mock(
            return_value=httpx.Response(200, json={"chunk": []})
        )

        with mock.patch.object(appservice_registration, "REPLY_POLL_ATTEMPTS", 2):
            with self.assertRaises(CommandError) as ctx:
                self._call()

        self.assertIn("did not reply", str(ctx.exception))

    @respx.mock
    def test_ambiguous_admin_room_is_reported_rather_than_guessed(self):
        self._mock_homeserver("Appservice registered with ID: waldur")
        respx.get(f"{HOMESERVER}/_matrix/client/v3/joined_rooms").mock(
            return_value=httpx.Response(
                200, json={"joined_rooms": [ADMIN_ROOM, "!other:example.com"]}
            )
        )

        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertIn("--admin-room", str(ctx.exception))

    def _admin_alias_resolves(self):
        respx.get(
            url__startswith=f"{HOMESERVER}/_matrix/client/v3/directory/room/"
        ).mock(return_value=httpx.Response(200, json={"room_id": ADMIN_ROOM}))

    @respx.mock
    def test_the_admin_alias_picks_the_room_among_several(self):
        self._mock_homeserver(REGISTERED_REPLY)
        self._admin_alias_resolves()
        respx.get(f"{HOMESERVER}/_matrix/client/v3/joined_rooms").mock(
            return_value=httpx.Response(
                200, json={"joined_rooms": ["!other:example.com", ADMIN_ROOM]}
            )
        )

        self.assertIn("registered on the homeserver", self._call())
        self.assertIn(
            "/rooms/%21admin%3Aexample.com/send/",
            str(self.send_route.calls[0].request.url),
        )

    @respx.mock
    def test_a_user_outside_the_admin_room_is_not_taken_for_the_admin(self):
        # Anyone can resolve #admins, so on a homeserver that already had users
        # the alias resolves for a bootstrap user that is not the admin. Tuwunel
        # 1.9.0 then refuses the command with a bare "membership `leave` is not
        # `join`", which says nothing about why.
        self._mock_homeserver(REGISTERED_REPLY)
        self._admin_alias_resolves()
        respx.get(f"{HOMESERVER}/_matrix/client/v3/joined_rooms").mock(
            return_value=httpx.Response(200, json={"joined_rooms": []})
        )

        with self.assertRaises(CommandError) as ctx:
            self._call()

        message = str(ctx.exception)
        self.assertIn(f"{BOOTSTRAP_USER} is not in the admin room", message)
        self.assertIn("not a homeserver admin", message)
        # The two ways out; being registered first is not one of them.
        self.assertIn(f"!admin users make-user-admin {BOOTSTRAP_USER}", message)
        self.assertIn("MATRIX_ADMIN_TOKEN", message)
        self.assertNotIn("first user", message)
        self.assertFalse(self.send_route.called)

    @respx.mock
    def test_an_admin_token_outside_the_admin_room_names_its_user(self):
        self._mock_homeserver(REGISTERED_REPLY)
        self._admin_alias_resolves()
        respx.get(f"{HOMESERVER}/_matrix/client/v3/joined_rooms").mock(
            return_value=httpx.Response(200, json={"joined_rooms": []})
        )

        with self.assertRaises(CommandError) as ctx:
            self._call("--admin-token", "admin-token")

        self.assertIn(f"@admin:{DOMAIN} is not in the admin room", str(ctx.exception))

    @respx.mock
    @mock.patch.dict(os.environ, {"MATRIX_ADMIN_TOKEN": "env-admin-token"})
    def test_the_admin_token_can_come_from_the_environment(self):
        # An argument shows up in process listings; the environment does not.
        self._mock_homeserver(REGISTERED_REPLY)
        self.bot_admin_route.mock(return_value=NOT_ADMIN)

        output = self._call()

        self.assertIn("registered on the homeserver", output)
        self.assertFalse(self.register_route.called)
        for route in (self.send_route, self.grant_route):
            self.assertEqual(
                route.calls.last.request.headers["Authorization"],
                "Bearer env-admin-token",
            )

    @respx.mock
    @mock.patch.dict(os.environ, {"MATRIX_ADMIN_TOKEN": "env-admin-token"})
    def test_an_admin_token_argument_wins_over_the_environment(self):
        self._mock_homeserver(REGISTERED_REPLY)

        self._call("--admin-token", "arg-admin-token")

        self.assertEqual(
            self.send_route.calls.last.request.headers["Authorization"],
            "Bearer arg-admin-token",
        )

    @respx.mock
    def test_admin_token_skips_bootstrap_registration(self):
        self._mock_homeserver("Appservice registered with ID: waldur")
        register_route = respx.post(SHARED_SECRET_REGISTER_URL)

        output = self._call("--admin-token", "admin-token", "--admin-room", ADMIN_ROOM)

        self.assertIn("registered on the homeserver", output)
        self.assertFalse(register_route.called)


class RegistrationDifferencesTest(TestCase):
    # Verbatim from Tuwunel 1.9.0 for a registration the command made.
    SHOWN = (
        "Config for waldur:\n\n```yaml\n"
        "id: waldur\n"
        "url: http://host.docker.internal:18200\n"
        "as_token: live-as-token\n"
        "hs_token: live-hs-token\n"
        "sender_localpart: waldur-bot\n"
        "namespaces:\n"
        "  users:\n"
        "  - exclusive: true\n"
        "    regex: '@waldur-bot:tm.localtest'\n"
        "  - exclusive: false\n"
        "    regex: '@.*:tm.localtest'\n"
        "  aliases:\n"
        "  - exclusive: true\n"
        "    regex: '#waldur-[^:]+:tm\\.localtest'\n"
        "receive_ephemeral: false\n"
        "io.element.msc4190: false\n"
        "org.matrix.msc3202: false\n"
        "\n```"
    )

    def _expected(self, **changes):
        return {
            **appservice_registration.build_registration(
                url="http://host.docker.internal:18200",
                as_token="live-as-token",
                hs_token="live-hs-token",
                sender_localpart="waldur-bot",
                homeserver_domain="tm.localtest",
            ),
            **changes,
        }

    def _differences(self, expected):
        return appservice_registration.registration_differences(
            appservice_registration.parse_shown_registration(self.SHOWN), expected
        )

    def test_the_homeservers_own_rendering_matches_what_waldur_registers(self):
        # Tuwunel drops the empty rooms list and adds keys of its own.
        self.assertEqual(self._differences(self._expected()), [])

    def test_differences_are_named_without_their_values(self):
        expected = self._expected(url="http://waldur:8080", hs_token="new-hs-token")
        expected["namespaces"]["aliases"] = []

        self.assertEqual(
            self._differences(expected), ["url", "hs_token", "namespaces.aliases"]
        )

    def test_an_error_reply_is_not_taken_for_a_registration(self):
        with self.assertRaises(
            appservice_registration.AppserviceRegistrationError
        ) as ctx:
            appservice_registration.parse_shown_registration(
                "Command failed with error:\n```\nAppservice does not exist.\n```"
            )

        self.assertIn("Appservice does not exist", str(ctx.exception))


class RedactTest(TestCase):
    """Homeserver messages that quote a token must not repeat it."""

    def test_every_form_a_token_is_quoted_in_is_redacted(self):
        cases = [
            # Tuwunel names the URL it failed to call.
            "url: http://waldur/_matrix/app/v1/ping?access_token=s3cr3t",
            # An admin bot echoing the YAML descriptor.
            "as_token: s3cr3t",
            "hs_token=s3cr3t",
            # JSON, as a homeserver or a proxy may echo the descriptor.
            '{"as_token": "s3cr3t", "url": "http://waldur"}',
            "{'hs_token':'s3cr3t'}",
            # JSON inside a JSON string, as in M_BAD_STATUS's body field.
            '"body": "{\\"hs_token\\": \\"s3cr3t\\"}"',
        ]
        for text in cases:
            with self.subTest(text):
                redacted = appservice_registration._redact(text)

                self.assertNotIn("s3cr3t", redacted)
                self.assertIn("<redacted>", redacted)

    def test_the_rest_of_the_message_survives(self):
        self.assertEqual(
            appservice_registration._redact('{"as_token": "s3cr3t", "id": "waldur"}'),
            '{"as_token": "<redacted>", "id": "waldur"}',
        )


class PingAppserviceTest(TestCase):
    @respx.mock
    def test_returns_the_round_trip(self):
        route = respx.post(PING_URL).mock(
            return_value=httpx.Response(200, json={"duration_ms": 7})
        )

        duration = appservice_registration.ping_appservice(HOMESERVER, "as-token")

        self.assertEqual(duration, 7)
        self.assertEqual(
            route.calls.last.request.headers["Authorization"], "Bearer as-token"
        )

    @respx.mock
    def test_a_homeserver_error_is_reported_with_its_code(self):
        respx.post(PING_URL).mock(
            return_value=httpx.Response(
                502,
                json={"errcode": "M_BAD_STATUS", "error": "403 from the appservice"},
            )
        )

        with self.assertRaises(
            appservice_registration.AppserviceRegistrationError
        ) as ctx:
            appservice_registration.ping_appservice(HOMESERVER, "as-token")

        self.assertIn("M_BAD_STATUS", str(ctx.exception))

    def test_an_appservice_that_turns_the_ping_away_is_told_apart(self):
        # Only a 4xx from whatever answers at the registered URL can be fixed
        # by registering again; Tuwunel reports that status alongside
        # M_BAD_STATUS.
        cases = [
            (_bad_status_ping(403, "Forbidden"), True),
            (_bad_status_ping(404, "Not Found"), True),
            (_bad_status_ping(503, "Service Unavailable"), False),
            (
                httpx.Response(
                    502, json={"errcode": "M_BAD_STATUS", "error": "no status"}
                ),
                False,
            ),
            (httpx.Response(502, json={"errcode": "M_CONNECTION_FAILED"}), False),
        ]
        for answer, refused in cases:
            with self.subTest(answer=answer.text), respx.mock:
                respx.post(PING_URL).mock(return_value=answer)

                with self.assertRaises(
                    appservice_registration.AppservicePingError
                ) as ctx:
                    appservice_registration.ping_appservice(HOMESERVER, "as-token")

                self.assertEqual(ctx.exception.refused_by_appservice, refused)

    @respx.mock
    def test_a_token_in_the_homeservers_error_is_not_repeated(self):
        # Tuwunel quotes the URL it failed to call, hs_token query included;
        # the message ends up in deployment logs and the diagnostics page.
        respx.post(PING_URL).mock(
            return_value=httpx.Response(
                502,
                json={
                    "errcode": "M_CONNECTION_FAILED",
                    "error": "Could not send request to appservice url: "
                    '"http://waldur:8080/_matrix/app/v1/ping?access_token=s3cr3t-hs", '
                    "source: Connection refused",
                },
            )
        )

        with self.assertRaises(
            appservice_registration.AppserviceRegistrationError
        ) as ctx:
            appservice_registration.ping_appservice(HOMESERVER, "as-token")

        self.assertNotIn("s3cr3t-hs", str(ctx.exception))
        self.assertIn("access_token=<redacted>", str(ctx.exception))

    @respx.mock
    def test_a_timeout_says_the_homeserver_may_still_be_trying(self):
        # Tuwunel 1.9.0 keeps the ping open for 35 s while it tries Waldur,
        # longer than Waldur waits, so the timeout usually means the
        # homeserver cannot reach Waldur.
        respx.post(PING_URL).mock(side_effect=httpx.ReadTimeout("timed out"))

        with self.assertRaises(appservice_registration.AppservicePingTimeout) as ctx:
            appservice_registration.ping_appservice(HOMESERVER, "as-token")

        self.assertIn("may still be trying to reach Waldur", str(ctx.exception))

    @respx.mock
    def test_an_unreachable_homeserver_is_reported(self):
        respx.post(PING_URL).mock(side_effect=httpx.ConnectError("refused"))

        with self.assertRaises(appservice_registration.AppserviceRegistrationError):
            appservice_registration.ping_appservice(HOMESERVER, "as-token")
