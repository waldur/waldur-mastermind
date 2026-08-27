from rest_framework import serializers

from waldur_core.passkeys.models import PasskeyCredential


class PasskeyCredentialSerializer(serializers.ModelSerializer):
    """List / retrieve. Every field here is public by nature — a WebAuthn
    public key and credential id are not secrets — but nothing beyond what the
    UI needs is exposed."""

    is_orphaned = serializers.BooleanField(read_only=True)
    revoked_by_username = serializers.ReadOnlyField(source="revoked_by.username")

    class Meta:
        model = PasskeyCredential
        fields = (
            "uuid",
            "name",
            "aaguid",
            "transports",
            "attachment",
            "rp_id",
            "is_backup_eligible",
            "is_backed_up",
            "is_discoverable",
            "is_user_verified",
            "is_orphaned",
            "created",
            "last_used_at",
            "use_count",
            "is_active",
            "revoked_at",
            "revoked_by_username",
            "revocation_reason",
        )
        read_only_fields = fields


class PasskeyCredentialUpdateSerializer(serializers.ModelSerializer):
    """Rename. The name is the only user-editable property of a credential."""

    class Meta:
        model = PasskeyCredential
        fields = ("name",)


class PasskeyRevokeSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class PasskeyRegistrationFinishSerializer(serializers.Serializer):
    ceremony = serializers.UUIDField()
    name = serializers.CharField(max_length=150)
    credential = serializers.JSONField()


class PasskeyAssertionFinishSerializer(serializers.Serializer):
    ceremony = serializers.UUIDField()
    credential = serializers.JSONField()


class PasskeyMfaBeginSerializer(serializers.Serializer):
    ceremony = serializers.UUIDField()


class PasskeyCeremonyOptionsSerializer(serializers.Serializer):
    ceremony = serializers.UUIDField()
    options = serializers.JSONField()


class PasskeyTokenSerializer(serializers.Serializer):
    token = serializers.CharField()
