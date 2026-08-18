"""Classification of sensitive ``Offering.secret_options`` keys and the model field.

``secret_options`` is a per-plugin JSON blob: some keys hold credentials that must
be encrypted at rest, while others (e.g. ``customer_uuid``) are queried by value and
must stay plaintext. Sensitivity is decided by **key name alone**, not the offering
type: encryption (``pre_save``) and decryption (``from_db_value``) must use the same
key set, and the read path cannot see the offering type — a type-specific rule could
not be applied symmetrically, which would reopen a decryption oracle.

Kept free of any ``models`` import so ``models.py`` can import the field without a
circular import.
"""

from waldur_core.core.encryption import is_credential_key
from waldur_core.core.fields import EncryptedJSONField

# Credential keys that the shared ``is_credential_key`` rule does not match.
# ``argocd_k8s_kubeconfig`` is a Rancher option, but it is classified by name (not
# offering type) so encrypt and decrypt agree; encrypting it on a non-Rancher offering,
# where it does not occur, is harmless. Non-secret metadata — endpoint URLs, usernames,
# public certificates — stays plaintext: it grants no access and keeps the column
# observable for support and for the ``customer_uuid`` value lookup.
#
# Coverage of this classification against every key the secret-options serializers
# declare is asserted by SecretOptionsClassificationDriftTest, so a newly added
# credential option cannot reach the column in cleartext unnoticed.
_EXTRA_SENSITIVE_KEYS = {"argocd_k8s_kubeconfig"}


def is_sensitive_key(key: str) -> bool:
    """Whether a ``secret_options`` value under this key must be encrypted at rest."""
    return is_credential_key(key) or key in _EXTRA_SENSITIVE_KEYS


class SecretOptionsField(EncryptedJSONField):
    """``Offering.secret_options``: encrypts the sensitive-named keys."""

    def _is_sensitive_key(self, key) -> bool:
        return is_sensitive_key(key)
