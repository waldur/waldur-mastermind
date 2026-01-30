from django.core.management.base import BaseCommand

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.policy import models


class Command(BaseCommand):
    help = (
        "Display status of SLURM periodic usage policies: "
        "current resource states, recent evaluation logs, and command history."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "-p",
            "--policy",
            dest="policy_uuid",
            help="UUID of a specific policy. If omitted, shows all SLURM policies.",
        )
        parser.add_argument(
            "-r",
            "--resource",
            dest="resource_uuid",
            help="Filter output to a specific resource UUID.",
        )
        parser.add_argument(
            "--logs",
            type=int,
            default=10,
            help="Number of recent evaluation logs to display (default: 10).",
        )
        parser.add_argument(
            "--commands",
            type=int,
            default=5,
            help="Number of recent command history entries to display (default: 5).",
        )

    def handle(self, *args, **options):
        policy_uuid = options.get("policy_uuid")
        resource_uuid = options.get("resource_uuid")
        log_limit = options["logs"]
        cmd_limit = options["commands"]

        if policy_uuid:
            try:
                policies = [
                    models.SlurmPeriodicUsagePolicy.objects.get(uuid=policy_uuid)
                ]
            except models.SlurmPeriodicUsagePolicy.DoesNotExist:
                self.stdout.write(self.style.ERROR(f"Policy {policy_uuid} not found."))
                return
            except ValueError:
                self.stdout.write(self.style.ERROR("Invalid policy UUID format."))
                return
        else:
            policies = list(models.SlurmPeriodicUsagePolicy.objects.all())

        if not policies:
            self.stdout.write(self.style.WARNING("No SLURM policies found."))
            return

        for policy in policies:
            self._show_policy(policy, resource_uuid, log_limit, cmd_limit)
            self.stdout.write("")

    def _show_policy(self, policy, resource_uuid, log_limit, cmd_limit):
        self.stdout.write(self.style.HTTP_INFO(f"=== Policy {policy.uuid} ==="))
        self.stdout.write(f"  Offering: {policy.scope}")
        self.stdout.write(f"  Actions: {policy.actions}")
        self.stdout.write(f"  Grace ratio: {policy.grace_ratio}")
        self.stdout.write(f"  Carryover: {policy.carryover_enabled}")
        self.stdout.write(f"  Has fired: {policy.has_fired}")
        if policy.fired_datetime:
            self.stdout.write(f"  Fired at: {policy.fired_datetime}")
        self.stdout.write("")

        # Resource states
        resources_qs = marketplace_models.Resource.objects.filter(
            offering=policy.scope,
        ).exclude(
            state__in=(
                marketplace_models.ResourceStates.TERMINATED,
                marketplace_models.ResourceStates.TERMINATING,
            )
        )
        if resource_uuid:
            resources_qs = resources_qs.filter(uuid=resource_uuid)

        resources = list(resources_qs)
        current_period = policy._get_current_period()

        self.stdout.write(
            f"  Resources ({len(resources)} active, period: {current_period}):"
        )
        for resource in resources:
            usage_pct = policy.get_resource_usage_percentage(resource, current_period)
            flags = []
            if resource.paused:
                flags.append(self.style.ERROR("PAUSED"))
            if resource.downscaled:
                flags.append(self.style.WARNING("DOWNSCALED"))
            flag_str = " ".join(flags) if flags else self.style.SUCCESS("normal")

            self.stdout.write(
                f"    {resource.name} ({resource.uuid}): "
                f"usage={usage_pct:.1f}% | {flag_str}"
            )

        # Evaluation logs
        self.stdout.write("")
        log_qs = models.SlurmPolicyEvaluationLog.objects.filter(
            policy=policy,
        ).order_by("-evaluated_at")
        if resource_uuid:
            log_qs = log_qs.filter(resource__uuid=resource_uuid)
        logs = list(log_qs[:log_limit])

        self.stdout.write(f"  Recent evaluation logs (last {log_limit}):")
        if not logs:
            self.stdout.write("    (none)")
        for log in logs:
            agent_status = "pending"
            if log.site_agent_confirmed is True:
                agent_status = self.style.SUCCESS("confirmed")
            elif log.site_agent_confirmed is False:
                agent_status = self.style.ERROR("failed")

            self.stdout.write(
                f"    [{log.evaluated_at:%Y-%m-%d %H:%M}] "
                f"resource={log.resource.name} "
                f"usage={log.usage_percentage:.1f}% "
                f"actions={log.actions_taken} "
                f"stomp={log.stomp_message_sent} "
                f"agent={agent_status}"
            )

        # Command history
        self.stdout.write("")
        cmd_qs = models.SlurmCommandHistory.objects.filter(
            policy=policy,
        ).order_by("-executed_at")
        if resource_uuid:
            cmd_qs = cmd_qs.filter(resource__uuid=resource_uuid)
        cmds = list(cmd_qs[:cmd_limit])

        self.stdout.write(f"  Recent commands (last {cmd_limit}):")
        if not cmds:
            self.stdout.write("    (none)")
        for cmd in cmds:
            success_str = ""
            if cmd.success is True:
                success_str = self.style.SUCCESS(" [OK]")
            elif cmd.success is False:
                success_str = self.style.ERROR(f" [FAIL: {cmd.error_message}]")

            self.stdout.write(
                f"    [{cmd.executed_at:%Y-%m-%d %H:%M}] "
                f"{cmd.command_type} | {cmd.execution_mode} | "
                f"{cmd.shell_command}{success_str}"
            )
