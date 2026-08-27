from django.contrib import admin

from waldur_core.passkeys import models


class PasskeyCredentialAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "user",
        "attachment",
        "is_discoverable",
        "is_active",
        "created",
        "last_used_at",
    )
    list_filter = ("is_active", "is_discoverable", "attachment")
    search_fields = ("name", "user__username", "aaguid")
    # A credential is established by the authenticator, not by an admin. Making
    # these writable would let somebody hand-edit a public key or a signature
    # counter, which is either useless or a way to weaken replay detection.
    readonly_fields = (
        "user",
        "credential_id",
        "public_key",
        "sign_count",
        "aaguid",
        "transports",
        "attachment",
        "rp_id",
        "is_backup_eligible",
        "is_backed_up",
        "is_discoverable",
        "is_user_verified",
        "created",
        "last_used_at",
        "last_used_ip",
        "use_count",
        "revoked_at",
        "revoked_by",
    )

    def has_add_permission(self, request):
        # Credentials can only come from a real ceremony.
        return False


class PasskeyCeremonyAdmin(admin.ModelAdmin):
    list_display = ("uuid", "kind", "user", "created", "expires_at", "attempts")
    list_filter = ("kind",)
    readonly_fields = (
        "kind",
        "challenge",
        "rp_id",
        "user",
        "created",
        "expires_at",
        "attempts",
        "consumed_at",
    )

    def has_add_permission(self, request):
        return False


admin.site.register(models.PasskeyCredential, PasskeyCredentialAdmin)
admin.site.register(models.PasskeyCeremony, PasskeyCeremonyAdmin)
