# Conflict of Interest (COI) Detection System

The Waldur proposal module includes an automated Conflict of Interest detection system that identifies potential conflicts between reviewers and proposals. This ensures fair and unbiased peer review processes.

## Architecture Overview

```text
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                              COI Detection System - Architecture                                 │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘

                                    ┌─────────────────────┐
                                    │    Call Manager     │
                                    │  triggers detection │
                                    └──────────┬──────────┘
                                               │
                                               ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                     API Endpoint                                                │
│                       POST /api/proposal-protected-calls/{uuid}/run-coi-detection/              │
└─────────────────────────────────────────────┬───────────────────────────────────────────────────┘
                                              │
                                              ▼
                               ┌──────────────────────────────┐
                               │     COIDetectionJob          │
                               │  (created with PENDING state)│
                               └──────────────┬───────────────┘
                                              │
                                              ▼
                               ┌──────────────────────────────┐
                               │     Celery Task Queue        │
                               │    run_coi_detection.delay() │
                               │      (Background Job)        │
                               └──────────────┬───────────────┘
                                              │
                                              ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                BACKGROUND COI DETECTION                                         │
│                                                                                                 │
│  ┌─────────────────┐      ┌─────────────────┐      ┌─────────────────────┐                     │
│  │ Get Reviewers   │─────▶│  Get Proposals  │─────▶│  For each pair:     │                     │
│  │ (Accepted pool) │      │  (in this call) │      │  Reviewer×Proposal  │                     │
│  └─────────────────┘      └─────────────────┘      └──────────┬──────────┘                     │
│                                                               │                                 │
│                           ┌───────────────────────────────────┼───────────────────────────┐     │
│                           │                                   │                           │     │
│                           ▼                                   ▼                           ▼     │
│            ┌──────────────────────────┐   ┌──────────────────────────┐   ┌────────────────────┐│
│            │ Named Personnel Check    │   │ Institutional Check      │   │ Co-authorship Check││
│            │                          │   │                          │   │                    ││
│            │ • User ID match          │   │ • Same organization      │   │ • Shared papers    ││
│            │ • ORCID match            │   │ • Same department        │   │ • ORCID coauthors  ││
│            │ • Email match            │   │ • Former affiliation     │   │ • Name fuzzy match ││
│            │ • Fuzzy name match       │   │   (within lookback)      │   │   (within lookback)││
│            └────────────┬─────────────┘   └────────────┬─────────────┘   └─────────┬──────────┘│
│                         │                              │                           │           │
│                         └──────────────────────────────┴───────────────────────────┘           │
│                                                        │                                        │
│                                                        ▼                                        │
│                                     ┌──────────────────────────────────────┐                   │
│                                     │  ConflictOfInterest Record Created   │                   │
│                                     │  (with evidence_data as JSON)        │                   │
│                                     └──────────────────────────────────────┘                   │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Detection Algorithms

The system runs three detection algorithms for each reviewer-proposal pair:

### 1. Named Personnel Detection

Checks if the reviewer is named in the proposal team. This is considered a **real conflict** that requires automatic recusal.

**Match criteria:**

- User ID match (most reliable)
- ORCID identifier match
- Email address match
- Fuzzy name match (threshold: 90% similarity)
- Alternative names match

### 2. Institutional Affiliation Detection

Identifies conflicts based on organizational affiliations between reviewers and applicant institutions.

**Detection rules:**

| Scenario | COI Type | Severity |
|----------|----------|----------|
| Current same institution | `INST_SAME` | Real |
| Former institution (within lookback) | `INST_FORMER` | Apparent |
| Same department | `INST_DEPT` | Apparent |

### 3. Co-authorship Detection

Analyzes shared publications between reviewers and proposal team members.

**Matching logic:**

1. Get reviewer's publications within the lookback period
2. Extract coauthors from each publication
3. Compare against proposal team members using:
   - ORCID matching (most reliable)
   - Fuzzy name matching (threshold: 85%)

**Severity determination:**

- Recent co-authorship (last year) + 3+ papers → Real conflict
- Older co-authorship or fewer papers → Apparent conflict

## COI Types

| Type Code | Description | Severity |
|-----------|-------------|----------|
| `ROLE_NAMED` | Reviewer is named in proposal personnel | Real |
| `INST_SAME` | Same current institution as applicant | Real |
| `INST_FORMER` | Former institution overlap within lookback | Apparent |
| `INST_DEPT` | Same department affiliation | Apparent |
| `COAUTH_RECENT` | Recent co-authored papers | Apparent |
| `COAUTH_OLD` | Older co-authored papers | Potential |
| `FIN_DIRECT` | Direct financial interest | Real |
| `REL_FAMILY` | Family relationship | Real |
| `REL_MENTOR` | Mentor/mentee relationship | Real |
| `REL_SUPERVISOR` | Supervisor/supervisee relationship | Real |
| `COLLAB_ACTIVE` | Active collaboration | Real |
| `COLLAB_GRANT` | Shared grant funding | Apparent |
| `REL_EDITORIAL` | Editorial relationship | Apparent |
| `COMPET` | Competitive relationship | Apparent |
| `ROLE_CONF` | Conference organizer relationship | Apparent |
| `INST_CONSORT` | Consortium membership | Potential |
| `CONF_ATTEND` | Conference participation | Potential |
| `SOC_MEMBER` | Professional society membership | Potential |

## Severity Levels

| Level | Description | Action Required |
|-------|-------------|-----------------|
| **Real** | Must recuse from review | Reviewer cannot participate |
| **Apparent** | Requires management | May proceed with management plan |
| **Potential** | Disclosure only | Document and monitor |

## Status Workflow

```text
   ┌─────────┐
   │ PENDING │◀──────────────── (initial state from detection)
   └────┬────┘
        │
        ├───────────────┬───────────────┐
        ▼               ▼               ▼
   ┌───────────┐   ┌──────────┐   ┌──────────┐
   │ DISMISSED │   │  WAIVED  │   │ RECUSED  │
   │(not valid)│   │(with mgmt│   │(reviewer │
   │           │   │  plan)   │   │ removed) │
   └───────────┘   └──────────┘   └──────────┘
```

### Status Descriptions

| Status | Description | Use Case |
|--------|-------------|----------|
| **PENDING** | Awaiting manager review | Initial state for all detected conflicts |
| **DISMISSED** | Not a valid conflict | False positive or outdated information |
| **WAIVED** | Allowed with management plan | Conflicts where reviewer may proceed with mitigation |
| **RECUSED** | Reviewer removed from assignment | Serious conflicts requiring removal |

## Configuration

Each call can have its own COI detection configuration via `CallCOIConfiguration`:

```python
CallCOIConfiguration:
├── coauthorship_lookback_years: 3      # How far back to check publications
├── coauthorship_threshold_papers: 1    # Min shared papers to flag
├── institutional_lookback_years: 2     # How far back for former affiliations
├── auto_detect_coauthorship: true      # Enable/disable coauthorship check
├── auto_detect_institutional: true     # Enable/disable institutional check
├── auto_detect_named_personnel: true   # Enable/disable named personnel check
├── include_same_institution: true      # Flag same-institution conflicts
├── recusal_required_types: [...]       # COI types requiring automatic recusal
├── management_allowed_types: [...]     # COI types that can be managed with plan
├── disclosure_only_types: [...]        # COI types requiring disclosure only
└── invitation_proposal_disclosure: str # Proposal info shown in invitations
```

### Invitation Proposal Disclosure Levels

The `invitation_proposal_disclosure` setting controls what proposal information reviewers see when receiving invitations:

| Level | Description | Use Case |
|-------|-------------|----------|
| `titles_only` | Only proposal titles shown | Maximum confidentiality |
| `titles_and_summaries` | Titles and project summaries | Balanced approach |
| `full_details` | Complete proposal details | Full transparency |

This helps reviewers identify potential conflicts before accepting invitations.

## Data Sources

The detection system uses data from multiple sources:

### Reviewer Data

```text
ReviewerProfile ─────────┬──── ReviewerPublication ──── coauthors (JSON)
     │                   │
     │                   └──── ReviewerAffiliation ──── organization, department
     │
     └── orcid_id, alternative_names
```

### Proposal Data

```text
Proposal ────────────────┬──── ProjectIndication ──── project_pi (User)
     │                   │
     │                   └──── team_members
     │
     └── project.customer (applicant organization)
```

## API Endpoints

### Trigger COI Detection

```http
POST /api/proposal-protected-calls/{uuid}/run-coi-detection/
```

Creates a `COIDetectionJob` and queues background processing.

### View Conflicts for a Call

```http
GET /api/proposal-protected-calls/{uuid}/conflicts/
```

Returns all detected conflicts for the call.

### Manage Individual Conflicts

```http
POST /api/conflicts-of-interest/{uuid}/dismiss/
POST /api/conflicts-of-interest/{uuid}/waive/
POST /api/conflicts-of-interest/{uuid}/recuse/
```

## Background Processing

COI detection runs as a Celery background task to handle large reviewer pools:

```python
@shared_task(name="waldur_mastermind.proposal.run_coi_detection")
def run_coi_detection(job_uuid: str):
    """
    Run automated COI detection for a call in the background.

    Processes all reviewer-proposal pairs and detects conflicts
    based on co-authorship, institutional affiliations, and named personnel.
    """
```

### Job States

| State | Description |
|-------|-------------|
| `PENDING` | Job created, waiting for worker |
| `RUNNING` | Detection in progress |
| `COMPLETED` | All pairs processed successfully |
| `FAILED` | Error occurred during processing |
| `CANCELLED` | Job cancelled by user |

### Progress Tracking

The job tracks progress during execution:

- `total_pairs`: Total reviewer-proposal pairs to check
- `processed_pairs`: Pairs checked so far
- `conflicts_found`: Number of conflicts detected

## Evidence Storage

Each detected conflict stores structured evidence:

```python
ConflictOfInterest:
├── evidence_description: str   # Human-readable description
├── evidence_data: JSON         # Structured evidence details
│   ├── shared_publications     # For co-authorship conflicts
│   ├── match_reason            # For named personnel conflicts
│   ├── affiliation_details     # For institutional conflicts
│   └── lookback_years          # Configuration used
├── detection_method: str       # automated/self_disclosed/reported
└── management_plan: str        # Required for waived conflicts
```

## Integration with Review Assignment

COI detection integrates with the review assignment workflow:

1. Before assigning reviewers, check for confirmed/recused conflicts
2. Reviewers with real conflicts are excluded from assignment pool
3. Reviewers with waived conflicts may be assigned with oversight

## Self-Disclosure

The system supports two types of self-disclosed conflicts:

### Periodic General Disclosures

Reviewers can submit periodic disclosure forms (annual, call-level) for general conflicts:

```http
POST /api/coi-disclosure-forms/
{
    "call": "<call-uuid>",
    "has_conflicts": true,
    "conflict_details": "I have consulting relationships with..."
}
```

These forms track general financial interests and relationships with `valid_until` expiry.

### Proposal-Specific Conflicts at Invitation Acceptance

When accepting a reviewer invitation, reviewers can optionally declare conflicts with specific proposals. This creates `ConflictOfInterest` records (not disclosure forms):

```http
POST /api/reviewer-invitations/{token}/accept/
{
    "declared_conflicts": [
        {
            "proposal_uuid": "<proposal-uuid>",
            "coi_type": "COAUTH_RECENT",
            "description": "I co-authored a paper with the PI in 2024"
        }
    ]
}
```

**Key differences from periodic disclosures:**

| Aspect | COIDisclosureForm | ConflictOfInterest (self-disclosed) |
|--------|-------------------|-------------------------------------|
| Scope | General/call-level | Specific proposal |
| Timing | Periodic/annual | At invitation acceptance |
| Fields | `valid_until`, `is_current` | `proposal`, `coi_type`, `severity` |
| Detection method | N/A | `self_disclosed` |

**Self-declared conflict workflow:**

1. Reviewer receives invitation with proposal list
2. Reviews proposals (based on `invitation_proposal_disclosure` setting)
3. Optionally declares conflicts with specific proposals
4. Accepts invitation (NOT blocked by declared conflicts)
5. Manager reviews self-declared conflicts via normal COI management

## Best Practices

1. **Run detection early**: Trigger COI detection as soon as the reviewer pool is finalized
2. **Review pending conflicts**: Don't leave conflicts in pending status
3. **Document waivers**: Always provide management plans for waived conflicts
4. **Update reviewer profiles**: Ensure reviewer publications and affiliations are current
5. **Configure appropriately**: Adjust lookback periods based on field norms

## Related Documentation

- [Proposals Overview](proposals.md) - Complete proposal module documentation
- [Reviewer-Proposal Matching](proposals-matching.md) - Affinity scoring and assignment algorithms
- [Review System](proposals.md#review-system-architecture) - Review assignment and scoring
