# Waldur Permission System Guide

## Permission Factory Usage

**ALWAYS use `permission_factory` instead of manual `has_permission` checks in ViewSets.**

### For ViewSet Actions

```python
# Define permissions as class attributes
compliance_overview_permissions = [
    permission_factory(PermissionEnum.UPDATE_CALL)
]

@action(detail=True, methods=["get"])
def compliance_overview(self, request, uuid=None):
    # No manual permission check needed - handled by permission_factory
    pass
```

### Permission Factory Patterns

- **Current Object**: `permission_factory(PermissionEnum.PERMISSION_NAME)` - no path needed
- **Related Object**: `permission_factory(PermissionEnum.PERMISSION_NAME, ["customer"])` - for related objects
- **Nested Path**: `permission_factory(PermissionEnum.PERMISSION_NAME, ["project.customer"])` - for nested relationships

### For perform_create/perform_destroy Methods

```python
# Use declarative permission attributes instead of manual perform_* overrides
def check_create_permissions(request, view, obj=None):
    """Check permissions for creating reviews."""
    serializer = view.get_serializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    proposal = serializer.validated_data["proposal"]

    if not has_permission(
        request.user,
        PermissionEnum.MANAGE_PROPOSAL_REVIEW,
        proposal.round.call,
    ):
        raise exceptions.PermissionDenied()

def check_destroy_permissions(request, view, obj=None):
    """Check permissions for destroying reviews."""
    if obj and not has_permission(
        request.user,
        PermissionEnum.MANAGE_PROPOSAL_REVIEW,
        obj.proposal.round.call,
    ):
        raise exceptions.PermissionDenied()

create_permissions = [check_create_permissions]
destroy_permissions = [check_destroy_permissions]
```

### When to Use Manual Checks

- Complex permission logic that doesn't map to standard object relationships
- Custom validation that requires dynamic permission targets
- Legacy code not yet refactored to declarative patterns

## Adding New Permissions

### 1. Define the Permission Enum

Add the new permission to `PermissionEnum` in `src/waldur_core/permissions/enums.py`:

```python
class PermissionEnum(StrEnum):
    MY_NEW_PERMISSION = "RESOURCE.MY_ACTION"
```

If the permission is for managing team members (creating/updating/deleting roles) on a scope type, also add it to the `CREATE_PERMISSIONS`, `UPDATE_PERMISSIONS`, and `DELETE_PERMISSIONS` dicts in the same file.

### 2. Assign to Roles via permissions.yaml

**Do NOT use data migrations to assign permissions to roles.** Instead, add the permission to the appropriate roles in `docker/rootfs/etc/waldur/permissions.yaml`:

```yaml
- role: CUSTOMER.OWNER
  scope: customer
  permissions:
    - RESOURCE.MY_ACTION   # Add here
```

This file is loaded by the `import_roles` management command, which runs on deployment. The command creates roles and syncs their permissions from the YAML definition.

**Operator-defined custom roles** (a deployment's own role, not one shipped with Waldur) go
through a separate file, `docker/rootfs/etc/waldur/custom-roles.yaml`, mounted at
`/etc/waldur/custom-roles.yaml` — same schema, also loaded by `import_roles` on every
deployment, but empty (`[]`) by default. The `waldur-helm` chart exposes it as
`waldur.customRoles`. Don't add operator-specific roles to `permissions.yaml` — that file ships
with the image and is the same for every deployment.

**Reusing a built-in role's name here is also valid**, and is how an operator fully replaces
that role's permission set rather than adding/dropping individual permissions.
`import_roles` matches by name, so an entry for e.g. `CUSTOMER.OWNER` in `custom-roles.yaml`
overwrites its permissions with exactly the list given — the built-in `permissions.yaml` loads
first (in `initdb`), so this always wins. This is a deliberate, one-way handover: from then on
the operator owns that role's full permission set, and any permission mastermind adds to it in
a later release is loaded and then immediately overwritten again on every deployment until the
operator adds it to their own list too. Use `permissions-override.yaml`
(`add_permissions`/`drop_permissions`, below) instead when the goal is to adjust a role while
still tracking future upstream changes to it.

Two more caveats, regardless of which name is used. A role loaded from this file is created as
a **system role**, so it can no longer be renamed or deleted through the API; if a role of that
name was created by hand in the UI, it is converted to a system role and its permission set is
replaced by the file's. And there is no counterpart to `drop_stale_permissions` for roles —
removing a role from the file does **not** remove it from the database, it only stops being
managed. Deactivate roles you no longer want via `permissions-override.yaml`
(`is_active: false`) rather than by deleting the entry.

### 3. Use in ViewSets

```python
my_action_permissions = [
    permission_factory(PermissionEnum.MY_NEW_PERMISSION, ["project.customer"])
]
```

## Site-wide roles

A role is usually granted on one organization, project, offering, or call. A
site-wide role is granted once, and each permission on it applies to every
object.

Define it in `custom-roles.yaml` with `scope: global`. `permissions.yaml` ships
no global role. A deployment opts in, for example:

```yaml
- role: GLOBAL.ORDER_DESK
  description: See every order and approve or reject it
  scope: global
  permissions:
    - ORDER.LIST
    - ORDER.APPROVE
    - ORDER.REJECT
```

Staff can also create the role with `POST /api/roles/` and
`"content_type": "global"`. The role is left out of the organization role
picker (`available_for_customer`).

### Granting

Staff assign and revoke holders at `/api/global-user-roles/`. A personal access
token needs the staff scope.

- `GET` lists the active grants.
- `POST` body: `user` (uuid), `role` (uuid or name), optional `expiration_time`.
- `DELETE /api/global-user-roles/<uuid>/` revokes the grant. The row stays
  inactive for the audit log.

Adding the role through an organization's add-user action fails. The role's
scope has to match the object. The site scope is also not a target for an
invitation, a personal access token's `allowed_scopes`, or an event consumer's
bindings: roles are granted on it, but nothing is bound to it.

### What a site-wide role cannot carry

A site-wide role cannot manage an organization's or a service provider's team:
`CUSTOMER.CREATE_PERMISSION`, `CUSTOMER.UPDATE_PERMISSION` and
`CUSTOMER.DELETE_PERMISSION`. Held on every organization, they would let the
holder make themselves owner of any of them. `POST /api/roles/` and
`PUT /api/roles/<uuid>/` reject them on a global role, `import_roles` drops them
with a warning, and a global grant never satisfies them even when a role
carries one anyway. `waldur check_role_names` reports such a role as
`global-denied-permission`.

Team management below the organization stays grantable. A desk holding
`PROJECT.CREATE_PERMISSION` can invite people into any project. Toggling
`is_staff`, impersonation, site settings and deleting an organization are staff
checks rather than permissions, so no role carries them.

### What the grant does

Staff still passes before this lookup. `is_support` does not enter
`has_permission`. It stays the blanket read, and it combines with a global
write: support can see a row, and the site role supplies the action.

`has_permission` is true on every object for a permission held on the site
scope. A personal access token must list that permission. A token bound to
another organization cannot use the grant.

A list opens only for the permission that list already checks. An action
permission does not open the list. `ORDER.APPROVE` alone cannot list orders,
and `OFFERING.UPDATE` alone cannot open an offering. The holder acts on a row
they can already see, because they also hold the list permission, because they
are support, or because they have a role on that object.

| Permission on the site role | What it lists |
|---|---|
| `ORDER.LIST` | Every order |
| `PROJECT.LIST` | Every project |
| `RESOURCE.LIST` | Every resource |
| `OFFERING.LIST` | Every offering on the provider list, including drafts. The public catalog is unchanged |
| `CALL.LIST` | Every call, and the call lists support can already read, including reviews. Conflict-of-interest disclosures and reviewer bids stay staff-only |
| `ROUND.LIST` | Every round. It does not open calls or disclosures |
| `PROPOSAL.LIST` | Every proposal |
| `INVITATION.LIST` | Every invitation |
| `SERVICE_ACCOUNT.MANAGE` | Every project service account |
| `PROJECT.COURSE_ACCOUNT_MANAGE` | Every course account |

`PROJECT.CREATE` does not list organizations. Creating a project still needs an
organization the user can see. Support plus `PROJECT.CREATE` can create one in
an organization they do not belong to.

A site role is not membership. `get_connected_customers` and
`get_connected_projects` stay empty for a user whose only role is site-wide.
A site-wide `RESOURCE.SET_LIMITS` sees every limit-change request. Approving
one still needs permission to create the order.

The pending-order dashboard follows the approve rule: an `ORDER.APPROVE` grant
on the project, on its organization, or on the site. This changes the dashboard
for deployments that grant no site-wide role too: an approver whose grant is on
the organization, an organization owner for instance, now sees the orders
waiting in its projects. Before, the dashboard looked at project grants only.

### What it costs

A site-wide grant is consulted on every permission check, so its cost is what
every request pays. Until the first global role is granted, the site scope does
not exist and the checks cost nothing extra: each process notes that once a
minute. After the first grant, a user's site-wide permissions are loaded once
per request, so a request pays one query however many checks it makes. A
Celery task or a shell asks the database on each check.

The first grant on a deployment applies at once in the process that made it,
and within a minute in the other API and worker processes. Until then they
treat the grant as absent, so it never applies early. Grants and revokes after
that apply on the next request.

`OFFERING.LIST` also ships on `CUSTOMER.OWNER`, `CUSTOMER.MANAGER`, and
`OFFERING.MANAGER`. A role on the organization, project, offering, or service
provider still sees the offerings that connection already included.

Homeport's role form keeps its scope list in `ROLE_TYPES`. Add `global` there
before the administration screen can create the role. The grant screen uses
`/api/global-user-roles/`.

## Team visibility

Listing the members of a scope (`GET .../list_users/`) is gated on a
permission, not on role membership. Staff and support always pass. Otherwise
the caller needs an active role that is one of the following:

- on the organization, and grants `CUSTOMER.VIEW_TEAM`
  (`PermissionEnum.VIEW_CUSTOMER_TEAM`);
- on any project of the organization, and grants `PROJECT.VIEW_TEAM`
  (`PermissionEnum.VIEW_PROJECT_TEAM`);
- on the scope itself, when that scope is not an organization or a project
  (resource, offering, call, proposal). No permission is needed here.

Provider-side access to a resource's team (`OFFERING.UPDATE` on the offering or
its organization) is unchanged.

`permissions.yaml` grants `CUSTOMER.VIEW_TEAM` to `CUSTOMER.OWNER`,
`CUSTOMER.SUPPORT` and `CUSTOMER.READER`. It grants `PROJECT.VIEW_TEAM` to
`PROJECT.ADMIN`, `PROJECT.MANAGER` and `PROJECT.MEMBER`. A role without it,
such as a zero-permission placeholder, can no longer see who else is in the
organization. A role cloned into an organization copies its template's
permissions when it is created, and later additions to the template do not
reach it. Migration `permissions.0029_view_team_permissions` therefore adds the
view-team permission to the six system roles above **and to their existing
clones**, so organization owners on a cloned role keep the team listing after
the upgrade. It also covers stacks that never run `import_roles`.

Migration `permissions.0030_view_team_for_existing_roles` does the same for
**every other organization or project role that exists at upgrade time**, such
as roles staff created by hand, because until then any role let its holder list
the team. SRAM placeholder roles are left out: they are meant to be private.
Roles created after the upgrade get the permission only when someone grants it.

Roles defined in `custom-roles.yaml` are different: `import_roles` replaces
their whole permission set on every deployment, so the migration's addition is
dropped again. Add `CUSTOMER.VIEW_TEAM` / `PROJECT.VIEW_TEAM` to those roles'
`permissions` lists, or to `add_permissions` in `permissions-override.yaml`,
which is applied afterwards. `import_roles` warns about every organization or
project role it loads without the matching permission.

The test suite mirrors the YAML grant: an autouse fixture in the root
`conftest.py` adds the matching permission to every customer- and
project-scoped system role that `get_system_role` creates. A test that needs
the permission absent calls `role.delete_permission(...)`.

## Accounting visibility

`CUSTOMER.VIEW_ACCOUNTING` (`PermissionEnum.VIEW_CUSTOMER_ACCOUNTING`) decides
whether HomePort shows an organization's Accounting pages: invoices, payments,
payment profiles and credits. HomePort checks the permission, not the role
name, so a clone of `CUSTOMER.OWNER` with `ORDER.*` removed still sees
Accounting but cannot place orders. Staff always see it.

`permissions.yaml` grants it to `CUSTOMER.OWNER` only, the role that saw these
pages before. `import_roles` adds it to the owner role on the next deployment;
a stack that skips initdb (the local dev stack) needs `waldur import_roles`
run by hand. A clone of the owner copies its template's permissions once, so
clones made after the upgrade have it, but clones that already exist do not:
grant it to them in the role editor. Until then such a clone sees every other
owner screen but not Accounting, as before. Other roles get it only when
someone grants it, in the role editor or in `custom-roles.yaml`.

The API does not enforce the permission yet. Invoice, payment, payment profile
and credit reads are filtered by `GenericRoleFilter`, so any role on the
organization can still list them. Creating, editing and deleting a project
credit stays with owners and their clones (`is_owner`), so a role that only
views accounting sees credits read-only. The permission does widen one read:
a project credit's organization-wide figures (`customer_credit`,
`allocated_customer_credit`, `offerings`) are shown to owners and to roles
that grant `CUSTOMER.VIEW_ACCOUNTING` on the organization, and hidden from
everyone else.

## Reporting visibility

`CUSTOMER.VIEW_REPORTING` (`PermissionEnum.VIEW_CUSTOMER_REPORTING`) decides
which organizations a user sees under Reporting in HomePort. The user gets the
Reporting menu and the organization-scoped reports (Organization summary,
Quotas, Usage, Usage by user, Project detail) for every organization where one
of their roles grants it. The `customer.show_organisation_reporting` feature
must be on as well. Staff and global support users see all reports without it.

`permissions.yaml` grants it to `CUSTOMER.OWNER` and `CUSTOMER.READER`.
`import_roles` adds it to those roles on the next deployment. Clones of the
owner made after the upgrade copy it; clones that already exist need it
granted in the role editor. Other roles get it in the role editor or in
`permissions-override.yaml`.

The API does not enforce the permission. The report endpoints return what the
role can already list, so a role given `CUSTOMER.VIEW_REPORTING` also needs
`PROJECT.LIST` and `RESOURCE.LIST`. Without them the Organization summary and
Project detail reports are empty. `CUSTOMER.SUPPORT`, for example, has neither:

```yaml
- role: CUSTOMER.SUPPORT
  add_permissions:
    - CUSTOMER.VIEW_REPORTING
    - PROJECT.LIST
    - RESOURCE.LIST
```

## Slug permissions

Site agents name backend accounts after slugs: a SLURM account from the resource
or project slug, a Kubernetes namespace from the resource slug. A wrong or late
slug points an account at another allocation, so `PROJECT.SET_SLUG` and
`RESOURCE.SET_SLUG` are granted to **no role** in `permissions.yaml`, and out of
the box only staff can set a slug.

A deployment that wants others to name allocations grants the permission to the
roles it chooses, in `permissions-override.yaml`:

```yaml
- role: OFFERING.MANAGER
  add_permissions:
    - RESOURCE.SET_SLUG
- role: CUSTOMER.OWNER
  add_permissions:
    - PROJECT.SET_SLUG
```

`initdb` runs `import_roles` and then `override_roles` on every deploy, so the
grant is re-applied each time. Do **not** grant them through the role editor in
the UI: `import_roles` resets system roles to `permissions.yaml` on the next
deploy and the grant disappears.

What a holder can do:

| Permission | Checked on | Where it applies |
|---|---|---|
| `RESOURCE.SET_SLUG` | the resource's project or organization, its offering, the offering's organization or service provider | `POST .../marketplace-resources/{uuid}/set_slug/` (and the provider-resources twin); `slug` on order create names the new resource |
| `PROJECT.SET_SLUG` | the project or its organization | `POST /api/projects/{uuid}/set_slug/`; `slug` on project create |

Without the permission, `slug` on create is ignored, even when malformed, and
the slug template applies, as before. Other than staff, a holder cannot change
a slug once a backend account may exist or is being created:

- a resource is locked once it has a `backend_id`, or once its creation order
  is executing, done or erred. While the order still awaits approval or its
  start date, the slug can change;
- a project is locked once any of its non-terminated resources is.

The check and the save run under row locks on the resources and their creation
orders, so an order cannot start executing in between. Slugs must be unique
per model. Every change is recorded in the event log with the old and new slug
and the caller.

## Permission System Behavior

### Expiration Handling

- Basic permission queries (`get_users_with_permission`, `get_scope_ids`) include all roles regardless of expiration
- Expiration checking is explicit via `has_user(expiration_time=False)`, not implicit in `has_permission()`
- Use `has_user(expiration_time=current_time)` for time-based validation

### Error Handling

- `permission_factory` doesn't catch `AttributeError` and convert to `PermissionDenied`
- Test for actual exceptions the system raises, not ideal ones
- Handle `AttributeError` when accessing missing nested attributes

## Data Accuracy Critical Areas

- **User counting**: Always use `distinct()` on user_id to avoid double-counting users with multiple roles
- **Permission checks**: Handle edge cases (None scope, missing attributes) gracefully
- **Financial calculations**: Never approximate - exact calculations required

## Performance Optimization

### Query Optimization Strategy

- Use `select_related()` for foreign keys
- Use `prefetch_related()` for reverse relationships
- Use `distinct()` for deduplication instead of manual logic
- Accept 20-30 queries for complex operations rather than approximations
- Verify permission checks use reasonable query counts (≤3 for most operations)

## Personal Access Tokens — entity scoping

`PersonalAccessToken` has two scope layers:

1. `scopes` — the permission allowlist (subset of `PermissionEnum`). A PAT
   can only ever exercise permissions that the user holds *and* that are
   listed here.
2. `allowed_scopes` — optional list of entity bindings restricting *where*
   the PAT can act. Stored as `[{content_type_id, object_id}, …]`. Created
   from `[{type, uuid}, …]` where `type` is a key of
   `permissions.enums.TYPE_MAP` (e.g. `customer`, `project`, `offering`,
   `resource`, `resource_project`, `call`, `proposal`, `service_provider`,
   `call_organizer`).

### Enforcement

`_pat_scope_check` (and the `_pat_entity_check` helper in
`waldur_core.permissions.utils`) runs ahead of the `is_staff` bypass so a
scoped PAT narrows even staff users. The rules:

- Empty `allowed_scopes` → no entity restriction (legacy behaviour).
- `scope=None` request + non-empty bindings → denied. A scoped PAT cannot
  perform scope-less / global actions.
- Otherwise → allowed iff the request scope, or any of its ancestors per
  `get_scope_ancestors`, matches one of the PAT's bindings. The walk is
  upward-only: a PAT bound to a child entity does **not** authorise actions
  on its parent.

### Restrictions

- `STAFF.ACCESS` / `SUPPORT.ACCESS` cannot be combined with `allowed_scopes` —
  those scopes are global by design.
- Non-staff users may only bind a PAT to entities where they hold at least
  one of the requested permissions (directly or via an ancestor) — this
  guard prevents privilege escalation through binding.
- Bindings are immutable. `rotate` preserves them; there is no PATCH
  endpoint. To change bindings, create a new PAT.
- Bindings do **not** auto-revoke when the granting role is removed. The
  stored `allowed_scopes` continue to surface entity names in the PAT
  list/detail response, but enforcement falls through to the user's
  current roles — the binding can only narrow access, never grant it.
  If a user is removed from a customer, their PAT may still display the
  customer's name (a minor info-leak about an entity they used to have
  access to); to scrub it, revoke and recreate the PAT.

### List-endpoint result filtering

`PATScopeListFilter` in `waldur_core.permissions.pat_filtering` narrows
both list and detail querysets so a scoped PAT only sees entities reachable
from its bindings. It is installed once at app ready by
monkey-patching `GenericAPIView.filter_queryset` — viewsets that override
`filter_backends` are still covered, since the patch wraps the original
implementation and applies the PAT filter after the viewset's own
backends. Detail endpoints inherit the same narrowing because DRF's
`get_object` calls `filter_queryset` before `get_object_or_404`.

**Coverage**: the nine `TYPE_MAP` entity models are filtered:

| Model | Reachable from binding type |
|---|---|
| `structure.Customer` | `customer` |
| `structure.Project` | `customer`, `project` |
| `marketplace.Offering` | `customer`, `offering` |
| `marketplace.Resource` | `customer`, `project`, `offering`, `resource` |
| `marketplace.ResourceProject` | `customer`, `project`, `offering`, `resource`, `resource_project` |
| `marketplace.ServiceProvider` | `customer`, `service_provider` |
| `proposal.CallManagingOrganisation` | `customer`, `call_organizer` |
| `proposal.Call` | `call` (no ancestor inheritance) |
| `proposal.Proposal` | `proposal` (no ancestor inheritance) |

Endpoints whose model does not appear above (e.g. `/api/marketplace-orders/`,
`/api/invoices/`) currently pass through unfiltered. Add a builder via
`register_pat_filter` in `pat_filtering.py` to extend coverage.

**Codepath limit**: the install only wraps `GenericAPIView.filter_queryset`.
Views that bypass that codepath — `APIView` subclasses, custom actions
that call `Model.objects.filter(...)` directly without going through
`self.filter_queryset(self.get_queryset())`, or bespoke CSV/export
endpoints — are **not** filtered. If you add such a view and it returns
data for an entity in `TYPE_MAP`, call `PATScopeListFilter().filter_queryset(...)`
on the queryset yourself before serialising.

### Performance notes for scoped-PAT hot paths

`_pat_entity_check` calls `get_scope_ancestors(scope)`, which dereferences
foreign-key attributes on the request scope. If the view's queryset
doesn't `select_related` those FKs, each scoped-PAT permission check
incurs an extra DB round-trip per ancestor. Views that are hot under
PAT auth should `select_related("customer", "project__customer",
"offering")` (or whichever ancestors apply) — scoped PATs walk the
ancestor chain on every permission check, so each missed `select_related`
multiplies into one query per ancestor per check.

## Role hygiene report

`waldur_core/permissions/hygiene.py` checks the role catalogue for names that
are not machine codes, system roles this release does not define, clones whose
name or organization binding drifted, custom roles that are silently offered in
every organization, and permissions that can never apply to a role's scope.
Offering catalog roles (`resource` / `resource_project`) are exempt — their
names are the provider's to choose.

Two entry points, both read-only:

```bash
waldur check_role_names                       # text, exits 1 on errors
waldur check_role_names --severity warning    # errors and warnings only
waldur check_role_names --format json
```

```http
GET /api/roles/hygiene_report/                # staff only
```

Only the findings that cannot be legitimately deployment-specific carry the
error severity that fails the command: a name that is not a machine code, a
system role bound to the wrong scope, a clone whose name drifted from its
organization, a clone that lost its organization binding, and a role bound to
more than one organization. A role name this release does not define is a
warning — `import_roles` marks every role in a deployment's own
`permissions.yaml` as a system role, and a deployment cannot add its roles to
`SYSTEM_ROLE_SCOPES`.

Two tables drive the scope checks and are the place to extend when a scope type
or permission category is added, both in `permissions/enums.py`:
`SCOPE_ANCESTORS` (which scopes a role governs from where it is granted) and
`PERMISSION_TARGET_SCOPES` (the scope each permission category acts on). A
permission is meaningful on its target scope and on every ancestor of it; a
category missing from the table is skipped rather than guessed at.

## Quiet grant sources

`UserRole.source` records who issued a machine-made grant (`rule:<uuid>`, `sram:<uuid>`, ...). Role events carry it as `role_source`. An app can register a source prefix with `waldur_core.permissions.utils.register_quiet_grant_source(prefix)` in `AppConfig.ready`: grants and revocations with that prefix are still logged, but their events carry `suppress_email: true` and email hooks skip them. The SRAM integration registers `sram:` and `sram-rule:`, since its membership sync would otherwise email on every change.
