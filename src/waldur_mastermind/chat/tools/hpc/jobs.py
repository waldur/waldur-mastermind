"""HPC tools that plan, submit and follow jobs."""

from waldur_firecrest import scripts
from waldur_firecrest.serializers import TIME_WINDOW_CHOICES, HpcJobSpecSerializer
from waldur_mastermind.chat.tools.base import MAX_LIST_RESULTS, BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.hpc.helpers import (
    ALLOCATION_PROPERTY,
    JOB_SPEC_PROPERTIES,
    allocation_summary,
    job_summary,
    run,
)
from waldur_mastermind.chat.tools.registry import tool_registry

# Output shown to the model is capped: enough to diagnose a failure,
# not enough to flood the context with a long log.
OUTPUT_LINES = 60
OUTPUT_CHARS = 6000

JOB_ID_PROPERTY = {
    "job_id": {"type": "string", "description": "Slurm job ID."},
}


def parse_spec(arguments: dict):
    data = {k: v for k, v in arguments.items() if k in JOB_SPEC_PROPERTIES}
    serializer = HpcJobSpecSerializer(data=data)
    if not serializer.is_valid():
        errors = "; ".join(
            f"{field}: {' '.join(str(m) for m in messages)}"
            for field, messages in serializer.errors.items()
        )
        return None, {"type": "validation_error", "summary": errors}
    return scripts.JobSpec(**serializer.validated_data), None


def job_card(workspace, spec, status, **extra) -> dict:
    return {
        "status": status,
        "name": spec.name,
        "allocation": workspace.resource.name,
        "allocation_uuid": workspace.resource.uuid.hex,
        "account": workspace.account,
        "cluster": workspace.system,
        "partition": spec.partition,
        **extra,
    }


class PlanHpcJobTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.PLAN_HPC_JOB,
            category=ToolCategory.HPC,
            description=(
                "Render and check a Slurm batch job without submitting it. Validates "
                "against the partition and QoS limits and shows the user a preview "
                "card with the generated script."
            ),
            inputSchema={
                "type": "object",
                "properties": {**ALLOCATION_PROPERTY, **JOB_SPEC_PROPERTIES},
                "required": ["name", "commands"],
            },
            usage_instructions=(
                "Always call before submit_hpc_job. If the result has errors, fix the "
                "parameters (e.g. lower time or nodes to the partition limits) and plan "
                "again. Ask the user with ask_user when the partition or size is unclear."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        spec, error = parse_spec(arguments)
        if error:
            return error

        def action(workspace):
            validation = workspace.validate(spec)
            script = scripts.build_script(spec, workspace.account)
            card = job_card(
                workspace,
                spec,
                "preview" if validation.is_valid else "error",
                script=script,
                errors=validation.errors,
                warnings=validation.warnings,
            )
            if not validation.is_valid:
                return {
                    "type": "validation_error",
                    "summary": "The job does not fit the cluster limits: "
                    + " ".join(validation.errors),
                    "ui_component": "hpc_job",
                    "ui_data": card,
                }
            return {
                "type": "success",
                "summary": "Job preview ready; wait for the user to confirm before "
                "calling submit_hpc_job with the same parameters.",
                "data": {"script": script, "warnings": validation.warnings},
                "ui_component": "hpc_job",
                "ui_data": card,
            }

        return run(user, arguments, action)


class SubmitHpcJobTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.SUBMIT_HPC_JOB,
            category=ToolCategory.HPC,
            description=(
                "Submit the batch job previewed with plan_hpc_job to the cluster, "
                "charged to the allocation's account. Use ONLY after the user "
                "confirmed the preview."
            ),
            inputSchema={
                "type": "object",
                "properties": {**ALLOCATION_PROPERTY, **JOB_SPEC_PROPERTIES},
                "required": ["name", "commands"],
            },
            usage_instructions=(
                "Call only after plan_hpc_job returned a preview and the user said "
                "'yes'/'submit'/'run it' (or pressed Submit). Pass exactly the "
                "parameters the preview was rendered from. A change request means "
                "plan_hpc_job again, not submit."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        spec, error = parse_spec(arguments)
        if error:
            return error

        def action(workspace):
            result = workspace.submit(spec)
            return {
                "type": "success",
                "summary": f"Submitted job {result['job_id']} on {workspace.system}.",
                "data": {"job_id": result["job_id"], "warnings": result["warnings"]},
                "ui_component": "hpc_job",
                "ui_data": job_card(
                    workspace,
                    spec,
                    "submitted",
                    job_id=result["job_id"],
                    script=result["script"],
                    warnings=result["warnings"],
                ),
            }

        return run(user, arguments, action)


class ListHpcJobsTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.LIST_HPC_JOBS,
            category=ToolCategory.HPC,
            description=(
                "List the jobs of an allocation: pending and running jobs, plus "
                "finished ones within the time window."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **ALLOCATION_PROPERTY,
                    "time_window": {
                        "type": "string",
                        "enum": TIME_WINDOW_CHOICES,
                        "description": "How far back to include finished jobs (default 24h).",
                    },
                },
                "required": [],
            },
            usage_instructions="Use for 'what are my jobs doing?' or 'did my job finish?'.",
        )

    def execute(self, user, arguments: dict) -> dict:
        time_window = arguments.get("time_window")
        if time_window and time_window not in TIME_WINDOW_CHOICES:
            return {
                "type": "validation_error",
                "summary": f"time_window must be one of {', '.join(TIME_WINDOW_CHOICES)}.",
            }

        def action(workspace):
            jobs = workspace.list_jobs(time_window)
            shown = [job_summary(job) for job in jobs[:MAX_LIST_RESULTS]]
            return {
                "type": "success",
                "summary": f"{len(jobs)} job(s) in allocation {workspace.resource.name}.",
                "data": {
                    "allocation": allocation_summary(workspace.resource),
                    "jobs": shown,
                    "_total_count": len(jobs),
                    "_truncated": len(jobs) > len(shown),
                },
            }

        return run(user, arguments, action)


def _trim(content):
    if content and len(content) > OUTPUT_CHARS:
        return "…" + content[-OUTPUT_CHARS:]
    return content


class GetHpcJobTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.GET_HPC_JOB,
            category=ToolCategory.HPC,
            description=(
                "Details of one job: state, exit code, timing, the batch script and the "
                "last lines of its standard output and error."
            ),
            inputSchema={
                "type": "object",
                "properties": {**ALLOCATION_PROPERTY, **JOB_ID_PROPERTY},
                "required": ["job_id"],
            },
            usage_instructions=(
                "Use to follow a job and to explain failures: read state_reason, "
                "exit_code and the error output, then tell the user the likely cause "
                "and a fix (e.g. a missing module, a time limit hit, out of memory)."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        job_id = str(arguments.get("job_id") or "").strip()
        if not job_id.isdigit():
            return {"type": "validation_error", "summary": "job_id must be numeric."}

        def action(workspace):
            output = workspace.job_output(job_id, lines=OUTPUT_LINES)
            return {
                "type": "success",
                "summary": f"Job {job_id} is {output['job'].get('status', {}).get('state')}.",
                "data": {
                    "job": job_summary(output["job"]),
                    "script": _trim(output.get("script")),
                    "standard_output": _trim(output["standardOutput"]["content"]),
                    "standard_error": _trim(output["standardError"]["content"]),
                },
            }

        return run(user, arguments, action)


class CancelHpcJobTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.CANCEL_HPC_JOB,
            category=ToolCategory.HPC,
            description="Cancel a pending or running job of the allocation.",
            inputSchema={
                "type": "object",
                "properties": {**ALLOCATION_PROPERTY, **JOB_ID_PROPERTY},
                "required": ["job_id"],
            },
            usage_instructions=(
                "Destructive: only after the user explicitly asked to cancel this "
                "specific job. If they did not name the job, list jobs and ask."
            ),
        )

    def execute(self, user, arguments: dict) -> dict:
        job_id = str(arguments.get("job_id") or "").strip()
        if not job_id.isdigit():
            return {"type": "validation_error", "summary": "job_id must be numeric."}

        def action(workspace):
            workspace.cancel(job_id)
            return {"type": "success", "summary": f"Cancelled job {job_id}."}

        return run(user, arguments, action)


tool_registry.register(PlanHpcJobTool())
tool_registry.register(SubmitHpcJobTool())
tool_registry.register(ListHpcJobsTool())
tool_registry.register(GetHpcJobTool())
tool_registry.register(CancelHpcJobTool())
