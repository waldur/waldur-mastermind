"""SLURM periodic usage policy preview calculations.

Standalone functions for previewing policy impact without requiring
full emulator context. Uses the same formulas as the slurm-emulator package.
"""


def calculate_carryover(
    base_allocation: float,
    previous_usage: float,
    carryover_factor: int = 50,
) -> dict:
    """Calculate carryover allocation for next period.

    Args:
        base_allocation: Base allocation for the period
        previous_usage: Usage from previous period
        carryover_factor: Maximum percentage of base that can carry over (0-100)

    Returns:
        Dictionary with carryover calculations
    """
    carryover_ratio = carryover_factor / 100.0
    unused = max(0, base_allocation - previous_usage)
    carryover_cap = carryover_ratio * base_allocation
    carryover = min(unused, carryover_cap)
    total = base_allocation + carryover

    return {
        "previous_usage": previous_usage,
        "carryover_factor": carryover_factor,
        "base_allocation": base_allocation,
        "unused": round(unused, 2),
        "carryover_cap": round(carryover_cap, 2),
        "carryover": round(carryover, 2),
        "total_allocation": round(total, 2),
    }


def preview_qos_thresholds(
    allocation: float, grace_ratio: float = 0.2, notification_ratio: float = 0.8
) -> dict:
    """Preview QoS trigger thresholds for a given allocation.

    Args:
        allocation: Total allocation (including carryover if applicable)
        grace_ratio: Ratio above allocation for grace period (default: 0.2 = 20%)
        notification_ratio: Ratio at which to send notifications (default: 0.8 = 80%)

    Returns:
        Dictionary with threshold values
    """
    notification_threshold = allocation * notification_ratio
    slowdown_threshold = allocation
    blocked_threshold = allocation * (1 + grace_ratio)

    return {
        "allocation": allocation,
        "grace_ratio": grace_ratio,
        "notification_ratio": notification_ratio,
        "notification_threshold": round(notification_threshold, 2),
        "slowdown_threshold": round(slowdown_threshold, 2),
        "blocked_threshold": round(blocked_threshold, 2),
    }


def preview_policy_impact(
    allocation: float,
    grace_ratio: float = 0.2,
    previous_usage: float = 0,
    carryover_factor: int = 50,
    carryover_enabled: bool = True,
) -> dict:
    """Preview full policy impact combining thresholds and carryover.

    Args:
        allocation: Base allocation for the period
        grace_ratio: Grace ratio for overconsumption
        previous_usage: Usage from previous period
        carryover_factor: Maximum percentage of base that can carry over (0-100)
        carryover_enabled: Whether carryover is enabled

    Returns:
        Complete policy impact preview
    """
    if carryover_enabled and previous_usage > 0:
        carryover = calculate_carryover(allocation, previous_usage, carryover_factor)
        effective_allocation = carryover["total_allocation"]
    else:
        carryover = None
        effective_allocation = allocation

    thresholds = preview_qos_thresholds(effective_allocation, grace_ratio)

    return {
        "base_allocation": allocation,
        "effective_allocation": effective_allocation,
        "carryover_enabled": carryover_enabled,
        "carryover": carryover,
        "thresholds": thresholds,
        "grace_ratio": grace_ratio,
        "carryover_factor": carryover_factor,
    }


def calculate_tres_billing_units(
    tres_usage: dict[str, float], tres_weights: dict[str, float] = None
) -> float:
    """Calculate billing units from TRES usage.

    Args:
        tres_usage: Dictionary of TRES type to raw usage
        tres_weights: Dictionary of TRES type to billing weight
            Defaults to standard weights if not provided

    Returns:
        Total billing units
    """
    if tres_weights is None:
        tres_weights = {
            "CPU": 0.015625,  # 64 CPUs = 1 billing unit
            "Mem": 0.001953125,  # 512 GB = 1 billing unit (per GB)
            "GRES/gpu": 0.25,  # 4 GPUs = 1 billing unit
        }

    billing_units = 0.0
    for tres_type, usage in tres_usage.items():
        if tres_type in tres_weights:
            billing_units += usage * tres_weights[tres_type]

    return round(billing_units, 4)


def calculate_days_until_threshold(
    current_usage: float,
    daily_usage_rate: float,
    threshold: float,
) -> int | None:
    """Calculate days until a threshold is reached at current usage rate.

    Args:
        current_usage: Current accumulated usage
        daily_usage_rate: Average daily usage rate
        threshold: Target threshold to reach

    Returns:
        Days until threshold, or None if rate is 0 or already exceeded
    """
    if current_usage >= threshold:
        return 0

    if daily_usage_rate <= 0:
        return None

    remaining = threshold - current_usage
    return int(remaining / daily_usage_rate)


def calculate_threshold_dates(
    current_usage: float,
    daily_usage_rate: float,
    thresholds: dict,
    start_date=None,
) -> dict:
    """Calculate projected dates when thresholds will be crossed.

    Args:
        current_usage: Current accumulated usage
        daily_usage_rate: Average daily usage rate
        thresholds: Dictionary with notification_threshold, slowdown_threshold, blocked_threshold
        start_date: Starting date for projections (default: today)

    Returns:
        Dictionary with projected dates and status for each threshold
    """
    import datetime

    if start_date is None:
        start_date = datetime.date.today()

    notification_threshold = thresholds.get("notification_threshold", 0)
    slowdown_threshold = thresholds.get("slowdown_threshold", 0)
    blocked_threshold = thresholds.get("blocked_threshold", 0)

    def get_projection(threshold):
        days = calculate_days_until_threshold(
            current_usage, daily_usage_rate, threshold
        )
        if days is None:
            return {"days": None, "date": None, "status": "never"}
        elif days == 0:
            return {"days": 0, "date": start_date.isoformat(), "status": "exceeded"}
        else:
            projected_date = start_date + datetime.timedelta(days=days)
            return {
                "days": days,
                "date": projected_date.isoformat(),
                "status": "projected",
            }

    return {
        "notification": get_projection(notification_threshold),
        "slowdown": get_projection(slowdown_threshold),
        "blocked": get_projection(blocked_threshold),
    }


def preview_policy_impact_with_resource(
    allocation: float,
    grace_ratio: float = 0.2,
    previous_usage: float = 0,
    carryover_factor: int = 50,
    carryover_enabled: bool = True,
    current_usage: float = 0,
    daily_usage_rate: float = 0,
) -> dict:
    """Preview full policy impact with date projections.

    Extends preview_policy_impact with current resource usage data
    to calculate when thresholds will be crossed.

    Args:
        allocation: Base allocation for the period
        grace_ratio: Grace ratio for overconsumption
        previous_usage: Usage from previous period
        carryover_factor: Maximum percentage of base that can carry over (0-100)
        carryover_enabled: Whether carryover is enabled
        current_usage: Current usage in this period (from resource)
        daily_usage_rate: Average daily usage rate (from resource)

    Returns:
        Complete policy impact preview with date projections
    """
    # Get base preview
    base_preview = preview_policy_impact(
        allocation=allocation,
        grace_ratio=grace_ratio,
        previous_usage=previous_usage,
        carryover_factor=carryover_factor,
        carryover_enabled=carryover_enabled,
    )

    # Calculate current QoS status
    effective_allocation = base_preview["effective_allocation"]
    notification_threshold = base_preview["thresholds"]["notification_threshold"]
    slowdown_threshold = base_preview["thresholds"]["slowdown_threshold"]
    blocked_threshold = base_preview["thresholds"]["blocked_threshold"]

    if current_usage >= blocked_threshold:
        current_qos_status = "blocked"
    elif current_usage >= slowdown_threshold:
        current_qos_status = "slowdown"
    elif current_usage >= notification_threshold:
        current_qos_status = "notification"
    else:
        current_qos_status = "normal"

    # Calculate usage percentage
    usage_percentage = (
        round((current_usage / effective_allocation) * 100, 1)
        if effective_allocation > 0
        else 0
    )

    # Calculate date projections
    date_projections = calculate_threshold_dates(
        current_usage=current_usage,
        daily_usage_rate=daily_usage_rate,
        thresholds=base_preview["thresholds"],
    )

    return {
        **base_preview,
        "current_usage": current_usage,
        "daily_usage_rate": daily_usage_rate,
        "usage_percentage": usage_percentage,
        "current_qos_status": current_qos_status,
        "date_projections": date_projections,
    }
