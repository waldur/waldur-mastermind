from rest_framework import serializers

from waldur_mastermind.chat.chart_spec import ChartSpecSerializer
from waldur_mastermind.marketplace import models as marketplace_models

from . import monitoring

TIME_WINDOW_CHOICES = ["1h", "8h", "24h", "3d", "7d"]


class HpcWorkspaceSerializer(serializers.ModelSerializer):
    resource_uuid = serializers.UUIDField(source="uuid", read_only=True)
    resource_name = serializers.CharField(source="name", read_only=True)
    account = serializers.CharField(source="backend_id", read_only=True)
    offering_uuid = serializers.UUIDField(source="offering.uuid", read_only=True)
    offering_name = serializers.CharField(source="offering.name", read_only=True)
    project_uuid = serializers.UUIDField(source="project.uuid", read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True)
    customer_name = serializers.CharField(
        source="project.customer.name", read_only=True
    )
    system_name = serializers.SerializerMethodField()
    state = serializers.CharField(source="get_state_display", read_only=True)
    monitoring_enabled = serializers.SerializerMethodField(
        help_text="Jobs have per-run performance samples from the cluster monitor."
    )

    class Meta:
        model = marketplace_models.Resource
        fields = (
            "resource_uuid",
            "resource_name",
            "account",
            "offering_uuid",
            "offering_name",
            "project_uuid",
            "project_name",
            "customer_name",
            "system_name",
            "state",
            "monitoring_enabled",
        )

    def get_system_name(self, resource) -> str:
        return (resource.offering.plugin_options or {}).get("firecrest_system_name", "")

    def get_monitoring_enabled(self, resource) -> bool:
        return monitoring.is_monitoring_enabled(resource.offering)


class HpcServiceHealthSerializer(serializers.Serializer):
    service_type = serializers.CharField(source="serviceType", allow_null=True)
    healthy = serializers.BooleanField(allow_null=True)
    message = serializers.CharField(allow_null=True, required=False)
    last_checked = serializers.CharField(
        source="lastChecked", allow_null=True, required=False
    )


class HpcWorkspaceOverviewSerializer(HpcWorkspaceSerializer):
    username = serializers.CharField(read_only=True, allow_null=True)
    home_directory = serializers.CharField(read_only=True, allow_null=True)
    healthy = serializers.BooleanField(read_only=True, allow_null=True)
    services_health = HpcServiceHealthSerializer(
        many=True, read_only=True, allow_null=True
    )
    error = serializers.CharField(read_only=True, allow_null=True)
    sign_in_provider = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Identity provider to sign in through again to restore HPC access, "
        "when that is what 'error' asks for.",
    )

    class Meta(HpcWorkspaceSerializer.Meta):
        fields = HpcWorkspaceSerializer.Meta.fields + (
            "username",
            "home_directory",
            "healthy",
            "services_health",
            "error",
            "sign_in_provider",
        )


class HpcPartitionSerializer(serializers.Serializer):
    name = serializers.CharField()
    published = serializers.BooleanField(
        help_text="The provider publishes this partition in Waldur."
    )
    live = serializers.BooleanField(help_text="The cluster reports this partition.")
    total_nodes = serializers.IntegerField(allow_null=True)
    idle_nodes = serializers.IntegerField(allow_null=True)
    cpus = serializers.IntegerField(allow_null=True)
    max_time = serializers.IntegerField(allow_null=True, help_text="Minutes")
    default_time = serializers.IntegerField(allow_null=True, help_text="Minutes")
    max_nodes = serializers.IntegerField(allow_null=True)
    max_cpus_per_node = serializers.IntegerField(allow_null=True)
    max_mem_per_node = serializers.IntegerField(allow_null=True, help_text="MB")
    cpu_arch = serializers.CharField(allow_blank=True)
    gpu_arch = serializers.CharField(allow_blank=True)
    qos = serializers.ListField(child=serializers.CharField())
    default_qos = serializers.CharField(allow_blank=True)


class HpcJobSerializer(serializers.Serializer):
    """A job as FirecREST reports it, in Waldur's field naming."""

    job_id = serializers.CharField(source="jobId")
    name = serializers.CharField()
    state = serializers.CharField(source="status.state")
    state_reason = serializers.CharField(
        source="status.stateReason", allow_null=True, required=False
    )
    exit_code = serializers.IntegerField(
        source="status.exitCode", allow_null=True, required=False
    )
    account = serializers.CharField(allow_null=True, required=False)
    partition = serializers.CharField(allow_null=True, required=False)
    user = serializers.CharField(allow_null=True, required=False)
    nodes = serializers.CharField(allow_null=True, required=False)
    allocation_nodes = serializers.IntegerField(
        source="allocationNodes", allow_null=True, required=False
    )
    working_directory = serializers.CharField(
        source="workingDirectory", allow_null=True, required=False
    )
    start_time = serializers.IntegerField(
        source="time.start", allow_null=True, required=False
    )
    end_time = serializers.IntegerField(
        source="time.end", allow_null=True, required=False
    )
    elapsed = serializers.IntegerField(
        source="time.elapsed", allow_null=True, required=False, help_text="Seconds"
    )
    time_limit = serializers.IntegerField(
        source="time.limit",
        allow_null=True,
        required=False,
        help_text="Seconds (FirecREST converts Slurm's minutes)",
    )
    submit_time = serializers.IntegerField(
        source="time.submission", allow_null=True, required=False
    )
    wait_seconds = serializers.IntegerField(
        source="metrics.wait_seconds",
        allow_null=True,
        required=False,
        help_text="From submission to start",
    )
    allocation_cpus = serializers.IntegerField(
        source="allocationCpus", allow_null=True, required=False
    )
    requested_memory_mb = serializers.IntegerField(
        source="requestedMemory", allow_null=True, required=False
    )
    cpu_time_seconds = serializers.IntegerField(
        source="metrics.cpu_time_seconds", allow_null=True, required=False
    )
    cpu_hours = serializers.FloatField(
        source="metrics.cpu_hours",
        allow_null=True,
        required=False,
        help_text="Allocated CPUs x elapsed time",
    )
    cpu_efficiency = serializers.FloatField(
        source="metrics.cpu_efficiency",
        allow_null=True,
        required=False,
        help_text="CPU time / (CPUs x elapsed), 0..1; finished jobs only",
    )
    max_rss_mb = serializers.IntegerField(
        source="metrics.max_rss_mb", allow_null=True, required=False
    )
    memory_efficiency = serializers.FloatField(
        source="metrics.memory_efficiency",
        allow_null=True,
        required=False,
        help_text="Peak RSS / requested memory, 0..1; finished jobs only",
    )
    allocation_gpus = serializers.IntegerField(
        source="allocationGpus", allow_null=True, required=False
    )
    gpu_hours = serializers.FloatField(
        source="metrics.gpu_hours",
        allow_null=True,
        required=False,
        help_text="Allocated GPUs x elapsed time",
    )
    energy_kwh = serializers.FloatField(
        source="metrics.energy_kwh",
        allow_null=True,
        required=False,
        help_text="Energy consumed; where accounting records the energy TRES",
    )
    mean_power_w = serializers.IntegerField(
        source="metrics.mean_power_w",
        allow_null=True,
        required=False,
        help_text="Energy / elapsed time, all of the job's nodes",
    )


class HpcJobListQuerySerializer(serializers.Serializer):
    time_window = serializers.ChoiceField(
        choices=TIME_WINDOW_CHOICES,
        required=False,
        help_text="How far back to look for finished jobs (default 24h).",
    )


class HpcJobSpecSerializer(serializers.Serializer):
    name = serializers.RegexField(r"^[A-Za-z0-9._-]{1,128}$", max_length=128)
    commands = serializers.CharField(
        max_length=64 * 1024, help_text="Shell commands the job runs."
    )
    partition = serializers.CharField(required=False, allow_blank=True, default="")
    qos = serializers.CharField(required=False, allow_blank=True, default="")
    nodes = serializers.IntegerField(min_value=1, default=1)
    ntasks_per_node = serializers.IntegerField(min_value=1, default=1)
    cpus_per_task = serializers.IntegerField(min_value=1, default=1)
    memory_mb = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    time_limit_minutes = serializers.IntegerField(min_value=1, default=60)
    gpus_per_node = serializers.IntegerField(min_value=0, default=0)
    modules = serializers.ListField(
        child=serializers.CharField(max_length=500),
        required=False,
        default=list,
        help_text="Setup lines, e.g. 'module load GROMACS/2024.1-foss-2023b'.",
    )
    working_directory = serializers.CharField(
        required=False, allow_blank=True, default=""
    )
    environment = serializers.DictField(
        child=serializers.CharField(), required=False, default=dict
    )

    def validate_modules(self, modules):
        for line in modules:
            if not line.startswith(("module ", "spack ", "source ")):
                raise serializers.ValidationError(
                    "Setup lines must be 'module …', 'spack …' or 'source …' commands."
                )
            if "\n" in line:
                raise serializers.ValidationError("Setup lines must be single lines.")
        return modules


class HpcScriptPreviewSerializer(serializers.Serializer):
    script = serializers.CharField()
    errors = serializers.ListField(child=serializers.CharField())
    warnings = serializers.ListField(child=serializers.CharField())


class HpcJobSubmitResponseSerializer(serializers.Serializer):
    job_id = serializers.CharField()
    script = serializers.CharField()
    warnings = serializers.ListField(child=serializers.CharField())


class HpcJobStreamSerializer(serializers.Serializer):
    path = serializers.CharField(allow_null=True)
    content = serializers.CharField(allow_null=True)


class HpcJobOutputSerializer(serializers.Serializer):
    job = HpcJobSerializer()
    script = serializers.CharField(allow_null=True)
    standard_output = HpcJobStreamSerializer(source="standardOutput")
    standard_error = HpcJobStreamSerializer(source="standardError")


class HpcJobOutputQuerySerializer(serializers.Serializer):
    lines = serializers.IntegerField(min_value=1, max_value=5000, default=100)


class HpcFileSerializer(serializers.Serializer):
    name = serializers.CharField()
    type = serializers.CharField(help_text="'d' directory, '-' file, 'l' link")
    link_target = serializers.CharField(
        source="linkTarget", allow_null=True, required=False
    )
    user = serializers.CharField()
    group = serializers.CharField()
    permissions = serializers.CharField()
    last_modified = serializers.CharField(source="lastModified")
    size = serializers.CharField()


class HpcFileListQuerySerializer(serializers.Serializer):
    path = serializers.CharField(
        required=False, help_text="Directory to list; defaults to home."
    )
    show_hidden = serializers.BooleanField(required=False, default=False)


class HpcFileContentQuerySerializer(serializers.Serializer):
    path = serializers.CharField()


class HpcFileContentSerializer(serializers.Serializer):
    path = serializers.CharField()
    content = serializers.CharField()


class HpcSoftwareQuerySerializer(serializers.Serializer):
    query = serializers.CharField(required=False, allow_blank=True, default="")
    partition = serializers.CharField(required=False, allow_blank=True, default="")


class HpcSoftwareSerializer(serializers.Serializer):
    name = serializers.CharField()
    version = serializers.CharField()
    catalog = serializers.CharField()
    catalog_type = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    setup = serializers.ListField(
        child=serializers.CharField(),
        help_text="Lines making the catalog available, placed before 'load'.",
    )
    load = serializers.CharField(help_text="Line loading this version in a job.")
    cpu_families = serializers.ListField(child=serializers.CharField())
    gpu_architectures = serializers.ListField(child=serializers.CharField())


# Insights


class HpcJobTotalsSerializer(serializers.Serializer):
    completed = serializers.IntegerField()
    failed = serializers.IntegerField()
    timeout = serializers.IntegerField()
    cancelled = serializers.IntegerField()
    running = serializers.IntegerField()
    pending = serializers.IntegerField()


class HpcJobFailureSerializer(serializers.Serializer):
    exit_code = serializers.IntegerField(allow_null=True)
    count = serializers.IntegerField()
    names = serializers.ListField(child=serializers.CharField(allow_blank=True))
    latest_job_id = serializers.CharField(allow_null=True)


class HpcPartitionWaitSerializer(serializers.Serializer):
    partition = serializers.CharField(allow_blank=True)
    jobs = serializers.IntegerField()
    median_wait_seconds = serializers.IntegerField()
    p90_wait_seconds = serializers.IntegerField()


class HpcSuggestionSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=["time_limit", "cpu_efficiency"])
    name = serializers.CharField(allow_blank=True, allow_null=True)
    jobs = serializers.IntegerField()
    message = serializers.CharField()
    current_limit_minutes = serializers.IntegerField(required=False)
    suggested_limit_minutes = serializers.IntegerField(required=False)


class HpcJobStatsSerializer(serializers.Serializer):
    days = serializers.IntegerField()
    since = serializers.IntegerField()
    until = serializers.IntegerField()
    totals = HpcJobTotalsSerializer()
    cpu_hours = serializers.FloatField()
    failures = HpcJobFailureSerializer(many=True)
    wait_by_partition = HpcPartitionWaitSerializer(many=True)
    suggestions = HpcSuggestionSerializer(many=True)
    charts = ChartSpecSerializer(many=True)


class HpcJobStatsQuerySerializer(serializers.Serializer):
    days = serializers.IntegerField(
        min_value=1,
        max_value=7,
        default=7,
        help_text="Days of job history, up to FirecREST's longest window (7 days).",
    )


class HpcNodeSerializer(serializers.Serializer):
    name = serializers.CharField()
    partitions = serializers.ListField(child=serializers.CharField())
    state = serializers.CharField()
    cpus = serializers.IntegerField()
    alloc_cpus = serializers.IntegerField(allow_null=True)
    load_percent = serializers.FloatField(allow_null=True)
    free_memory_mb = serializers.IntegerField(allow_null=True)
    reason = serializers.CharField(allow_blank=True)


class HpcPartitionLoadSerializer(serializers.Serializer):
    name = serializers.CharField()
    total = serializers.IntegerField()
    idle = serializers.IntegerField()
    mixed = serializers.IntegerField()
    allocated = serializers.IntegerField()
    unavailable = serializers.IntegerField()
    reserved = serializers.IntegerField()
    average_load_percent = serializers.FloatField(allow_null=True)


class HpcReservationSerializer(serializers.Serializer):
    name = serializers.CharField()
    start = serializers.IntegerField()
    end = serializers.IntegerField()
    node_list = serializers.CharField(allow_blank=True)
    node_count = serializers.IntegerField(allow_null=True)
    maintenance = serializers.BooleanField()
    active = serializers.BooleanField()
    partitions = serializers.ListField(child=serializers.CharField())
    announced = serializers.BooleanField(
        allow_null=True,
        help_text="For maintenance reservations: whether Waldur announces it.",
    )


class HpcMaintenanceAnnouncementSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()
    name = serializers.CharField()
    message = serializers.CharField(allow_blank=True)
    state = serializers.CharField()
    impact = serializers.CharField()
    outage = serializers.BooleanField()
    impact_description = serializers.CharField(allow_blank=True)
    start = serializers.IntegerField()
    end = serializers.IntegerField()
    partitions = serializers.ListField(child=serializers.CharField())
    expected_reservation = serializers.CharField(
        allow_blank=True,
        help_text="Reservation the site agent creates for it, when sync is enabled.",
    )
    reservation_in_place = serializers.BooleanField(
        allow_null=True,
        help_text="Whether the cluster holds it; null when no reservation is due.",
    )


class HpcClusterLoadSerializer(serializers.Serializer):
    announcements = HpcMaintenanceAnnouncementSerializer(many=True)
    nodes = HpcNodeSerializer(many=True)
    partitions = HpcPartitionLoadSerializer(many=True)
    reservations = HpcReservationSerializer(many=True)
    charts = ChartSpecSerializer(many=True)


class HpcUsagePointSerializer(serializers.Serializer):
    date = serializers.DateField()
    value = serializers.FloatField()


class HpcMonthlyUsageSerializer(serializers.Serializer):
    month = serializers.DateField()
    value = serializers.FloatField()


class HpcUserUsageSerializer(serializers.Serializer):
    username = serializers.CharField()
    total = serializers.FloatField()
    monthly = HpcMonthlyUsageSerializer(many=True)


class HpcComponentUsageSerializer(serializers.Serializer):
    type = serializers.CharField()
    name = serializers.CharField()
    unit = serializers.CharField(allow_blank=True)
    limit = serializers.FloatField(allow_null=True)
    limit_period = serializers.CharField()
    period_start = serializers.DateField()
    period_end = serializers.DateField(allow_null=True)
    consumed = serializers.FloatField()
    percent = serializers.FloatField(allow_null=True)
    daily_rate = serializers.FloatField(allow_null=True)
    runs_out = serializers.DateField(allow_null=True)
    runs_out_before_period_end = serializers.BooleanField()
    cumulative = HpcUsagePointSerializer(many=True)
    monthly = HpcMonthlyUsageSerializer(many=True)
    users = HpcUserUsageSerializer(many=True)


class HpcAllocationUsageSerializer(serializers.Serializer):
    as_of = serializers.DateField()
    components = HpcComponentUsageSerializer(many=True)
    charts = ChartSpecSerializer(many=True)


class HpcOutputSeriesQuerySerializer(serializers.Serializer):
    path = serializers.CharField()
    forecast_hours = serializers.FloatField(
        required=False,
        allow_null=True,
        min_value=0,
        help_text="WRF only: forecast length, if namelist.input next to the log "
        "does not say.",
    )


class HpcOutputSeriesSerializer(serializers.Serializer):
    path = serializers.CharField()
    kind = serializers.ChoiceField(choices=["xvg", "csv", "wrf_rsl"])
    title = serializers.CharField()
    summary = serializers.DictField()
    chart = ChartSpecSerializer()


class HpcJobPerformanceSummarySerializer(serializers.Serializer):
    nodes = serializers.IntegerField()
    gpus = serializers.IntegerField(allow_null=True)
    gpu_hours_observed = serializers.FloatField(
        allow_null=True, help_text="GPU samples x sample interval"
    )
    gpu_use_percent = serializers.FloatField(
        allow_null=True, help_text="Mean over the job's GPUs and samples"
    )
    idle_share_percent = serializers.FloatField(
        allow_null=True, help_text="Samples where every GPU of the job is under 5%"
    )
    gpu_memory_percent = serializers.FloatField(allow_null=True)
    gpu_memory_peak_gib = serializers.FloatField(
        allow_null=True, help_text="Highest reading of one GPU"
    )
    gpu_memory_total_gib = serializers.FloatField(
        allow_null=True, help_text="Memory of one GPU"
    )
    gpu_power_watts = serializers.FloatField(allow_null=True, help_text="Mean per GPU")
    cpu_cores_mean = serializers.FloatField(
        allow_null=True, help_text="Summed over the job's nodes"
    )
    host_memory_peak_gib = serializers.FloatField(allow_null=True)
    host_memory_mean_gib = serializers.FloatField(allow_null=True)
    samples = serializers.IntegerField()
    coverage_percent = serializers.FloatField(
        allow_null=True, help_text="Sample times seen out of those expected"
    )
    first_sample = serializers.IntegerField(allow_null=True, help_text="Epoch seconds")
    last_sample = serializers.IntegerField(allow_null=True, help_text="Epoch seconds")


class HpcJobProcessSerializer(serializers.Serializer):
    command = serializers.CharField()
    nodes = serializers.IntegerField()
    samples = serializers.IntegerField()
    mean_cpu_percent = serializers.FloatField(allow_null=True)
    max_cpu_percent = serializers.FloatField(allow_null=True)
    peak_rss_gib = serializers.FloatField()


class HpcJobPerformanceSerializer(serializers.Serializer):
    available = serializers.BooleanField()
    message = serializers.CharField(
        allow_blank=True, help_text="Why there is nothing to show, when not available"
    )
    start_time = serializers.IntegerField(required=False, help_text="Epoch seconds")
    end_time = serializers.IntegerField(required=False, help_text="Epoch seconds")
    resolution = serializers.IntegerField(
        required=False, help_text="Seconds between points"
    )
    summary = HpcJobPerformanceSummarySerializer(required=False)
    charts = ChartSpecSerializer(many=True, required=False)
    processes = HpcJobProcessSerializer(many=True, required=False)
    warnings = serializers.ListField(child=serializers.CharField(), required=False)
