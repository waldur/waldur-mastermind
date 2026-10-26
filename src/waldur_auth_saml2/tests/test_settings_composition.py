import copy
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

import waldur_auth_saml2
from waldur_auth_saml2.extension import SAML2Extension

CONTAINER_SETTINGS = (
    Path(waldur_auth_saml2.__file__).resolve().parents[2]
    / "docker/rootfs/etc/waldur/settings.py"
)

PROBE = textwrap.dedent(
    """
    import json, runpy, sys
    config = runpy.run_path(sys.argv[1])["SAML_CONFIG"]
    print(json.dumps(config, default=str))
    """
)


@unittest.skipUnless(CONTAINER_SETTINGS.exists(), "container settings not available")
class ContainerSamlConfigTest(SimpleTestCase):
    """The container's settings.py composes SAML_CONFIG from the extension and
    from the deployment's override.conf.py and saml2.conf.py."""

    def compose(self, saml2_conf):
        with tempfile.TemporaryDirectory() as conf_dir:
            Path(conf_dir, "override.conf.py").write_text(
                'WALDUR_CORE["MASTERMIND_URL"] = "https://waldur.example.com"\n'
            )
            Path(conf_dir, "saml2.conf.py").write_text(textwrap.dedent(saml2_conf))
            result = subprocess.run(
                [sys.executable, "-c", PROBE, str(CONTAINER_SETTINGS)],
                env={
                    **os.environ,
                    "WALDUR_BASE_CONFIG_DIR": conf_dir,
                    "GLOBAL_SECRET_KEY": "test",
                },
                capture_output=True,
                text=True,
                timeout=300,
                check=True,
            )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_what_saml2_conf_sets_in_saml_config_is_kept(self):
        config = self.compose(
            """
            SAML_CONFIG.update({
                "contact_person": [{"contact_type": "technical", "email_address": "mailto:ops@example.com"}],
                "service": {"sp": {"want_response_signed": False, "allow_unsolicited": False}},
            })
            """
        )
        sp = config["service"]["sp"]
        self.assertEqual(
            config["contact_person"][0]["email_address"], "mailto:ops@example.com"
        )
        self.assertIs(sp["want_response_signed"], False)
        self.assertIs(sp["allow_unsolicited"], False)

    def test_values_derived_from_mastermind_url_follow_override_conf(self):
        config = self.compose("SAML_CONFIG.update({'service': {'sp': {}}})\n")
        self.assertEqual(
            config["entityid"], "https://waldur.example.com/api-auth/saml2/metadata/"
        )
        acs = config["service"]["sp"]["endpoints"]["assertion_consumer_service"]
        self.assertEqual(
            acs[0][0], "https://waldur.example.com/api-auth/saml2/login/complete/"
        )

    def test_endpoints_are_not_taken_from_saml2_conf(self):
        config = self.compose(
            """
            SAML_CONFIG.update({"service": {"sp": {"endpoints": {
                "assertion_consumer_service": [
                    ("https://waldur.example.com", "/api-auth/saml2/login/complete/", "post"),
                ],
            }}}})
            """
        )
        acs = config["service"]["sp"]["endpoints"]["assertion_consumer_service"]
        self.assertEqual(
            acs[0][0], "https://waldur.example.com/api-auth/saml2/login/complete/"
        )

    def test_saml2_conf_cannot_weaken_signature_checks(self):
        config = self.compose(
            """
            SAML_CONFIG.update({
                "only_use_keys_in_metadata": False,
                "service": {"sp": {"want_assertions_or_response_signed": False}},
            })
            """
        )
        self.assertIs(config["only_use_keys_in_metadata"], True)
        self.assertIs(
            config["service"]["sp"]["want_assertions_or_response_signed"], True
        )

    def test_unsolicited_responses_are_accepted_by_default(self):
        # docker-compose's saml2.conf.py only sets WALDUR_AUTH_SAML2.
        config = self.compose("WALDUR_AUTH_SAML2.update({'NAME': 'saml2'})\n")
        self.assertIs(config["service"]["sp"]["allow_unsolicited"], True)


class ExtensionSamlConfigTest(SimpleTestCase):
    def test_unsolicited_responses_can_be_refused(self):
        namespace = {
            name: copy.deepcopy(getattr(settings, name))
            for name in ("WALDUR_CORE", "AUTHENTICATION_BACKENDS", "LOGGING")
        }
        namespace["WALDUR_AUTH_SAML2"] = {
            **settings.WALDUR_AUTH_SAML2,
            "ALLOW_UNSOLICITED": False,
        }
        SAML2Extension.update_settings(namespace)
        self.assertIs(
            namespace["SAML_CONFIG"]["service"]["sp"]["allow_unsolicited"], False
        )
