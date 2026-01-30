from django.core.management.base import BaseCommand

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.policy import models, tasks


class Command(BaseCommand):
    help = (
        "Manually trigger SLURM periodic usage policy evaluation. "
        "Can evaluate a specific resource against a specific policy, "
        "or all resources for a policy."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "-p",
            "--policy",
            dest="policy_uuid",
            required=True,
            help="UUID of the SlurmPeriodicUsagePolicy to evaluate.",
        )
        parser.add_argument(
            "-r",
            "--resource",
            dest="resource_uuid",
            help="UUID of a specific resource to evaluate. "
            "If omitted, evaluates all resources in the policy's offering.",
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            default=False,
            help="Run evaluation synchronously (blocking) instead of queuing Celery tasks.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Only calculate and display usage percentages without applying actions.",
        )

    def handle(self, *args, **options):
        policy_uuid = options["policy_uuid"]
        resource_uuid = options.get("resource_uuid")
        sync = options["sync"]
        dry_run = options["dry_run"]

        try:
            policy = models.SlurmPeriodicUsagePolicy.objects.get(uuid=policy_uuid)
        except models.SlurmPeriodicUsagePolicy.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"Policy {policy_uuid} not found."))
            return
        except ValueError:
            self.stdout.write(self.style.ERROR("Invalid policy UUID format."))
            return

        self.stdout.write(f"Policy: {policy.uuid}")
        self.stdout.write(f"  Offering: {policy.scope}")
        self.stdout.write(f"  Actions: {policy.actions}")
        self.stdout.write(f"  Grace ratio: {policy.grace_ratio}")
        self.stdout.write(f"  Has fired: {policy.has_fired}")
        self.stdout.write("")

        if resource_uuid:
            resources = self._get_single_resource(resource_uuid)
            if resources is None:
                return
        else:
            resources = self._get_offering_resources(policy)

        if not resources:
            self.stdout.write(self.style.WARNING("No resources found to evaluate."))
            return

        self.stdout.write(f"Resources to evaluate: {len(resources)}")
        self.stdout.write("")

        if dry_run:
            self._dry_run(policy, resources)
        elif sync:
            self._sync_evaluate(policy, resources)
        else:
            self._async_evaluate(policy, resources)

    def _get_single_resource(self, resource_uuid):
        try:
            resource = marketplace_models.Resource.objects.get(uuid=resource_uuid)
            return [resource]
        except marketplace_models.Resource.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"Resource {resource_uuid} not found."))
            return None
        except ValueError:
            self.stdout.write(self.style.ERROR("Invalid resource UUID format."))
            return None

    def _get_offering_resources(self, policy):
        return list(
            marketplace_models.Resource.objects.filter(
                offering=policy.scope,
            ).exclude(
                state__in=(
                    marketplace_models.ResourceStates.TERMINATED,
                    marketplace_models.ResourceStates.TERMINATING,
                )
            )
        )

    def _dry_run(self, policy, resources):
        self.stdout.write(self.style.WARNING("=== DRY RUN (no actions applied) ==="))
        self.stdout.write("")

        current_period = policy._get_current_period()
        grace_limit = (1 + policy.grace_ratio) * 100

        for resource in resources:
            usage_pct = policy.get_resource_usage_percentage(resource, current_period)

            actions = []
            if (
                usage_pct >= grace_limit
                and "request_slurm_resource_pausing" in policy.actions
            ):
                actions.append("pause")
            if (
                usage_pct >= 100
                and "request_slurm_resource_downscaling" in policy.actions
            ):
                actions.append("downscale")
            if usage_pct >= 80 and "notify_organization_owners" in policy.actions:
                actions.append("notify")

            status = self.style.SUCCESS("OK")
            if actions:
                status = self.style.ERROR(", ".join(actions))
            elif usage_pct >= 80:
                status = self.style.WARNING("near threshold")

            self.stdout.write(
                f"  {resource.name} ({resource.uuid}): "
                f"usage={usage_pct:.1f}%, "
                f"paused={resource.paused}, downscaled={resource.downscaled}, "
                f"would trigger: {status}"
            )

        self.stdout.write("")
        self.stdout.write(f"Period: {current_period}")
        self.stdout.write(f"Grace limit: {grace_limit:.0f}%")

    def _sync_evaluate(self, policy, resources):
        self.stdout.write("Running synchronous evaluation...")
        self.stdout.write("")

        for resource in resources:
            self.stdout.write(f"  Evaluating {resource.name} ({resource.uuid})...")
            try:
                tasks.evaluate_resource_against_policy(
                    str(resource.uuid), str(policy.uuid)
                )
                # Reload to show updated state
                resource.refresh_from_db()
                self.stdout.write(
                    self.style.SUCCESS(
                        f"    Done. paused={resource.paused}, downscaled={resource.downscaled}"
                    )
                )
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"    Error: {e}"))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Synchronous evaluation complete."))

    def _async_evaluate(self, policy, resources):
        self.stdout.write("Queuing Celery tasks...")

        for resource in resources:
            tasks.evaluate_resource_against_policy.delay(
                str(resource.uuid), str(policy.uuid)
            )
            self.stdout.write(f"  Queued: {resource.name} ({resource.uuid})")

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Queued {len(resources)} evaluation tasks. "
                "Check Celery worker logs for results."
            )
        )
