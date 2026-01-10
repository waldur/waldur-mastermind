# Reviewer-Proposal Matching System

The Waldur proposal module includes an automated reviewer-proposal matching system that computes expertise affinity scores and generates optimal reviewer assignments. This ensures qualified reviewers are matched with proposals in their area of expertise.

## Architecture Overview

```text
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                           Reviewer-Proposal Matching Architecture                                │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                   REVIEWER DISCOVERY                                             │
│                                                                                                 │
│  ┌─────────────────────┐      ┌─────────────────────┐      ┌─────────────────────┐            │
│  │  Published Profiles │─────▶│  Affinity Algorithm │─────▶│ ReviewerSuggestion  │            │
│  │  (is_published=true)│      │  compute_suggestions │      │    (pending)        │            │
│  └─────────────────────┘      └─────────────────────┘      └──────────┬──────────┘            │
│                                                                        │                       │
│                                              ┌────────────────┬────────┴────────┐              │
│                                              ▼                ▼                 ▼              │
│                                        ┌──────────┐    ┌──────────┐    ┌────────────┐        │
│                                        │ CONFIRMED│    │ REJECTED │    │  INVITED   │        │
│                                        │(approved)│    │(declined)│    │(pool added)│        │
│                                        └──────────┘    └──────────┘    └────────────┘        │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                │
                                                ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                   AFFINITY SCORING                                               │
│                                                                                                 │
│  ┌─────────────────────┐      ┌─────────────────────┐      ┌─────────────────────┐            │
│  │   Reviewer Profile  │      │  MatchingConfig     │      │     Proposals       │            │
│  │   • Expertise       │      │  • affinity_method  │      │   • Title           │            │
│  │   • Publications    │      │  • keyword_weight   │      │   • Summary         │            │
│  │   • Biography       │      │  • text_weight      │      │   • Description     │            │
│  └──────────┬──────────┘      └──────────┬──────────┘      └──────────┬──────────┘            │
│             │                            │                            │                        │
│             └────────────────────────────┼────────────────────────────┘                        │
│                                          ▼                                                     │
│                           ┌──────────────────────────────┐                                     │
│                           │     Affinity Computation     │                                     │
│                           │  ┌─────────┐   ┌─────────┐  │                                     │
│                           │  │ Keyword │ + │ TF-IDF  │  │                                     │
│                           │  │  Score  │   │  Score  │  │                                     │
│                           │  └─────────┘   └─────────┘  │                                     │
│                           └──────────────┬───────────────┘                                     │
│                                          ▼                                                     │
│                           ┌──────────────────────────────┐                                     │
│                           │  ReviewerProposalAffinity    │                                     │
│                           │  (cached score matrix)       │                                     │
│                           └──────────────────────────────┘                                     │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                │
                                                ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                   ASSIGNMENT ALGORITHMS                                          │
│                                                                                                 │
│  ┌─────────────────────┐      ┌─────────────────────┐      ┌─────────────────────┐            │
│  │      MinMax         │      │     FairFlow        │      │     Hungarian       │            │
│  │  (balanced load)    │      │ (quality threshold) │      │  (global optimum)   │            │
│  └─────────────────────┘      └─────────────────────┘      └─────────────────────┘            │
│                                          │                                                     │
│                                          ▼                                                     │
│                           ┌──────────────────────────────┐                                     │
│                           │    ProposedAssignment        │                                     │
│                           │  (reviewer → proposal)       │                                     │
│                           └──────────────────────────────┘                                     │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Affinity Scoring Methods

The system computes affinity scores between reviewers and proposals using configurable methods.

### Keyword-Based Scoring

Matches reviewer expertise keywords against proposal text content.

**How it works:**

1. Extracts reviewer's expertise keywords with proficiency weights
2. Searches proposal text (title, summary, description) for keyword matches
3. Calculates weighted match score

**Proficiency weights:**

| Proficiency Level | Weight |
|-------------------|--------|
| Expert | 1.0 |
| Familiar | 0.7 |
| Basic | 0.3 |

```python
# Example: Keyword affinity computation
reviewer_keywords = {
    "machine learning": 1.0,      # Expert
    "neural networks": 0.7,       # Familiar
    "data science": 0.3           # Basic
}

proposal_text = "Machine learning approaches for neural network optimization..."
# Matches: "machine learning" (1.0), "neural networks" (0.7)
# Score: (1.0 + 0.7) / (1.0 + 0.7 + 0.3) = 0.85
```

### TF-IDF Text Similarity

Computes semantic similarity between reviewer expertise and proposal content using Term Frequency-Inverse Document Frequency (TF-IDF) vectors.

**Reviewer text sources:**

- Expertise keywords (weighted by proficiency)
- Recent publication titles and abstracts (last 5 years)
- Biography text

**Proposal text sources:**

- Proposal name/title
- Project summary
- Project description

**Algorithm:**

1. Tokenize text (lowercase, remove stopwords)
2. Compute TF-IDF vectors for reviewer and proposal
3. Calculate cosine similarity between vectors

```python
# TF-IDF similarity example
reviewer_vector = {"machine": 0.3, "learning": 0.4, "neural": 0.2, ...}
proposal_vector = {"machine": 0.5, "learning": 0.3, "optimization": 0.4, ...}

# Cosine similarity = dot_product / (magnitude1 * magnitude2)
similarity = 0.72
```

### Combined Method

Default method that combines keyword and text-based scoring with configurable weights.

```python
affinity_score = (keyword_weight × keyword_score) + (text_weight × text_score)

# Default weights
keyword_weight = 0.4
text_weight = 0.6
```

## Matching Configuration

Each call can have its own matching configuration via `MatchingConfiguration`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `affinity_method` | choice | `combined` | Scoring method: `keyword`, `tfidf`, or `combined` |
| `keyword_weight` | float | 0.4 | Weight for keyword scoring (0-1) |
| `text_weight` | float | 0.6 | Weight for TF-IDF scoring (0-1) |
| `min_reviewers_per_proposal` | int | 3 | Minimum reviewers per proposal |
| `max_reviewers_per_proposal` | int | 5 | Maximum reviewers per proposal |
| `min_proposals_per_reviewer` | int | 3 | Minimum proposals per reviewer |
| `max_proposals_per_reviewer` | int | 10 | Maximum proposals per reviewer |
| `algorithm` | choice | `minmax` | Assignment algorithm |
| `min_affinity_threshold` | float | 0.1 | Minimum affinity for suggestions |
| `use_reviewer_bids` | bool | true | Consider reviewer preferences |
| `bid_weight` | float | 0.3 | Weight for reviewer bids |

**Validation:** `keyword_weight + text_weight` must equal 1.0

## Assignment Algorithms

Three algorithms are available for computing optimal reviewer-proposal assignments:

### MinMax (Balanced Load)

Balances reviewer workload while maximizing total affinity.

**Characteristics:**

- Prioritizes even distribution of reviews
- Good for calls with many proposals and limited reviewers
- Prevents reviewer overload

### FairFlow (Quality Threshold)

Ensures minimum quality threshold for all assignments.

**Characteristics:**

- Only assigns pairs above `min_affinity_threshold`
- Better match quality at cost of some assignments
- Useful for specialized domains

### Hungarian (Global Optimum)

Finds globally optimal assignment maximizing total affinity.

**Characteristics:**

- Optimal solution for the assignment problem
- May result in uneven workload distribution
- Best for small to medium-sized calls

## Reviewer Bids

Reviewers can express preferences for reviewing specific proposals:

| Bid Value | Weight | Description |
|-----------|--------|-------------|
| `eager` | +1.0 | Reviewer wants to review this proposal |
| `willing` | +0.5 | Reviewer is willing to review |
| `not_willing` | -0.5 | Reviewer prefers not to review |
| `conflict` | -1.0 | Reviewer has conflict of interest |

When `use_reviewer_bids` is enabled, bid weights are incorporated into the final affinity score:

```python
final_score = affinity_score + (bid_weight × bid_value)
```

## Reviewer Discovery Workflow

The system supports two paths for finding reviewers:

### Path A: Algorithm-Based Discovery

For reviewers with published profiles:

```text
1. Reviewer publishes profile
   ├── POST /api/reviewer-profiles/me/publish/
   └── Sets is_published=true, available_for_reviews=true

2. Manager triggers suggestion generation
   └── POST /api/proposal-protected-calls/{uuid}/generate-suggestions/

3. Algorithm evaluates all published profiles
   ├── Excludes reviewers already in pool
   ├── Excludes reviewers already suggested
   └── Creates ReviewerSuggestion records with affinity scores

4. Manager reviews suggestions
   ├── Confirm: POST /api/reviewer-suggestions/{uuid}/confirm/
   └── Reject: POST /api/reviewer-suggestions/{uuid}/reject/ (with reason)

5. Manager sends invitations to confirmed suggestions
   └── POST /api/proposal-protected-calls/{uuid}/send-invitations/

6. Reviewer views invitation details
   ├── GET /api/reviewer-invitations/{token}/
   └── Returns: call info, COI config, proposals (based on disclosure level)

7. Reviewer accepts/declines with optional COI disclosure
   ├── POST /api/reviewer-invitations/{token}/accept/
   │   └── Can include declared_conflicts for specific proposals
   └── POST /api/reviewer-invitations/{token}/decline/
```

### Path B: Direct Email Invitation

For reviewers without existing profiles:

```text
1. Manager invites by email
   └── POST /api/proposal-protected-calls/{uuid}/invite-by-email/

2. Invitation sent to email address
   └── CallReviewerPool created (reviewer=null, invited_email set)

3. Invited person clicks invitation link
   ├── GET /api/reviewer-invitations/{token}/
   └── Response indicates profile_status: "missing" or "unpublished"

4. Invited person creates and publishes profile
   ├── POST /api/reviewer-profiles/ (create)
   └── POST /api/reviewer-profiles/me/publish/ (publish)

5. Accept invitation with optional COI disclosure
   ├── POST /api/reviewer-invitations/{token}/accept/
   └── Profile automatically linked to CallReviewerPool
```

### Reviewer Profile Visibility

Reviewer profiles have visibility controls:

| Field | Default | Description |
|-------|---------|-------------|
| `is_published` | false | Profile discoverable by algorithm |
| `available_for_reviews` | true | Currently accepting review requests |
| `published_at` | null | When profile was published |

**Visibility rules:**

- **Own profile**: Always visible to the profile owner
- **Pool members**: Managers see ACCEPTED pool members only
- **Suggestions**: Managers see full profile in suggestion list
- **Discovery**: Algorithm only considers published + available profiles

## Suggestion Status Workflow

```text
   ┌─────────┐
   │ PENDING │◀──────────────── (algorithm generates)
   └────┬────┘
        │
        ├─────────────────────────────────────┐
        ▼                                     ▼
   ┌───────────┐                        ┌──────────┐
   │ CONFIRMED │                        │ REJECTED │
   │ (approved │                        │(declined)│
   │  by mgr)  │                        └──────────┘
   └─────┬─────┘
         │
         ▼
   ┌───────────┐
   │  INVITED  │
   │(invitation│
   │   sent)   │
   └───────────┘
```

## API Endpoints

### Affinity Computation

```http
POST /api/proposal-protected-calls/{uuid}/compute-affinities/
```

Computes affinity scores for all reviewer-proposal pairs in the call.

**Response:**

```json
{
  "affinities_computed": 150,
  "reviewers": 10,
  "proposals": 15
}
```

### Get Affinity Matrix

```http
GET /api/proposal-protected-calls/{uuid}/affinity-matrix/
```

Returns the complete affinity matrix for visualization.

**Response:**

```json
{
  "reviewers": [
    {"uuid": "...", "name": "Dr. Smith"}
  ],
  "proposals": [
    {"uuid": "...", "name": "Proposal A"}
  ],
  "matrix": [
    [0.85, 0.32, 0.67, ...]
  ]
}
```

### Generate Suggestions

```http
POST /api/proposal-protected-calls/{uuid}/generate-suggestions/
```

Runs affinity algorithm on all published profiles to generate reviewer suggestions.

**Response:**

```json
{
  "suggestions_created": 15,
  "reviewers_evaluated": 42,
  "suggestions": ["uuid1", "uuid2", "..."]
}
```

### View Suggestions

```http
GET /api/proposal-protected-calls/{uuid}/suggestions/
```

Lists all suggestions for a call with affinity scores.

**Filter parameters:**

- `status`: Filter by status (`pending`, `confirmed`, `rejected`, `invited`)
- `min_affinity_score`: Minimum affinity score (0-1)
- `reviewer_uuid`: Filter by specific reviewer

### Manage Suggestions

```http
POST /api/reviewer-suggestions/{uuid}/confirm/
POST /api/reviewer-suggestions/{uuid}/reject/
```

Manager confirms or rejects suggestions. Rejection requires a reason.

### Send Invitations

```http
POST /api/proposal-protected-calls/{uuid}/send-invitations/
```

Sends invitations to all confirmed suggestions.

### Invite by Email

```http
POST /api/proposal-protected-calls/{uuid}/invite-by-email/
```

Invites a reviewer by email address (profile not required initially).

**Request:**

```json
{
  "email": "reviewer@example.com",
  "invitation_message": "We invite you to review...",
  "max_assignments": 5
}
```

### View Invitation Details

```http
GET /api/reviewer-invitations/{token}/
```

Returns invitation details for reviewers to review before accepting.

**Response:**

```json
{
  "call": {
    "uuid": "...",
    "name": "2025 Spring HPC Allocation Call",
    "description": "..."
  },
  "invitation_status": "pending",
  "profile_status": "published",
  "requires_profile": false,
  "coi_configuration": {
    "recusal_required_types": ["REL_FAMILY", "FIN_DIRECT"],
    "management_allowed_types": ["COLLAB_ACTIVE", "COAUTH_RECENT"],
    "disclosure_only_types": ["INST_SAME"],
    "proposal_disclosure_level": "titles_and_summaries"
  },
  "coi_types": [["ROLE_NAMED", "Named in proposal"], ...],
  "proposals": [
    {"uuid": "...", "name": "Quantum Computing Research", "summary": "..."}
  ]
}
```

### Accept Invitation

```http
POST /api/reviewer-invitations/{token}/accept/
```

Accepts an invitation with optional COI self-declaration.

**Request:**

```json
{
  "declared_conflicts": [
    {
      "proposal_uuid": "<uuid>",
      "coi_type": "COAUTH_RECENT",
      "severity": "apparent",
      "description": "Co-authored 2 papers with PI in 2024"
    }
  ]
}
```

**Notes:**

- `declared_conflicts` is optional
- Creates `ConflictOfInterest` records with `detection_method='self_disclosed'`
- Acceptance NOT blocked by declared conflicts (manager handles via COI workflow)
- For email invitations, requires published profile first

### Decline Invitation

```http
POST /api/reviewer-invitations/{token}/decline/
```

Declines a reviewer invitation.

**Request:**

```json
{
  "reason": "Schedule conflict during review period"
}
```

### Manage Reviewer Bids

```http
GET /api/reviewer-bids/
POST /api/reviewer-bids/
PATCH /api/reviewer-bids/{uuid}/
```

Manage reviewer preferences for proposals.

## Data Models

### ReviewerProposalAffinity

Cached affinity scores between reviewers and proposals.

| Field | Type | Description |
|-------|------|-------------|
| `call` | FK | Call for this affinity |
| `reviewer` | FK | Reviewer profile |
| `proposal` | FK | Proposal |
| `affinity_score` | float | Combined score (0-1) |
| `keyword_score` | float | Keyword-based score |
| `text_score` | float | TF-IDF score |

### ReviewerSuggestion

Algorithm-generated reviewer suggestions.

| Field | Type | Description |
|-------|------|-------------|
| `call` | FK | Call for suggestion |
| `reviewer` | FK | Suggested reviewer profile |
| `affinity_score` | float | Combined score (0-1) |
| `keyword_score` | float | Keyword match score |
| `text_score` | float | Text similarity score |
| `status` | choice | pending/confirmed/rejected/invited |
| `reviewed_by` | FK | Manager who reviewed |
| `reviewed_at` | datetime | When reviewed |
| `rejection_reason` | text | Reason for rejection |

### ProposedAssignment

Final reviewer assignments from matching algorithm.

| Field | Type | Description |
|-------|------|-------------|
| `call` | FK | Call for assignment |
| `reviewer` | FK | Assigned reviewer |
| `proposal` | FK | Assigned proposal |
| `affinity_score` | float | Score at assignment time |
| `algorithm_used` | choice | minmax/fairflow/hungarian |
| `rank` | int | Assignment priority (1=best) |
| `is_deployed` | bool | Assignment finalized |
| `deployed_at` | datetime | When deployed |
| `deployed_by` | FK | Who deployed |

### ReviewerBid

Reviewer preferences for proposals.

| Field | Type | Description |
|-------|------|-------------|
| `call` | FK | Call |
| `reviewer` | FK | Reviewer profile |
| `proposal` | FK | Proposal |
| `bid` | choice | eager/willing/not_willing/conflict |
| `comment` | text | Optional explanation |

## Integration with COI Detection

The matching system integrates with [Conflict of Interest Detection](proposals-coi.md):

1. Before computing suggestions, COI status is checked
2. Reviewers with confirmed COIs are excluded from matching
3. Self-disclosed conflicts (via bids) affect affinity scores
4. Waived conflicts may still be assigned with oversight

```text
┌──────────────────────────────────────────────────────────────────────────┐
│                    Matching + COI Integration                             │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────────────┐ │
│  │  Affinity   │───▶│ COI Filter  │───▶│ Final Suggestions/Assign.  │ │
│  │ Computation │    │ (exclude    │    │ (COI-free reviewers only)   │ │
│  └─────────────┘    │  conflicts) │    └─────────────────────────────┘ │
│                     └─────────────┘                                     │
│                                                                          │
│  Excluded from matching:                                                 │
│  • CONFIRMED conflicts                                                   │
│  • RECUSED reviewers                                                     │
│  • Reviewers with bid="conflict"                                         │
│                                                                          │
│  May be assigned with oversight:                                         │
│  • WAIVED conflicts (with management plan)                               │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

## Performance Considerations

### Affinity Computation

- Computation is O(reviewers × proposals)
- Results cached in `ReviewerProposalAffinity` table
- Recompute when profiles or proposals change significantly

### Corpus IDF

- TF-IDF uses corpus-wide IDF for better term weighting
- Corpus includes all reviewer texts and proposal texts
- Computed once per call, reused for all pairs

### Large Calls

- For calls with 100+ proposals and 50+ reviewers:
  - Use batch processing
  - Consider incremental updates
  - Monitor computation time

## Related Documentation

- [Proposals Overview](proposals.md) - Complete proposal module documentation
- [Conflict of Interest Detection](proposals-coi.md) - COI detection and management
- [Review System](proposals.md#review-system-architecture) - Review assignment and scoring
