from waldur_core.core.tests.helpers import override_waldur_core_settings

RP_ID = "waldur.example.com"
ORIGIN = "https://waldur.example.com"


def enable_passkeys(*, signin=True, mfa=True, rp_id=RP_ID, origins=(ORIGIN,)):
    methods = ["LOCAL_SIGNIN"]
    if signin:
        methods.append("PASSKEY_SIGNIN")
    if mfa:
        methods.append("PASSKEY_MFA")
    return override_waldur_core_settings(
        AUTHENTICATION_METHODS=methods,
        PASSKEY_RP_ID=rp_id,
        PASSKEY_RP_NAME="Waldur",
        PASSKEY_ALLOWED_ORIGINS=list(origins),
    )
