# Single sign-on for Matrix clients

With `MATRIX_EXTERNAL_LOGIN_METHOD = oidc`, users who want a full Matrix client such as Element sign in to
the homeserver through the same identity provider (IdP) they use for Waldur. They land in the Matrix
account Waldur provisioned for them, with the project rooms Waldur manages, and Waldur hands out no
password or token. Waldur's own chat drawer is unaffected: it keeps using short-lived web sessions.

This page covers Tuwunel, the homeserver the Helm chart and docker-compose ship.

## How the accounts line up

Three settings must produce the same name, or the user's SSO login is refused instead of reaching the
account Waldur provisioned:

1. **Waldur's identity provider** turns an IdP claim into the Waldur username: `user_claim` (default `sub`)
   is stored in `user_field`, which must be `username` (with any other field, new users get a random
   username).
2. **`MATRIX_USER_ID_FORMAT = username`** makes the Waldur username the Matrix localpart. Waldur lowercases
   it and replaces characters a localpart may not hold.
3. **The homeserver's identity provider** turns the same claim into the localpart (`userid_claims`).
   Tuwunel 1.9.0 reads only `sub`, `preferred_username`, `username`, `nickname`, the local part of
   `email`, and `login` (GitHub). Other claims, such as eduTEAMS' `voperson_id`, cannot be used.

The claim must already be a valid Matrix localpart: lowercase letters after folding, digits and
`. _ - / +`. Tuwunel uses a valid claim as it is (lowercased) and refuses anything else, so a username
with a space, `@`, `:` or a non-ASCII letter can never reach the same account over SSO. Waldur escapes
`=` as `=3d`, as it is the escape character for non-ASCII letters, so a username with `=` does not reach
the same account either:

| IdP subject | Waldur provisions | Tuwunel SSO signs in to |
|---|---|---|
| `alice` | `@alice` | `@alice` |
| `Alice.Smith` | `@alice.smith` | `@alice.smith` |
| `alice+lab` | `@alice+lab` | `@alice+lab` |
| `alice smith`, `alice@example.org`, `jüri` | `@alice_smith`, `@alice_example.org`, `@j=c3=bcri` | refused |
| `a=b` | `@a=3db` | `@a=b`, a different account |

Every refused row is also a takeover risk. Tuwunel takes a claim as the localpart as it is, and `trusted`
signs in to any account so named, so `@alice_smith` is the account of whichever IdP subject's claim reads
`alice_smith`, not of the user `alice smith`. With `MATRIX_EXTERNAL_LOGIN_METHOD = oidc` Waldur therefore
refuses to provision a user whose username does not become their Matrix ID unchanged (apart from
lowercasing), and their chat answers "Chat is unavailable right now". Accounts provisioned before
switching to `oidc` are not refused, so their chat keeps working; the `sso_id_collisions` check of the
**Diagnostics** action under **Administration → Configuration → Matrix chat** lists them. Rename or
relink those users. `waldur link_matrix_account` warns when it links such an ID. The IdP should also
refuse claims that are not valid localparts, so that no such user exists in the first place.

Waldur refuses two more kinds of user under `oidc`, and the same check lists their existing accounts:

- **Users whose username another user's differs from only in case.** Tuwunel lowercases the claim, so the
  subjects `Alice` and `alice` both sign in to `@alice`. A non-ASCII username that lowercases to ASCII
  counts too: the Kelvin sign (U+212A) becomes `k`, so its holder would reach `@kate`. Deactivated
  users count, since deactivating them in Waldur does not remove them from the IdP. Rename or remove one
  of the users.
- **Users who do not sign in to Waldur through the homeserver's IdP.** A local, SAML or other-IdP user
  `bob` holds `@bob`, and any subject of the homeserver's IdP whose claim reads `bob` would sign in to that
  account. Waldur provisions only users whose registration method is `MATRIX_SSO_REGISTRATION_METHOD`
  (see [Waldur configuration](#waldur-configuration)), and none while it is blank.

Accounts also fail to line up when a user's Matrix ID no longer follows from their username, because
Waldur keeps the ID it derived when it first provisioned them. That covers users renamed afterwards,
including to the claim by `OIDC_MATCHMAKING_BY_EMAIL`, users provisioned while `MATRIX_USER_ID_FORMAT` was
`uuid` or `email_local`, `+` usernames provisioned before Waldur kept `+` (they still have `_`), and `=`
usernames provisioned before Waldur escaped `=` (they still have `=` where it now derives `=3d`).
After a database reset, `waldur link_matrix_account --all` looks those `+` and `=` users up under the
ID derived now and misses them, and their next chat creates a new, empty account. Link each of them by
hand before they next open chat: `waldur link_matrix_account <username> <their old matrix id>`.

Waldur provisions a user's Matrix account when they open its chat or are added to a project room. Tuwunel
also remembers the account an IdP identity first signed in to, so a stray account created by an earlier
test login keeps winning. Waldur also refuses to provision a user whose Matrix ID such a stray account
holds, and their chat answers "Chat is unavailable right now". If the account is theirs, link it
deliberately with `waldur link_matrix_account <username> <matrix id>`. Deactivating it on the homeserver
does not free the ID, and SSO reaches no other account for them; see
[Existing Matrix accounts](matrix-appservice-setup.md#existing-matrix-accounts).

**Choose a claim users cannot set themselves.** Keycloak's `sub` is a UUID: it becomes both the Waldur
username and the Matrix ID, which reads poorly but cannot be chosen. `preferred_username` reads better,
but is safe only if users cannot pick or change their username at the IdP, through self-registration or an
"edit username" setting. See `trusted` below for why.

## Homeserver configuration

Register a client for the homeserver at the IdP with the redirect URI
`https://<homeserver>/_matrix/client/unstable/login/sso/callback/<client_id>`, then add to `tuwunel.toml`:

```toml
[global]
# Element and other clients sign in through SSO only. Waldur's drawer is not
# affected: it signs users in through the appservice, not with a password.
login_with_password = false

# A trusted provider can sign in to any account whose name matches the claim;
# these never match. Add any other homeserver admin's username too.
forbidden_usernames = ["^waldur-bot$", "^waldur-bootstrap$"]

[[global.identity_provider]]
brand = "keycloak"
name = "Waldur SSO"
client_id = "<client_id>"
client_secret_file = "/etc/tuwunel/.client_secret"
issuer_url = "https://idp.example.org/realms/waldur"
callback_url = "https://<homeserver>/_matrix/client/unstable/login/sso/callback/<client_id>"
userid_claims = ["sub"]
trusted = true
unique_id_fallbacks = false
registration = false
```

What each choice does:

- **`trusted = true`** signs the user in to **any** existing local account whose name matches the claim,
  not only the ones Waldur provisioned. Without it, Tuwunel refuses accounts it did not create through SSO,
  which includes every account Waldur provisioned. Set it only for an IdP the operator controls, with a
  claim users cannot choose. Names also match by accident: a Waldur user who signs in locally or through
  another IdP as `bob` holds `@bob`, and an IdP user whose claim reads `bob` signs in to that account; the
  same goes for `alice smith`, who holds `@alice_smith`. Waldur therefore gives Matrix accounts under
  `oidc` only to users who sign in to Waldur through this IdP and whose username is their Matrix ID
  unchanged (see [How the accounts line up](#how-the-accounts-line-up)), but accounts created on the
  homeserver by other means stay exposed.
- **`forbidden_usernames`** keeps SSO out of the accounts that matter most. Waldur's bot holds the highest
  power level in every room it manages, so an IdP user whose claim reads `waldur-bot` would otherwise take
  it over. The same goes for homeserver admins. Use the bot's localpart
  (`MATRIX_APPSERVICE_SENDER_LOCALPART`), `waldur-bootstrap` (a name reserved for the bootstrap admin
  that automatic registration will create) and any other admin's username, anchored as shown. When the
  Helm chart or docker-compose configures single sign-on, it forbids the bot and `waldur-bootstrap` for
  you. Tuwunel creates the bot when the appservice is registered, and a forbidden name can still be
  created that way or through the shared-secret registration API; SSO cannot sign in to it.
- **`registration = false`**: SSO creates no accounts, it only signs in to existing ones. A user Waldur
  has not provisioned yet is refused until it has.
- **`unique_id_fallbacks = false`** refuses a claim that cannot become the localpart instead of inventing a
  random ID.
- **`userid_claims`** must name the claim Waldur's identity provider uses as `user_claim`, and be one of
  those Tuwunel reads (above). Name exactly one: Tuwunel tries the listed claims in its own order and
  moves on to the next when one is not a valid localpart or is forbidden, and with `trusted` the next one
  can name someone else's account.
- **`brand`** names the IdP software (`keycloak`); Tuwunel applies provider-specific workarounds by it.
  `name` is the label on the login button.
- **`login_with_password = false`** removes the password form from clients. An admin created with a
  password then cannot sign in to a client either; keep it `true` while you still need to. That includes
  making the bot a homeserver admin, which locking deactivated users depends on (see
  [Making the bot a homeserver admin](matrix-appservice-setup.md#making-the-bot-a-homeserver-admin)), and
  registering the appservice again after a Setup rotates its tokens. Keep an admin signed in to a client
  for those, or turn password login on while you do them. It is also
  **untested** with the drawer's encryption reset: the drawer answers the homeserver's interactive
  authentication with a temporary password that Waldur sets through the admin API, and the homeserver
  may refuse password authentication altogether once it is off. Reset encryption for a test user from
  the drawer with this setting before enabling it for everyone.

Tuwunel's SSO cookie is `Secure`, so the homeserver must be served over HTTPS; plain HTTP only works on
`localhost`, which browsers treat as secure.

## Waldur configuration

- Set `MATRIX_EXTERNAL_LOGIN_METHOD` to `oidc`. The external client dialog then shows the room and the
  homeserver and tells users to sign in with single sign-on; it shows no password and no Matrix ID.
- Configure Waldur's identity provider against the same IdP, with `user_claim` matching the homeserver's
  `userid_claims`.
- Set `MATRIX_SSO_REGISTRATION_METHOD` to the registration method Waldur records for that identity
  provider's users, which is its name in Waldur (`provider`, such as `keycloak`; `eduteams` for eduTEAMS),
  or `OIDC_REGISTRATION_METHOD` for users who sign in with OIDC bearer tokens. Only those users get a
  Matrix account. While it is blank, no user does, and the `sso_id_collisions` diagnostic fails.

## Checking it

After a user has opened Waldur's chat once:

1. Sign in to Element with the homeserver URL and choose the SSO button.
2. Element shows the user's Waldur Matrix ID and their project rooms. Messages show as encrypted and
   unreadable until the Element session is verified (see [Limits](#limits)).
3. The user's device list on the homeserver shows Waldur's `WALDUR_WEB_*` devices and the Element device,
   and no second account exists for them.

## Limits

- Chat is end-to-end encrypted. Waldur's drawer and bot share message keys only with devices that their
  owner's encryption identity has signed, and the drawer does not decrypt messages from any other device.
  Until Element's session is verified, Element cannot read the drawer's or the bot's messages, and the
  drawer cannot read Element's. The drawer cannot verify other devices, so verifying takes the user's
  recovery key, which Waldur's external client dialog shows them (see
  [Showing the recovery key](matrix-appservice-setup.md#showing-the-recovery-key)). The user guide's
  "Chat encryption" page (under end users) tells users what the key unlocks and what is encrypted.
- Element may offer to reset the encryption identity instead. Whether the homeserver lets it depends on
  how Element signed in. Tuwunel (since 1.6.0, so 1.9.x included) runs its own OAuth server whenever an
  identity provider and `well_known.client` are configured, and Element versions with next-generation
  authentication (MSC2965) sign in through it. For such a session Tuwunel allows the reset once the user
  signs in at the IdP again (MSC4312). A session that signed in through the older SSO redirect
  (`m.login.sso`) has to answer with the account's password, which SSO users do not have, so it cannot
  reset. (Tuwunel offers single sign-on for that prompt only to accounts it registered itself at an SSO
  login with a single identity provider; the accounts Waldur provisions are not among them.) Either way, the recovery key Waldur shows is how to open the user's history in Element; tell
  users not to reset elsewhere unless every key is lost. A reset replaces the identity whose recovery key
  Waldur holds and deletes the user's key backup. Element then shows a new recovery key, and the drawer
  asks for it once on its next session (see
  [Locked identities](matrix-appservice-setup.md#locked-identities)); until the user enters it, chat in
  Waldur stays locked.
- Disabling a user at the IdP stops new SSO logins only. An Element session that is already signed in
  stays valid until it is signed out; deactivate the user in Waldur to end it.
- Deactivating or deleting a user in Waldur signs out every device, Element included, and locks their
  Matrix account, which refuses new SSO logins too. Locking needs the bot to be a homeserver admin (see
  [Making the bot a homeserver admin](matrix-appservice-setup.md#making-the-bot-a-homeserver-admin)).
  Until it is, the account stays unlocked and the user can still sign in over SSO, to an account without
  Waldur's project rooms, so disable them at the IdP as well. See
  [Automatic member management](matrix-appservice-setup.md#automatic-member-management).
