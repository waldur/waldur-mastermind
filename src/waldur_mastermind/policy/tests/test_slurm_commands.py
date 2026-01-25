"""Tests for SLURM command generation module."""

from django.test import SimpleTestCase

from waldur_mastermind.policy import slurm_commands


class SlurmCommandGenerationTest(SimpleTestCase):
    """Test SLURM command generation functions."""

    def test_generate_fairshare_command(self):
        """Test fairshare command generation."""
        result = slurm_commands.generate_fairshare_command("test_account", 500)

        self.assertEqual(result["type"], "fairshare")
        self.assertIn("500", result["description"])
        self.assertEqual(
            result["command"],
            "sacctmgr --immediate modify account test_account set fairshare=500",
        )
        self.assertEqual(result["parameters"]["account"], "test_account")
        self.assertEqual(result["parameters"]["fairshare"], 500)

    def test_generate_limits_command_single_tres(self):
        """Test limits command generation with single TRES."""
        result = slurm_commands.generate_limits_command(
            "test_account", "GrpTRESMins", {"billing": 72000}
        )

        self.assertEqual(result["type"], "limits")
        self.assertIn("GrpTRESMins", result["description"])
        self.assertEqual(
            result["command"],
            "sacctmgr --immediate modify account test_account set GrpTRESMins=billing=72000",
        )
        self.assertEqual(result["parameters"]["limit_type"], "GrpTRESMins")
        self.assertEqual(result["parameters"]["limits"], {"billing": 72000})

    def test_generate_limits_command_multiple_tres(self):
        """Test limits command generation with multiple TRES."""
        result = slurm_commands.generate_limits_command(
            "test_account", "GrpTRESMins", {"cpu": 1000, "mem": 2000}
        )

        self.assertEqual(result["type"], "limits")
        # Should contain multiple commands separated by semicolon
        self.assertIn(";", result["command"])
        self.assertIn("cpu=1000", result["command"])
        self.assertIn("mem=2000", result["command"])

    def test_generate_qos_command(self):
        """Test QoS command generation."""
        result = slurm_commands.generate_qos_command(
            "test_account", "slowdown", "Usage exceeds threshold"
        )

        self.assertEqual(result["type"], "qos")
        self.assertIn("slowdown", result["description"])
        self.assertIn("Usage exceeds threshold", result["description"])
        self.assertEqual(
            result["command"],
            "sacctmgr --immediate modify account test_account set qos=slowdown",
        )
        self.assertEqual(result["parameters"]["qos"], "slowdown")

    def test_generate_reset_usage_command(self):
        """Test reset usage command generation."""
        result = slurm_commands.generate_reset_usage_command("test_account")

        self.assertEqual(result["type"], "reset_usage")
        self.assertIn("Reset raw usage", result["description"])
        self.assertEqual(
            result["command"],
            "sacctmgr --immediate modify account test_account set RawUsage=0",
        )


class SlurmPreviewCommandsTest(SimpleTestCase):
    """Test the generate_preview_commands function."""

    def test_generate_preview_commands_with_fairshare(self):
        """Test preview commands include fairshare when specified."""
        settings = {"fairshare": 333}

        commands = slurm_commands.generate_preview_commands("test_account", settings)

        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["type"], "fairshare")
        self.assertEqual(commands[0]["parameters"]["fairshare"], 333)

    def test_generate_preview_commands_with_limits(self):
        """Test preview commands include limits when specified."""
        settings = {"grp_tres_mins": {"billing": 72000}}

        commands = slurm_commands.generate_preview_commands("test_account", settings)

        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["type"], "limits")
        self.assertIn("GrpTRESMins", commands[0]["description"])

    def test_generate_preview_commands_qos_change_to_slowdown(self):
        """Test QoS command when usage exceeds threshold."""
        settings = {
            "threshold": 1000,
            "grace_limit": 1200,
        }

        commands = slurm_commands.generate_preview_commands(
            "test_account",
            settings,
            current_usage=1050,  # Above threshold
            current_qos="normal",  # Currently normal
        )

        # Should generate QoS change command
        qos_commands = [c for c in commands if c["type"] == "qos"]
        self.assertEqual(len(qos_commands), 1)
        self.assertEqual(qos_commands[0]["parameters"]["qos"], "slowdown")

    def test_generate_preview_commands_qos_change_to_blocked(self):
        """Test QoS command when usage exceeds grace limit."""
        settings = {
            "threshold": 1000,
            "grace_limit": 1200,
        }

        commands = slurm_commands.generate_preview_commands(
            "test_account",
            settings,
            current_usage=1300,  # Above grace limit
            current_qos="slowdown",
        )

        qos_commands = [c for c in commands if c["type"] == "qos"]
        self.assertEqual(len(qos_commands), 1)
        self.assertEqual(qos_commands[0]["parameters"]["qos"], "blocked")

    def test_generate_preview_commands_no_qos_change_if_same(self):
        """Test no QoS command if already at correct level."""
        settings = {
            "threshold": 1000,
            "grace_limit": 1200,
        }

        commands = slurm_commands.generate_preview_commands(
            "test_account",
            settings,
            current_usage=1050,  # Above threshold -> slowdown
            current_qos="slowdown",  # Already slowdown
        )

        # Should NOT generate QoS change command (already at correct level)
        qos_commands = [c for c in commands if c["type"] == "qos"]
        self.assertEqual(len(qos_commands), 0)

    def test_generate_preview_commands_with_reset_usage(self):
        """Test reset usage command when enabled."""
        settings = {"reset_raw_usage": True}

        commands = slurm_commands.generate_preview_commands("test_account", settings)

        reset_commands = [c for c in commands if c["type"] == "reset_usage"]
        self.assertEqual(len(reset_commands), 1)

    def test_generate_preview_commands_multiple_settings(self):
        """Test preview commands with multiple settings."""
        settings = {
            "fairshare": 500,
            "grp_tres_mins": {"billing": 72000},
            "threshold": 1000,
            "grace_limit": 1200,
            "reset_raw_usage": True,
        }

        commands = slurm_commands.generate_preview_commands(
            "test_account",
            settings,
            current_usage=500,  # Normal usage
            current_qos="normal",
        )

        # Should have fairshare, limits, and reset_usage (no QoS change since usage is normal)
        command_types = [c["type"] for c in commands]
        self.assertIn("fairshare", command_types)
        self.assertIn("limits", command_types)
        self.assertIn("reset_usage", command_types)
        # No QoS change since current usage is normal and qos is already normal
        self.assertNotIn("qos", command_types)

    def test_generate_preview_commands_custom_qos_levels(self):
        """Test custom QoS level names."""
        settings = {
            "threshold": 1000,
            "grace_limit": 1200,
        }
        qos_levels = {
            "default": "custom_normal",
            "slowdown": "custom_slowdown",
            "blocked": "custom_blocked",
        }

        commands = slurm_commands.generate_preview_commands(
            "test_account",
            settings,
            current_usage=1050,
            current_qos="custom_normal",
            qos_levels=qos_levels,
        )

        qos_commands = [c for c in commands if c["type"] == "qos"]
        self.assertEqual(len(qos_commands), 1)
        self.assertEqual(qos_commands[0]["parameters"]["qos"], "custom_slowdown")
