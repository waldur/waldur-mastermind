"""SLURM command generation for preview and history tracking.

This module generates SLURM shell commands from policy settings,
mirroring the command format used by waldur-site-agent's SlurmClient.

Commands are used for:
1. Preview - showing what commands WILL be executed
2. History - recording what commands WERE executed
"""


def generate_fairshare_command(account: str, fairshare: int) -> dict:
    """Generate sacctmgr command for setting fairshare.

    Args:
        account: SLURM account name (resource backend_id)
        fairshare: Fairshare weight value

    Returns:
        Command dict with type, description, command, and parameters
    """
    return {
        "type": "fairshare",
        "description": f"Set fairshare weight to {fairshare} for allocation priority",
        "command": f"sacctmgr --immediate modify account {account} set fairshare={fairshare}",
        "parameters": {"account": account, "fairshare": fairshare},
    }


def generate_limits_command(account: str, limit_type: str, limits: dict) -> dict:
    """Generate sacctmgr command for setting TRES limits.

    Args:
        account: SLURM account name
        limit_type: Type of limit (GrpTRESMins, MaxTRESMins, GrpTRES)
        limits: Dict of TRES type to limit value

    Returns:
        Command dict with type, description, command, and parameters
    """
    # Site-agent sets limits one TRES at a time, but we combine for display
    commands = []
    for tres_type, value in limits.items():
        limit_spec = f"{limit_type}={tres_type}={value}"
        commands.append(
            f"sacctmgr --immediate modify account {account} set {limit_spec}"
        )

    return {
        "type": "limits",
        "description": f"Set {limit_type} limits: {limits}",
        "command": "; ".join(commands),
        "parameters": {"account": account, "limit_type": limit_type, "limits": limits},
    }


def generate_qos_command(account: str, qos: str, reason: str) -> dict:
    """Generate sacctmgr command for setting QoS.

    Args:
        account: SLURM account name
        qos: QoS level name (e.g., normal, slowdown, blocked)
        reason: Human-readable reason for QoS change

    Returns:
        Command dict with type, description, command, and parameters
    """
    return {
        "type": "qos",
        "description": f"Set QoS to '{qos}': {reason}",
        "command": f"sacctmgr --immediate modify account {account} set qos={qos}",
        "parameters": {"account": account, "qos": qos, "reason": reason},
    }


def generate_reset_usage_command(account: str) -> dict:
    """Generate sacctmgr command for resetting raw usage.

    Args:
        account: SLURM account name

    Returns:
        Command dict with type, description, command, and parameters
    """
    return {
        "type": "reset_usage",
        "description": "Reset raw usage counter for new billing period",
        "command": f"sacctmgr --immediate modify account {account} set RawUsage=0",
        "parameters": {"account": account},
    }


def generate_preview_commands(
    account: str,
    settings: dict,
    current_usage: float = 0,  # noqa: ARG001
    current_qos: str = "normal",  # noqa: ARG001
    qos_levels: dict | None = None,  # noqa: ARG001
) -> list[dict]:
    """Generate all commands that would be executed for given settings.

    Args:
        account: SLURM account name (resource backend_id)
        settings: Policy settings dict containing:
            - fairshare: int - fairshare weight
            - grp_tres_mins: dict - GrpTRESMins limits
            - max_tres_mins: dict - MaxTRESMins limits
            - grp_tres: dict - GrpTRES limits
            - reset_raw_usage: bool - whether to reset usage
        current_usage: unused; kept for backwards-compatible callers.
        current_qos: unused; kept for backwards-compatible callers.
        qos_levels: unused; kept for backwards-compatible callers.

    QoS state is no longer derived here. It is driven by Mastermind's
    policy engine via ``resource.paused`` / ``resource.downscaled`` and
    propagated to the site agent through the standard RESOURCE update
    event — not through this command-preview path.

    Returns:
        List of command dicts, each with type, description, command, parameters
    """
    commands = []

    # Fairshare command
    if settings.get("fairshare"):
        commands.append(generate_fairshare_command(account, settings["fairshare"]))

    # Limits commands
    if settings.get("grp_tres_mins"):
        commands.append(
            generate_limits_command(account, "GrpTRESMins", settings["grp_tres_mins"])
        )

    if settings.get("max_tres_mins"):
        commands.append(
            generate_limits_command(account, "MaxTRESMins", settings["max_tres_mins"])
        )

    if settings.get("grp_tres"):
        commands.append(
            generate_limits_command(account, "GrpTRES", settings["grp_tres"])
        )

    # Reset usage command (if enabled)
    if settings.get("reset_raw_usage"):
        commands.append(generate_reset_usage_command(account))

    return commands
