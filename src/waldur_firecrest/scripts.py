"""Batch scripts built from structured job parameters and the offering's limits.

The UI and the AI assistant describe a job as parameters (partition, nodes,
time, modules, commands); this module renders the Slurm script and checks
the parameters against what Waldur knows about the cluster: the offering's
partitions (``OfferingPartition``) and the QoS each partition allows.
"""

import re
from dataclasses import dataclass, field

from waldur_mastermind.marketplace import models as marketplace_models

JOB_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# A file the job writes into its working directory: a plain name, no path,
# and not hidden, so a job cannot quietly replace ~/.bashrc and the like.
FILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$")
MAX_JOB_FILES = 16
MAX_JOB_FILES_SIZE = 256 * 1024


@dataclass
class JobSpec:
    name: str
    commands: str
    partition: str = ""
    qos: str = ""
    nodes: int = 1
    ntasks_per_node: int = 1
    cpus_per_task: int = 1
    memory_mb: int | None = None
    time_limit_minutes: int = 60
    gpus_per_node: int = 0
    modules: list[str] = field(default_factory=list)
    working_directory: str = ""
    environment: dict = field(default_factory=dict)
    # Files written into the working directory before the commands run, as
    # {"name", "content"}: a program the job runs stays readable on its own
    # instead of being folded into the commands.
    files: list[dict] = field(default_factory=list)


@dataclass
class Validation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self):
        return not self.errors


def format_time(minutes: int) -> str:
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}-{hours:02d}:{mins:02d}:00"
    return f"{hours:02d}:{mins:02d}:00"


def build_script(spec: JobSpec, account: str) -> str:
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name={spec.name}",
        f"#SBATCH --account={account}",
    ]
    if spec.partition:
        lines.append(f"#SBATCH --partition={spec.partition}")
    if spec.qos:
        lines.append(f"#SBATCH --qos={spec.qos}")
    lines += [
        f"#SBATCH --nodes={spec.nodes}",
        f"#SBATCH --ntasks-per-node={spec.ntasks_per_node}",
        f"#SBATCH --cpus-per-task={spec.cpus_per_task}",
        f"#SBATCH --time={format_time(spec.time_limit_minutes)}",
    ]
    if spec.memory_mb:
        lines.append(f"#SBATCH --mem={spec.memory_mb}M")
    if spec.gpus_per_node:
        lines.append(f"#SBATCH --gpus-per-node={spec.gpus_per_node}")
    lines.append("")
    if spec.modules:
        lines.append("module purge")
        # Keep order (catalog setup lines come first) but drop repeats.
        lines += list(dict.fromkeys(spec.modules))
        lines.append("")
    for job_file in spec.files:
        lines += _write_file(job_file["name"], job_file["content"])
        lines.append("")
    lines.append(spec.commands.strip())
    return "\n".join(lines) + "\n"


def _write_file(name: str, content: str) -> list[str]:
    """A quoted here-document that writes ``content`` to ``name``.

    The quoted delimiter keeps the shell from expanding anything in the file,
    and no line of the file equals it, so none ends it early. The file ends
    with exactly one newline, as a here-document always does.
    """
    file_lines = content.rstrip("\n").split("\n")
    delimiter = "EOF"
    while delimiter in file_lines:
        delimiter = f"_{delimiter}_"
    return [f"cat > {name} <<'{delimiter}'", *file_lines, delimiter]


def get_partition(offering, name):
    if not name:
        return None
    return marketplace_models.OfferingPartition.objects.filter(
        offering=offering, partition_name=name
    ).first()


def validate(spec: JobSpec, offering) -> Validation:
    result = Validation()
    if not JOB_NAME_PATTERN.match(spec.name or ""):
        result.errors.append(
            "Job name must be 1-128 characters of letters, digits, '.', '_' or '-'."
        )
    if not spec.commands.strip():
        result.errors.append("The job has no commands to run.")
    for value, label in (
        (spec.nodes, "Nodes"),
        (spec.ntasks_per_node, "Tasks per node"),
        (spec.cpus_per_task, "CPUs per task"),
        (spec.time_limit_minutes, "Time limit"),
    ):
        if value < 1:
            result.errors.append(f"{label} must be at least 1.")

    partitions = marketplace_models.OfferingPartition.objects.filter(offering=offering)
    if not partitions.exists():
        if spec.partition:
            result.warnings.append(
                "The offering does not publish its partitions; "
                f"'{spec.partition}' is passed to the cluster unchecked."
            )
        return result

    partition = partitions.filter(partition_name=spec.partition).first()
    if not spec.partition:
        result.warnings.append(
            "No partition given; the cluster's default partition will be used."
        )
        return result
    if partition is None:
        names = ", ".join(sorted(partitions.values_list("partition_name", flat=True)))
        result.errors.append(
            f"Partition '{spec.partition}' does not exist. Available: {names}."
        )
        return result

    if partition.max_time and spec.time_limit_minutes > partition.max_time:
        result.errors.append(
            f"Time limit {format_time(spec.time_limit_minutes)} exceeds the "
            f"partition maximum of {format_time(partition.max_time)}."
        )
    if partition.max_nodes and spec.nodes > partition.max_nodes:
        result.errors.append(
            f"{spec.nodes} nodes exceed the partition maximum of {partition.max_nodes}."
        )
    if partition.min_nodes and spec.nodes < partition.min_nodes:
        result.errors.append(
            f"The partition requires at least {partition.min_nodes} nodes."
        )
    cpus_per_node = spec.ntasks_per_node * spec.cpus_per_task
    if partition.max_cpus_per_node and cpus_per_node > partition.max_cpus_per_node:
        result.errors.append(
            f"{cpus_per_node} CPUs per node exceed the partition maximum of "
            f"{partition.max_cpus_per_node}."
        )
    if (
        spec.memory_mb
        and partition.max_mem_per_node
        and spec.memory_mb > partition.max_mem_per_node
    ):
        result.errors.append(
            f"{spec.memory_mb} MB per node exceed the partition maximum of "
            f"{partition.max_mem_per_node} MB."
        )
    if spec.gpus_per_node and not partition.gpu_arch:
        result.warnings.append(
            f"Partition '{partition.partition_name}' does not declare GPUs."
        )

    allowed_qos = {
        link.qos.name: link.qos for link in partition.qos_options.select_related("qos")
    }
    if spec.qos:
        if allowed_qos and spec.qos not in allowed_qos:
            result.errors.append(
                f"QoS '{spec.qos}' is not allowed on partition "
                f"'{partition.partition_name}'. Allowed: {', '.join(sorted(allowed_qos))}."
            )
        qos = allowed_qos.get(spec.qos)
        if qos and qos.max_time and spec.time_limit_minutes > qos.max_time:
            result.errors.append(
                f"Time limit exceeds the QoS '{qos.name}' maximum of "
                f"{format_time(qos.max_time)}."
            )
        if qos and qos.max_nodes and spec.nodes > qos.max_nodes:
            result.errors.append(
                f"{spec.nodes} nodes exceed the QoS '{qos.name}' maximum of {qos.max_nodes}."
            )
    return result
