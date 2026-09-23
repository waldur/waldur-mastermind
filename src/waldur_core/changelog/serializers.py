from rest_framework import serializers


class SecurityAlertEntrySerializer(serializers.Serializer):
    version = serializers.CharField()
    max_urgency = serializers.CharField()


class SecurityAlertSerializer(serializers.Serializer):
    max_urgency = serializers.CharField()
    count = serializers.IntegerField()
    versions = SecurityAlertEntrySerializer(many=True)


class ChangelogSummarySerializer(serializers.Serializer):
    versions_behind = serializers.IntegerField()
    breaking_release_count = serializers.IntegerField()
    has_breaking_changes = serializers.BooleanField()
    security_alert = SecurityAlertSerializer(required=False, allow_null=True)


class ImpactSerializer(serializers.Serializer):
    risk = serializers.CharField()
    affected_scope = serializers.CharField(required=False)
    affected_resources = serializers.DictField(required=False)
    affected_users = serializers.DictField(required=False)


class SecurityDetailSerializer(serializers.Serializer):
    urgency = serializers.CharField()
    cve = serializers.CharField(required=False, allow_null=True)
    ghsa = serializers.CharField(required=False, allow_null=True)
    affected_versions = serializers.CharField()
    exploitability = serializers.CharField()
    mitigation = serializers.CharField()
    advisory_url = serializers.CharField(required=False, allow_null=True)


class ActionSerializer(serializers.Serializer):
    type = serializers.CharField()
    description = serializers.CharField()
    automatic = serializers.BooleanField(required=False, default=False)
    deadline = serializers.CharField(required=False, allow_null=True)


class RelevantWhenSerializer(serializers.Serializer):
    plugins = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    feature_flags = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    settings = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )


class ChangelogEntrySerializer(serializers.Serializer):
    id = serializers.CharField()
    type = serializers.CharField()
    category = serializers.CharField()
    title = serializers.CharField()
    description = serializers.CharField()
    scope = serializers.CharField()
    component = serializers.ListField(child=serializers.CharField())
    highlight = serializers.BooleanField(required=False, default=False)
    impact = ImpactSerializer()
    security = SecurityDetailSerializer(required=False, allow_null=True)
    actions = ActionSerializer(many=True, required=False, default=list)
    relevant_when = RelevantWhenSerializer(required=False)
    # Added by relevance matching
    relevant = serializers.BooleanField(required=False)
    relevance_reasons = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    # Added by impact analysis
    affected_resources_count = serializers.IntegerField(required=False)
    affected_users_count = serializers.IntegerField(required=False)
    settings_analysis = serializers.DictField(required=False)
    plugin_analysis = serializers.DictField(required=False)


class ComponentActivitySerializer(serializers.Serializer):
    commits_since_base = serializers.IntegerField(required=False)
    commits_since_previous = serializers.IntegerField(required=False)
    compare_url_base = serializers.CharField(required=False)
    compare_url_previous = serializers.CharField(required=False)


class ChangelogReleaseSerializer(serializers.Serializer):
    version = serializers.CharField()
    date = serializers.CharField()
    type = serializers.CharField()
    summary = serializers.CharField()
    entries = ChangelogEntrySerializer(many=True)
    since_previous = ChangelogEntrySerializer(many=True, required=False)
    component_activity = serializers.DictField(
        child=ComponentActivitySerializer(), required=False
    )


class ChangelogPendingSerializer(serializers.Serializer):
    current_version = serializers.CharField()
    latest_version = serializers.CharField()
    versions_behind = serializers.IntegerField()
    impact_analysis_status = serializers.CharField(required=False)
    impact_analysis_computed_at = serializers.DateTimeField(required=False)
    releases = ChangelogReleaseSerializer(many=True)


class ChangelogFlatEntrySerializer(ChangelogEntrySerializer):
    """Extended entry serializer with version metadata for flat list endpoint."""

    version = serializers.CharField()
    release_date = serializers.CharField()
    release_type = serializers.CharField()


class ChangelogEntryListSerializer(serializers.Serializer):
    """Paginated list response for the table endpoint."""

    count = serializers.IntegerField()
    current_version = serializers.CharField()
    latest_version = serializers.CharField()
    versions_behind = serializers.IntegerField()
    results = ChangelogFlatEntrySerializer(many=True)


class UpgradeCommandsSerializer(serializers.Serializer):
    helm = serializers.CharField()
    docker_compose = serializers.CharField()


class ChangelogUpgradeReportSerializer(serializers.Serializer):
    """Upgrade report and announcement covering every pending entry."""

    current_version = serializers.CharField()
    latest_version = serializers.CharField()
    entry_count = serializers.IntegerField()
    commands = UpgradeCommandsSerializer()
    report = serializers.CharField(help_text="Markdown upgrade report")
    announcement = serializers.CharField(
        help_text="Markdown text for a maintenance announcement"
    )
    announcement_type = serializers.ChoiceField(choices=["information", "warning"])
