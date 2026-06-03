"""Live publish-confirm latency probe against the configured broker.

Publishes N no-op tasks with a very long countdown so they land in a
delayed queue without ever executing (or executing as no-ops if they
do). Measures the publish-confirm RTT for each call and reports
percentiles.

Designed to be safe to run on a production deployment: the task body
short-circuits on a deliberately invalid scope_id and produces no
side effects.

Exit codes:
    0  probe succeeded
    1  p99 exceeded --p99-threshold-ms
    2  one or more publishes raised an exception
"""

from __future__ import annotations

import statistics
import sys
import time
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

DEFAULT_POLICY_CLASS = "waldur_mastermind.policy.models.OfferingEstimatedCostPolicy"


class Command(BaseCommand):
    help = (
        "Publish N no-op messages and report publish-confirm RTT "
        "percentiles. Safe on production; messages have no effect."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--samples",
            type=int,
            default=100,
            help="Number of publishes to attempt (default: 100).",
        )
        parser.add_argument(
            "--countdown",
            type=int,
            default=86400,
            help=(
                "Celery countdown in seconds. The probe payload lands in a "
                "celery_delayed_* bucket sized for this delay and never "
                "executes within the probe's lifetime (default: 86400)."
            ),
        )
        parser.add_argument(
            "--p99-threshold-ms",
            type=float,
            default=None,
            help=(
                "If set, exit non-zero when p99 publish-confirm RTT exceeds "
                "this many milliseconds. Useful for CI alerting."
            ),
        )
        parser.add_argument(
            "--policy-class",
            default=DEFAULT_POLICY_CLASS,
            help=(
                "Dotted path of a policy class for the no-op payload "
                "(default: %(default)s). Any class that evaluate_policies_async "
                "can import will work; the task body short-circuits because "
                "scope_id=-1 matches no rows."
            ),
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit results as a single JSON object on stdout.",
        )

    def handle(self, *args, **options) -> None:
        samples: int = options["samples"]
        countdown: int = options["countdown"]
        threshold_ms: float | None = options["p99_threshold_ms"]
        policy_class: str = options["policy_class"]
        json_output: bool = options["json"]

        if samples <= 0:
            self.stderr.write(self.style.ERROR("--samples must be > 0"))
            sys.exit(2)

        # Import here so the command file itself stays importable in any
        # environment, even if the policy module is unavailable.
        from waldur_mastermind.policy import tasks

        rtts_ms: list[float] = []
        errors: list[str] = []

        if not json_output:
            self.stdout.write(
                f"Probing broker with {samples} publishes "
                f"(countdown={countdown}s, payload={policy_class}, "
                f"scope_id=-1) ..."
            )

        start_wall = time.perf_counter()
        for _ in range(samples):
            t0 = time.perf_counter()
            try:
                tasks.evaluate_policies_async.apply_async(
                    args=[policy_class, {"scope_id": -1}],
                    countdown=countdown,
                )
                rtts_ms.append((time.perf_counter() - t0) * 1000.0)
            except Exception as e:  # noqa: BLE001 — probe must keep going
                errors.append(f"{type(e).__name__}: {e}")
        total_wall_s = time.perf_counter() - start_wall

        if not rtts_ms:
            payload = {
                "samples": samples,
                "ok_count": 0,
                "error_count": len(errors),
                "first_errors": errors[:5],
            }
            self._emit(payload, json_output)
            sys.exit(2)

        rtts_ms.sort()
        n = len(rtts_ms)

        def pct(p: float) -> float:
            idx = min(int(n * p / 100.0), n - 1)
            return rtts_ms[idx]

        result: dict[str, Any] = {
            "samples": samples,
            "ok_count": n,
            "error_count": len(errors),
            "wall_clock_s": round(total_wall_s, 3),
            "rtt_ms": {
                "min": round(rtts_ms[0], 2),
                "p50": round(pct(50), 2),
                "p90": round(pct(90), 2),
                "p95": round(pct(95), 2),
                "p99": round(pct(99), 2),
                "max": round(rtts_ms[-1], 2),
                "mean": round(statistics.fmean(rtts_ms), 2),
            },
            "over_1s": sum(1 for x in rtts_ms if x > 1000.0),
            "over_5s": sum(1 for x in rtts_ms if x > 5000.0),
            "over_30s": sum(1 for x in rtts_ms if x > 30000.0),
        }
        if errors:
            result["first_errors"] = errors[:5]

        self._emit(result, json_output)

        if errors:
            sys.exit(2)
        if threshold_ms is not None and result["rtt_ms"]["p99"] > threshold_ms:
            self.stderr.write(
                self.style.ERROR(
                    f"p99={result['rtt_ms']['p99']}ms "
                    f"exceeds threshold {threshold_ms}ms"
                )
            )
            sys.exit(1)
        sys.exit(0)

    def _emit(self, payload: dict[str, Any], as_json: bool) -> None:
        if as_json:
            import json

            self.stdout.write(json.dumps(payload, sort_keys=True))
            return

        rtt = payload.get("rtt_ms")
        self.stdout.write(
            f"samples={payload['samples']}  "
            f"ok={payload['ok_count']}  "
            f"errors={payload['error_count']}  "
            f"wall={payload.get('wall_clock_s', '?')}s"
        )
        if rtt:
            self.stdout.write(
                f"  min={rtt['min']}ms  p50={rtt['p50']}ms  "
                f"p90={rtt['p90']}ms  p95={rtt['p95']}ms  "
                f"p99={rtt['p99']}ms  max={rtt['max']}ms"
            )
            self.stdout.write(
                f"  publishes >1s: {payload['over_1s']}  "
                f">5s: {payload['over_5s']}  "
                f">30s: {payload['over_30s']}"
            )
        for err in payload.get("first_errors", []):
            self.stderr.write(self.style.ERROR(f"  err: {err}"))
