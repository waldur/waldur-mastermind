from constance import config
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import ResourceStates

from . import enums, models, query
from .models import ATTRIBUTE_KEY_VALIDATOR


class RetentionPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = models.RetentionPolicy
        fields = ("uuid", "name", "raw_days", "hourly_days", "daily_days")
        read_only_fields = ("uuid",)

    def validate(self, attrs):
        raw = attrs.get("raw_days", getattr(self.instance, "raw_days", 90))
        hourly = attrs.get("hourly_days", getattr(self.instance, "hourly_days", 400))
        daily = attrs.get("daily_days", getattr(self.instance, "daily_days", None))
        default = config.METRICS_DEFAULT_RETENTION_POLICY
        if (
            self.instance is not None
            and self.instance.name == default
            and attrs.get("name", default) != default
        ):
            raise serializers.ValidationError(
                {"name": _("The default retention policy cannot be renamed.")}
            )
        if raw <= config.METRICS_LATE_DATA_DAYS:
            # Roll-ups recompute the late-data window from raw points.
            raise serializers.ValidationError(
                {"raw_days": _("Must exceed the late-data window.")}
            )
        if hourly < raw or (daily is not None and daily < hourly):
            raise serializers.ValidationError(
                _("Coarser tiers must be kept at least as long as finer ones.")
            )
        return attrs


class MetricDefinitionSerializer(serializers.ModelSerializer):
    retention_policy = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.RetentionPolicy.objects.all(),
        required=False,
        allow_null=True,
    )
    owner_customer = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=structure_models.Customer.objects.all(),
        required=False,
        allow_null=True,
        help_text=_("Service provider the definition is private to; empty is global."),
    )
    owner_customer_name = serializers.ReadOnlyField(source="owner_customer.name")
    attribute_keys = serializers.ListField(
        child=serializers.CharField(
            max_length=50, validators=[ATTRIBUTE_KEY_VALIDATOR]
        ),
        required=False,
        max_length=10,
    )

    class Meta:
        model = models.MetricDefinition
        fields = (
            "uuid",
            "key",
            "name",
            "description",
            "unit",
            "kind",
            "good_direction",
            "attribute_keys",
            "max_attribute_values",
            "retention_policy",
            "owner_customer",
            "owner_customer_name",
            "state",
            "created",
        )
        read_only_fields = ("uuid", "created")
        # The key is unique per owner, global ones included; DRF would read
        # the conditional constraints as making the owner required, so the
        # clash is checked in validate() instead.
        validators = []

    def validate(self, attrs):
        instance = self.instance
        if instance is not None:
            # Services report against the key; the owner decides who may edit.
            for field in ("key", "owner_customer"):
                if field in attrs and attrs[field] != getattr(instance, field):
                    raise serializers.ValidationError({field: _("Cannot be changed.")})
            if instance.has_data():
                locked = {
                    field: _("Cannot be changed once data has been reported.")
                    for field in ("kind", "unit")
                    if field in attrs and attrs[field] != getattr(instance, field)
                }
                removed = set(instance.attribute_keys or []) - set(
                    attrs.get("attribute_keys", instance.attribute_keys) or []
                )
                if removed:
                    locked["attribute_keys"] = _(
                        "Attributes cannot be removed once data has been reported."
                    )
                if locked:
                    raise serializers.ValidationError(locked)
        if (
            instance is not None
            and attrs.get("kind") == enums.MetricKinds.COUNTER
            and instance.offering_metrics.exclude(
                project_aggregation=enums.ProjectAggregations.SUM
            ).exists()
        ):
            raise serializers.ValidationError(
                {
                    "kind": _(
                        "Offerings average this metric across resources; a "
                        "counter can only be added up."
                    )
                }
            )
        keys = attrs.get("attribute_keys")
        if keys is not None and len(set(keys)) != len(keys):
            raise serializers.ValidationError(
                {"attribute_keys": _("Attribute names must be unique.")}
            )
        if keys is not None and query.GROUP_BY_RESOURCE in keys:
            raise serializers.ValidationError(
                {
                    "attribute_keys": _(
                        "'%s' is reserved: figures are broken down by resource under that name."
                    )
                    % query.GROUP_BY_RESOURCE
                }
            )
        if instance is None:
            owner = attrs.get("owner_customer")
            clash = models.MetricDefinition.objects.filter(
                key=attrs["key"], owner_customer=owner
            )
            if clash.exists():
                raise serializers.ValidationError(
                    {"key": _("A definition with this key already exists.")}
                )
        return attrs


class OfferingMetricSerializer(serializers.ModelSerializer):
    offering = serializers.SlugRelatedField(
        slug_field="uuid", queryset=marketplace_models.Offering.objects.all()
    )
    definition = serializers.SlugRelatedField(
        slug_field="uuid", queryset=models.MetricDefinition.objects.all()
    )
    offering_name = serializers.ReadOnlyField(source="offering.name")
    name = serializers.CharField(read_only=True)
    key = serializers.ReadOnlyField(source="definition.key")
    unit = serializers.ReadOnlyField(source="definition.unit")
    kind = serializers.ReadOnlyField(source="definition.kind")
    good_direction = serializers.ReadOnlyField(source="definition.good_direction")
    attribute_keys = serializers.ListField(
        child=serializers.CharField(),
        source="definition.attribute_keys",
        read_only=True,
    )

    class Meta:
        model = models.OfferingMetric
        fields = (
            "uuid",
            "offering",
            "offering_name",
            "definition",
            "key",
            "name",
            "display_name",
            "unit",
            "kind",
            "good_direction",
            "attribute_keys",
            "project_aggregation",
            "state",
            "created",
        )
        read_only_fields = ("uuid", "state", "created")

    def validate(self, attrs):
        instance = self.instance
        if instance is not None:
            for field in ("offering", "definition"):
                if field in attrs and attrs[field] != getattr(instance, field):
                    raise serializers.ValidationError({field: _("Cannot be changed.")})
        offering = attrs.get("offering") or instance.offering
        definition = attrs.get("definition") or instance.definition
        if instance is None:
            if definition.state != enums.DefinitionStates.ACTIVE:
                raise serializers.ValidationError(
                    {"definition": _("A deprecated definition cannot be adopted.")}
                )
            if definition.owner_customer_id not in (None, offering.customer_id):
                raise serializers.ValidationError(
                    {"definition": _("This definition belongs to another provider.")}
                )
            if models.OfferingMetric.objects.filter(
                offering=offering, definition__key=definition.key
            ).exists():
                raise serializers.ValidationError(
                    {
                        "definition": _(
                            "The offering already reports a metric with this key."
                        )
                    }
                )
        aggregation = attrs.get(
            "project_aggregation",
            getattr(instance, "project_aggregation", enums.ProjectAggregations.SUM),
        )
        if (
            definition.kind == enums.MetricKinds.COUNTER
            and aggregation != enums.ProjectAggregations.SUM
        ):
            raise serializers.ValidationError(
                {"project_aggregation": _("Counted amounts can only be added up.")}
            )
        return attrs


class MetricPointSerializer(serializers.Serializer):
    resource = serializers.UUIDField(help_text=_("UUID of the resource"))
    metric = serializers.CharField(
        max_length=100, help_text=_("Key of a metric the resource's offering adopts")
    )
    timestamp = serializers.DateTimeField()
    value = serializers.DecimalField(
        max_digits=24, decimal_places=6, coerce_to_string=False
    )
    start_time = serializers.DateTimeField(
        required=False,
        allow_null=True,
        help_text=_(
            "Only for a counter reported as a running total: when the total "
            "started counting. Omit it to report the increment since the "
            "previous point."
        ),
    )
    attributes = serializers.DictField(
        required=False,
        default=dict,
        help_text=_("Values of the attributes the metric declares"),
    )


class RejectedPointSerializer(serializers.Serializer):
    index = serializers.IntegerField()
    reason = serializers.CharField()


class MetricReportResultSerializer(serializers.Serializer):
    accepted = serializers.IntegerField()
    rejected = RejectedPointSerializer(many=True)


class MetricGoalSerializer(serializers.ModelSerializer):
    offering_metric = serializers.SlugRelatedField(
        slug_field="uuid", queryset=models.OfferingMetric.objects.all()
    )
    project = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=structure_models.Project.objects.all(),
        required=False,
        allow_null=True,
        help_text=_("Empty sets the offering's default goal."),
    )
    project_name = serializers.ReadOnlyField(source="project.name")
    metric_name = serializers.CharField(source="offering_metric.name", read_only=True)

    class Meta:
        model = models.MetricGoal
        fields = (
            "uuid",
            "offering_metric",
            "metric_name",
            "project",
            "project_name",
            "comparator",
            "value",
            "period",
            "created",
        )
        read_only_fields = ("uuid", "created")
        # Checked in validate(): DRF reads the conditional constraints as
        # making the project required.
        validators = []

    def validate(self, attrs):
        instance = self.instance
        if instance is not None:
            for field in ("offering_metric", "project"):
                if field in attrs and attrs[field] != getattr(instance, field):
                    raise serializers.ValidationError({field: _("Cannot be changed.")})
            return attrs
        offering_metric = attrs["offering_metric"]
        project = attrs.get("project")
        if offering_metric.state == enums.OfferingMetricStates.ARCHIVED:
            raise serializers.ValidationError(
                {"offering_metric": _("An archived metric cannot get a goal.")}
            )
        if models.MetricGoal.objects.filter(
            offering_metric=offering_metric, project=project
        ).exists():
            raise serializers.ValidationError(
                _("A goal for this metric and scope already exists.")
            )
        if (
            project is not None
            and not marketplace_models.Resource.objects.filter(
                project=project, offering=offering_metric.offering
            )
            .exclude(state=ResourceStates.TERMINATED)
            .exists()
        ):
            raise serializers.ValidationError(
                {
                    "offering_metric": _(
                        "The project does not use this metric's offering."
                    )
                }
            )
        return attrs


class MetricSeriesQuerySerializer(serializers.Serializer):
    offering_metric_uuid = serializers.UUIDField()
    resource_uuid = serializers.UUIDField(required=False)
    project_uuid = serializers.UUIDField(required=False)
    start = serializers.DateTimeField()
    end = serializers.DateTimeField(required=False)
    granularity = serializers.ChoiceField(
        choices=["auto", *[c[0] for c in enums.Granularities.CHOICES]],
        default="auto",
    )
    group_by = serializers.CharField(required=False, max_length=50)
    aggregate = serializers.ChoiceField(
        choices=["mean", "last", "min", "max"],
        default="mean",
        help_text=_(
            "How a gauge's points within a bucket combine; counters always sum."
        ),
    )

    def validate(self, attrs):
        attrs.setdefault("end", timezone.now())
        if attrs["start"] >= attrs["end"]:
            raise serializers.ValidationError(_("Start must be before end."))
        return attrs


class MetricPointValueSerializer(serializers.Serializer):
    timestamp = serializers.DateTimeField()
    # Read-side figures are for charts: plain JSON numbers, not decimal strings.
    value = serializers.FloatField(allow_null=True)


class MetricSeriesGroupSerializer(serializers.Serializer):
    attributes = serializers.DictField()
    points = MetricPointValueSerializer(many=True)


class MetricSeriesResponseSerializer(serializers.Serializer):
    granularity = serializers.CharField()
    series = MetricSeriesGroupSerializer(many=True)


class ProjectMetricSerializer(serializers.Serializer):
    offering_metric = OfferingMetricSerializer()
    period = serializers.CharField()
    period_start = serializers.DateTimeField()
    current = serializers.FloatField(allow_null=True)
    previous = serializers.FloatField(allow_null=True)
    goal = MetricGoalSerializer(allow_null=True)
    goal_is_project = serializers.BooleanField()
    goal_met = serializers.BooleanField(allow_null=True)


class ResourceMetricSerializer(serializers.Serializer):
    offering_metric = OfferingMetricSerializer()
    period = serializers.CharField()
    period_start = serializers.DateTimeField()
    current = serializers.FloatField(allow_null=True)
    previous = serializers.FloatField(allow_null=True)


class MetricBreakdownItemSerializer(serializers.Serializer):
    value = serializers.JSONField(
        allow_null=True,
        help_text="The attribute's value, or the resource's name when broken down by resource.",
    )
    figure = serializers.FloatField(allow_null=True)
    resource_uuid = serializers.UUIDField(allow_null=True)
    resource_name = serializers.CharField(allow_null=True)
