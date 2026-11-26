"""cidr, ipsec_vpn_config and secret options (#1334)."""

import json

import sentry_sdk
from ddt import data, ddt, unpack
from django.conf import settings
from django.test import SimpleTestCase
from rest_framework import status, test
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import ValidationError
from sentry_sdk.transport import Transport

from waldur_core.core import encryption
from waldur_core.logging import sentry
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_mastermind.common.serializers import (
    validate_egress_destination,
    validate_options,
)
from waldur_mastermind.marketplace import models, serializers
from waldur_mastermind.marketplace.enums import OfferingStates, ResourceStates
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture
from waldur_mastermind.marketplace.tests.test_order_crud import BaseOrderCreateTest

SECRET = "Sup3r-Secret-Psk!"
OTHER_SECRET = "An0ther-Secret-Key#"
PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7Vb8sHdQzLnY\n"
    "\tq3cPxYwKzLnVb8sHd2vLqZxRmTq3cPxYwKzLnVb8sHd==\r\n"
    "-----END PRIVATE KEY-----\n"
)
IMPERSONATED_USER_HEADER = settings.WALDUR_CORE["REQUEST_HEADER_IMPERSONATED_USER_UUID"]


def secret(**default_configs):
    return {
        "type": "secret",
        "label": "Pre-shared key",
        "default_configs": default_configs,
    }


IMPERSONATED_USER_HEADER = settings.WALDUR_CORE["REQUEST_HEADER_IMPERSONATED_USER_UUID"]

VPN_VALUE = {
    "peer_gateway_ip": "198.51.100.10",
    "remote_subnets": ["192.168.10.0/24"],
}


def cidr(**default_configs):
    return {
        "type": "cidr",
        "label": "Cluster network",
        "default_configs": default_configs,
    }


def vpn(**default_configs):
    return {
        "type": "ipsec_vpn_config",
        "label": "VPN",
        "default_configs": default_configs,
    }


def clean(options, values, **kwargs):
    return validate_options(options, values, **kwargs)


def errors_of(options, values, **kwargs):
    try:
        validate_options(options, values, **kwargs)
    except ValidationError as e:
        return e.detail
    raise AssertionError("The values were accepted.")


def flat(detail):
    """Every message of a nested error detail, as one string."""
    return json.dumps(detail, default=str)


@ddt
class CidrValueTest(test.APISimpleTestCase):
    def test_network_is_stored_normalised(self):
        values = clean({"net": cidr()}, {"net": " 2001:DB8:0:0::/32 "})
        self.assertEqual(values["net"], "2001:db8::/32")

    @data("10.0.0.1", "10.0.0.0/255.255.0.0", "not a network", "10.0.0.0/33", "")
    def test_value_that_is_not_a_network_is_refused(self, value):
        detail = errors_of({"net": cidr()}, {"net": value})
        self.assertIn("net", detail)

    def test_bare_address_gets_the_cidr_hint(self):
        detail = errors_of({"net": cidr()}, {"net": "10.0.0.1"})
        self.assertEqual(
            str(detail["net"][0]),
            "Enter a network in CIDR notation, such as 10.0.0.0/16.",
        )

    def test_host_bits_are_refused_with_the_network_suggested(self):
        detail = errors_of({"net": cidr()}, {"net": "10.0.0.5/24"})
        self.assertEqual(
            str(detail["net"][0]),
            "10.0.0.5/24 has host bits set. Did you mean 10.0.0.0/24?",
        )

    @data(("ipv4", "fd00::/64"), ("ipv6", "10.0.0.0/16"))
    @unpack
    def test_wrong_ip_version_is_refused(self, ip_version, value):
        detail = errors_of({"net": cidr(ip_version=ip_version)}, {"net": value})
        self.assertIn("net", detail)

    @data("10.0.0.0/8", "10.0.0.0/30")
    def test_prefix_outside_the_limits_is_refused(self, value):
        options = {"net": cidr(prefix_length_min=16, prefix_length_max=28)}
        self.assertIn("net", errors_of(options, {"net": value}))

    def test_prefix_inside_the_limits_is_accepted(self):
        options = {"net": cidr(prefix_length_min=16, prefix_length_max=28)}
        self.assertEqual(clean(options, {"net": "10.1.0.0/16"})["net"], "10.1.0.0/16")

    def test_network_outside_allowed_ranges_is_refused(self):
        options = {"net": cidr(allowed_ranges=["10.0.0.0/8", "192.168.0.0/16"])}
        self.assertEqual(clean(options, {"net": "10.2.0.0/16"})["net"], "10.2.0.0/16")
        detail = errors_of(options, {"net": "172.16.0.0/16"})
        self.assertIn("not inside the allowed ranges", flat(detail))

    def test_overlap_with_excluded_range_names_that_range(self):
        options = {"net": cidr(excluded_ranges=["10.96.0.0/12"])}
        detail = errors_of(options, {"net": "10.96.0.0/16"})
        self.assertIn("10.96.0.0/12", flat(detail))

    def test_list_is_accepted_when_multiple(self):
        options = {"net": cidr(multiple=True)}
        values = clean(options, {"net": ["10.0.0.0/16", "FD00:1::/64"]})
        self.assertEqual(values["net"], ["10.0.0.0/16", "fd00:1::/64"])

    def test_single_string_is_wrapped_when_multiple(self):
        values = clean({"net": cidr(multiple=True)}, {"net": "10.0.0.0/16"})
        self.assertEqual(values["net"], ["10.0.0.0/16"])

    def test_list_is_refused_unless_multiple(self):
        self.assertIn("net", errors_of({"net": cidr()}, {"net": ["10.0.0.0/16"]}))

    def test_overlapping_entries_name_both(self):
        detail = errors_of(
            {"net": cidr(multiple=True)}, {"net": ["10.0.0.0/16", "10.0.1.0/24"]}
        )
        self.assertIn("10.0.1.0/24", flat(detail))
        self.assertIn("10.0.0.0/16", flat(detail))

    def test_too_many_entries_are_refused(self):
        detail = errors_of(
            {"net": cidr(multiple=True, max_items=1)},
            {"net": ["10.0.0.0/16", "10.1.0.0/16"]},
        )
        self.assertIn("at most 1", flat(detail))

    def test_bad_entry_is_reported_by_position(self):
        detail = errors_of(
            {"net": cidr(multiple=True)}, {"net": ["10.0.0.0/16", "10.1.0.1/16"]}
        )
        self.assertIn(1, detail["net"])

    def test_egress_rules_still_accept_host_bits(self):
        # The shared network parser must not change the egress rules' rules.
        validate_egress_destination("cidr", "10.0.0.5/24")


@ddt
class IpsecValueTest(test.APISimpleTestCase):
    def test_defaults_fill_the_phases_and_the_provider_end(self):
        options = {
            "vpn": vpn(
                provider_gateway_ip="203.0.113.10",
                provider_local_subnets=["10.0.0.0/16"],
            )
        }
        value = clean(options, {"vpn": VPN_VALUE})["vpn"]
        self.assertEqual(
            value,
            {
                "peer_gateway_ip": "198.51.100.10",
                "peer_id": "",
                "remote_subnets": ["192.168.10.0/24"],
                "ike_version": "ikev2",
                "phase1": {
                    "encryption": "aes256",
                    "integrity": "sha256",
                    "dh_group": 14,
                    "lifetime": 28800,
                },
                "phase2": {
                    "encryption": "aes256",
                    "integrity": "sha256",
                    "pfs_group": 14,
                    "lifetime": 3600,
                },
                "dpd": None,
                "provider": {
                    "gateway_ip": "203.0.113.10",
                    "local_subnets": ["10.0.0.0/16"],
                },
            },
        )

    def test_customer_cannot_change_the_provider_end(self):
        options = {"vpn": vpn(provider_gateway_ip="203.0.113.10")}
        value = clean(
            options,
            {"vpn": {**VPN_VALUE, "provider": {"gateway_ip": "1.2.3.4"}}},
        )["vpn"]
        self.assertEqual(value["provider"]["gateway_ip"], "203.0.113.10")

    def test_stored_provider_end_is_kept(self):
        stored = {
            "vpn": {"provider": {"gateway_ip": "203.0.113.99", "local_subnets": []}}
        }
        value = clean(
            {"vpn": vpn(provider_gateway_ip="203.0.113.10")},
            {"vpn": VPN_VALUE},
            stored=stored,
        )["vpn"]
        self.assertEqual(value["provider"]["gateway_ip"], "203.0.113.99")

    def test_provider_may_change_the_provider_end(self):
        value = clean(
            {"vpn": vpn(provider_gateway_ip="203.0.113.10")},
            {
                "vpn": {
                    **VPN_VALUE,
                    "provider": {
                        "gateway_ip": "203.0.113.20",
                        "local_subnets": ["10.1.0.0/16"],
                    },
                }
            },
            trust_provider_end=True,
        )["vpn"]
        self.assertEqual(
            value["provider"],
            {"gateway_ip": "203.0.113.20", "local_subnets": ["10.1.0.0/16"]},
        )

    @data(
        "10.1.2.3",
        "192.168.1.1",
        "172.16.0.1",
        "127.0.0.1",
        "169.254.1.1",
        "100.64.0.1",
        "224.0.0.1",
        "240.0.0.1",
        "0.0.0.0",
        "fd00::1",
        "fe80::1",
        "::1",
    )
    def test_non_public_peer_is_refused(self, address):
        detail = errors_of(
            {"vpn": vpn()}, {"vpn": {**VPN_VALUE, "peer_gateway_ip": address}}
        )
        self.assertIn("peer_gateway_ip", detail["vpn"])

    def test_private_peer_is_accepted_when_allowed(self):
        value = clean(
            {"vpn": vpn(allow_private_peer=True)},
            {"vpn": {**VPN_VALUE, "peer_gateway_ip": "10.1.2.3"}},
        )["vpn"]
        self.assertEqual(value["peer_gateway_ip"], "10.1.2.3")

    def test_peer_that_is_not_an_address_is_refused(self):
        detail = errors_of(
            {"vpn": vpn()}, {"vpn": {**VPN_VALUE, "peer_gateway_ip": "vpn.example.com"}}
        )
        self.assertIn("peer_gateway_ip", detail["vpn"])

    @data("vpn.example.com", "198.51.100.10", "vpn@example.com")
    def test_peer_id_may_be_an_address_name_or_email_identity(self, peer_id):
        value = clean({"vpn": vpn()}, {"vpn": {**VPN_VALUE, "peer_id": peer_id}})["vpn"]
        self.assertEqual(value["peer_id"], peer_id)

    def test_invalid_peer_id_is_refused(self):
        detail = errors_of(
            {"vpn": vpn()}, {"vpn": {**VPN_VALUE, "peer_id": "not an id!"}}
        )
        self.assertIn("peer_id", detail["vpn"])

    def test_ikev1_is_refused_unless_allowed(self):
        values = {"vpn": {**VPN_VALUE, "ike_version": "ikev1"}}
        self.assertIn("ike_version", errors_of({"vpn": vpn()}, values)["vpn"])
        self.assertEqual(
            clean({"vpn": vpn(ikev1_allowed=True)}, values)["vpn"]["ike_version"],
            "ikev1",
        )

    @data(
        ("phase1", {"encryption": "aes128"}, "encryption"),
        ("phase1", {"integrity": "sha1"}, "integrity"),
        ("phase1", {"dh_group": 2}, "dh_group"),
        ("phase2", {"pfs_group": None}, "pfs_group"),
        ("phase1", {"lifetime": 60}, "lifetime"),
        ("phase2", {"lifetime": 99999}, "lifetime"),
    )
    @unpack
    def test_proposal_outside_the_policy_is_refused(self, phase, given, key):
        options = {
            "vpn": vpn(
                phase1={"encryption": ["aes256"]},
            )
        }
        detail = errors_of(options, {"vpn": {**VPN_VALUE, phase: given}})
        self.assertIn(key, detail["vpn"][phase])

    def test_no_pfs_is_accepted_when_the_provider_lists_it(self):
        options = {"vpn": vpn(phase2={"pfs_groups": [None, 14]})}
        value = clean(options, {"vpn": {**VPN_VALUE, "phase2": {"pfs_group": None}}})[
            "vpn"
        ]
        self.assertIsNone(value["phase2"]["pfs_group"])

    def test_default_is_the_first_allowed_when_the_builtin_one_is_not(self):
        options = {"vpn": vpn(phase1={"dh_groups": [19, 20]})}
        value = clean(options, {"vpn": VPN_VALUE})["vpn"]
        self.assertEqual(value["phase1"]["dh_group"], 19)

    def test_remote_subnets_follow_the_cidr_rules(self):
        options = {"vpn": vpn(remote_subnets={"ip_version": "ipv4", "max_items": 2})}
        for subnets in (
            ["192.168.10.1/24"],
            ["fd00::/64"],
            ["192.168.0.0/16", "192.168.10.0/24"],
            ["10.0.0.0/24", "10.0.1.0/24", "10.0.2.0/24"],
            [],
        ):
            detail = errors_of(
                options, {"vpn": {**VPN_VALUE, "remote_subnets": subnets}}
            )
            self.assertIn("remote_subnets", detail["vpn"], subnets)

    def test_remote_subnets_must_not_overlap_the_named_cidr_option(self):
        options = {"net": cidr(), "vpn": vpn(no_overlap_with=["net"])}
        detail = errors_of(
            options,
            {"net": "192.168.0.0/16", "vpn": VPN_VALUE},
        )
        self.assertIn("192.168.0.0/16", flat(detail["vpn"]["remote_subnets"]))
        clean(options, {"net": "10.0.0.0/16", "vpn": VPN_VALUE})

    def test_stored_cidr_value_is_checked_on_a_partial_change(self):
        options = {"vpn": vpn(no_overlap_with=["net"])}
        detail = errors_of(
            options, {"vpn": VPN_VALUE}, optional=True, stored={"net": "192.168.0.0/16"}
        )
        self.assertIn("vpn", detail)

    def test_changed_cidr_is_checked_against_the_stored_vpn(self):
        options = {"net": cidr(), "vpn": vpn(no_overlap_with=["net"])}
        detail = errors_of(
            options,
            {"net": "192.168.0.0/16"},
            optional=True,
            stored={"vpn": {"remote_subnets": ["192.168.10.0/24"]}},
        )
        self.assertIn("net", detail)

    @data(
        {"action": "restart", "interval": 0, "timeout": 120},
        {"action": "restart", "interval": 30, "timeout": 10},
        {"action": "explode", "interval": 30, "timeout": 120},
    )
    def test_invalid_dpd_is_refused(self, dpd):
        detail = errors_of({"vpn": vpn()}, {"vpn": {**VPN_VALUE, "dpd": dpd}})
        self.assertIn("dpd", detail["vpn"])

    def test_dpd_is_kept(self):
        dpd = {"action": "restart", "interval": 30, "timeout": 120}
        value = clean({"vpn": vpn()}, {"vpn": {**VPN_VALUE, "dpd": dpd}})["vpn"]
        self.assertEqual(value["dpd"], dpd)


@ddt
class SecretValueTest(test.APISimpleTestCase):
    def test_secret_is_never_part_of_the_stored_values(self):
        values = clean(
            {"key": secret(), "net": cidr()}, {"key": SECRET, "net": "10.0.0.0/8"}
        )
        self.assertEqual(values, {"net": "10.0.0.0/8"})

    @data(
        ("short", {"min_length": 8}),
        ("x" * 300, {"max_length": 255}),
        ("line one\nline two-secret", {}),
        ("tab\tinside-secret", {}),
        ("bell\x07inside-secret", {"multiline": True}),
    )
    @unpack
    def test_malformed_secret_is_refused_without_echoing_it(self, value, configs):
        detail = errors_of({"key": secret(**configs)}, {"key": value})
        self.assertIn("key", detail)
        self.assertNotIn(value, flat(detail))

    def test_multiline_secret_is_accepted_when_allowed(self):
        values = clean({"key": secret(multiline=True)}, {"key": PEM})
        self.assertNotIn("key", values)

    def test_non_text_secret_is_refused(self):
        self.assertIn("key", errors_of({"key": secret()}, {"key": {"a": 1}}))

    def test_customer_secret_is_refused_when_the_provider_sets_it(self):
        detail = errors_of({"key": secret(secret_source="provider")}, {"key": SECRET})
        self.assertIn("key", detail)
        self.assertNotIn(SECRET, flat(detail))

    def test_required_customer_option_needs_a_value(self):
        options = {"key": {**secret(), "required": True}}
        self.assertIn("key", errors_of(options, {}))
        self.assertIn("key", errors_of(options, {"key": ""}))
        clean(options, {"key": SECRET})

    def test_required_provider_option_needs_no_value(self):
        clean({"key": {**secret(secret_source="provider"), "required": True}}, {})

    def test_empty_optional_value_sets_nothing(self):
        self.assertEqual(clean({"key": secret()}, {"key": ""}), {})

    def test_secret_is_refused_in_the_providers_direct_update(self):
        detail = errors_of({"key": secret()}, {"key": SECRET}, trust_provider_end=True)
        self.assertIn("key", detail)

    def test_ipsec_value_carries_no_key(self):
        # A key sent inside the VPN value the old way is dropped, not stored.
        values = clean({"vpn": vpn()}, {"vpn": {**VPN_VALUE, "psk": SECRET}})
        self.assertNotIn(SECRET, flat(values))
        self.assertNotIn("psk", values["vpn"])


@ddt
class PolicyOnSaveTest(test.APISimpleTestCase):
    def validate(self, options, for_resource=False):
        serializer = serializers.OfferingOptionsSerializer(
            data={"order": list(options), "options": options}, for_resource=for_resource
        )
        valid = serializer.is_valid()
        return valid, serializer

    def configs(self, serializer, name):
        return json.loads(
            json.dumps(serializer.validated_data["options"][name]["default_configs"])
        )

    def test_cidr_policy_is_kept_normalised(self):
        valid, serializer = self.validate(
            {
                "net": cidr(
                    ip_version="ipv4",
                    prefix_length_min=16,
                    prefix_length_max=28,
                    allowed_ranges=["10.0.0.0/8"],
                    excluded_ranges=["10.96.0.0/12"],
                    multiple=True,
                    max_items=5,
                    topology_mode="1-datacenter",
                    secret_source="provider",
                )
            }
        )
        self.assertTrue(valid, serializer.errors)
        self.assertEqual(
            self.configs(serializer, "net"),
            {
                "ip_version": "ipv4",
                "prefix_length_min": 16,
                "prefix_length_max": 28,
                "allowed_ranges": ["10.0.0.0/8"],
                "excluded_ranges": ["10.96.0.0/12"],
                "multiple": True,
                "max_items": 5,
            },
        )

    def test_kubernetes_option_keeps_its_keys_only(self):
        valid, serializer = self.validate(
            {
                "k8s": {
                    "type": "k8s_cluster_config",
                    "label": "Cluster",
                    "default_configs": {
                        "topology_mode": "1-datacenter",
                        "ip_version": "ipv4",
                        "customer_can_reveal": True,
                    },
                }
            }
        )
        self.assertTrue(valid, serializer.errors)
        self.assertEqual(
            self.configs(serializer, "k8s"), {"topology_mode": "1-datacenter"}
        )

    @data(
        {"allowed_ranges": ["10.0.0.5/8"]},
        {"excluded_ranges": ["nonsense"]},
        {"ip_version": "ipv4", "prefix_length_max": 64},
        {"prefix_length_min": 28, "prefix_length_max": 16},
        {"max_items": 0},
        {"ip_version": "ipv4", "allowed_ranges": ["fd00::/8"]},
    )
    def test_invalid_cidr_policy_is_refused(self, configs):
        valid, serializer = self.validate({"net": cidr(**configs)})
        self.assertFalse(valid)
        self.assertIn("default_configs", flat(serializer.errors))

    @data(
        {"provider_gateway_ip": "not-an-ip"},
        {"provider_local_subnets": ["10.0.0.1/16"]},
        {"phase1": {"encryption": ["aes256"], "defaults": {"encryption": "aes128"}}},
        {
            "phase1": {
                "lifetime_min": 3600,
                "lifetime_max": 7200,
                "defaults": {"lifetime": 28800},
            }
        },
        {"phase2": {"lifetime_min": 7200, "lifetime_max": 3600}},
        {"phase2": {"pfs_groups": [14], "defaults": {"pfs_group": None}}},
        {"remote_subnets": {"allowed_ranges": ["bad"]}},
    )
    def test_invalid_ipsec_policy_is_refused(self, configs):
        valid, serializer = self.validate({"vpn": vpn(**configs)})
        self.assertFalse(valid)
        self.assertIn("default_configs", flat(serializer.errors))

    def test_ipsec_policy_is_kept_without_key_settings(self):
        configs = {
            "ikev1_allowed": False,
            "allow_private_peer": True,
            "provider_gateway_ip": "203.0.113.10",
            "provider_local_subnets": ["10.0.0.0/16"],
            "remote_subnets": {"ip_version": "ipv4", "max_items": 10},
            "phase1": {
                "encryption": ["aes256", "aes256gcm16"],
                "dh_groups": [14, 19],
                "defaults": {"encryption": "aes256", "dh_group": 19},
            },
            "phase2": {"pfs_groups": [None, 14]},
        }
        valid, serializer = self.validate(
            {"vpn": vpn(**configs, psk_source="provider", secret_source="provider")}
        )
        self.assertTrue(valid, serializer.errors)
        self.assertEqual(self.configs(serializer, "vpn"), configs)

    def test_secret_policy_is_kept(self):
        configs = {
            "secret_source": "provider",
            "customer_can_reveal": False,
            "min_length": 8,
            "max_length": 255,
            "multiline": True,
        }
        valid, serializer = self.validate({"key": secret(**configs, ip_version="ipv4")})
        self.assertTrue(valid, serializer.errors)
        self.assertEqual(self.configs(serializer, "key"), configs)

    @data(
        {"min_length": 0},
        {"max_length": 0},
        {"max_length": 65537},
        {"min_length": 10, "max_length": 5},
        {"min_length": 5000},
        {"secret_source": "someone"},
        {"customer_can_reveal": "maybe"},
    )
    def test_invalid_secret_policy_is_refused(self, configs):
        valid, serializer = self.validate({"key": secret(**configs)})
        self.assertFalse(valid)
        self.assertIn("default_configs", flat(serializer.errors))

    def test_secret_option_has_no_default(self):
        valid, serializer = self.validate({"key": {**secret(), "default": SECRET}})
        self.assertFalse(valid)
        self.assertIn("default", serializer.errors["options"]["key"])
        self.assertNotIn(SECRET, flat(serializer.errors))

    def test_no_overlap_with_must_name_a_cidr_option(self):
        valid, serializer = self.validate(
            {"vpn": vpn(no_overlap_with=["missing"]), "net": cidr()}
        )
        self.assertFalse(valid)
        valid, serializer = self.validate(
            {"vpn": vpn(no_overlap_with=["net"]), "net": cidr()}
        )
        self.assertTrue(valid, serializer.errors)


class OfferingResourceOptionReferenceTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.options = {"order": ["net"], "options": {"net": cidr()}}
        self.offering.save()
        self.client.force_authenticate(self.fixture.staff)

    def update_resource_options(self, options):
        url = factories.OfferingFactory.get_url(
            self.offering, "update_resource_options"
        )
        return self.client.post(
            url,
            {"resource_options": {"order": list(options), "options": options}},
            format="json",
        )

    def test_resource_option_may_name_an_order_cidr_option(self):
        response = self.update_resource_options({"vpn": vpn(no_overlap_with=["net"])})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_resource_option_naming_nothing_is_refused(self):
        response = self.update_resource_options({"vpn": vpn(no_overlap_with=["nope"])})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_named_order_option_cannot_be_removed(self):
        self.update_resource_options({"vpn": vpn(no_overlap_with=["net"])})
        url = factories.OfferingFactory.get_url(self.offering, "update_options")
        response = self.client.post(
            url, {"options": {"order": [], "options": {}}}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class SecretAbsenceMixin:
    def assert_secret_absent(self, *payloads):
        for payload in payloads:
            text = (
                payload
                if isinstance(payload, str)
                else json.dumps(payload, default=str)
            )
            for value in (
                SECRET,
                OTHER_SECRET,
                PEM,
                "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7Vb8sHdQzLnY",
            ):
                self.assertNotIn(value, text)

    def assert_secret_absent_everywhere(self):
        """No stored attribute, option or event holds a secret in plain text."""
        for order in models.Order.objects.all():
            self.assert_secret_absent(order.attributes)
        for resource in models.Resource.objects.all():
            self.assert_secret_absent(resource.attributes, resource.options)
        for event in Event.objects.all():
            self.assert_secret_absent(event.message, event.context)
        for stored in models.ResourceOptionSecret.objects.all():
            self.assert_secret_absent(stored.value)


class SecretOrderTest(SecretAbsenceMixin, BaseOrderCreateTest):
    def make_offering(self, **secret_policy):
        return factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            options={
                "order": ["net", "vpn", "vpn_key"],
                "options": {
                    "net": {**cidr(allowed_ranges=["10.0.0.0/8"]), "required": True},
                    "vpn": {
                        **vpn(
                            no_overlap_with=["net"], provider_gateway_ip="203.0.113.10"
                        ),
                        "required": True,
                    },
                    "vpn_key": {
                        **secret(min_length=8, **secret_policy),
                        "required": True,
                    },
                },
            },
            resource_options={
                "order": ["net", "vpn", "vpn_key"],
                "options": {
                    "net": cidr(),
                    "vpn": vpn(no_overlap_with=["net"]),
                    "vpn_key": secret(min_length=8, **secret_policy),
                },
            },
        )

    def order(self, offering, attributes):
        return self.create_order(
            self.fixture.owner, offering, add_payload={"attributes": attributes}
        )

    def test_order_stores_clean_values_and_the_secret_encrypted(self):
        response = self.order(
            self.make_offering(),
            {"net": "10.20.0.0/16", "vpn": VPN_VALUE, "vpn_key": SECRET},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assert_secret_absent(response.content.decode())
        order = models.Order.objects.get(uuid=response.data["uuid"])
        resource = order.resource
        self.assertEqual(order.attributes["net"], "10.20.0.0/16")
        self.assertNotIn("vpn_key", order.attributes)
        self.assertEqual(
            order.attributes["vpn"]["provider"]["gateway_ip"], "203.0.113.10"
        )
        # Seeded from the order, without the secret.
        self.assertEqual(resource.options["vpn"], order.attributes["vpn"])
        self.assertNotIn("vpn_key", resource.options)
        self.assertNotIn("vpn_key", resource.attributes)
        stored = models.ResourceOptionSecret.objects.get(resource=resource)
        self.assertEqual(stored.option_name, "vpn_key")
        self.assertEqual(stored.source, "customer")
        self.assertEqual(stored.set_by, self.fixture.owner)
        self.assertEqual(encryption.decrypt_value(stored.value), SECRET)
        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.MARKETPLACE_RESOURCE_OPTION_SECRET_SET
            ).exists()
        )
        self.assert_secret_absent_everywhere()

    def test_multiline_secret_is_ordered_and_stored(self):
        response = self.order(
            self.make_offering(multiline=True),
            {"net": "10.20.0.0/16", "vpn": VPN_VALUE, "vpn_key": PEM},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        stored = models.ResourceOptionSecret.objects.get()
        self.assertEqual(encryption.decrypt_value(stored.value), PEM)
        self.assert_secret_absent(response.content.decode())
        self.assert_secret_absent_everywhere()

    def test_multiline_secret_is_refused_unless_allowed(self):
        response = self.order(
            self.make_offering(),
            {"net": "10.20.0.0/16", "vpn": VPN_VALUE, "vpn_key": PEM},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("vpn_key", response.data)
        self.assert_secret_absent(response.content.decode())
        self.assertFalse(models.ResourceOptionSecret.objects.exists())

    def test_order_detail_shows_only_whether_the_secret_is_set(self):
        response = self.order(
            self.make_offering(),
            {"net": "10.20.0.0/16", "vpn": VPN_VALUE, "vpn_key": SECRET},
        )
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.client.force_authenticate(self.fixture.staff)
        detail = self.client.get(factories.OrderFactory.get_url(order))
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertTrue(detail.data["option_secrets"]["vpn_key"]["is_set"])
        self.assertEqual(detail.data["option_secrets"]["vpn_key"]["source"], "customer")
        self.assertNotIn("vpn", detail.data["option_secrets"])
        self.assert_secret_absent(detail.content.decode())
        listing = self.client.get(factories.OrderFactory.get_list_url())
        self.assert_secret_absent(listing.content.decode())

    def test_required_secret_without_a_value_is_refused(self):
        response = self.order(
            self.make_offering(), {"net": "10.20.0.0/16", "vpn": VPN_VALUE}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("vpn_key", response.data)

    def test_refused_order_does_not_echo_or_keep_the_secret(self):
        response = self.order(
            self.make_offering(),
            {
                "net": "10.20.0.0/16",
                "vpn": {**VPN_VALUE, "peer_gateway_ip": "10.0.0.1"},
                "vpn_key": SECRET,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assert_secret_absent(response.content.decode())
        self.assertFalse(models.ResourceOptionSecret.objects.exists())
        self.assert_secret_absent_everywhere()

    def test_too_short_secret_is_refused_without_echoing_it(self):
        response = self.order(
            self.make_offering(),
            {"net": "10.20.0.0/16", "vpn": VPN_VALUE, "vpn_key": "abc123"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("abc123", response.content.decode())

    def test_overlap_with_the_ordered_network_is_refused(self):
        response = self.order(
            self.make_offering(),
            {
                "net": "10.20.0.0/16",
                "vpn": {**VPN_VALUE, "remote_subnets": ["10.20.5.0/24"]},
                "vpn_key": SECRET,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("remote_subnets", response.data["vpn"])

    def test_provider_sourced_secret_takes_no_value_from_the_customer(self):
        offering = self.make_offering(secret_source="provider")
        response = self.order(
            offering, {"net": "10.20.0.0/16", "vpn": VPN_VALUE, "vpn_key": SECRET}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assert_secret_absent(response.content.decode())
        response = self.order(offering, {"net": "10.20.0.0/16", "vpn": VPN_VALUE})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertFalse(models.ResourceOptionSecret.objects.exists())

    def test_resource_only_option_is_not_stored_from_unchecked_attributes(self):
        # No order options at all: the attributes are stored as sent.
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            options={},
            resource_options={"order": ["key"], "options": {"key": secret()}},
        )
        response = self.order(offering, {"key": SECRET, "name": "kept"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.attributes, {"name": "kept"})
        self.assertNotIn("key", order.resource.options or {})
        self.assertFalse(models.ResourceOptionSecret.objects.exists())
        self.assert_secret_absent_everywhere()

    def test_pending_order_edit_replaces_the_secret(self):
        offering = self.make_offering()
        response = self.order(
            offering, {"net": "10.20.0.0/16", "vpn": VPN_VALUE, "vpn_key": SECRET}
        )
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            factories.OrderFactory.get_url(order),
            {
                "attributes": {
                    "net": "10.30.0.0/16",
                    "vpn": VPN_VALUE,
                    "vpn_key": OTHER_SECRET,
                }
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        stored = models.ResourceOptionSecret.objects.get(resource=order.resource)
        self.assertEqual(encryption.decrypt_value(stored.value), OTHER_SECRET)
        self.assert_secret_absent(response.content.decode())
        self.assert_secret_absent_everywhere()


class ResourceSecretTestBase(SecretAbsenceMixin, test.APITestCase):
    secret_policy = {}

    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.options = {"order": ["net"], "options": {"net": cidr()}}
        self.offering.resource_options = {
            "order": ["vpn", "key"],
            "options": {
                "vpn": vpn(no_overlap_with=["net"], provider_gateway_ip="203.0.113.10"),
                "key": secret(**self.secret_policy),
            },
        }
        self.offering.save()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.attributes = {"net": "10.20.0.0/16"}
        self.resource.save()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_OPTIONS)
        CustomerRole.OWNER.add_permission(PermissionEnum.REVEAL_RESOURCE_API_KEY)
        ProjectRole.MEMBER.add_permission(PermissionEnum.REVEAL_RESOURCE_API_KEY)

    def url(self, action, provider=False):
        if provider:
            return factories.ResourceFactory.get_provider_resource_url(
                self.resource, action
            )
        return factories.ResourceFactory.get_url(self.resource, action)

    def post(self, user, action, payload, provider=False):
        self.client.force_authenticate(user)
        return self.client.post(self.url(action, provider), payload, format="json")

    def reveal(self, user, provider=False):
        return self.post(user, "reveal_option_secret", {"option_name": "key"}, provider)

    def store(self, value=SECRET, source="customer"):
        return models.ResourceOptionSecret.objects.create(
            resource=self.resource,
            option_name="key",
            value=encryption.encrypt_value(value),
            source=source,
        )


class ResourceOptionsUpdateTest(ResourceSecretTestBase):
    def test_customer_update_stores_the_secret_and_keeps_the_provider_end(self):
        self.resource.options = {
            "vpn": {"provider": {"gateway_ip": "203.0.113.99", "local_subnets": []}}
        }
        self.resource.save()
        response = self.post(
            self.fixture.owner,
            "update_options",
            {
                "options": {
                    "vpn": {**VPN_VALUE, "provider": {"gateway_ip": "1.1.1.1"}},
                    "key": SECRET,
                }
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.options["vpn"]["provider"]["gateway_ip"], "203.0.113.99"
        )
        self.assertNotIn("key", self.resource.options)
        self.assertEqual(
            encryption.decrypt_value(models.ResourceOptionSecret.objects.get().value),
            SECRET,
        )
        self.assert_secret_absent_everywhere()

    def test_invalid_secret_in_update_is_refused_without_echoing_it(self):
        response = self.post(
            self.fixture.owner,
            "update_options",
            {"options": {"key": "two\nlines-secret"}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("lines-secret", response.content.decode())
        self.assertFalse(models.ResourceOptionSecret.objects.exists())

    def test_overlap_with_the_ordered_network_is_refused(self):
        response = self.post(
            self.fixture.owner,
            "update_options",
            {"options": {"vpn": {**VPN_VALUE, "remote_subnets": ["10.20.1.0/24"]}}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("remote_subnets", response.data["options"]["vpn"])

    def test_update_order_never_carries_the_secret(self):
        self.offering.plugin_options = {"create_orders_on_resource_option_change": True}
        self.offering.save()
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        response = self.post(
            self.fixture.owner,
            "update_options",
            {"options": {"vpn": VPN_VALUE, "key": SECRET}},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertIn("vpn", order.attributes["new_options"])
        self.assertNotIn("key", order.attributes["new_options"])
        self.assertTrue(models.ResourceOptionSecret.objects.exists())
        self.assert_secret_absent_everywhere()

    def test_provider_overrides_the_provider_end(self):
        response = self.post(
            self.fixture.offering_owner,
            "update_options_direct",
            {
                "options": {
                    "vpn": {
                        **VPN_VALUE,
                        "provider": {
                            "gateway_ip": "203.0.113.20",
                            "local_subnets": ["10.9.0.0/16"],
                        },
                    }
                }
            },
            provider=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.options["vpn"]["provider"],
            {"gateway_ip": "203.0.113.20", "local_subnets": ["10.9.0.0/16"]},
        )

    def test_provider_direct_update_refuses_a_secret(self):
        response = self.post(
            self.fixture.offering_owner,
            "update_options_direct",
            {"options": {"key": SECRET}},
            provider=True,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assert_secret_absent(response.content.decode())
        self.assert_secret_absent_everywhere()

    def test_resource_shows_only_whether_the_secret_is_set(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(factories.ResourceFactory.get_url(self.resource))
        self.assertEqual(
            response.data["option_secrets"],
            {"key": {"is_set": False, "source": None, "modified": None}},
        )
        self.store()
        response = self.client.get(factories.ResourceFactory.get_url(self.resource))
        self.assertTrue(response.data["option_secrets"]["key"]["is_set"])
        self.assertEqual(response.data["option_secrets"]["key"]["source"], "customer")
        self.assert_secret_absent(response.content.decode())
        listing = self.client.get(factories.ResourceFactory.get_list_url())
        self.assertTrue(listing.data[0]["option_secrets"]["key"]["is_set"])
        self.assert_secret_absent(listing.content.decode())


@ddt
class CustomerSetSecretTest(ResourceSecretTestBase):
    def test_owner_sets_and_replaces_the_secret(self):
        for value in (SECRET, OTHER_SECRET):
            response = self.post(
                self.fixture.owner,
                "set_option_secret",
                {"option_name": "key", "value": value},
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            self.assertTrue(response.data["is_set"])
            self.assert_secret_absent(response.content.decode())
        stored = models.ResourceOptionSecret.objects.get()
        self.assertEqual(encryption.decrypt_value(stored.value), OTHER_SECRET)
        self.assertEqual(stored.source, "customer")
        self.assertEqual(
            Event.objects.filter(
                event_type=EventType.MARKETPLACE_RESOURCE_OPTION_SECRET_SET
            ).count(),
            2,
        )
        self.assert_secret_absent_everywhere()

    @data("admin", "manager", "member")
    def test_roles_that_cannot_change_options_cannot_set_it(self, user):
        response = self.post(
            getattr(self.fixture, user),
            "set_option_secret",
            {"option_name": "key", "value": SECRET},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.ResourceOptionSecret.objects.exists())

    def test_value_outside_the_policy_is_refused_without_echoing_it(self):
        response = self.post(
            self.fixture.owner,
            "set_option_secret",
            {"option_name": "key", "value": "first line\nsecond-line-value"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("value", response.data)
        self.assertNotIn("second-line-value", response.content.decode())

    def test_option_that_is_not_a_secret_is_refused(self):
        response = self.post(
            self.fixture.owner,
            "set_option_secret",
            {"option_name": "vpn", "value": SECRET},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assert_secret_absent(response.content.decode())

    def test_terminated_resource_is_refused(self):
        self.resource.state = ResourceStates.TERMINATED
        self.resource.save()
        response = self.post(
            self.fixture.owner,
            "set_option_secret",
            {"option_name": "key", "value": SECRET},
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_customer_cannot_reveal_by_default(self):
        self.store()
        response = self.reveal(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assert_secret_absent(response.content.decode())


class MultilineSecretTest(ResourceSecretTestBase):
    secret_policy = {"multiline": True, "customer_can_reveal": True}

    def test_pem_is_set_and_revealed_unchanged(self):
        response = self.post(
            self.fixture.owner,
            "set_option_secret",
            {"option_name": "key", "value": PEM},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assert_secret_absent(response.content.decode())
        response = self.reveal(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["value"], PEM)
        self.assert_secret_absent_everywhere()


@ddt
class CustomerRevealSecretTest(ResourceSecretTestBase):
    secret_policy = {"secret_source": "provider"}

    def test_customer_cannot_set_a_provider_sourced_secret(self):
        response = self.post(
            self.fixture.owner,
            "set_option_secret",
            {"option_name": "key", "value": SECRET},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("owner", "member")
    def test_holders_of_the_reveal_permission_read_the_secret(self, user):
        self.store(source="provider")
        response = self.reveal(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data, {"option_name": "key", "value": SECRET})
        self.assertEqual(response["Cache-Control"], "no-store")
        event = Event.objects.get(
            event_type=EventType.MARKETPLACE_RESOURCE_OPTION_SECRET_REVEALED
        )
        self.assertIn(getattr(self.fixture, user).username, event.message)
        self.assert_secret_absent(event.message, event.context)

    @data("admin", "manager", "staff", "offering_owner")
    def test_others_cannot_reveal_it(self, user):
        self.store(source="provider")
        response = self.reveal(getattr(self.fixture, user))
        self.assertIn(
            response.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)
        )
        self.assertFalse(
            Event.objects.filter(
                event_type=EventType.MARKETPLACE_RESOURCE_OPTION_SECRET_REVEALED
            ).exists()
        )

    def test_the_policy_decides_not_who_set_the_value(self):
        # A provider-sourced option revealable by default, also when the
        # provider stored a value the customer gave it some other way.
        self.store(source="customer")
        self.assertEqual(
            self.reveal(self.fixture.owner).status_code, status.HTTP_200_OK
        )

    def test_impersonated_reveal_names_the_impersonator(self):
        self.store(source="provider")
        token = Token.objects.get_or_create(user=self.fixture.staff)[0]
        self.client.credentials(
            **{
                "HTTP_AUTHORIZATION": f"Token {token.key}",
                IMPERSONATED_USER_HEADER: self.fixture.owner.uuid.hex,
            }
        )
        response = self.client.post(
            self.url("reveal_option_secret"), {"option_name": "key"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        event = Event.objects.get(
            event_type=EventType.MARKETPLACE_RESOURCE_OPTION_SECRET_REVEALED
        )
        self.assertIn("impersonated by", event.message)
        self.assertIn(self.fixture.staff.username, event.message)

    def test_nothing_set_is_a_conflict(self):
        response = self.reveal(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_undecryptable_secret_is_a_conflict(self):
        models.ResourceOptionSecret.objects.create(
            resource=self.resource,
            option_name="key",
            value="not-a-token",
            source="provider",
        )
        response = self.reveal(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("FIELD_ENCRYPTION_KEY", response.data["detail"])


class CustomerRevealTurnedOffTest(ResourceSecretTestBase):
    secret_policy = {"secret_source": "provider", "customer_can_reveal": False}

    def test_provider_sourced_secret_stays_hidden_when_the_policy_says_so(self):
        self.store(source="provider")
        response = self.reveal(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            Event.objects.filter(
                event_type=EventType.MARKETPLACE_RESOURCE_OPTION_SECRET_REVEALED
            ).exists()
        )


@ddt
class ProviderSecretTest(ResourceSecretTestBase):
    secret_policy = {"secret_source": "provider"}

    def test_provider_sets_and_reads_the_secret(self):
        response = self.post(
            self.fixture.offering_owner,
            "set_option_secret",
            {"option_name": "key", "value": SECRET},
            provider=True,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["source"], "provider")
        self.assert_secret_absent(response.content.decode())
        response = self.reveal(self.fixture.offering_owner, provider=True)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["value"], SECRET)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(
            Event.objects.filter(
                event_type__in=(
                    EventType.MARKETPLACE_RESOURCE_OPTION_SECRET_SET,
                    EventType.MARKETPLACE_RESOURCE_OPTION_SECRET_REVEALED,
                )
            ).count(),
            2,
        )
        self.assert_secret_absent_everywhere()

    def test_provider_reads_a_customer_secret_hidden_from_the_customer(self):
        self.offering.resource_options["options"]["key"] = secret(
            secret_source="customer"
        )
        self.offering.save()
        self.store(source="customer")
        response = self.reveal(self.fixture.offering_owner, provider=True)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @data("owner", "member", "admin")
    def test_consumers_cannot_use_the_provider_actions(self, user):
        self.store(source="provider")
        for action, payload in (
            ("set_option_secret", {"option_name": "key", "value": OTHER_SECRET}),
            ("reveal_option_secret", {"option_name": "key"}),
        ):
            response = self.post(
                getattr(self.fixture, user), action, payload, provider=True
            )
            self.assertIn(
                response.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
            )
        self.assertEqual(
            encryption.decrypt_value(models.ResourceOptionSecret.objects.get().value),
            SECRET,
        )


class _Capture(Transport):
    def __init__(self):
        super().__init__()
        self.events = []

    def capture_envelope(self, envelope):
        event = envelope.get_event()
        if event:
            self.events.append(event)


class SentrySecretRedactionTest(SimpleTestCase):
    def setUp(self):
        sentry.reset_secret_values()
        self.addCleanup(sentry.reset_secret_values)

    def test_set_option_secret_body_value_is_filtered_by_path(self):
        event = sentry.redact_option_secrets(
            {
                "request": {
                    "url": "https://waldur.example.com/api/marketplace-resources/abc/set_option_secret/",
                    "data": {"option_name": "key", "value": SECRET},
                }
            }
        )
        self.assertEqual(
            event["request"]["data"], {"option_name": "key", "value": "[Filtered]"}
        )

    def test_raw_set_option_secret_body_is_filtered(self):
        event = sentry.redact_option_secrets(
            {
                "request": {
                    "url": "/api/marketplace-provider-resources/abc/set_option_secret/",
                    "data": json.dumps({"value": SECRET}),
                }
            }
        )
        self.assertEqual(event["request"]["data"], "[Filtered]")

    def test_values_a_secret_option_received_are_filtered_anywhere(self):
        # A secret option's name is free-form, so its value is found by content.
        clean({"vpn_key": secret(multiline=True)}, {"vpn_key": PEM})
        clean({"api_token": secret()}, {"api_token": SECRET})
        event = sentry.redact_option_secrets(
            {
                "request": {
                    "url": "/api/marketplace-orders/",
                    "data": {
                        "attributes": {"vpn_key": PEM, "api_token": SECRET, "name": "x"}
                    },
                },
                "extra": {"note": f"failed with {SECRET} inside"},
            }
        )
        text = json.dumps(event)
        self.assert_absent(text)
        self.assertEqual(event["request"]["data"]["attributes"]["name"], "x")

    def test_reset_forgets_the_values_of_an_earlier_request(self):
        clean({"api_token": secret()}, {"api_token": SECRET})
        sentry.reset_secret_values()
        event = sentry.redact_option_secrets({"extra": {"note": SECRET}})
        self.assertEqual(event["extra"]["note"], SECRET)

    def test_a_short_secret_is_filtered_only_as_a_whole_value(self):
        clean({"pin": secret()}, {"pin": "abc"})
        event = sentry.redact_option_secrets(
            {"extra": {"pin": "abc", "word": "abcdef"}}
        )
        self.assertEqual(event["extra"], {"pin": "[Filtered]", "word": "abcdef"})

    def test_frame_locals_of_a_captured_error_are_filtered(self):
        clean({"vpn_key": secret()}, {"vpn_key": SECRET})
        clean({"tls_key": secret(multiline=True)}, {"tls_key": PEM})

        def fail():
            token = SECRET  # noqa: F841
            note = f"request carried {PEM}"  # noqa: F841
            raise RuntimeError("backend refused")

        transport = _Capture()
        client = sentry_sdk.Client(
            dsn="https://key@sentry.invalid/1",
            transport=transport,
            event_scrubber=sentry.event_scrubber(),
            before_send=sentry.before_send,
        )
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(client)
            try:
                fail()
            except RuntimeError:
                scope.capture_exception()
        client.flush()
        self.assertTrue(transport.events)
        frames = transport.events[0]["exception"]["values"][0]["stacktrace"]["frames"]
        frame_vars = next(f["vars"] for f in frames if f["function"] == "fail")
        self.assertIn("token", frame_vars)
        self.assert_absent(json.dumps(transport.events, default=str))

    def assert_absent(self, text):
        self.assertNotIn(SECRET, text)
        self.assertNotIn(
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7Vb8sHdQzLnY", text
        )
        self.assertNotIn("BEGIN PRIVATE KEY-----\\n", text)


PROVIDER_OVERLAP = "{subnet} overlaps the provider's subnet {provider_subnet}."


class ProviderSubnetOverlapValueTest(test.APISimpleTestCase):
    options = {"vpn": vpn(provider_local_subnets=["192.168.0.0/16"])}

    def test_overlap_with_the_policys_provider_subnets_is_refused(self):
        detail = errors_of(self.options, {"vpn": VPN_VALUE})
        self.assertEqual(
            [str(message) for message in detail["vpn"]["remote_subnets"]],
            [
                PROVIDER_OVERLAP.format(
                    subnet="192.168.10.0/24", provider_subnet="192.168.0.0/16"
                )
            ],
        )

    def test_stored_provider_end_wins_over_the_policy_defaults(self):
        stored = {
            "vpn": {"provider": {"gateway_ip": None, "local_subnets": ["10.0.0.0/16"]}}
        }
        # The policy's 192.168.0.0/16 no longer applies to this resource.
        clean(self.options, {"vpn": VPN_VALUE}, stored=stored)
        detail = errors_of(
            self.options,
            {"vpn": {**VPN_VALUE, "remote_subnets": ["10.0.4.0/24"]}},
            stored=stored,
        )
        self.assertEqual(
            str(detail["vpn"]["remote_subnets"][0]),
            PROVIDER_OVERLAP.format(
                subnet="10.0.4.0/24", provider_subnet="10.0.0.0/16"
            ),
        )

    def test_customer_cannot_dodge_it_by_sending_a_provider_end(self):
        detail = errors_of(
            self.options,
            {"vpn": {**VPN_VALUE, "provider": {"local_subnets": ["10.0.0.0/16"]}}},
        )
        self.assertIn("remote_subnets", detail["vpn"])

    def test_provider_override_overlapping_the_remote_subnets_is_refused(self):
        detail = errors_of(
            {"vpn": vpn()},
            {"vpn": {**VPN_VALUE, "provider": {"local_subnets": ["192.168.10.0/25"]}}},
            trust_provider_end=True,
        )
        self.assertNotIn("remote_subnets", detail["vpn"])
        self.assertEqual(
            str(detail["vpn"]["provider"]["local_subnets"][0]),
            "192.168.10.0/25 overlaps the remote subnet 192.168.10.0/24.",
        )

    def test_disjoint_networks_are_accepted(self):
        value = clean(
            self.options, {"vpn": {**VPN_VALUE, "remote_subnets": ["172.16.0.0/24"]}}
        )["vpn"]
        self.assertEqual(value["provider"]["local_subnets"], ["192.168.0.0/16"])


class ProviderSubnetOverlapOrderTest(BaseOrderCreateTest):
    def test_order_overlapping_the_provider_subnets_is_refused(self):
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            options={
                "order": ["vpn"],
                "options": {"vpn": vpn(provider_local_subnets=["192.168.0.0/16"])},
            },
        )
        response = self.create_order(
            self.fixture.owner, offering, add_payload={"attributes": {"vpn": VPN_VALUE}}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["vpn"]["remote_subnets"],
            [
                PROVIDER_OVERLAP.format(
                    subnet="192.168.10.0/24", provider_subnet="192.168.0.0/16"
                )
            ],
        )
        response = self.create_order(
            self.fixture.owner,
            offering,
            add_payload={
                "attributes": {
                    "vpn": {**VPN_VALUE, "remote_subnets": ["172.16.0.0/24"]}
                }
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class ProviderSubnetOverlapResourceTest(ResourceSecretTestBase):
    def setUp(self):
        super().setUp()
        self.resource.options = {
            "vpn": clean(
                {"vpn": vpn(provider_local_subnets=["10.9.0.0/16"])},
                {"vpn": {**VPN_VALUE, "remote_subnets": ["172.16.0.0/24"]}},
            )["vpn"]
        }
        self.resource.save()

    def test_customer_update_overlapping_the_stored_provider_end_is_refused(self):
        # The policy has no provider subnets; the resource's stored end counts.
        response = self.post(
            self.fixture.owner,
            "update_options",
            {"options": {"vpn": {**VPN_VALUE, "remote_subnets": ["10.9.1.0/24"]}}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["options"]["vpn"]["remote_subnets"],
            [
                PROVIDER_OVERLAP.format(
                    subnet="10.9.1.0/24", provider_subnet="10.9.0.0/16"
                )
            ],
        )
        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.options["vpn"]["remote_subnets"], ["172.16.0.0/24"]
        )

    def test_provider_override_overlapping_the_remote_subnets_is_refused(self):
        stored = self.resource.options["vpn"]
        response = self.post(
            self.fixture.offering_owner,
            "update_options_direct",
            {
                "options": {
                    "vpn": {
                        **stored,
                        "provider": {
                            "gateway_ip": "203.0.113.20",
                            "local_subnets": ["172.16.0.0/16"],
                        },
                    }
                }
            },
            provider=True,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["options"]["vpn"]["provider"]["local_subnets"],
            ["172.16.0.0/16 overlaps the remote subnet 172.16.0.0/24."],
        )
        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.options["vpn"]["provider"]["local_subnets"], ["10.9.0.0/16"]
        )
