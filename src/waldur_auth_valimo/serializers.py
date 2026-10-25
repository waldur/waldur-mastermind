import re
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions, serializers

from . import models

# Each login sends a signing prompt to the phone. One login at a time per phone,
# and a limited number of unsuccessful ones an hour, keep anybody from flooding
# a phone with prompts. The flip side: whoever knows a number can use up its
# unsuccessful attempts and so delay its owner's login until the hour has passed,
# which is why successful logins never count and the hourly limit is configurable.
PHONE_LOGIN_TIMEOUT = timedelta(minutes=5)  # how long the service waits for an answer


def normalize_phone(phone):
    phone = re.sub(r"[\s().-]", "", phone)
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    return phone


def check_phone_login_limit(phone):
    now = timezone.now()
    logins = models.AuthResult.objects.filter(phone=phone)
    in_progress = logins.filter(
        state__in=(
            models.AuthResult.States.SCHEDULED,
            models.AuthResult.States.PROCESSING,
        ),
        created__gte=now - PHONE_LOGIN_TIMEOUT,
    ).order_by("created")
    oldest = in_progress.first()
    if oldest:
        wait = oldest.created + PHONE_LOGIN_TIMEOUT - now
    else:
        limit = settings.WALDUR_AUTH_VALIMO.get("LOGIN_ATTEMPTS_PER_PHONE_PER_HOUR")
        unsuccessful = (
            logins.exclude(state=models.AuthResult.States.OK)
            .filter(created__gte=now - timedelta(hours=1))
            .order_by("created")
        )
        if not limit or unsuccessful.count() < limit:
            return
        wait = unsuccessful.first().created + timedelta(hours=1) - now
    raise exceptions.Throttled(
        wait=max(wait.total_seconds(), 1),
        detail=str(_("Too many Mobile ID logins for this phone number.")),
    )


class AuthResultUUIDSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(help_text="UUID of the authentication result.")


class AuthResultSerializer(serializers.ModelSerializer):
    token = serializers.SerializerMethodField()

    class Meta:
        model = models.AuthResult
        fields = (
            "uuid",
            "token",
            "phone",
            "message",
            "state",
            "error_message",
            "details",
        )
        write_only_fields = ("phone",)
        read_only_fields = (
            "uuid",
            "token",
            "message",
            "state",
            "error_message",
            "details",
        )

    def validate_phone(self, phone):
        return normalize_phone(phone)

    def get_token(self, auth_result) -> str:
        if auth_result.user:
            return auth_result.user.auth_token.key
        return None
