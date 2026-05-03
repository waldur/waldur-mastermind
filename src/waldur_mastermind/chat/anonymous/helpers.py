"""Pseudonymisation (``compute_user_slug``), HMAC feedback token, and history-replay helpers for the anonymous chat endpoint.

Slug + HMAC fail safe — missing secret or input returns empty/False rather than a usable but insecure result.
"""

import hashlib
import hmac
import logging
from types import SimpleNamespace

from constance import config
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from django.contrib.auth.models import AnonymousUser
from django.db.models import Q

from waldur_mastermind.chat.anonymous import models as anonymous_models
from waldur_mastermind.chat.context_assembler import (
    EXCLUDED_SEVERITIES,
    blocks_to_llm_messages,
)
from waldur_mastermind.chat.tools.marketplace.helpers import offerings_queryset_for
from waldur_mastermind.marketplace import models as marketplace_models

logger = logging.getLogger(__name__)


def compute_user_slug(ip_address: str) -> str:
    """Empty string when salt is missing — a salt-less hash is reversible across the entire IPv4 space."""
    if not ip_address:
        return ""

    salt = (config.ANONYMOUS_CHAT_USER_SLUG_SALT or "").strip()
    if not salt:
        return ""

    kdf = Scrypt(
        salt=salt.encode("utf-8"),
        length=32,
        n=2
        ** 14,  # ~50ms/derivation — high enough to prevent bulk reversal of the IPv4 space
        r=8,
        p=1,
    )
    return kdf.derive(ip_address.encode("utf-8")).hex()


def _token_message(interaction_uuid: str, session_id: str, ip_address: str) -> bytes:
    """Newline separator — can't appear in UUIDs, session IDs, or IP literals, so no length-extension ambiguity."""
    return f"{interaction_uuid}\n{session_id}\n{ip_address}".encode()


def compute_feedback_token(
    interaction_uuid: str, session_id: str, ip_address: str
) -> str:
    """HMAC-SHA256(secret, uuid + session + ip). Empty string when any input or the configured secret is missing."""
    if not (interaction_uuid and session_id and ip_address):
        return ""
    secret = (config.ANONYMOUS_CHAT_FEEDBACK_TOKEN_SECRET or "").strip()
    if not secret:
        return ""
    return hmac.new(
        key=secret.encode("utf-8"),
        msg=_token_message(interaction_uuid, session_id, ip_address),
        digestmod=hashlib.sha256,
    ).hexdigest()


def verify_feedback_token(
    expected: str,
    interaction_uuid: str,
    session_id: str,
    ip_address: str,
) -> bool:
    """Constant-time comparison. Returns False when any input is missing — no configured secret means all feedback POSTs get 403."""
    if not expected:
        return False
    actual = compute_feedback_token(interaction_uuid, session_id, ip_address)
    if not actual:
        return False
    return hmac.compare_digest(expected, actual)


def build_domain_context() -> str:
    """Dynamic context injected into the persona so the assistant adapts to any deployment's marketplace shape."""
    site_description = (config.SITE_DESCRIPTION or "").strip()
    visible_offerings = offerings_queryset_for(AnonymousUser())
    category_titles = list(
        marketplace_models.Category.objects.filter(offerings__in=visible_offerings)
        .order_by("title")
        .values_list("title", flat=True)
        .distinct()
    )
    description_part = site_description or "the marketplace catalog"
    if category_titles:
        return (
            f"You help users discover {description_part}. "
            f"Available service categories include: {', '.join(category_titles)}."
        )
    return f"You help users discover {description_part}."


_FORMAT_HINT_WITHOUT_COUNTRY = """\
  **Offering Name** (Provider)
  Why it matches: [specific reason tied to the user's stated need]
  Key details: [what matters for the user's stated need]
  Access: [link to offering page or next step]"""

_FORMAT_HINT_WITH_COUNTRY = """\
  **Offering Name** (Provider, Country)
  Why it matches: [specific reason tied to the user's stated need]
  Key details: [what matters for the user's stated need]
  Access: [link to offering page or next step]"""


def build_offering_format_hint() -> str:
    """Recommendation format the persona instructs the LLM to use.

    Includes "Country" only when the visible catalog actually spans
    ≥2 distinct customer countries — otherwise it's noise (every line
    repeats the same country). HPC-Euro deployments with offerings
    from 20+ NCCs get the country line; a single-country government
    cloud deployment doesn't.
    """
    try:
        visible = offerings_queryset_for(AnonymousUser())
        country_count = (
            marketplace_models.Offering.objects.filter(pk__in=visible)
            .exclude(customer__country="")
            .values("customer__country")
            .distinct()
            .count()
        )
    except Exception:
        # Schema-generation / no-DB: default to without-country (smaller prompt).
        country_count = 0
    return (
        _FORMAT_HINT_WITH_COUNTRY
        if country_count >= 2
        else _FORMAT_HINT_WITHOUT_COUNTRY
    )


def _interaction_to_llm_messages(interaction) -> list[dict]:
    """Anon stores one DB row per turn (user input + assistant blocks). Auth's ``blocks_to_llm_messages``
    expects a row-per-message shape, so wrap the assistant half in a duck-typed shim.
    """
    out: list[dict] = []
    if interaction.user_input:
        out.append({"role": "user", "content": interaction.user_input})
    blocks = interaction.assistant_blocks or []
    if blocks:
        shim = SimpleNamespace(role="assistant", blocks=blocks, uuid=interaction.uuid)
        out.extend(blocks_to_llm_messages(shim))
    return out


def build_session_history(session_id: str) -> list[dict]:
    """Replays the most recent turns of this session, dropping confirmed-injection turns.

    Filter mirrors auth ``_get_thread_messages``: PII-redacted turns survive (their persisted
    text is already safe), MEDIUM+ injection turns are excluded.
    """
    limit = config.AI_ASSISTANT_HISTORY_LIMIT
    if not isinstance(limit, int) or limit <= 0:
        return []

    injection_exclusion = Q(
        severity__in=EXCLUDED_SEVERITIES,
        injection_categories__gt=[],
    )
    recent = list(
        anonymous_models.AnonymousChatInteraction.objects.filter(session_id=session_id)
        .exclude(injection_exclusion)
        .order_by("-created")[:limit]
    )
    recent.reverse()

    out: list[dict] = []
    for interaction in recent:
        out.extend(_interaction_to_llm_messages(interaction))
    return out
