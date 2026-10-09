import re

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.validators import RegexValidator
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from waldur_core.core.serializers import GenericRelatedField
from waldur_core.structure.models import Project

from . import models, recovery_keys, room_provisioning
from .models import CryptoLeaseKinds

# Matrix appservice registration regexes are built by string interpolation into
# YAML the homeserver compiles as Python re. Validate inputs up front so a
# crafted localpart like ".*" cannot claim the entire user namespace.
SENDER_LOCALPART_RE = re.compile(r"^[a-z0-9._=\-/]+$")
HOMESERVER_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9.\-]+$")


def validate_sender_localpart(value):
    if not SENDER_LOCALPART_RE.fullmatch(value):
        raise serializers.ValidationError(
            "Localpart may only contain lowercase letters, digits, "
            "and the characters . _ = - /"
        )
    return value


def validate_homeserver_domain(value):
    if not HOMESERVER_DOMAIN_RE.fullmatch(value):
        raise serializers.ValidationError(
            "Domain may only contain letters, digits, dots, and hyphens."
        )
    return value


class MatrixCredentialsSerializer(serializers.Serializer):
    method = serializers.ChoiceField(
        choices=settings.CONSTANCE_CONFIG_CHOICES["MATRIX_EXTERNAL_LOGIN_METHOD"]
    )
    homeserver_url = serializers.CharField()
    matrix_user_id = serializers.CharField()


class MatrixPasswordSerializer(serializers.Serializer):
    homeserver_url = serializers.CharField()
    matrix_user_id = serializers.CharField()
    password = serializers.CharField()


class MatrixSessionSerializer(serializers.Serializer):
    homeserver_url = serializers.CharField()
    matrix_user_id = serializers.CharField()
    device_id = serializers.CharField()
    access_token = serializers.CharField()
    refresh_token = serializers.CharField(allow_null=True)
    expires_in_ms = serializers.IntegerField(allow_null=True)
    recovery_key = serializers.CharField(
        allow_null=True,
        help_text="The user's secret-storage recovery key, or null before "
        "encryption is set up. Only ever returned to its owner.",
    )


class MatrixCryptoLeaseRequestSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=CryptoLeaseKinds.CHOICES)


class MatrixCryptoLeaseSerializer(serializers.Serializer):
    lease = serializers.CharField()
    expires_at = serializers.DateTimeField()
    temporary_password = serializers.CharField(
        allow_null=True,
        help_text="For a reset only: the password to answer the homeserver's "
        "interactive auth with. Replaced once the new key is escrowed.",
    )


LEASE_FIELD = dict(
    max_length=64,
    validators=[RegexValidator(r"^[A-Za-z0-9_-]+$", "Not a lease.")],
)


class MatrixCryptoEscrowSerializer(serializers.Serializer):
    lease = serializers.CharField(**LEASE_FIELD)
    recovery_key = serializers.CharField(
        max_length=recovery_keys.MAX_RECOVERY_KEY_LENGTH
    )

    def validate_recovery_key(self, value):
        try:
            recovery_keys.decode_recovery_key(value)
        except recovery_keys.InvalidRecoveryKey as e:
            raise serializers.ValidationError(str(e))
        return value


class MatrixCryptoLeaseReleaseSerializer(serializers.Serializer):
    lease = serializers.CharField(**LEASE_FIELD)


class MatrixCryptoConflictSerializer(serializers.Serializer):
    state = serializers.ChoiceField(
        choices=["set_up", "locked", "not_locked", "in_progress", "no_lease"]
    )
    detail = serializers.CharField()


class MatrixRoomOpenSerializer(serializers.Serializer):
    room_id = serializers.CharField()


class MatrixRoomMemberSummarySerializer(serializers.Serializer):
    user_full_name = serializers.CharField()
    matrix_user_id = serializers.CharField()
    membership_state = serializers.CharField()


class MatrixRoomSerializer(serializers.HyperlinkedModelSerializer):
    scope = GenericRelatedField(read_only=True)
    scope_uuid = serializers.SerializerMethodField()
    scope_name = serializers.SerializerMethodField()
    customer_uuid = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    members_count = serializers.SerializerMethodField()
    members = serializers.SerializerMethodField()
    current_user_membership_state = serializers.SerializerMethodField()

    class Meta:
        model = models.MatrixRoom
        fields = (
            "uuid",
            "url",
            "room_id",
            "room_name",
            "room_alias",
            "state",
            "error_message",
            "scope",
            "scope_uuid",
            "scope_name",
            "customer_uuid",
            "customer_name",
            "members_count",
            "members",
            "current_user_membership_state",
            "created",
            "modified",
        )
        read_only_fields = (
            "uuid",
            "room_id",
            "room_alias",
            "state",
            "error_message",
            "created",
            "modified",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "matrix-room-detail"},
        }

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_scope_uuid(self, obj):
        if obj.scope and hasattr(obj.scope, "uuid"):
            return obj.scope.uuid.hex
        return None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_scope_name(self, obj):
        if obj.scope and hasattr(obj.scope, "name"):
            return obj.scope.name
        return None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_customer_uuid(self, obj):
        if obj.project and obj.project.customer:
            return obj.project.customer.uuid.hex
        return None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_customer_name(self, obj):
        if obj.project and obj.project.customer:
            return obj.project.customer.name
        return None

    @extend_schema_field(serializers.IntegerField())
    def get_members_count(self, obj):
        return obj.members.exclude(
            membership_state=models.MembershipStates.LEFT
        ).count()

    @extend_schema_field(MatrixRoomMemberSummarySerializer(many=True))
    def get_members(self, obj):
        qs = obj.members.select_related("user").order_by("membership_state", "created")[
            :10
        ]
        return [
            {
                "user_full_name": m.user.full_name,
                "matrix_user_id": m.matrix_user_id,
                "membership_state": m.membership_state,
            }
            for m in qs
        ]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_current_user_membership_state(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None
        member = obj.members.filter(user=request.user).first()
        return member.membership_state if member else None


class EligibleProjectSerializer(serializers.Serializer):
    uuid = serializers.CharField()
    name = serializers.CharField()
    customer_uuid = serializers.CharField()
    customer_name = serializers.CharField()


class MatrixRoomCreateSerializer(serializers.Serializer):
    project = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=Project.available_objects.all(),
    )

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request is None or getattr(
            self.context.get("view"), "swagger_fake_view", False
        ):
            return fields
        # A project the caller cannot create for gets the same "does not
        # exist" as a made-up UUID, so the endpoint reveals neither whether it
        # exists nor whether it already has a room.
        fields["project"].queryset = room_provisioning.creatable_projects(request.user)
        return fields

    def validate_project(self, project):
        if not room_provisioning.can_create_room(self.context["request"], project):
            raise PermissionDenied()
        ct = ContentType.objects.get_for_model(project)
        if models.MatrixRoom.objects.filter(
            content_type=ct, object_id=project.id
        ).exists():
            raise serializers.ValidationError(
                "A Matrix room already exists for this project."
            )
        return project


class MatrixRoomMemberSerializer(serializers.HyperlinkedModelSerializer):
    user_uuid = serializers.ReadOnlyField(source="user.uuid")
    user_full_name = serializers.ReadOnlyField(source="user.full_name")
    user_image = serializers.ImageField(source="user.image", read_only=True)

    class Meta:
        model = models.MatrixRoomMember
        fields = (
            "uuid",
            "user_uuid",
            "user_full_name",
            "user_image",
            "matrix_user_id",
            "power_level",
            "membership_state",
            "created",
            "modified",
        )
        read_only_fields = fields


class MatrixHistoryExportSerializer(serializers.HyperlinkedModelSerializer):
    room_uuid = serializers.ReadOnlyField(source="room.uuid")
    room_name = serializers.ReadOnlyField(source="room.room_name")
    export_file_url = serializers.SerializerMethodField()
    media_file_url = serializers.SerializerMethodField()

    class Meta:
        model = models.MatrixHistoryExport
        fields = (
            "uuid",
            "url",
            "room_uuid",
            "room_name",
            "export_type",
            "message_count",
            "media_count",
            "state",
            "error_message",
            "export_file_url",
            "media_file_url",
            "started_at",
            "completed_at",
            "created",
        )
        read_only_fields = fields
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "matrix-export-detail",
            },
        }

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_export_file_url(self, obj):
        if not obj.export_file:
            return None
        return self._download_url(obj, "export")

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_media_file_url(self, obj):
        if not obj.media_file:
            return None
        return self._download_url(obj, "media")

    def _download_url(self, obj, kind):
        # Route through MatrixHistoryExportDownloadView so the file is gated by
        # the same room-access check as the export itself. The raw FileField
        # URL would be served by the storage backend without any auth check.
        from django.urls import reverse

        path = reverse(
            "matrix-export-download",
            kwargs={"uuid": str(obj.uuid), "kind": kind},
        )
        request = self.context.get("request")
        if request:
            return request.build_absolute_uri(path)
        return path


class MatrixRoomDisableSerializer(serializers.Serializer):
    delete_history = serializers.BooleanField(default=False)


class MatrixAppserviceSetupSerializer(serializers.Serializer):
    url = serializers.CharField(
        required=False,
        help_text="Waldur URL reachable by the Matrix homeserver (for webhook callbacks)",
    )
    sender_localpart = serializers.CharField(
        required=False,
        help_text="Localpart for the appservice bot user, e.g. 'waldur-bot'",
    )
    homeserver_url = serializers.URLField(
        required=False,
        allow_blank=True,
        help_text=(
            "Matrix homeserver base URL. Only persisted if MATRIX_HOMESERVER_URL "
            "is not already configured."
        ),
    )
    homeserver_public_url = serializers.URLField(
        required=False,
        allow_blank=True,
        help_text=(
            "Optional. Matrix homeserver URL used by browser clients. Leave "
            "blank when the homeserver URL above is reachable from both "
            "servers and browsers. Set this for deployments where the two "
            "differ (e.g. Docker-internal vs. Caddy-proxied). Only persisted "
            "if MATRIX_HOMESERVER_PUBLIC_URL is not already configured."
        ),
    )
    homeserver_domain = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text=(
            "Matrix homeserver server_name domain. Only persisted if "
            "MATRIX_HOMESERVER_DOMAIN is not already configured."
        ),
    )
    user_registration_secret = serializers.CharField(
        required=False,
        allow_blank=True,
        write_only=True,
        help_text=(
            "Shared secret configured in the homeserver for user registration. "
            "Only persisted if MATRIX_USER_REGISTRATION_SECRET is not already "
            "configured."
        ),
    )

    def validate_sender_localpart(self, value):
        if not value:
            return value
        return validate_sender_localpart(value)

    def validate_homeserver_domain(self, value):
        if not value:
            return value
        return validate_homeserver_domain(value)


class MatrixAppserviceSetupResponseSerializer(serializers.Serializer):
    registration_yaml = serializers.CharField()
    as_token = serializers.CharField()
    hs_token = serializers.CharField()
    sender_localpart = serializers.CharField()
    webhook_url = serializers.CharField()
    bot_provision_status = serializers.CharField()


class MatrixAppserviceStatusSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    as_token_configured = serializers.BooleanField()
    hs_token_configured = serializers.BooleanField()
    sender_localpart = serializers.CharField()
    bot_user_id = serializers.CharField()
    webhook_path = serializers.CharField()
    homeserver_url = serializers.CharField()
    homeserver_domain = serializers.CharField()
    transaction_count = serializers.IntegerField()


class MatrixDiagnosticCheckSerializer(serializers.Serializer):
    name = serializers.CharField()
    label = serializers.CharField()
    ok = serializers.BooleanField()
    detail = serializers.CharField()


class MatrixDiagnosticsResponseSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    checks = MatrixDiagnosticCheckSerializer(many=True)


class MatrixReprovisionResponseSerializer(serializers.Serializer):
    rooms_reprovisioned = serializers.IntegerField()
    users_reset = serializers.IntegerField()


class LiveKitTrackSerializer(serializers.Serializer):
    sid = serializers.CharField()
    name = serializers.CharField(allow_blank=True)
    type = serializers.CharField()
    muted = serializers.BooleanField()
    width = serializers.IntegerField()
    height = serializers.IntegerField()


class LiveKitParticipantSerializer(serializers.Serializer):
    sid = serializers.CharField()
    identity = serializers.CharField()
    state = serializers.CharField()
    is_publisher = serializers.BooleanField()
    joined_at = serializers.IntegerField()
    tracks = LiveKitTrackSerializer(many=True)


class LiveKitRoomSummarySerializer(serializers.Serializer):
    sid = serializers.CharField()
    name = serializers.CharField()
    num_participants = serializers.IntegerField()
    num_publishers = serializers.IntegerField()
    creation_time = serializers.IntegerField()
    max_participants = serializers.IntegerField()
    metadata = serializers.CharField(allow_blank=True)


class LiveKitTotalsSerializer(serializers.Serializer):
    room_count = serializers.IntegerField()
    participant_count = serializers.IntegerField()
    publisher_count = serializers.IntegerField()


class LiveKitOverviewResponseSerializer(serializers.Serializer):
    rooms = LiveKitRoomSummarySerializer(many=True)
    totals = LiveKitTotalsSerializer()
    livekit_url = serializers.CharField()
