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

    def test_generate_preview_commands_does_not_emit_qos(self):
        """Preview no longer derives QoS commands from threshold/grace_limit.

        QoS state is owned by Mastermind via resource.paused / downscaled.
        The preview omits QoS commands even when legacy threshold fields
        are present, so SlurmCommandHistory does not duplicate that state.
        """
        settings = {
            "threshold": 1000,
            "grace_limit": 1200,
        }

        commands = slurm_commands.generate_preview_commands(
            "test_account",
            settings,
            current_usage=1300,
            current_qos="normal",
        )

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
            "reset_raw_usage": True,
        }

        commands = slurm_commands.generate_preview_commands("test_account", settings)

        command_types = [c["type"] for c in commands]
        self.assertIn("fairshare", command_types)
        self.assertIn("limits", command_types)
        self.assertIn("reset_usage", command_types)
        self.assertNotIn("qos", command_types)
