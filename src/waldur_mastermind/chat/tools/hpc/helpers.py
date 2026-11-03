"""Shared plumbing for the HPC tools: allocation lookup and error shaping.

The tools are a thin layer over ``waldur_firecrest.workspace.HpcWorkspace``,
the same service the HPC workspace REST API uses, so the assistant can never
do more than the user could do in the UI: FirecREST runs everything as the
user, and the workspace confines jobs to the allocation's Slurm account.
"""

from waldur_firecrest.client import FirecrestException
from waldur_firecrest.workspace import (
    HpcWorkspace,
    WorkspaceUnavailable,
    resources_for_user,
)
from waldur_mastermind.chat.tools.account.helpers import validate_uuid

ALLOCATION_PROPERTY = {
    "allocation": {
        "type": "string",
        "description": (
            "HPC allocation: its UUID from list_hpc_allocations, or its name. "
            "May be omitted when the user has exactly one allocation."
        ),
    }
}


class AllocationNotResolved(Exception):
    def __init__(self, result: dict):
        super().__init__(result.get("summary", ""))
        self.result = result


def allocation_summary(resource) -> dict:
    return {
        "uuid": resource.uuid.hex,
        "name": resource.name,
        "account": resource.backend_id,
        "cluster": (resource.offering.plugin_options or {}).get(
            "firecrest_system_name", ""
        ),
        "offering": resource.offering.name,
        "project": resource.project.name,
        "organization": resource.project.customer.name,
    }


def resolve_workspace(user, arguments: dict) -> HpcWorkspace:
    """Find the allocation the user means, or raise with a result to return."""
    resources = resources_for_user(user)
    reference = (arguments.get("allocation") or "").strip()
    if reference:
        if validate_uuid(reference):
            matches = list(resources.filter(uuid=reference))
        else:
            matches = list(resources.filter(name__iexact=reference)) or list(
                resources.filter(name__icontains=reference)
            )
    else:
        matches = list(resources[:16])

    if not matches:
        summary = (
            f"No HPC allocation matches '{reference}'."
            if reference
            else "You have no HPC allocations connected to FirecREST."
        )
        raise AllocationNotResolved({"type": "error", "summary": summary})
    if len(matches) > 1:
        raise AllocationNotResolved(
            {
                "type": "validation_error",
                "summary": "Several HPC allocations match; ask the user which one "
                "and pass its uuid as `allocation`.",
                "data": {"allocations": [allocation_summary(r) for r in matches]},
            }
        )
    try:
        return HpcWorkspace.for_resource(matches[0], user)
    except WorkspaceUnavailable as e:
        raise AllocationNotResolved({"type": "error", "summary": str(e)})


def run(user, arguments: dict, action) -> dict:
    """Resolve the workspace and run *action(workspace)*, mapping failures."""
    try:
        workspace = resolve_workspace(user, arguments)
        return action(workspace)
    except AllocationNotResolved as e:
        return e.result
    except WorkspaceUnavailable as e:
        return {"type": "error", "summary": str(e)}
    except FirecrestException as e:
        return {"type": "error", "summary": str(e)}


def job_summary(job: dict) -> dict:
    status = job.get("status") or {}
    time = job.get("time") or {}
    return {
        "job_id": job.get("jobId"),
        "name": job.get("name"),
        "state": status.get("state"),
        "state_reason": status.get("stateReason"),
        "exit_code": status.get("exitCode"),
        "partition": job.get("partition"),
        "nodes": job.get("nodes"),
        "elapsed_seconds": time.get("elapsed"),
        # FirecREST reports the limit in seconds.
        "time_limit_minutes": time["limit"] // 60 if time.get("limit") else None,
        "working_directory": job.get("workingDirectory"),
        **{
            key: value
            for key, value in (job.get("metrics") or {}).items()
            if value is not None
        },
    }


JOB_SPEC_PROPERTIES = {
    "name": {
        "type": "string",
        "description": "Job name: letters, digits, '.', '_' or '-'.",
    },
    "commands": {
        "type": "string",
        "description": "Shell commands the job runs, e.g. 'srun gmx mdrun -s topol.tpr'. "
        "Do not include #SBATCH lines or module loads; use the other fields.",
    },
    "partition": {"type": "string", "description": "Partition name."},
    "qos": {"type": "string", "description": "QoS name, if not the default."},
    "nodes": {"type": "integer", "minimum": 1},
    "ntasks_per_node": {"type": "integer", "minimum": 1},
    "cpus_per_task": {"type": "integer", "minimum": 1},
    "memory_mb": {"type": "integer", "minimum": 1, "description": "Memory per node."},
    "time_limit_minutes": {"type": "integer", "minimum": 1},
    "gpus_per_node": {"type": "integer", "minimum": 0},
    "modules": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Setup lines exactly as find_hpc_software returned them "
        "('setup' lines first, then 'load' lines).",
    },
    "working_directory": {
        "type": "string",
        "description": "Absolute path; defaults to the user's home directory.",
    },
}
