# Field encryption at rest

Waldur encrypts sensitive credential columns at rest with application-level
[Fernet](https://cryptography.io/en/latest/fernet/) (`waldur_core.core.encryption`).
The goal is defense in depth: a database dump, a stray `SELECT`, a SQL-injection
read or a leaked backup yields opaque tokens rather than plaintext secrets. It is
**not** a secrets manager — an attacker who compromises the application process can
still decrypt, because the key lives in process memory.

This page covers what is encrypted, how the key is configured and rotated, and the
accepted limitations. For the resource API key lifecycle specifically, see
[Resource API Keys](resource-api-keys.md#encryption-at-rest).

## What is encrypted

| Model / field | Scope | Notes |
| --- | --- | --- |
| `marketplace.Offering.secret_options` | **selective** — only sensitive values in the JSON | keys and non-sensitive values stay plaintext |
| `structure.ServiceSettings.password` | whole value | |
| `structure.ServiceSettings.token` | whole value | |
| `structure.ServiceSettings.options` | **selective** — only credential-named values in the JSON | `client_secret`, `keycloak_password`, `vault_token`, … |
| `marketplace.ResourceApiKey.key_ciphertext` | whole value | see [Resource API Keys](resource-api-keys.md#encryption-at-rest) |

Encryption is transparent: values are encrypted at the database-serialization
boundary (`pre_save`) and decrypted on read (`from_db_value`). The in-memory model
attribute always holds plaintext, so `FieldTracker` compares plaintext and does not
report a phantom change (handlers gated on a change do not fire on an unchanged
save). Because a Fernet token uses a random IV, the same plaintext encrypts to a
different token every time; encrypted values therefore **cannot be queried by
value** (`filter(token=...)` will not match) — this is fine, as none of the
encrypted fields is queried by value.

## Which JSON values are encrypted

`Offering.secret_options` and `ServiceSettings.options` are both per-plugin JSON blobs
that mix credentials with ordinary configuration — endpoints, tenant ids, usernames,
tuning flags — and `secret_options.customer_uuid` is even queried by value. Encrypting
the whole blob would break those lookups and make the column unreadable for support, so
only the credential values are encrypted, key by key.

A value is encrypted when its key is:

- the exact keys `password`, `token` or `secret`, or any key suffixed `_password`,
  `_token` or `_secret` — `vault_token`, `keycloak_password`, `heappe_password`,
  `shared_user_password`, `client_secret`. A new key with one of those suffixes, from
  any plugin, is covered automatically the day it is added. This shared rule lives in
  `waldur_core.core.encryption` (`is_credential_key`).
- for `secret_options` only, a small set of named exceptions carrying no such suffix —
  currently only `argocd_k8s_kubeconfig` (a Rancher kubeconfig that embeds cluster
  credentials), listed in `waldur_mastermind.marketplace.secret_options`.

Sensitivity is decided by **key name alone**, not the offering type: encryption and
decryption must agree on the key set, and the read path (`from_db_value`) cannot see
the offering type. Encrypting `argocd_k8s_kubeconfig` on a non-Rancher offering, where
it does not occur, is harmless.

Everything else stays plaintext, including endpoint URLs (`api_url`, `backend_url`,
`vault_host`, …), usernames, identifiers (`client_id`, `tenant_id`,
`subscription_id`), the public OpenStack TLS certificate
(`openstack_api_tls_certificate`), and Script hooks.

**Adding a credential option.** Name it `*_password`, `*_token` or `*_secret` and it is
encrypted with no further work. If it must carry another name, add it to
`_EXTRA_SENSITIVE_KEYS`. `SecretOptionsClassificationDriftTest` fails the build on any
key declared by a secret-options serializer that is neither classified sensitive nor
listed there as knowingly plaintext, so a new credential cannot reach the column in
cleartext unnoticed.

## Configuration

`FIELD_ENCRYPTION_KEY` is a Fernet key, deliberately a **separate setting from
`SECRET_KEY`**: leaking Django settings must not, by itself, unlock encrypted DB
fields. Generate one with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

and set it in the environment:

```bash
FIELD_ENCRYPTION_KEY=<44-char urlsafe-base64 Fernet key>
```

When no dedicated key is configured, the key is derived from `SECRET_KEY` (with a
startup warning). That derived key always remains an **implicit last-resort decrypt
fallback**, so a deployment that starts without a dedicated key can introduce
`FIELD_ENCRYPTION_KEY` later without losing rows written before the switch. Running
in production without a dedicated `FIELD_ENCRYPTION_KEY` is discouraged: it removes
the separation from `SECRET_KEY`.

See also the [settings reference](settings-policy.md).

## Key rotation

`FIELD_ENCRYPTION_KEY_FALLBACKS` (a comma-separated list) holds previous keys so the
app can be re-keyed without downtime, using `MultiFernet`: encryption always uses the
primary `FIELD_ENCRYPTION_KEY`, decryption is attempted against the primary *and*
every fallback. To rotate:

1. Generate a new Fernet key, set it as `FIELD_ENCRYPTION_KEY`, and move the previous
   key into `FIELD_ENCRYPTION_KEY_FALLBACKS`. Existing rows still decrypt (via the
   fallback); new writes use the new primary.
2. Run `waldur reencrypt_fields`, which rewrites every stored token under the new
   primary. Waiting for rows to be re-saved on their own does not work: a value is
   only rewritten when it happens to be rotated, so there is no point at which you
   could tell the old key had become unnecessary.
3. Drop the old key from `FIELD_ENCRYPTION_KEY_FALLBACKS`.

`waldur reencrypt_fields --dry-run` reports the same counts without writing, and in
particular how many rows **no** configured key can decrypt — worth checking on its
own, since such rows are invisible until something tries to read them. The command
covers every encrypted field: the scalar columns and the selectively-encrypted
values inside `secret_options`.

## Backups

The encryption key is **not** part of a database dump. Back up `FIELD_ENCRYPTION_KEY`
(and any `FIELD_ENCRYPTION_KEY_FALLBACKS`) separately and at least as carefully as the
database:

- a database backup **without** the key is unreadable for the encrypted columns;
- **losing the key loses the data** — there is no recovery path for values only that
  key could decrypt (`reencrypt_fields` reports them as undecryptable).

## Effect on offering revision history

`Offering.secret_options` is **excluded from django-reversion tracking**, and migration
`0266` removed it from the version history that already existed (it was serialised in
plaintext there, which defeated the point of encrypting the column). Two consequences
worth knowing before you use the admin history view:

- **`secret_options` is not versioned.** Offering versions no longer record it, so the
  history view cannot show what the credentials used to be, and there is no way to see
  when they changed. Use the event log for that.
- **Reverting an offering leaves the current credentials in place.** Because the field
  is absent from the stored version, a plain revert would deserialise it as the empty
  default and wipe the live credentials on save. A `pre_save` handler
  (`encrypt_secret_options_on_raw_save`) restores the current value on a raw save
  instead, so a revert changes every other field and leaves `secret_options` alone.
  A revert is therefore **not** a way to roll credentials back.

The same handler covers `loaddata`, which is also a raw save: a fixture's
`secret_options` is encrypted on the way in rather than stored verbatim.

## Known limitations and accepted risks

- **Application-server compromise is not protected.** The process holds the key and
  can decrypt. This is field encryption, not an HSM/KMS.
- **Fernet embeds a creation timestamp.** Someone with database access can tell when
  each secret was last written. Accepted — metadata only, no plaintext.
- **No per-row binding (requires database write).** Encryption and decryption use the
  same key set and encryption is unconditional, so the field cannot be turned into a
  decryption oracle through the API: a token planted under a non-sensitive key is
  returned untouched (never decrypted), and one placed under a sensitive key is
  re-wrapped rather than passed through. A Fernet token still carries no row identity,
  so someone with direct database *write* access could copy a ciphertext between the
  same field on two rows — but that already requires a full database compromise.
- **The offering export endpoint decrypts by design.** Exporting an offering with
  `include_secret_options=true` writes decrypted `secret_options` into the export
  payload, so it can be imported into another instance. This intentionally produces
  an unencrypted copy; treat such exports as secret material.
