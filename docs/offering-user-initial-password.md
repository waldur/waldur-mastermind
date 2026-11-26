# Offering user initial password

Some backends create a person's account with a password of their own: the
LiteLLM plugin of the site agent, under `managed_password`, creates each
member's Open WebUI chat account with a password derived for that person.
Waldur keeps that password on the member's offering user, so the member reveals
it in Waldur instead of the operator sending it by email or ticket.

It is the **initial** password. Waldur never learns about a change made at the
backend, so once the member changes it there, the stored value no longer works.

## Storing it

The site agent stores it with
`POST /api/marketplace-offering-users/<uuid>/set_initial_password/`
(`{"password": "...", "username": "..."}`), which replaces any value stored
before. The caller needs `UPDATE_OFFERING_USER` on the offering or its customer,
the same check as `set_ok`. The agent lists the members still waiting for one
with `?has_initial_password=false` and stores a password only for OK offering
users.

`username` is optional: the offering user's username the password was created
for. When it is given and the offering user no longer has it (the username
changed between the agent's listing and its store), the store is refused with
`409`, and the agent stores the new account's password on a later pass. A store
for a `DELETED` offering user is refused with `409` too.

The value is encrypted at rest (see [Field encryption](field-encryption.md)). The
offering user exposes only `has_initial_password`, never the value. The Django
admin does not show the field.

## Clearing it

The provider removes a stored value, for example a wrong one, with
`POST /api/marketplace-offering-users/<uuid>/clear_initial_password/`. It needs
the same permission as storing one, succeeds when nothing is stored, and logs
`marketplace_offering_user_initial_password_cleared` when it removed a value.
If the agent still holds the account it created, it stores the password again on
its next pass.

Waldur also wipes the value by itself, without an event, when it no longer opens
the account it was created for:

- **The offering user reaches `DELETED`.** `DELETION_REQUESTED` and `DELETING`
  keep it, because both can still go back to OK. An offering user restored from
  `DELETED` has none until the agent stores one again.
- **The username changes** from one value to another, for example after an
  address change gave the member a new account, or is removed. A first username
  wipes nothing.
  The agent stores the new account's password on its next pass.

## Revealing it

`GET /api/marketplace-offering-users/<uuid>/reveal_initial_password/` returns
`{"password": "..."}` with `Cache-Control: no-store`, and logs
`marketplace_offering_user_initial_password_revealed`. It answers `409` when
nothing is stored or the value cannot be decrypted with the configured keys.

Only the offering user's own user reveals it, signed in as themselves:

| Caller | Result |
| --- | --- |
| The member, with their API token or session | `200` |
| The member, with an OIDC access token | `200` |
| The member, with a personal access token | `403`, whatever its scopes |
| Staff impersonating the member | `403` |
| Anyone else who can see the offering user: service provider, staff, support | `403` |
| Anyone who cannot see it | `404` |

- **Personal access tokens** are a script's credential and are not trusted with
  a live secret, as with an [API key's reveal](resource-api-keys.md).
- **Impersonation** is refused because the password is the member's own login at
  the backend. Whatever is done with it there is attributed to the member, and
  the backend keeps no record of the impersonator. The Matrix recovery key takes
  the same line. Staff who need to help a member ask the provider to reset the
  account instead.
- **OIDC access tokens** are admitted, as for an API key's reveal, because
  HomePort may sign in with one (`OIDC_ACCESS_TOKEN_ENABLED`). Such a token may
  also belong to another client of the identity provider, so a client trusted
  with the member's OIDC token can reveal the password. This is a deliberate
  choice, kept consistent with API keys.

The reveal does not yet ask for step-up re-authentication, so a stolen session
or API token is enough to read it. The API key reveal has the same gap; both are
tracked in waldur/waldur-mastermind#1296.
