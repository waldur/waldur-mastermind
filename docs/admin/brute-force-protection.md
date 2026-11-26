# Brute-Force Protection for Password Sign-In

Waldur slows down password guessing in two ways:

- it locks an **account** after a few failed passwords;
- it limits the failed passwords one **client address** may send, across all
  accounts, and refuses an address that exceeds the limit for a short
  cool-off.

One user's failures do not lock another user's account. The users behind one
address, such as a campus network or a company proxy, share only the address
limit, which is set high enough that their typos do not reach it.

Both apply to:

- password sign-in (`/api-auth/password/`);
- re-entering the password to confirm one's identity before a sensitive change
  (`/api/reauthentication/password/`);
- the Django admin login.

## Account lockout

After **5** failed passwords for one username, from any addresses, that username
is locked for **10 minutes**. Waldur uses django-axes for this, and the settings
are in `waldur_core/server/base_settings.py`:

| Setting | Value | Meaning |
|---|---|---|
| `AXES_LOCKOUT_PARAMETERS` | `["username"]` | Lock the username only, never the address |
| `AXES_FAILURE_LIMIT` | `5` | Failures before the lock |
| `AXES_COOLOFF_TIME` | 10 minutes | How long the lock lasts |

Password sign-in and re-authentication also keep a counter of their own, per
username and connection address. After **4** failures, sign-in answers
"Username is locked out. Try in 10 minutes." The admin login does not use this
counter, only the axes lockout.

Locked accounts are recorded as `user_blocked` events.

## Failed passwords per address

Failed passwords are also counted per client address, across all usernames. An
IPv6 address counts as its whole /64, which a single client usually holds.

When an address reaches the limit within the window, it is refused for the
cool-off, whichever account it tries, and is then counted afresh:

- the API answers with **HTTP 429**, a `Retry-After` header and a body
  `{"detail": "...", "retry_after": <seconds>}`;
- the admin login answers with a plain 429 page and a `Retry-After` header.

Successful sign-ins do not count, so the users behind a busy address never add
up to a refusal. Other addresses are not affected.

The limit is a runtime setting, under **Authentication settings**:

| Setting | Default | Meaning |
|---|---|---|
| `PASSWORD_FAILURES_PER_ADDRESS_LIMIT` | `50` | Failed passwords allowed from one address within the window; `0` turns the limit off |
| `PASSWORD_FAILURES_PER_ADDRESS_WINDOW_MINUTES` | `60` | The window, in minutes from the address's first failure |
| `PASSWORD_FAILURES_PER_ADDRESS_COOLOFF_MINUTES` | `10` | How long an address that reached the limit is refused |

## The client address behind proxies

Behind an ingress or a reverse proxy, every connection comes from the proxy.
Waldur takes the client address from the `X-Forwarded-For` header instead, and
only from the entries its own proxies added. A client can put any text at the
start of that header, and many proxies keep it and append the real address
after it.

One setting and one resolver decide the client address for everything that
uses it, not only for brute-force protection:

- organization access subnets, and the resources, orders and role grants they
  hide;
- offering subnet concealment;
- personal access token network ACLs, on the REST API and the MCP server
  (which joins repeated `X-Forwarded-For` header lines, as the WSGI server
  does);
- the limit on failed passwords per address, the per-username lockout and the
  axes records, and the per-address limits of the MCP OAuth registration,
  the Matrix call token API and the anonymous chat;
- the addresses recorded in events, the user data access log, event
  subscriptions, passkey use and `/api/users/me/`.

A network restriction fails closed: a request whose address cannot be
determined is treated as outside every allowed network, even when the proxy's
own address is inside it. Staff and
support are not restricted by access subnets or concealment.

`WALDUR_CORE["TRUSTED_PROXY_COUNT"]` says how many proxies in front of Waldur
add an entry. The client is the entry that many places from the right, as for
`NUM_PROXIES` in Django REST framework. Without the header, the connection's
own address is the client. With fewer entries, or an entry that is not an
address, the request has no client address: network restrictions refuse it,
the audit trail records none, and per-address limits count it under the
connection's address. Waldur logs a warning (at most once a minute) when that
happens; it usually means the count is wrong. The default is `1`.

| Deployment | `TRUSTED_PROXY_COUNT` |
|---|---|
| One ingress or reverse proxy that replaces the header with the client's address, such as ingress-nginx with its defaults | `1` |
| One that appends the client's address, such as HAProxy with `option forwardfor`, the OpenShift router, Envoy for the Gateway API, or ingress-nginx with `compute-full-forwarded-for` | `1` |
| Traefik, or Docker Compose with Caddy | `1` |
| A load balancer in front of an ingress that appends, both adding an entry | `2` |
| A load balancer in front of an ingress that replaces the header | `1`, with the ingress told to trust the load balancer (below) |
| Waldur reached directly, without a proxy | `0` |

ingress-nginx with `use-forwarded-headers` does not append: it replaces the
header with the address its real-IP module finds. That module trusts every
sender unless `proxy-real-ip-cidr` names the load balancer's range, and then
takes the client's own first entry, so a client can choose its address again.
Set `proxy-real-ip-cidr` to the load balancer's addresses whenever
`use-forwarded-headers` is on. Traefik and Caddy likewise take forwarded
headers only from their trusted proxies (`forwardedHeaders.trustedIPs`,
`trusted_proxies`).

The proxy closest to the client must set the header to the real client
address, or append it. If the count is too high, a client can choose its
address: it can satisfy an access subnet or a token's network ACL, and write
any address into the audit trail. If the count is too low, every user is seen
under a proxy's address: access subnets then admit or refuse everybody alike,
and everybody shares one failed-password limit. After changing the count, check
that `/api/users/me/` reports your own address in `ip_address`.

The ingress must see the real client address in the first place. On
Kubernetes, a `LoadBalancer` or `NodePort` Service with the default
`externalTrafficPolicy: Cluster` replaces it with a node's address, and so does
a layer-4 load balancer without the PROXY protocol. Every user is then counted
under a few addresses: 50 failed sign-ins by anyone refuse password sign-in and
password re-authentication for everybody for the cool-off. Use
`externalTrafficPolicy: Local`, or the PROXY protocol between the load balancer
and the ingress, and check that the ingress logs real client addresses.

Axes records the same address (`AXES_CLIENT_IP_CALLABLE`). When the client
address cannot be resolved, the lockout records (axes access attempts and logs,
and the `user_blocked` event axes triggers) keep the connection's own address,
the same one the limits count the request under.
