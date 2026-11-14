from django.conf import settings
from django.db import models, transaction
from django.db.models import OuterRef, Q, StringAgg, Subquery, Value
from django.utils import timezone
from model_utils.models import TimeStampedModel
from oauth2_provider.models import (
    get_access_token_model,
    get_grant_model,
    get_refresh_token_model,
    refresh_token_expire_timedelta,
)

from waldur_core.core.models import User, UuidMixin


def joined_scopes(tokens):
    """The scopes of ``tokens`` for the connection, as one space-separated string."""
    return Subquery(
        tokens.filter(user=OuterRef("user"), application=OuterRef("application"))
        .order_by()
        .values("user")
        .annotate(scopes=StringAgg("scope", Value(" ")))
        .values("scopes")
    )


class ConnectedAppQuerySet(models.QuerySet):
    def live(self):
        """Connections that hold a pending code or a token DOT would still accept.

        A refresh token works until REFRESH_TOKEN_EXPIRE_SECONDS after its access
        token expires, and DOT refuses one without an access token, so an access
        token stands for its refresh token too.
        """
        now = timezone.now()
        live_access = Q(expires__gt=now)
        refresh_lifetime = refresh_token_expire_timedelta()
        refreshable = Q(
            refresh_token__isnull=False, refresh_token__revoked__isnull=True
        )
        if refresh_lifetime:
            refreshable &= Q(expires__gt=now - refresh_lifetime)
        live_access |= refreshable

        return self.annotate(
            token_scopes=joined_scopes(
                get_access_token_model().objects.filter(live_access)
            ),
            grant_scopes=joined_scopes(
                get_grant_model().objects.filter(expires__gt=now)
            ),
        ).filter(Q(token_scopes__isnull=False) | Q(grant_scopes__isnull=False))

    def ended(self):
        return self.exclude(pk__in=self.live().values("pk"))


class ConnectedApp(UuidMixin, TimeStampedModel):
    """An OAuth client a user has authorised, with what DOT doesn't record.

    ``created`` is when the user first authorised it. ``last_used_at`` is for
    the MCP token verifier to set once it accepts OAuth tokens.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    application = models.ForeignKey(
        settings.OAUTH2_PROVIDER_APPLICATION_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
    )
    last_used_at = models.DateTimeField(null=True, blank=True)

    objects = ConnectedAppQuerySet.as_manager()

    class Meta:
        ordering = ["-created", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "application"],
                name="mcp_connected_app_unique_user_application",
            )
        ]

    def __str__(self):
        return f"{self.user} - {self.application}"

    def granted_scopes(self) -> list[str]:
        """Its scopes, on a connection read through ConnectedApp.objects.live()."""
        joined = " ".join(filter(None, [self.token_scopes, self.grant_scopes]))
        return sorted(set(joined.split()))

    def revoke(self) -> bool:
        """Delete the user's codes and tokens for the client, and the connection.

        Returns False if a concurrent revoke deleted the connection first.
        """
        tokens = {"user_id": self.user_id, "application_id": self.application_id}
        RefreshToken = get_refresh_token_model()
        with transaction.atomic():
            if not ConnectedApp.objects.select_for_update().filter(pk=self.pk).exists():
                return False
            # Codes first: a code exchange holds its code's lock until its
            # tokens are saved, so they are visible to the deletes below.
            get_grant_model().objects.filter(**tokens).delete()
            # Lock them all before deleting any: deleting next to a refresh that
            # holds one can deadlock. A refresh in flight holds its token's lock
            # until its successor is saved, so look again until none is new.
            locked = set()
            while True:
                found = set(
                    RefreshToken.objects.select_for_update()
                    .filter(**tokens)
                    .values_list("pk", flat=True)
                )
                if found <= locked:
                    break
                locked |= found
            # Deleted, not marked revoked: DOT re-reads a refresh token under
            # lock but still mints a successor for one revoked meanwhile.
            # This deletes the rotated ones REFRESH_TOKEN_REUSE_PROTECTION keeps
            # too, so replaying one later is "not found" rather than a family
            # revocation; there is nothing left to revoke by then.
            RefreshToken.objects.filter(**tokens).delete()
            get_access_token_model().objects.filter(**tokens).delete()
            self.delete()
        return True
