import logging

from django.db.models import Q, Sum
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.serializers import ValidationError

from waldur_core.core import utils as core_utils
from waldur_core.core.views import ActionsViewSet
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import permissions as structure_permissions
from waldur_mastermind.marketplace import models as marketplace_models

from . import (
    filters,
    models,
    serializers,
    slurm_commands,
    slurm_preview,
)
from . import (
    tasks as policy_tasks,
)

logger = logging.getLogger(__name__)


class ProjectEstimatedCostPolicyViewSet(ActionsViewSet):
    queryset = models.ProjectEstimatedCostPolicy.objects.all().order_by("-created")
    serializer_class = serializers.ProjectEstimatedCostPolicySerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    filterset_class = filters.ProjectEstimatedCostPolicyFilter
    lookup_field = "uuid"
    destroy_permissions = update_permissions = partial_update_permissions = [
        structure_permissions.is_owner
    ]

    def _check_terminated_project(self, policy):
        """Check if policy scope (project) is terminated and raise error if so"""
        if policy.scope.is_removed:
            raise ValidationError("Cannot update policies for terminated projects.")

    def update(self, request, *args, **kwargs):
        policy = self.get_object()
        self._check_terminated_project(policy)
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        policy = self.get_object()
        self._check_terminated_project(policy)
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(parameters=[])
    @action(detail=False, methods=["get"])
    def actions(self, request, *args, **kwargs):
        data = list(models.ProjectEstimatedCostPolicy.available_actions)
        return Response(data, status=status.HTTP_200_OK)


class CustomerEstimatedCostPolicyViewSet(ActionsViewSet):
    queryset = models.CustomerEstimatedCostPolicy.objects.all().order_by("-created")
    serializer_class = serializers.CustomerEstimatedCostPolicySerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    filterset_class = filters.CustomerEstimatedCostPolicyFilter
    lookup_field = "uuid"
    destroy_permissions = update_permissions = partial_update_permissions = [
        structure_permissions.is_staff
    ]

    @extend_schema(parameters=[])
    @action(detail=False, methods=["get"])
    def actions(self, request, *args, **kwargs):
        data = list(models.CustomerEstimatedCostPolicy.available_actions)
        return Response(data, status=status.HTTP_200_OK)


class OfferingEstimatedCostPolicyViewSet(ActionsViewSet):
    queryset = models.OfferingEstimatedCostPolicy.objects.all().order_by("-created")
    serializer_class = serializers.OfferingEstimatedCostPolicySerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    filterset_class = filters.PolicyFilter
    lookup_field = "uuid"
    destroy_permissions = update_permissions = partial_update_permissions = [
        structure_permissions.is_owner
    ]

    @extend_schema(
        parameters=[],
        description="List available actions for OfferingEstimatedCostPolicy",
    )
    @action(detail=False, methods=["get"])
    def actions(self, request, *args, **kwargs):
        data = list(models.OfferingEstimatedCostPolicy.available_actions)
        return Response(data, status=status.HTTP_200_OK)


class OfferingUsagePolicyViewSet(ActionsViewSet):
    queryset = models.OfferingUsagePolicy.objects.all().order_by("-created")
    serializer_class = serializers.OfferingUsagePolicySerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    filterset_class = filters.PolicyFilter
    lookup_field = "uuid"
    destroy_permissions = update_permissions = partial_update_permissions = [
        structure_permissions.is_owner
    ]

    @extend_schema(parameters=[])
    @action(detail=False, methods=["get"])
    def actions(self, request, *args, **kwargs):
        data = list(models.OfferingUsagePolicy.available_actions)
        return Response(data, status=status.HTTP_200_OK)


class CustomerComponentUsagePolicyViewSet(ActionsViewSet):
    queryset = models.CustomerComponentUsagePolicy.objects.all().order_by("-created")
    serializer_class = serializers.CustomerComponentUsagePolicySerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    filterset_class = filters.CustomerComponentUsagePolicyFilter
    lookup_field = "uuid"
    create_permissions = destroy_permissions = update_permissions = (
        partial_update_permissions
    ) = [structure_permissions.is_staff]

    @extend_schema(parameters=[])
    @action(detail=False, methods=["get"])
    def actions(self, request, *args, **kwargs):
        data = list(models.CustomerComponentUsagePolicy.available_actions)
        return Response(data, status=status.HTTP_200_OK)


class SlurmPeriodicUsagePolicyViewSet(ActionsViewSet):
    queryset = models.SlurmPeriodicUsagePolicy.objects.all().order_by("-created")
    serializer_class = serializers.SlurmPeriodicUsagePolicySerializer
    filter_backends = [
        DjangoFilterBackend,
        structure_filters.GenericRoleFilter,
    ]
    filterset_class = filters.PolicyFilter
    lookup_field = "uuid"
    destroy_permissions = update_permissions = partial_update_permissions = [
        structure_permissions.is_owner
    ]
    # preview_impact is a non-detail action (no specific object), so we use is_staff
    # instead of is_owner which requires an object to check ownership against
    preview_impact_permissions = [structure_permissions.is_staff]
    report_command_result_permissions = [structure_permissions.is_staff]
    evaluation_logs_permissions = [structure_permissions.is_owner]
    command_history_list_permissions = [structure_permissions.is_owner]
    dry_run_permissions = [structure_permissions.is_staff]
    evaluate_permissions = [structure_permissions.is_staff]

    @extend_schema(parameters=[])
    @action(detail=False, methods=["get"])
    def actions(self, request, *args, **kwargs):
        data = list(models.SlurmPeriodicUsagePolicy.available_actions)
        return Response(data, status=status.HTTP_200_OK)

    def _fetch_resource_usage_data(self, resource_uuid, defaults):
        """Fetch usage data from resource if available.

        Returns a dict with allocation, current_usage, daily_usage_rate, and previous_usage.
        The preview API stays scalar (frontend-facing with simple example values).

        Uses the policy's period setting to determine the correct date range.
        If no policy exists, queries all usage without date bounds.
        """
        result = defaults.copy()

        try:
            resource = marketplace_models.Resource.objects.get(uuid=resource_uuid)
        except marketplace_models.Resource.DoesNotExist:
            logger.debug(
                "Resource with UUID %s not found, using provided values", resource_uuid
            )
            return result

        # Get resource limits as allocation — use sum for the scalar preview API
        if hasattr(resource, "limits") and resource.limits:
            total_alloc = sum(float(v) for v in resource.limits.values() if v and v > 0)
            if total_alloc > 0:
                result["allocation"] = total_alloc

        today = timezone.now().date()

        # Look up the policy for this offering to determine the correct period
        policy = models.SlurmPeriodicUsagePolicy.objects.filter(
            scope=resource.offering,
        ).first()

        if policy:
            current_period = policy._get_current_period()
            date_range = policy._get_period_date_range(current_period)
            if date_range:
                period_start, period_end = date_range
            else:
                # TOTAL period: no date range, query all usage
                period_start = None
                period_end = None

            previous_period = policy._get_previous_period(current_period)
        else:
            # No policy for offering — query all usage without date bounds
            period_start = None
            period_end = None
            previous_period = None

        # Get per-component usages for the current period, summed into scalar
        usage_qs = marketplace_models.ComponentUsage.objects.filter(resource=resource)
        if period_start is not None and period_end is not None:
            usage_qs = usage_qs.filter(
                billing_period__gte=period_start,
                billing_period__lte=period_end,
            )
        usages = usage_qs.values("component__type").annotate(total=Sum("usage"))
        current_usage = sum(float(u["total"]) for u in usages if u["total"])

        # If no usage in current period, try to get most recent usage
        if current_usage == 0:
            recent_usage = (
                marketplace_models.ComponentUsage.objects.filter(resource=resource)
                .order_by("-billing_period")
                .first()
            )
            if recent_usage:
                current_usage = float(recent_usage.usage)
                period_start = recent_usage.billing_period

        result["current_usage"] = current_usage

        # Calculate daily usage rate
        if period_start is not None:
            days_in_period = max(1, (today - period_start).days + 1)
        else:
            # TOTAL period: use earliest usage date as start
            earliest = (
                marketplace_models.ComponentUsage.objects.filter(resource=resource)
                .order_by("billing_period")
                .values_list("billing_period", flat=True)
                .first()
            )
            days_in_period = max(1, (today - earliest).days + 1) if earliest else 1
        if current_usage > 0:
            result["daily_usage_rate"] = current_usage / days_in_period

        # Get previous period usage
        if policy and previous_period:
            prev_date_range = policy._get_period_date_range(previous_period)
            if prev_date_range:
                prev_start, prev_end = prev_date_range
                prev_usages = marketplace_models.ComponentUsage.objects.filter(
                    resource=resource,
                    billing_period__gte=prev_start,
                    billing_period__lte=prev_end,
                )
                prev_usage_sum = prev_usages.aggregate(total=Sum("usage"))["total"]
                if prev_usage_sum:
                    result["previous_usage"] = float(prev_usage_sum)

        return result

    def _build_command_preview_data(
        self, resource_uuid, data, allocation, grace_ratio, current_usage, current_qos
    ):
        """Build command preview and history data for a resource."""
        preview_commands = []
        command_history = []
        billing_period_start = None
        billing_period_end = None

        try:
            resource = marketplace_models.Resource.objects.get(uuid=resource_uuid)
        except marketplace_models.Resource.DoesNotExist:
            logger.debug(
                "Resource with UUID %s not found for command preview", resource_uuid
            )
            return (
                preview_commands,
                command_history,
                billing_period_start,
                billing_period_end,
            )

        today = timezone.now().date()
        billing_period_start = core_utils.month_start(today).date()
        billing_period_end = core_utils.month_end(today)

        account = resource.backend_id or resource.name

        settings = {
            "fairshare": data.get("fairshare", 500),
            "reset_raw_usage": data.get("raw_usage_reset", False),
        }

        if hasattr(resource, "limits") and resource.limits:
            limit_type = data.get("limit_type", "GrpTRESMins")
            if limit_type == "GrpTRESMins":
                settings["grp_tres_mins"] = resource.limits
            elif limit_type == "MaxTRESMins":
                settings["max_tres_mins"] = resource.limits
            elif limit_type == "GrpTRES":
                settings["grp_tres"] = resource.limits

        preview_commands = slurm_commands.generate_preview_commands(
            account=account,
            settings=settings,
            current_usage=current_usage,
            current_qos=current_qos,
        )

        command_history = list(
            models.SlurmCommandHistory.objects.filter(
                resource=resource,
                billing_period__gte=billing_period_start,
            ).order_by("-executed_at")[:50]
        )

        return (
            preview_commands,
            command_history,
            billing_period_start,
            billing_period_end,
        )

    @extend_schema(
        request=serializers.SlurmPolicyPreviewRequestSerializer,
        responses={200: serializers.SlurmPolicyPreviewResponseSerializer},
        description="Preview policy impact without saving. "
        "Returns threshold calculations, carryover projections, and QoS trigger points.",
    )
    @action(detail=False, methods=["post"])
    def preview_impact(self, request, *args, **kwargs):
        """Preview policy impact based on configuration parameters.

        If resource_uuid is provided, fetches current usage from the resource.
        Otherwise, uses current_usage and daily_usage_rate from the request.
        """
        serializer = serializers.SlurmPolicyPreviewRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        resource_uuid = data.get("resource_uuid")

        # Set default values from request data
        defaults = {
            "allocation": data.get("allocation", 1000),
            "current_usage": data.get("current_usage", 0),
            "daily_usage_rate": data.get("daily_usage_rate", 0),
            "previous_usage": data.get("previous_usage", 0),
        }

        # Override defaults with resource data if resource_uuid is provided
        if resource_uuid:
            defaults = self._fetch_resource_usage_data(resource_uuid, defaults)

        allocation = defaults["allocation"]
        grace_ratio = data.get("grace_ratio", 0.2)

        # Calculate policy impact preview
        result = slurm_preview.preview_policy_impact_with_resource(
            allocation=allocation,
            grace_ratio=grace_ratio,
            previous_usage=defaults["previous_usage"],
            carryover_factor=data.get("carryover_factor", 50),
            carryover_enabled=data.get("carryover_enabled", True),
            current_usage=defaults["current_usage"],
            daily_usage_rate=defaults["daily_usage_rate"],
        )

        # Build command preview data if resource is available
        preview_commands = []
        command_history = []
        billing_period_start = None
        billing_period_end = None

        if resource_uuid:
            current_qos = result.get("current_qos_status", "normal")
            (
                preview_commands,
                command_history,
                billing_period_start,
                billing_period_end,
            ) = self._build_command_preview_data(
                resource_uuid,
                data,
                allocation,
                grace_ratio,
                defaults["current_usage"],
                current_qos,
            )

        result["preview_commands"] = preview_commands
        result["command_history"] = serializers.SlurmCommandHistorySerializer(
            command_history, many=True
        ).data
        result["billing_period_start"] = billing_period_start
        result["billing_period_end"] = billing_period_end

        response_serializer = serializers.SlurmPolicyPreviewResponseSerializer(result)
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        request=serializers.SlurmCommandResultSerializer,
        responses={200: None},
        description="Report command execution result from site agent.",
    )
    @action(detail=True, methods=["post"], url_path="report-command-result")
    def report_command_result(self, request, uuid=None):
        """Accept a command execution result from the site agent."""
        policy = self.get_object()
        serializer = serializers.SlurmCommandResultSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        resource_uuid = str(data["resource_uuid"])
        success = data["success"]
        error_message = data.get("error_message", "")
        mode = data.get("mode", "production")
        commands_executed = data.get("commands_executed", [])

        try:
            resource = marketplace_models.Resource.objects.get(uuid=resource_uuid)
        except marketplace_models.Resource.DoesNotExist:
            raise ValidationError({"resource_uuid": "Resource not found."})

        # Update the most recent SlurmCommandHistory records for this resource
        recent_commands = models.SlurmCommandHistory.objects.filter(
            policy=policy,
            resource=resource,
        ).order_by("-executed_at")[:20]

        for cmd in recent_commands:
            cmd.execution_mode = mode
            if not success:
                cmd.success = False
                cmd.error_message = error_message
            cmd.save()

        # Update the most recent evaluation log for this resource
        evaluation_log = (
            models.SlurmPolicyEvaluationLog.objects.filter(
                policy=policy,
                resource=resource,
            )
            .order_by("-evaluated_at")
            .first()
        )
        if evaluation_log:
            evaluation_log.site_agent_confirmed = success
            evaluation_log.site_agent_response = {
                "success": success,
                "error_message": error_message,
                "mode": mode,
                "commands_executed": commands_executed,
            }
            evaluation_log.save()

        return Response(
            {"detail": "Command result recorded."},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        responses={200: serializers.SlurmPolicyEvaluationLogSerializer(many=True)},
        description="List evaluation logs for this policy.",
    )
    @action(detail=True, methods=["get"], url_path="evaluation-logs")
    def evaluation_logs(self, request, uuid=None):
        """Return evaluation history for this policy."""
        policy = self.get_object()
        queryset = models.SlurmPolicyEvaluationLog.objects.filter(
            policy=policy,
        ).select_related("resource")

        resource_uuid = request.query_params.get("resource_uuid")
        if resource_uuid:
            queryset = queryset.filter(resource__uuid=resource_uuid)

        billing_period = request.query_params.get("billing_period")
        if billing_period:
            queryset = queryset.filter(billing_period=billing_period)

        queryset = queryset.order_by("-evaluated_at")[:100]
        serializer = serializers.SlurmPolicyEvaluationLogSerializer(queryset, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        responses={200: serializers.SlurmCommandHistorySerializer(many=True)},
        description="List command history for this policy.",
    )
    @action(detail=True, methods=["get"], url_path="command-history")
    def command_history_list(self, request, uuid=None):
        """Return command execution history for this policy."""
        policy = self.get_object()
        queryset = models.SlurmCommandHistory.objects.filter(
            policy=policy,
        ).select_related("resource")

        resource_uuid = request.query_params.get("resource_uuid")
        if resource_uuid:
            queryset = queryset.filter(resource__uuid=resource_uuid)

        queryset = queryset.order_by("-executed_at")[:100]
        serializer = serializers.SlurmCommandHistorySerializer(queryset, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def _get_policy_resources(self, policy, resource_uuid=None):
        """Get resources for a policy, optionally filtered to a single resource."""
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
        return list(resources_qs)

    @extend_schema(
        request=serializers.SlurmPolicyEvaluateRequestSerializer,
        responses={200: serializers.SlurmPolicyDryRunResponseSerializer},
        description="Staff-only. Dry-run evaluation: calculates usage percentages and "
        "shows what actions would be triggered, without applying any changes.",
    )
    @action(detail=True, methods=["post"], url_path="dry-run")
    def dry_run(self, request, uuid=None):
        """Dry-run policy evaluation — read-only, no state changes."""
        policy = self.get_object()
        serializer = serializers.SlurmPolicyEvaluateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        resource_uuid = serializer.validated_data.get("resource_uuid")

        resources = self._get_policy_resources(policy, resource_uuid)
        current_period = policy._get_current_period()
        grace_limit_percentage = (1 + policy.grace_ratio) * 100

        results = []
        for resource in resources:
            usage_pct = policy.get_resource_usage_percentage(resource, current_period)
            would_trigger = []
            if (
                usage_pct >= grace_limit_percentage
                and "request_slurm_resource_pausing" in policy.actions
            ):
                would_trigger.append("pause")
            if (
                usage_pct >= 100
                and "request_slurm_resource_downscaling" in policy.actions
            ):
                would_trigger.append("downscale")
            if usage_pct >= 80 and "notify_organization_owners" in policy.actions:
                would_trigger.append("notify")

            results.append(
                {
                    "resource_uuid": resource.uuid,
                    "resource_name": resource.name,
                    "usage_percentage": round(usage_pct, 2),
                    "paused": bool(resource.paused),
                    "downscaled": bool(resource.downscaled),
                    "would_trigger": would_trigger,
                }
            )

        response_data = {
            "policy_uuid": policy.uuid,
            "billing_period": current_period,
            "grace_limit_percentage": grace_limit_percentage,
            "resources": results,
        }
        return Response(
            serializers.SlurmPolicyDryRunResponseSerializer(response_data).data,
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        request=serializers.SlurmPolicyEvaluateRequestSerializer,
        responses={200: serializers.SlurmPolicyEvaluateResponseSerializer},
        description="Staff-only. Run synchronous policy evaluation: calculates usage, "
        "applies actions (pause/downscale/notify), and creates evaluation logs.",
    )
    @action(detail=True, methods=["post"], url_path="evaluate")
    def evaluate(self, request, uuid=None):
        """Run synchronous policy evaluation — applies actions and creates logs."""

        policy = self.get_object()
        serializer = serializers.SlurmPolicyEvaluateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        resource_uuid = serializer.validated_data.get("resource_uuid")

        resources = self._get_policy_resources(policy, resource_uuid)
        current_period = policy._get_current_period()

        results = []
        for resource in resources:
            # Run the evaluation task synchronously (call directly, not .delay())
            policy_tasks.evaluate_resource_against_policy(
                str(resource.uuid), str(policy.uuid)
            )

            # Fetch the log that was just created
            log = (
                models.SlurmPolicyEvaluationLog.objects.filter(
                    policy=policy,
                    resource=resource,
                )
                .order_by("-evaluated_at")
                .first()
            )

            if log:
                results.append(
                    {
                        "resource_uuid": resource.uuid,
                        "resource_name": resource.name,
                        "usage_percentage": log.usage_percentage,
                        "actions_taken": log.actions_taken,
                        "previous_state": log.previous_state,
                        "new_state": log.new_state,
                    }
                )

        response_data = {
            "policy_uuid": policy.uuid,
            "billing_period": current_period,
            "resources": results,
        }
        return Response(
            serializers.SlurmPolicyEvaluateResponseSerializer(response_data).data,
            status=status.HTTP_200_OK,
        )

    force_period_reset_permissions = [structure_permissions.is_staff]

    @extend_schema(
        request=serializers.SlurmPolicyEvaluateRequestSerializer,
        responses={200: serializers.SlurmPolicyEvaluateResponseSerializer},
        description="Staff-only. Force-trigger period reset: re-evaluates paused/downscaled "
        "resources whose usage in the current period is below thresholds. "
        "Useful after a Celery beat outage or to immediately unblock resources.",
    )
    @action(detail=True, methods=["post"], url_path="force-period-reset")
    def force_period_reset(self, request, uuid=None):
        """Force-trigger period reset for paused/downscaled resources."""

        policy = self.get_object()
        serializer = serializers.SlurmPolicyEvaluateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        resource_uuid = serializer.validated_data.get("resource_uuid")

        current_period = policy._get_current_period()

        # Get paused/downscaled resources under this policy's offering
        resources_qs = (
            marketplace_models.Resource.objects.filter(
                offering=policy.scope,
            )
            .exclude(
                state__in=(
                    marketplace_models.ResourceStates.TERMINATED,
                    marketplace_models.ResourceStates.TERMINATING,
                ),
            )
            .filter(
                Q(paused=True) | Q(downscaled=True),
            )
        )
        if resource_uuid:
            resources_qs = resources_qs.filter(uuid=resource_uuid)

        resources = list(resources_qs)

        results = []
        for resource in resources:
            usage_pct = policy.get_resource_usage_percentage(resource, current_period)
            if usage_pct < 100:
                policy_tasks.evaluate_resource_against_policy(
                    str(resource.uuid), str(policy.uuid)
                )

                log = (
                    models.SlurmPolicyEvaluationLog.objects.filter(
                        policy=policy,
                        resource=resource,
                    )
                    .order_by("-evaluated_at")
                    .first()
                )

                if log:
                    results.append(
                        {
                            "resource_uuid": resource.uuid,
                            "resource_name": resource.name,
                            "usage_percentage": log.usage_percentage,
                            "actions_taken": log.actions_taken,
                            "previous_state": log.previous_state,
                            "new_state": log.new_state,
                        }
                    )

        response_data = {
            "policy_uuid": policy.uuid,
            "billing_period": current_period,
            "resources": results,
        }
        return Response(
            serializers.SlurmPolicyEvaluateResponseSerializer(response_data).data,
            status=status.HTTP_200_OK,
        )
