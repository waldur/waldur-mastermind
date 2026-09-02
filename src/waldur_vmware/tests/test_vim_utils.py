"""Unit tests for the vim25 helpers that need no vCenter."""

from django.test import SimpleTestCase

from waldur_vmware import constants, vim_utils


class GuestOsMappingTest(SimpleTestCase):
    def test_every_offered_guest_os_resolves(self):
        """Every choice the API offers must be provisionable.

        `guest_os` is a choice field backed by GUEST_OS_CHOICES, so a key that
        does not resolve is an order that always fails at provisioning time.
        """
        unresolved = []
        for guest_os in constants.GUEST_OS_CHOICES:
            try:
                vim_utils.guest_os_to_guest_id(guest_os)
            except ValueError:
                unresolved.append(guest_os)
        self.assertEqual(unresolved, [])

    def test_guest_name_carrying_guest_in_the_middle_resolves(self):
        """`otherGuest64` spells `Guest` mid-identifier, unlike every other one.

        Treating `Guest` as a suffix truncates it to `othergu` and loses the
        mapping, so "Other Operating System (64 bit)" becomes unorderable.
        """
        self.assertEqual(vim_utils.guest_os_to_guest_id("OTHER_64"), "otherGuest64")

    def test_representative_identifiers_map_to_vim_spelling(self):
        self.assertEqual(vim_utils.guest_os_to_guest_id("UBUNTU_64"), "ubuntu64Guest")
        self.assertEqual(vim_utils.guest_os_to_guest_id("WIN_31"), "win31Guest")
        self.assertEqual(vim_utils.guest_os_to_guest_id("DOS"), "dosGuest")

    def test_unknown_guest_os_is_rejected(self):
        with self.assertRaises(ValueError):
            vim_utils.guest_os_to_guest_id("NO_SUCH_OS")

    def test_empty_guest_os_is_rejected(self):
        """The enumeration subclasses str, so a table built by walking `dir()`
        picks up string methods and maps an empty value onto `guestId="Array"`.
        """
        with self.assertRaises(ValueError):
            vim_utils.guest_os_to_guest_id("")


class StateMappingTest(SimpleTestCase):
    def test_power_states(self):
        self.assertEqual(vim_utils.map_power_state("poweredOn"), "POWERED_ON")
        self.assertEqual(vim_utils.map_power_state("poweredOff"), "POWERED_OFF")
        self.assertEqual(vim_utils.map_power_state("suspended"), "SUSPENDED")

    def test_tools_states(self):
        self.assertEqual(vim_utils.map_tools_state("guestToolsRunning"), "RUNNING")
        self.assertEqual(
            vim_utils.map_tools_state("guestToolsNotRunning"), "NOT_RUNNING"
        )
        self.assertEqual(
            vim_utils.map_tools_state("guestToolsExecutingScripts"), "STARTING"
        )

    def test_unknown_guest_state_falls_back_to_unavailable(self):
        """vCenter reports guest states this plugin has no constant for, and a
        pull task must not crash on one."""
        self.assertEqual(vim_utils.map_guest_power_state("running"), "RUNNING")
        self.assertEqual(vim_utils.map_guest_power_state(None), "UNAVAILABLE")
        self.assertEqual(vim_utils.map_guest_power_state("somethingNew"), "UNAVAILABLE")

    def test_port_state(self):
        self.assertEqual(vim_utils.map_port_state(True), "CONNECTED")
        self.assertEqual(vim_utils.map_port_state(False), "NOT_CONNECTED")
