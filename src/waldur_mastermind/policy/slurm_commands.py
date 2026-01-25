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
    current_usage: float = 0,
    current_qos: str = "normal",
    qos_levels: dict | None = None,
) -> list[dict]:
    """Generate all commands that would be executed for given settings.

    Args:
        account: SLURM account name (resource backend_id)
        settings: Policy settings dict containing:
            - fairshare: int - fairshare weight
            - grp_tres_mins: dict - GrpTRESMins limits
            - max_tres_mins: dict - MaxTRESMins limits
            - grp_tres: dict - GrpTRES limits
            - threshold: float - usage threshold for QoS change
            - grace_limit: float - grace limit for blocked status
            - reset_raw_usage: bool - whether to reset usage
        current_usage: Current usage in billing units
        current_qos: Current QoS level name
        qos_levels: QoS level names dict {"default": "...", "slowdown": "...", "blocked": "..."}

    Returns:
        List of command dicts, each with type, description, command, parameters
    """
    if qos_levels is None:
        qos_levels = {"default": "normal", "slowdown": "slowdown", "blocked": "blocked"}

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

    # QoS command based on thresholds
    threshold = settings.get("threshold", 0)
    grace_limit = settings.get("grace_limit", float("inf"))

    if threshold > 0 or grace_limit < float("inf"):
        if current_usage >= grace_limit:
            new_qos = qos_levels.get("blocked", "blocked")
            reason = "Usage exceeds grace limit"
        elif current_usage >= threshold:
            new_qos = qos_levels.get("slowdown", "slowdown")
            reason = "Usage exceeds allocation threshold"
        else:
            new_qos = qos_levels.get("default", "normal")
            reason = "Usage within normal limits"

        if new_qos != current_qos:
            commands.append(generate_qos_command(account, new_qos, reason))

    # Reset usage command (if enabled)
    if settings.get("reset_raw_usage"):
        commands.append(generate_reset_usage_command(account))

    return commands
