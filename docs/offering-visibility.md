# Marketplace Offering Visibility

This document describes how marketplace offering visibility can be configured to control which offerings regular users can see.

## Overview

The `RESTRICTED_OFFERING_VISIBILITY_MODE` Constance setting controls how offerings with restrictions (organization group-limited plans) are displayed to regular users. Staff and support users always see all offerings regardless of this setting.

## Configuration

The setting is configured in the backend via Constance (Admin > Config > Marketplace section):

**Setting name:** `RESTRICTED_OFFERING_VISIBILITY_MODE`

**Default value:** `show_all`

## Visibility Modes

| Mode | Description |
|------|-------------|
| `show_all` | **Default behavior.** Show all shared offerings. Users see restricted offerings but cannot order if they lack access to any plans. |
| `show_restricted_disabled` | Show all offerings, but inaccessible ones are visually marked as disabled with a lock icon and tooltip explaining why the user cannot order. |
| `hide_inaccessible` | Hide offerings where the user has no accessible plans (due to organization group restrictions). |
| `require_membership` | Most restrictive mode. Hide ALL offerings unless the user belongs to at least one organization or project. Users with membership then see only accessible offerings (same as `hide_inaccessible`). |

## Key Behaviors Matrix

| User Type | `show_all` | `show_restricted_disabled` | `hide_inaccessible` | `require_membership` |
|-----------|------------|---------------------------|---------------------|---------------------|
| Anonymous | Shared only | Shared only | Shared only | Shared only |
| Staff/Support | All | All | All | All |
| Regular (no membership) | All shared | All shared (some disabled) | Only accessible | **None** |
| Regular (with membership) | All shared | All shared (some disabled) | Only accessible | Only accessible |

### Notes

- **Anonymous users** are controlled by the separate `ANONYMOUS_USER_CAN_VIEW_OFFERINGS` setting
- **Staff/Support** users always see all offerings regardless of the visibility mode
- **"Accessible"** means the user has at least one non-archived plan available (either public plans or plans whose organization groups include the user's organization)
- **"Membership"** means the user has a role in at least one organization, project, or offering

## Frontend Behavior

### `show_restricted_disabled` Mode

When this mode is active, the frontend:

1. Checks the `is_accessible` field returned by the API for each offering
2. For offerings where `is_accessible === false`:
   - Applies a `disabled` CSS class to gray out the card
   - Shows a lock icon with tooltip: "This offering is restricted. Contact your organization admin for access."
   - Disables the "Deploy" button

### Other Modes

For `show_all`, `hide_inaccessible`, and `require_membership` modes, the backend handles filtering - the frontend simply displays what the API returns.

## API Changes

The `PublicOfferingDetailsSerializer` includes an `is_accessible` boolean field that indicates whether the current user can order the offering. This field is:

- `true` for staff/support users (always)
- `true` if the user has at least one accessible, non-archived plan
- `false` if all plans are restricted to organization groups the user doesn't belong to

## Role-based restriction (`restricted_to_roles`)

Independently of `RESTRICTED_OFFERING_VISIBILITY_MODE` (which governs organization-group/plan access), an individual offering can be restricted to specific **project or organization roles** via the `restricted_to_roles` plugin option — a list of role names, e.g. `["PROJECT.MANAGER", "PROJECT.ADMIN"]`. It is set per offering under **Integration → Operations → Orders & approval** in the UI, or via the `update_integration` API.

When set:

- The offering is **hidden from the catalog** for users who do not hold one of the listed roles in any scope. Users who already consume resources from the offering keep access.
- Only users holding one of the listed roles **on the target project or its customer** can create an order; others are rejected with HTTP 403. Staff and support bypass the check.
- `is_accessible` is `false` for users who lack the roles.
- The order form's organization and project selectors are limited to scopes where the user holds one of the roles.

Approval is unaffected: whether an order skips consumer review still depends on the role's `ORDER.APPROVE` permission, exactly as for any other offering. Role names must be valid project- or organization-scoped roles.

## Role-based approval skip (`auto_approve_for_roles`)

Independently of `restricted_to_roles`, an offering can designate **project or organization roles whose orders skip consumer review** via the `auto_approve_for_roles` plugin option — a list of role names, e.g. `["PROJECT.MANAGER"]`. It is set per offering under **Integration → Operations → Orders & approval** in the UI, or via the `update_integration` API. **Only staff can change this option** (service providers and customer owners cannot), even though they may edit other integration settings.

When set:

- An order whose creator holds one of the listed roles **on the target project or its customer** skips the consumer review step (it is auto-approved by the consumer), without the role needing the global `ORDER.APPROVE` permission.
- The role must be held in the order's own project/customer scope — holding it in an unrelated project does not auto-approve.
- Provider-side review still applies: the order proceeds to `PENDING_PROVIDER` unless the provider also auto-approves.
- Offerings requiring a purchase-order upload still block auto-approval until the attachment is present.

This option is orthogonal to `restricted_to_roles`: an offering can be public and still auto-approve for a role, restricted without auto-approving, or both. Role names must be valid project- or organization-scoped roles.

## Use Cases

### Public Marketplace (default)

Use `show_all` for open marketplaces where users should see all available offerings and learn about restrictions when they try to order.

### Enterprise with Soft Restrictions

Use `show_restricted_disabled` when you want users to be aware of offerings they cannot access (e.g., premium tiers) while clearly indicating they need to contact their admin for access.

### Enterprise with Hard Restrictions

Use `hide_inaccessible` when users should only see offerings they can actually order, reducing confusion and support requests.

### Closed Community

Use `require_membership` for portals where only registered organization members should browse offerings. New users without any affiliation see an empty marketplace until they're added to an organization.

## Implementation Details

### Backend Files

- `src/waldur_core/server/constance_settings.py` - Setting definition
- `src/waldur_mastermind/marketplace/managers.py` - `filter_by_ordering_availability_for_user()` method
- `src/waldur_mastermind/marketplace/serializers.py` - `is_accessible` field in `PublicOfferingDetailsSerializer`

### Frontend Files

- `src/auth/types.ts` - `OfferingVisibilityMode` type and `RESTRICTED_OFFERING_VISIBILITY_MODE` in config
- `src/marketplace/common/OfferingCard.tsx` - Disabled display logic

### Tests

Backend tests are in `src/waldur_mastermind/marketplace/tests/test_offerings.py` in the `RestrictedOfferingVisibilityModeTest` class.
