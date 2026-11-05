"""HPC tools that plan, submit and follow jobs."""

from waldur_firecrest import scripts
from waldur_firecrest.serializers import TIME_WINDOW_CHOICES, HpcJobSpecSerializer
from waldur_firecrest.workspace import is_job_id
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


def _error_lines(errors, path=""):
    """DRF errors as 'files[0].name: message' lines, nested ones included.

    A list field's errors come keyed by the item's index, as a list or as a
    dict; non_field_errors belong to the field itself.
    """
    if isinstance(errors, dict):
        items = errors.items()
    elif isinstance(errors, list) and not all(isinstance(e, str) for e in errors):
        items = enumerate(errors)
    else:
        messages = errors if isinstance(errors, list) else [errors]
        text = " ".join(str(message) for message in messages)
        yield f"{path}: {text}" if path else text
        return
    for key, value in items:
        if not value:
            continue
        if isinstance(key, int):
            child = f"{path}[{key}]"
        elif key == "non_field_errors":
            child = path
        else:
            child = f"{path}.{key}" if path else key
        yield from _error_lines(value, child)


def parse_spec(arguments: dict):
    data = {k: v for k, v in arguments.items() if k in JOB_SPEC_PROPERTIES}
    serializer = HpcJobSpecSerializer(data=data)
    if not serializer.is_valid():
        errors = "; ".join(_error_lines(serializer.errors))
        return None, {"type": "validation_error", "summary": errors}
    return scripts.JobSpec(**serializer.validated_data), None


def job_facts(workspace, spec) -> dict:
    """What the job asks for, with the defaults it was rendered with: the
    model describes the job from these, not from the script it is not given."""
    return {
        "account": workspace.account,
        "partition": spec.partition or "(cluster default)",
        "nodes": spec.nodes,
        "ntasks_per_node": spec.ntasks_per_node,
        "cpus_per_task": spec.cpus_per_task,
        "time_limit_minutes": spec.time_limit_minutes,
        "memory_mb_per_node": spec.memory_mb,
        "gpus_per_node": spec.gpus_per_node,
        "cpu_hours_at_most": round(
            spec.nodes
            * spec.ntasks_per_node
            * spec.cpus_per_task
            * spec.time_limit_minutes
            / 60,
            2,
        ),
        "files": [job_file["name"] for job_file in spec.files],
    }


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


# The job card shows the user the script. The model is not given the script
# and is told the card shows it: a model that has the script pastes it into
# its reply as well. It reads a result's data when there is any, else its
# summary (llm_streamer), so the note goes where the model reads it.
CARD_SHOWS_SCRIPT = (
    "The user sees the batch script in the job card; do not repeat it in your reply."
)


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
            # Not planned where it cannot be submitted.
            workspace.require_firecrest()
            validation = workspace.validate(spec)
            script = scripts.build_script(spec, workspace.account)
            card = job_card(
                workspace,
                spec,
                "preview" if validation.is_valid else "error",
                script=script,
                files=spec.files,
                errors=validation.errors,
                warnings=validation.warnings,
            )
            if not validation.is_valid:
                return {
                    "type": "validation_error",
                    "summary": "The job does not fit the cluster limits: "
                    + " ".join(validation.errors)
                    + " "
                    + CARD_SHOWS_SCRIPT,
                    "ui_component": "hpc_job",
                    "ui_data": card,
                }
            summary = (
                "Job preview ready; wait for the user to confirm before "
                "calling submit_hpc_job with the same parameters. "
                + CARD_SHOWS_SCRIPT
                + " The card has Submit and Modify buttons; say briefly what the "
                "job asks for (nodes, time, CPU-hours) and any warnings."
            )
            return {
                "type": "success",
                "summary": summary,
                "data": {
                    "note": summary,
                    "job": job_facts(workspace, spec),
                    "warnings": validation.warnings,
                },
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
                "data": {
                    "job_id": result["job_id"],
                    "warnings": result["warnings"],
                    "note": CARD_SHOWS_SCRIPT,
                },
                "ui_component": "hpc_job",
                "ui_data": job_card(
                    workspace,
                    spec,
                    "submitted",
                    job_id=result["job_id"],
                    script=result["script"],
                    files=spec.files,
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
                "finished ones within the time window. With only a cluster "
                "monitor (can_run_jobs false) a job is listed once it has "
                "started running: pending jobs are not."
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
                "last lines of its standard output and error. The script and output "
                "are read through FirecREST: with only a cluster monitor "
                "(can_run_jobs false) the job comes without them."
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
        if not is_job_id(job_id):
            return {"type": "validation_error", "summary": "job_id must be numeric."}

        def action(workspace):
            if not workspace.has_firecrest:
                # The cluster monitor has the job, not the files it wrote.
                job = workspace.get_job(job_id)
                return {
                    "type": "success",
                    "summary": f"Job {job_id} is {job['status']['state']}. Its batch "
                    "script and output cannot be read without FirecREST.",
                    "data": {"job": job_summary(job)},
                }
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
        if not is_job_id(job_id):
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
