import os
import unittest
from io import StringIO
from unittest import mock

from constance import config
from constance import settings as constance_settings
from constance.codecs import dumps
from constance.models import Constance
from constance.test import override_config
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.test.utils import override_settings

from waldur_mastermind.matrix_chat.management.commands import init_matrix_settings
from waldur_mastermind.matrix_chat.management.commands.init_matrix_settings import (
    DEPLOYMENT_ONLY,
    SEEDABLE,
    UNDECRYPTABLE_PREFIX,
)

ENCRYPTED_BACKEND = "waldur_core.core.constance_backend.EncryptedDatabaseBackend"
encrypted_at_rest = unittest.skipUnless(
    settings.CONSTANCE_BACKEND == ENCRYPTED_BACKEND,
    "Constance secrets are not encrypted at rest on this branch",
)


def _store_raw(key, value):
    """Write a row as the backend would, bypassing its set()."""
    Constance.objects.update_or_create(key=key, defaults={"value": dumps(value)})


class InitMatrixSettingsTest(TestCase):
    """The env -> Constance contract both packagers depend on.

    waldur-helm's wire Job and waldur-docker-compose's init-matrix.sh set the
    same environment variable names and expect this command to be the only
    place the mapping is written down.
    """

    ENV = dict(
        MATRIX_HOMESERVER_URL="http://matrix-homeserver.waldur.svc:6167",
        MATRIX_HOMESERVER_PUBLIC_URL="https://chat.example.com",
        MATRIX_HOMESERVER_DOMAIN="chat.example.com",
        MATRIX_APPSERVICE_AS_TOKEN="as-token-value",
        MATRIX_APPSERVICE_HS_TOKEN="hs-token-value",
        MATRIX_APPSERVICE_SENDER_LOCALPART="waldur-bot",
        MATRIX_USER_REGISTRATION_SECRET="registration-secret",
    )

    def _call(self, env=None, **kwargs):
        out = StringIO()
        with mock.patch.dict(os.environ, env or self.ENV, clear=True):
            call_command("init_matrix_settings", stdout=out, **kwargs)
        return out.getvalue()

    def _stored_rows(self):
        return list(Constance.objects.order_by("key").values_list("key", "value"))

    def _assert_refused_without_writing(self, env, *expected_in_message):
        before = self._stored_rows()

        with self.assertRaises(CommandError) as ctx:
            self._call(env=env)

        for text in expected_in_message:
            self.assertIn(text, str(ctx.exception))
        self.assertEqual(self._stored_rows(), before)

    def test_every_supplied_setting_is_written_to_constance(self):
        self._call()

        for key, value in self.ENV.items():
            self.assertEqual(getattr(config, key), value)

    def test_matrix_is_enabled_even_though_the_packagers_do_not_pass_the_flag(self):
        self.assertFalse(Constance.objects.filter(key="MATRIX_ENABLED").exists())

        self._call()

        self.assertTrue(config.MATRIX_ENABLED)

    def test_matrix_is_enabled_even_if_something_already_read_the_flag(self):
        """Reading a constance setting that has no row stores its default, and
        signal handlers and periodic tasks read this flag long before the
        packagers' seeding Job runs. A stored False is no sign of a choice."""
        self.assertFalse(config.MATRIX_ENABLED)
        self.assertTrue(Constance.objects.filter(key="MATRIX_ENABLED").exists())

        self._call()

        self.assertTrue(config.MATRIX_ENABLED)

    def test_an_administrator_who_switched_chat_off_keeps_it_off(self):
        """The command runs on every deploy, so defaulting the flag on each time
        would undo the switch at the next routine upgrade."""
        self._call()
        config.MATRIX_ENABLED = False

        self._call()

        self.assertFalse(config.MATRIX_ENABLED)

    def test_clearing_only_the_marker_does_not_switch_chat_back_on(self):
        """A blank marker is also what an administrator leaves behind when
        handing the tokens back. The tokens are still stored then, so this is
        no fresh install and the flag the administrator set stays as it is."""
        self._call()
        config.MATRIX_TOKENS_MANAGED_BY = ""
        config.MATRIX_ENABLED = False

        self._call()

        self.assertFalse(config.MATRIX_ENABLED)
        self.assertEqual(config.MATRIX_TOKENS_MANAGED_BY, "deployment")

    def test_clearing_marker_and_tokens_starts_over_and_switches_chat_on(self):
        """The refusal's remedy clears both tokens along with the marker, and
        asks for a redeploy. That is a fresh seeding, so chat comes on."""
        self._call()
        config.MATRIX_TOKENS_MANAGED_BY = ""
        config.MATRIX_APPSERVICE_AS_TOKEN = ""
        config.MATRIX_APPSERVICE_HS_TOKEN = ""
        config.MATRIX_ENABLED = False

        self._call()

        self.assertTrue(config.MATRIX_ENABLED)

    @override_config(MATRIX_APPSERVICE_HS_TOKEN="hs-token-value")
    def test_one_stored_token_is_enough_to_leave_the_flag_alone(self):
        self._call()

        self.assertFalse(config.MATRIX_ENABLED)

    def test_the_flag_is_still_honoured_when_it_is_passed(self):
        with override_config(MATRIX_ENABLED=True):
            self._call(env=dict(self.ENV, MATRIX_ENABLED="false"))

            self.assertFalse(config.MATRIX_ENABLED)

    def test_a_passed_flag_overrides_an_administrators_choice(self):
        self._call()
        config.MATRIX_ENABLED = False

        self._call(env=dict(self.ENV, MATRIX_ENABLED="true"))

        self.assertTrue(config.MATRIX_ENABLED)

    def test_settings_the_deployment_does_not_manage_are_left_alone(self):
        with override_config(MATRIX_EXTERNAL_LOGIN_METHOD="oidc"):
            self._call()

            self.assertEqual(config.MATRIX_EXTERNAL_LOGIN_METHOD, "oidc")

    def test_a_missing_credential_fails_loudly(self):
        env = {k: v for k, v in self.ENV.items() if k != "MATRIX_APPSERVICE_AS_TOKEN"}

        with self.assertRaises(CommandError) as ctx:
            self._call(env=env)

        self.assertIn("MATRIX_APPSERVICE_AS_TOKEN", str(ctx.exception))

    def test_a_whitespace_only_token_counts_as_missing(self):
        """An unset Secret key often renders as a stray newline, not nothing."""
        self._assert_refused_without_writing(
            dict(self.ENV, MATRIX_APPSERVICE_HS_TOKEN=" \n"),
            "MATRIX_APPSERVICE_HS_TOKEN",
        )

    def test_a_missing_registration_secret_fails_loudly(self):
        """Without it the homeserver refuses every user Waldur provisions."""
        env = {
            k: v for k, v in self.ENV.items() if k != "MATRIX_USER_REGISTRATION_SECRET"
        }

        self._assert_refused_without_writing(env, "MATRIX_USER_REGISTRATION_SECRET")

    def test_an_invalid_url_writes_nothing(self):
        self._assert_refused_without_writing(
            dict(self.ENV, MATRIX_HOMESERVER_URL="not a url"),
            "MATRIX_HOMESERVER_URL",
        )

    def test_an_invalid_sender_localpart_writes_nothing(self):
        """The localpart is interpolated into the registration's user regex."""
        self._assert_refused_without_writing(
            dict(self.ENV, MATRIX_APPSERVICE_SENDER_LOCALPART=".*"),
            "MATRIX_APPSERVICE_SENDER_LOCALPART",
        )

    def test_an_invalid_homeserver_domain_writes_nothing(self):
        """The domain is interpolated into the registration's namespace regexes."""
        self._assert_refused_without_writing(
            dict(self.ENV, MATRIX_HOMESERVER_DOMAIN=".*"),
            "MATRIX_HOMESERVER_DOMAIN",
        )

    def test_livekit_credentials_are_not_printed(self):
        output = self._call(
            env=dict(
                self.ENV,
                MATRIX_LIVEKIT_KEY="livekit-key-value",
                MATRIX_LIVEKIT_SECRET="livekit-secret-value",
            )
        )

        self.assertNotIn("livekit-key-value", output)
        self.assertNotIn("livekit-secret-value", output)
        self.assertIn("MATRIX_LIVEKIT_SECRET", output)

    def test_a_secret_setting_is_redacted_whatever_its_name(self):
        """A seedable setting's name alone cannot be trusted to say whether the
        value may be logged."""
        with (
            mock.patch.dict(
                constance_settings.CONFIG,
                {"MATRIX_FUTURE_CREDENTIAL": ("", "A secret.", "secret_field")},
            ),
            mock.patch.object(
                init_matrix_settings,
                "SEEDABLE",
                (*SEEDABLE, "MATRIX_FUTURE_CREDENTIAL"),
            ),
        ):
            output = self._call(
                env=dict(self.ENV, MATRIX_FUTURE_CREDENTIAL="future-credential")
            )

        self.assertNotIn("future-credential", output)
        self.assertIn("MATRIX_FUTURE_CREDENTIAL", output)

    def test_a_matrix_setting_outside_the_allowlist_is_not_seeded(self):
        """The packagers' Jobs carry homeserver admin credentials in MATRIX_*
        variables. A Constance setting that came to share such a name must not
        have the credential written into the database."""
        with mock.patch.dict(
            constance_settings.CONFIG,
            {
                "MATRIX_ADMIN_TOKEN": ("", "Hypothetical.", "secret_field"),
                "MATRIX_BOOTSTRAP_PASSWORD": ("", "Hypothetical.", "secret_field"),
            },
        ):
            output = self._call(
                env=dict(
                    self.ENV,
                    MATRIX_ADMIN_TOKEN="admin-token-value",
                    MATRIX_BOOTSTRAP_PASSWORD="bootstrap-password-value",
                )
            )

            self.assertFalse(
                Constance.objects.filter(
                    key__in=["MATRIX_ADMIN_TOKEN", "MATRIX_BOOTSTRAP_PASSWORD"]
                ).exists()
            )
        self.assertNotIn("MATRIX_ADMIN_TOKEN", output)
        self.assertNotIn("MATRIX_BOOTSTRAP_PASSWORD", output)

    def test_every_seedable_setting_exists(self):
        """A renamed setting would otherwise drop out of seeding silently."""
        self.assertEqual(
            [key for key in SEEDABLE if key not in constance_settings.CONFIG], []
        )

    def test_deployment_credentials_are_never_seedable(self):
        self.assertEqual(set(SEEDABLE) & set(DEPLOYMENT_ONLY), set())

    def test_every_matrix_setting_is_either_seedable_or_set_by_the_command(self):
        """A new MATRIX_* setting is not seeded until it is added to SEEDABLE.
        This fails so that the choice is made, and the packagers told, rather
        than an operator finding the variable they set has no effect."""
        unclassified = sorted(
            key
            for key in constance_settings.CONFIG
            if key.startswith("MATRIX_")
            and key not in SEEDABLE
            and key != "MATRIX_TOKENS_MANAGED_BY"
        )
        self.assertEqual(unclassified, [])

    def test_credentials_in_a_url_are_not_printed(self):
        output = self._call(
            env=dict(
                self.ENV,
                MATRIX_HOMESERVER_URL="https://user:url-password@matrix.example.org:8448/",
            )
        )

        self.assertNotIn("url-password", output)
        self.assertIn("https://matrix.example.org:8448/", output)
        self.assertEqual(
            config.MATRIX_HOMESERVER_URL,
            "https://user:url-password@matrix.example.org:8448/",
        )

    def test_tokens_are_not_printed(self):
        output = self._call()

        self.assertNotIn("as-token-value", output)
        self.assertNotIn("hs-token-value", output)
        self.assertNotIn("registration-secret", output)
        self.assertIn("MATRIX_APPSERVICE_AS_TOKEN", output)

    def test_reseeding_the_same_values_is_a_no_op(self):
        self._call()
        self._call()

        self.assertEqual(config.MATRIX_HOMESERVER_DOMAIN, "chat.example.com")

    def test_seeding_marks_the_tokens_as_deployment_managed(self):
        self._call()

        self.assertEqual(config.MATRIX_TOKENS_MANAGED_BY, "deployment")

    @override_config(
        MATRIX_APPSERVICE_AS_TOKEN="hand-as-token",
        MATRIX_APPSERVICE_HS_TOKEN="hand-hs-token",
    )
    def test_hand_configured_tokens_are_not_replaced(self):
        """A deployment that ran the Setup wizard before the packagers seeded
        Matrix has a homeserver registered with those tokens. Replacing them
        breaks chat until someone re-registers it."""
        self._assert_refused_without_writing(
            self.ENV,
            "MATRIX_APPSERVICE_AS_TOKEN",
            "clear MATRIX_APPSERVICE_AS_TOKEN and MATRIX_APPSERVICE_HS_TOKEN",
            "start from a fresh stack",
        )

    @override_config(MATRIX_APPSERVICE_AS_TOKEN="hand-as-token")
    def test_the_refusal_names_no_switch_to_override_it(self):
        """Matrix chat has never run in production, so there is nothing to
        migrate: hand-configured tokens are cleared, not adopted."""
        with self.assertRaises(CommandError) as ctx:
            self._call()

        self.assertNotIn("adopt", str(ctx.exception).lower())
        self.assertNotIn("zero-touch", str(ctx.exception))

    @override_config(MATRIX_APPSERVICE_HS_TOKEN="hand-hs-token")
    def test_one_hand_configured_token_is_enough_to_refuse(self):
        self._assert_refused_without_writing(self.ENV, "MATRIX_APPSERVICE_HS_TOKEN")

    @override_config(
        MATRIX_APPSERVICE_AS_TOKEN="as-token-value",
        MATRIX_APPSERVICE_HS_TOKEN="hs-token-value",
    )
    def test_unchanged_tokens_from_before_the_marker_are_taken_over(self):
        """Every compose stack seeded before the marker existed re-seeds the
        same secrets on its next deploy, and must come up without a prompt."""
        self._call()

        self.assertEqual(config.MATRIX_TOKENS_MANAGED_BY, "deployment")
        self.assertEqual(config.MATRIX_APPSERVICE_AS_TOKEN, "as-token-value")
        self.assertEqual(config.MATRIX_APPSERVICE_HS_TOKEN, "hs-token-value")

    @override_config(
        MATRIX_APPSERVICE_AS_TOKEN="as-token-value",
        MATRIX_APPSERVICE_HS_TOKEN="hs-token-value",
    )
    def test_a_trailing_newline_on_a_supplied_token_is_not_a_difference(self):
        self._call(env=dict(self.ENV, MATRIX_APPSERVICE_AS_TOKEN="as-token-value\n"))

        self.assertEqual(config.MATRIX_TOKENS_MANAGED_BY, "deployment")
        self.assertEqual(config.MATRIX_APPSERVICE_AS_TOKEN, "as-token-value")

    @override_config(
        MATRIX_TOKENS_MANAGED_BY="deployment",
        MATRIX_APPSERVICE_AS_TOKEN="old-as-token",
        MATRIX_APPSERVICE_HS_TOKEN="old-hs-token",
    )
    def test_tokens_the_deployment_seeded_can_be_rotated(self):
        self._call()

        self.assertEqual(config.MATRIX_APPSERVICE_AS_TOKEN, "as-token-value")
        self.assertEqual(config.MATRIX_APPSERVICE_HS_TOKEN, "hs-token-value")

    def test_an_undecryptable_hand_configured_token_is_refused_by_its_cause(self):
        """A token stored under a lost FIELD_ENCRYPTION_KEY reads as a random
        stand-in. It differs from every supplied value, but that says nothing
        about the homeserver, so the refusal names the key, not a mismatch."""
        _store_raw("MATRIX_APPSERVICE_AS_TOKEN", f"{UNDECRYPTABLE_PREFIX}random")

        self._assert_refused_without_writing(
            self.ENV,
            "MATRIX_APPSERVICE_AS_TOKEN",
            "cannot be decrypted with any configured FIELD_ENCRYPTION_KEY",
            "FIELD_ENCRYPTION_KEY_FALLBACKS",
        )
        with self.assertRaises(CommandError) as ctx:
            self._call()
        self.assertNotIn("differ from the supplied ones", str(ctx.exception))

    @override_config(MATRIX_TOKENS_MANAGED_BY="deployment")
    def test_undecryptable_tokens_the_deployment_seeded_are_reseeded(self):
        """The deployment still holds the tokens it seeded, so writing them
        again under the current key restores working ones."""
        _store_raw("MATRIX_APPSERVICE_AS_TOKEN", f"{UNDECRYPTABLE_PREFIX}random")
        _store_raw("MATRIX_APPSERVICE_HS_TOKEN", f"{UNDECRYPTABLE_PREFIX}other")

        self._call()

        self.assertEqual(config.MATRIX_APPSERVICE_AS_TOKEN, "as-token-value")
        self.assertEqual(config.MATRIX_APPSERVICE_HS_TOKEN, "hs-token-value")

    @encrypted_at_rest
    def test_a_token_stored_under_a_lost_key_is_refused(self):
        with override_settings(
            FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode(),
            FIELD_ENCRYPTION_KEY_FALLBACKS=[],
        ):
            config.MATRIX_APPSERVICE_HS_TOKEN = "hs-token-value"
        # Read once first: the backend logs the first failed decryption, and
        # the logging handler stores a default of its own.
        self.assertTrue(
            config.MATRIX_APPSERVICE_HS_TOKEN.startswith(UNDECRYPTABLE_PREFIX)
        )

        self._assert_refused_without_writing(
            self.ENV,
            "MATRIX_APPSERVICE_HS_TOKEN",
            "cannot be decrypted with any configured FIELD_ENCRYPTION_KEY",
        )

    @encrypted_at_rest
    def test_seeded_secrets_are_stored_as_ciphertext(self):
        self._call()

        for key in (
            "MATRIX_APPSERVICE_AS_TOKEN",
            "MATRIX_APPSERVICE_HS_TOKEN",
            "MATRIX_USER_REGISTRATION_SECRET",
        ):
            row = Constance.objects.get(key=key).value
            self.assertNotIn(self.ENV[key], row)
            self.assertEqual(getattr(config, key), self.ENV[key])

    def test_the_environment_cannot_clear_the_marker(self):
        """The marker records that this command ran, not an operator's opinion.

        Left settable from the environment, a deployment could seed tokens on
        every sync and still present the Setup wizard as authoritative, which
        is the silent-revert bug the marker exists to prevent.
        """
        self._call(env=dict(self.ENV, MATRIX_TOKENS_MANAGED_BY=""))

        self.assertEqual(config.MATRIX_TOKENS_MANAGED_BY, "deployment")
