# Auto-Provisioning

Waldur's auto-provisioning feature turns what an identity provider asserts about a user into projects, roles and resources, without an administrator acting on each account.

## Overview

A rule matches users on their profile attributes and identity provider claims. For every matching rule the system:

1. Resolves the organization — the rule's own, or the one named by the user's organization claim
2. Creates or joins a project, unless the rule is organization-level only
3. Grants the roles the rule asserts: a project role, an organization role, or both
4. Optionally provisions a marketplace resource and processes the order asynchronously
5. Withdraws roles it previously granted once the user stops matching, when the rule opts into revocation

Provisioning (steps 1–4) happens once, when the account first appears. Role reconciliation (steps 3 and 5) re-runs every time identity data is refreshed, which is what keeps a grant in step with a claim that comes and goes.

## Core Components

### Rule Model

The `Rule` model (`src/waldur_autoprovisioning/models.py:11`) defines auto-provisioning configurations with the following key fields:

- **customer**: Target customer for project creation (optional when using organization mapping)
- **plan**: Optional marketplace plan to provision
- **plan_attributes**: Custom attributes for resource provisioning
- **plan_limits**: Resource limits (e.g., `{"vcpu": 4, "ram": 8192, "storage": 100}`)
- **project_role**: Role assigned to users in created projects
- **customer_role**: Role granted on the organization itself (optional)
- **create_project**: Whether to create/join a project at all (default `true`)
- **revoke_when_unmatched**: Withdraw this rule's grants once a user stops matching (default `false`)
- **use_user_organization_as_customer_name**: Map user's organization claim to existing customer
- **project_name_template**: Template for project naming (e.g., `"{username}_workspace"`)

### User Matching

Rules use the `UserDetailsMatchMixin` for pattern matching:

- **user_email_patterns**: Regex patterns for email matching (e.g., `[".+@example.com"]`)
- **user_affiliations**: Organization affiliations for matching (e.g., `["staff", "faculty"]`)
- **user_identity_sources**: Identity provider matching (e.g., `["eduGAIN", "SAML"]`)

#### AAI-Based Filtering

Rules also support AAI (Authentication and Authorization Infrastructure) attributes for more granular user matching:

- **user_nationalities**: ISO 3166-1 alpha-2 country codes (e.g., `["DE", "FR", "IT"]`)
- **user_organization_types**: SCHAC organization type URNs (e.g., `["urn:schac:homeOrganizationType:int:university"]`)
- **user_assurance_levels**: REFEDS assurance profile URIs (e.g., `["https://refeds.org/assurance/IAP/high"]`)

Pattern matching uses OR logic within a field: a user matches if ANY email pattern OR ANY affiliation OR ANY identity source matches.

**Note:** For assurance levels, AND logic is used - user must have ALL specified assurance URIs.

#### Identity provider claims

- **user_claims**: a map of claim name to accepted values, e.g.
  `{"roles": ["acme-owner", "acme-admin"], "entitlements": ["urn:mace:example.org:group:hpc-*"]}`

Every configured claim must match (AND); within one claim any listed value
matches (OR). A value ending in `*` matches by prefix, which is what entitlement
URNs usually need because they carry a trailing `#authority` fragment. Values
are compared literally — this field decides whether a role is granted, so unlike
`user_email_patterns` it is deliberately not a regex.

Claims sit alongside the AAI filters as an additional requirement, **not** in the
basic OR group: a rule that grants a role off a claim is never satisfied just
because an email pattern happened to match.

Values are read from `User.details`, which is populated from the claims listed in
`IdentityProvider.extra_fields` — **a claim the identity provider is not
configured to pass through will never match.** A claim whose name is also a
mapped `User` field (`affiliations`, `organization`, `identity_source`,
`nationality`, …) falls back to that field, so a deployment that maps the claim
through `attribute_mapping` instead also works.

## Organization Mapping Feature

### Organization Mapping Overview

Organization mapping allows auto-provisioning rules to resolve the customer dynamically from the user's organization claim. This enables multi-tenant scenarios where each organization has its own customer in Waldur.

### How It Works

When `use_user_organization_as_customer_name` is enabled, `resolve_customer()` checks, in this order:

1. The user's details are protected — their registration method is listed in
   `PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS`. An unprotected user's
   organization claim is self-asserted and is not trusted here.
2. The user carries an organization claim at all.
3. Exactly one customer has that name. Zero matches and ambiguous matches both
   block the rule rather than guessing.

The resolved customer is then used for the project (when `create_project` is on)
and for any organization-level role the rule grants.

### Protected User Details

For security, organization mapping requires users to have protected details:

```python
@property
def should_protect_user_details(self) -> bool:
    """Return True if user profile fields must be read-only."""
    protected_methods = django_settings.WALDUR_CORE[
        "PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS"
    ]
    return bool(
        self.registration_method and self.registration_method in protected_methods
    )
```

### Customer Resolution Logic

`resolve_customer()` (`src/waldur_autoprovisioning/reconciliation.py`) is the single
place that answers "which organization does this rule target for this user?", and
it is shared by three callers: the provisioning handler, the dry-run evaluator
behind `test-match`, and reconciliation. They therefore always agree on the
verdict *and* on the wording of the reason.

It returns a `CustomerResolution` rather than logging and bailing out:

```python
@dataclass(frozen=True)
class CustomerResolution:
    """Outcome of working out which organization a rule applies to for a user."""

    customer: Customer | None
    block_reason: str = ""
    candidates: tuple = ()
    ambiguous: bool = False
    lookup_performed: bool = False
```

A `None` customer always carries a `block_reason` naming what went wrong — no
organization configured, registration method not protected, no organization
claim, no customer with that name, or several customers sharing it. That string
is what the caller logs and what the test-match dialog shows the administrator,
so a rule that silently provisions nothing can be diagnosed without reading the
server log.

## API Endpoints

### Rules Management

**Endpoint**: `/api/autoprovisioning-rules/`

**Permissions**:

- List/Read: Customer role permissions
- Create/Update/Delete: Staff only

**Serialization**: `RuleSerializer` provides comprehensive API access with related object details:

```json
{
    "name": "University Users Rule",
    "uuid": "...",
    "user_email_patterns": [".+@university\\.edu"],
    "user_affiliations": ["staff", "faculty"],
    "user_identity_sources": ["eduGAIN"],
    "user_claims": {"roles": ["acme-owner"]},
    "customer": "customer-uuid",
    "create_project": true,
    "revoke_when_unmatched": false,
    "customer_role": "role-uuid",
    "customer_role_display_name": "Owner",
    "use_user_organization_as_customer_name": false,
    "project_role": "role-uuid",
    "project_role_display_name": "Admin",
    "plan": "plan-uuid",
    "plan_attributes": {"flavor": "m1.small"},
    "plan_limits": {"vcpu": 2, "ram": 4096}
}
```

### Dry-run evaluation

**Endpoint**: `POST /api/autoprovisioning-rules/<uuid>/test-match/`, staff only.

Takes `{"user_uuid": "..."}` and evaluates the rule against that user without
writing anything — no project, no grant, no order. The response carries a
per-filter breakdown (`configured`, `matched`, the user's value and the rule's
value for each of affiliations, email patterns, identity sources, nationalities,
organization types, assurance levels and claims), the customer-lookup verdict
when `use_user_organization_as_customer_name` is set, a top-line
`would_provision` flag and a human-readable `block_reason`.

It also reports `unconfigured_claims`: claims the rule matches on that no active
identity provider lists in its `extra_fields`. That distinguishes "the provider
sent a different value" from "the provider never sends this claim", which look
identical in the raw filter breakdown.

### Validation Rules

The serializer enforces these validation constraints:

- Either `customer` or `use_user_organization_as_customer_name=true` must be specified
- Either `project_role` or `customer_role` must be provided — a rule has to grant something
- A rule with `create_project=false` must specify a `customer_role`, since it has nothing else to grant
- `project_role` / `customer_role` may each be given as a URL or by name (`*_role_name`), but not both
- Roles must be valid for their scope (project role on projects, organization role on organizations)
- Email patterns must be valid regex expressions
- A claim must have a non-empty name and at least one accepted value; a bare `*` is rejected

## Processing Flow

### Trigger Mechanism

Two triggers, deliberately separate.

**Provisioning** (projects and orders) runs once, when the account first appears:

```python
signals.post_save.connect(
    handlers.handle_new_user,
    sender=User,
    dispatch_uid="waldur_autoprovisioning.handle_new_user",
)
```

The handler returns early unless `created`, so an ordinary profile edit never
materialises projects.

**Role reconciliation** runs whenever identity data is refreshed — after an OIDC
login and after a SCIM pull — via `waldur_core.core.signals.user_identity_synced`:

```python
core_signals.user_identity_synced.connect(
    handlers.handle_identity_synced,
    dispatch_uid="waldur_autoprovisioning.handle_identity_synced",
)
```

That is the moment a claim may have been added or withdrawn, which `post_save`
cannot distinguish.

### Auto-Provisioning Workflow

1. **User Creation**: New user triggers `handle_new_user` handler
2. **Rule Matching**: System finds applicable rules using `Rule.get_objects_by_user_patterns()`
3. **Customer Resolution**: `resolve_customer()` — either the configured customer or the one
   resolved from the organization claim
4. **Project Creation**: `get_or_create_project()` creates or assigns the project, when
   `create_project` is enabled
5. **Resource Provisioning**: If plan is specified, creates marketplace order
6. **Order Processing**: Marketplace processes the order asynchronously
7. **Role Reconciliation**: `reconcile_autoprovisioned_roles()` issues every role the matching
   rules assert, and withdraws the ones they no longer do

## Role reconciliation

A rule with `revoke_when_unmatched` enabled withdraws the roles it granted once
the user stops matching it, the same way inbound SCIM revokes a membership the
identity provider no longer asserts in a group `PUT`. Without it a rule is
grant-only: a matching user gains a role and nothing takes it away.

### Provenance

Every rule-issued grant records `UserRole.source = "rule:<rule uuid>"`.
Reconciliation only ever revokes rows carrying the source of a rule it is
currently evaluating. Two consequences worth stating plainly:

- A role granted **by a person** carries an empty `source` and is never revoked
  automatically, even when it names the same (user, scope, role) triple as a rule.
- One rule never cleans up after another.

Grants that already existed before this field was introduced have an empty
source, so no pre-existing grant becomes eligible for automatic revocation.

### Opting in

`revoke_when_unmatched` defaults to `false`. Enabling claim matching on a live
deployment therefore cannot silently strip access that is already in use; an
administrator turns revocation on per rule, deliberately.

When it is on, note that revoking a user's **last** role triggers the usual
`role_revoked` consequences — which include deactivating the user if
`DEACTIVATE_USER_IF_NO_ROLES` is enabled, and removing them from provider-side
groups (FreeIPA, Matrix rooms, site-agent queues).

### Backfill

Reconciliation is driven by logins, so editing a rule leaves existing users out
of step until each of them next signs in. To close that window:

```bash
# Preview one user
waldur reconcile_autoprovisioned_roles --username alice --dry-run

# Apply to everyone
waldur reconcile_autoprovisioned_roles --all

# Throttle a large run to 20 users per second
waldur reconcile_autoprovisioned_roles --all --rate 20
```

`--username` and `--all` are mutually exclusive and one of them is required.
`--dry-run` reports what would change and writes nothing.

## Configuration Examples

### Organization role from an identity provider claim

Grant organization ownership to whoever carries `roles: acme-owner`, and take it
away when they stop:

```json
{
    "name": "Acme owners",
    "user_claims": {"roles": ["acme-owner"]},
    "customer": "acme-customer-uuid",
    "customer_role_name": "CUSTOMER.OWNER",
    "create_project": false,
    "revoke_when_unmatched": true
}
```

`roles` must be listed in the identity provider's `extra_fields` for the claim to
reach Waldur at all.

### Basic Project Creation

Create projects without resources:

```json
{
    "name": "Basic Project Rule",
    "user_email_patterns": [".+@company\\.com"],
    "customer": "company-customer-uuid",
    "project_role": "admin-role-uuid"
}
```

### Resource Provisioning

Auto-provision OpenStack tenants:

```json
{
    "name": "OpenStack Auto-Provision",
    "user_email_patterns": [".+@research\\.org"],
    "customer": "research-customer-uuid",
    "plan": "openstack-plan-uuid",
    "plan_limits": {
        "vcpu": 8,
        "ram": 16384,
        "storage": 500
    },
    "plan_attributes": {
        "flavor": "m1.large",
        "network_config": "private"
    }
}
```

### Organization-Based Provisioning

Use user's organization for customer assignment:

```json
{
    "name": "Organization Rule",
    "user_email_patterns": [".+@.*\\.edu"],
    "use_user_organization_as_customer_name": true,
    "project_name_template": "{username}_research_project"
}
```

### Multi-Tenant Academic Setup

Support multiple universities with their own customers:

```json
{
    "name": "Academic Institutions",
    "user_email_patterns": [".+@.*\\.edu", ".+@.*\\.ac\\.[a-z]{2}"],
    "use_user_organization_as_customer_name": true,
    "project_role_name": "PROJECT.ADMIN",
    "plan": "basic-research-plan-uuid",
    "plan_limits": {
        "vcpu": 4,
        "ram": 8192,
        "storage": 200
    }
}
```

### AAI-Based Access Control

Restrict provisioning to users with verified identity from EU universities:

```json
{
    "name": "EU Research Universities",
    "user_email_patterns": [".+@.*\\.edu", ".+@.*\\.ac\\.[a-z]{2}"],
    "user_nationalities": ["DE", "FR", "IT", "ES", "NL", "BE", "AT", "PL", "SE", "FI"],
    "user_organization_types": ["urn:schac:homeOrganizationType:int:university"],
    "user_assurance_levels": ["https://refeds.org/assurance/IAP/medium"],
    "use_user_organization_as_customer_name": true,
    "project_role_name": "PROJECT.ADMIN"
}
```

This rule only matches users who:

1. Have an academic email address
2. Have nationality from one of the listed EU countries
3. Are from a university (SCHAC organization type)
4. Have medium or higher identity assurance from their IdP

## Security and Validation

### Protected Users

Organization-based provisioning requires protected user details to ensure organization claims come from trusted identity providers and cannot be manipulated by users.

### Input Validation

- Email patterns validated as proper regex
- Project role must be valid project-level role
- Customer or organization requirement enforced
- Plan compatibility verified
- Mutual exclusion of customer specification methods

### Permission Controls

- Rules managed by staff users only
- Customer-scoped access for viewing
- Project creation respects customer permissions

## Monitoring and Logging

The system provides comprehensive logging for troubleshooting:

- Invalid regex patterns logged and skipped
- Missing organization claims logged
- Multiple customer matches warned
- Order creation and processing tracked
- Protected user validation failures logged

## Integration Points

### Marketplace Integration

Auto-provisioning integrates with Waldur's marketplace:

- Uses `marketplace_utils.generate_resource_name()` for naming
- Creates `Resource` and `Order` objects
- Triggers `process_order_on_commit()` for async processing
- Respects marketplace offering types and constraints

### User Management Integration

- Hooks into user creation process
- Respects user protection settings
- Leverages organization claims from identity providers
- Integrates with role-based access control

### Identity Provider Integration

- Reads organization claims from SAML/OIDC providers
- Validates user registration method for security
- Maps organization names to existing customers
- Supports multi-tenant identity scenarios

## Best Practices

1. **Rule Design**: Create specific rules for different user groups
2. **Organization Mapping**: Ensure customer names match organization claims exactly
3. **Naming Templates**: Use descriptive project naming templates
4. **Resource Limits**: Set appropriate defaults for auto-provisioned resources
5. **Monitoring**: Monitor logs for failed provisioning attempts
6. **Security**: Configure protected registration methods for organization-based rules
7. **Multi-Tenancy**: Use organization mapping for SaaS scenarios with multiple customers
