# Core Checklists

The core checklist module provides a flexible questionnaire system that enables organizations to manage various types of compliance and metadata requirements through customizable questionnaires with conditional logic and review workflows.

## Overview

The checklist system is designed as an extendable staff-configured metadata schema
to be used in different scenarios, for example:

- **Project Metadata**: Extendable schema for project metadata
- **Project Compliance**: Ensures projects meet organizational standards
- **Proposal Compliance**: Validates proposals before submission
- **Offering Compliance**: Verifies marketplace offerings meet requirements

## Core Models

### Category

Groups checklists by category with icon support for UI display. Categories provide organizational structure for managing different types of compliance checklists.

### Checklist

Main container for compliance questions. Each checklist has a type (project/proposal/offering compliance/project metadata) and contains an ordered set of questions that users must complete.

**Key features:**

- Type-based categorization (project_compliance, proposal_compliance, offering_compliance, project_metadata)
- Dynamic question visibility based on user context and dependencies
- Optional category grouping for UI organization
- Timestamped for audit trail

### Question

Individual questions with configurable types, optional images, ordering, and review trigger logic based on answer values.

**Question Types:**

- **Boolean**: Yes/No/N/A responses
- **Single Select**: Choose one option from a list
- **Multi Select**: Choose multiple options from a list
- **Text Input**: Short text responses
- **Text Area**: Long text responses
- **Number**: Numeric input
- **Date**: Date selection
- **File**: File upload (planned)

**Features:**

- Conditional visibility based on dependencies
- Review triggering based on answer values
- Required/optional questions
- Image support for visual questions
- Ordered display

### QuestionOption

Multiple choice options for select-type questions with ordering support. Provides the available choices for single-select and multi-select questions.

### QuestionDependency

Conditional visibility logic - questions can depend on other questions' answers with circular dependency prevention. This enables dynamic questionnaires that adapt based on user responses.

**Operators supported:**

- `equals`: Exact match
- `not_equals`: Not equal to
- `contains`: Text contains substring
- `in`: Value in list
- `not_in`: Value not in list

### ChecklistCompletion

Generic completion tracking model that links checklists to any domain object (proposals, projects, etc.) using Django's generic foreign key system.

**Features:**

- Generic foreign key to any model (scope)
- Completion status tracking
- Review requirement detection
- Reviewer assignment and notes
- Completion percentage calculation

### Answer

User responses linked to ChecklistCompletion objects, stored as JSON with automatic review flagging and reviewer tracking.

**Features:**

- Flexible JSON storage for different answer types
- Automatic review requirement detection based on question configuration
- Review workflow with reviewer assignment and notes
- Audit trail with timestamps
- Answer validation based on question type
- Unique constraints per completion/question/user

## API Endpoints

### Core Endpoints

- `GET /api/marketplace-checklists-categories/` - List checklist categories
- `GET /api/marketplace-checklists-categories/{uuid}/` - Category details

### Admin Endpoints (Staff Only)

- `GET /api/marketplace-checklists-admin/` - List checklists (staff only)
- `POST /api/marketplace-checklists-admin/` - Create checklist (staff only)
- `GET /api/marketplace-checklists-admin/{uuid}/` - Checklist details (staff only)
- `PUT/PATCH /api/marketplace-checklists-admin/{uuid}/` - Update checklist (staff only)
- `DELETE /api/marketplace-checklists-admin/{uuid}/` - Delete checklist (staff only)
- `GET /api/marketplace-checklists-admin/{uuid}/questions/` - List checklist questions (staff only)

- `GET /api/marketplace-checklists-admin-questions/` - List all questions (staff only)
- `POST /api/marketplace-checklists-admin-questions/` - Create question (staff only)
- `GET /api/marketplace-checklists-admin-questions/{uuid}/` - Question details (staff only)
- `PUT/PATCH /api/marketplace-checklists-admin-questions/{uuid}/` - Update question (staff only)
- `DELETE /api/marketplace-checklists-admin-questions/{uuid}/` - Delete question (staff only)

- `GET /api/marketplace-checklists-admin-question-options/` - List question options (staff only)
- `POST /api/marketplace-checklists-admin-question-options/` - Create option (staff only)
- Full CRUD operations on question options and dependencies

### Integration via ViewSet Mixins

The core checklist module provides ViewSet mixins for integration into other apps:

**UserChecklistMixin** - For end users filling checklists:

- `GET /{app}/{uuid}/checklist/` - Get checklist questions with user's answers
- `GET /{app}/{uuid}/completion_status/` - Get completion status
- `POST /{app}/{uuid}/submit_answers/` - Submit answers

**ReviewerChecklistMixin** - For reviewers (with sensitive review logic):

- `GET /{app}/{uuid}/checklist_review/` - Get full checklist with review triggers
- `GET /{app}/{uuid}/completion_review_status/` - Get completion with review details

Examples:

- `GET /api/proposals/{uuid}/checklist/` - Get proposal checklist
- `POST /api/proposals/{uuid}/submit_answers/` - Submit proposal answers
- `GET /api/proposals/{uuid}/checklist_review/` - Review proposal checklist (reviewers only)

## Question Dependencies

The system supports sophisticated conditional logic through question dependencies:

1. **Simple Dependencies**: Show Question B only if Question A equals specific value
2. **Complex Dependencies**: Multiple conditions with different operators
3. **Circular Prevention**: Automatic detection and prevention of circular dependencies
4. **Dynamic Visibility**: Real-time question showing/hiding based on current answers

Example: A security questionnaire might only show cloud-specific questions if the user indicates they use cloud services.

## Review Workflow

Questions can be configured to trigger reviews based on answers:

1. **Automatic Review Triggers**: Specific answer values trigger review requirements
2. **Always Review**: Questions that always require review regardless of answer
3. **Review Assignment**: Staff can be assigned to review flagged answers
4. **Review Notes**: Internal notes and approval tracking

## Permission System

Access control is implemented through:

- **Staff Administration**: Direct checklist management restricted to staff users
- **App-level Integration**: Checklist access controlled via host application permissions
- **Mixin-based Permissions**: Apps define their own permission requirements for checklist actions
- **Review Segregation**: Separate permissions for users vs reviewers to hide sensitive review logic

## Validation and Data Integrity

The system includes comprehensive validation:

- **Answer Type Validation**: Ensures answers match expected question types
- **Required Question Enforcement**: Prevents submission of incomplete required questions
- **UUID Validation**: Proper UUID format checking for references
- **Circular Dependency Prevention**: Automatic detection of invalid dependency chains

## Integration with Waldur Apps

The checklist system integrates with various Waldur applications:

- **Generic Foreign Key System**: Can be attached to any Django model (proposals, projects, resources, etc.)
- **ViewSet Mixins**: Easy integration through `UserChecklistMixin` and `ReviewerChecklistMixin`
- **Flexible Completion Tracking**: Each integration controls its own completion lifecycle
- **Permission Delegation**: Host applications define appropriate permission checks

## Usage Patterns

### Basic Integration Flow

1. **Admin Setup**: Staff creates checklists with questions, dependencies, and review triggers
2. **App Integration**: Host app (e.g., proposals) creates `ChecklistCompletion` objects linking checklists to domain objects
3. **User Interaction**: End users access checklists through app-specific endpoints using `UserChecklistMixin`
4. **Answer Submission**: Users submit answers, triggering automatic completion status updates
5. **Review Process**: Reviewers access full checklist information through `ReviewerChecklistMixin`
6. **Completion Tracking**: Host apps monitor completion status and take appropriate actions

### Example Integration (Proposals)

```python
# proposals/models.py
class Proposal(models.Model):
    # ... other fields
    checklist_completion = models.OneToOneField(
        ChecklistCompletion,
        on_delete=models.CASCADE,
        null=True, blank=True
    )

# proposals/views.py
class ProposalViewSet(UserChecklistMixin, ReviewerChecklistMixin, ActionsViewSet):
    # User permissions
    checklist_permissions = [permission_factory(PermissionEnum.MANAGE_PROPOSAL)]
    submit_answers_permissions = [permission_factory(PermissionEnum.MANAGE_PROPOSAL)]

    # Reviewer permissions
    checklist_review_permissions = [permission_factory(PermissionEnum.REVIEW_PROPOSALS)]
```

## Technical Implementation

The module follows Waldur's standard architecture patterns:

- **Django Models**: Standard ORM with mixins (UuidMixin, DescribableMixin, TimeStampedModel)
- **Generic Foreign Keys**: Flexible linking to any Django model through ChecklistCompletion
- **DRF Serializers**: REST API serialization with context-aware field exposure
- **ViewSet Mixins**: Reusable mixins for consistent integration across applications
- **Admin-Only Core APIs**: Direct checklist management restricted to staff
- **Permissions**: Delegated to host applications with mixin-based controls
- **Filtering**: Advanced filtering for admin interfaces
- **Validation**: Answer validation based on question types and business rules

### Architecture Principles

- **Separation of Concerns**: Core checklist logic separated from app-specific business logic
- **Flexible Integration**: Generic foreign keys allow attachment to any model
- **Security by Design**: Review logic hidden from users, exposed only to authorized reviewers
- **Extensible Question Types**: Support for multiple answer formats with validation
- **Dependency Management**: Sophisticated conditional logic with circular prevention

The system is designed for scalability and extensibility, supporting complex compliance scenarios while maintaining ease of integration for host applications.
