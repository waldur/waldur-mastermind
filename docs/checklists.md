# Marketplace Checklists

The marketplace checklist module provides a comprehensive compliance questionnaire system that enables organizations to manage various types of compliance requirements through customizable questionnaires with conditional logic and review workflows.

## Overview

The checklist system is designed for three main compliance types:

- **Project Compliance**: Ensures projects meet organizational standards
- **Proposal Compliance**: Validates proposals before submission
- **Offering Compliance**: Verifies marketplace offerings meet requirements

## Core Models

### Category

Groups checklists by category with icon support for UI display. Categories provide organizational structure for managing different types of compliance checklists.

### Checklist

Main container for compliance questions, associated with specific customers and user roles. Each checklist has a type (project/proposal/offering compliance) and contains an ordered set of questions that users must complete.

**Key features:**

- Customer and role-based access control
- Dynamic question visibility based on user context
- Categorization for better organization
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
- Solution guidance for complex questions
- Marketplace category association

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

### Answer

User responses stored as JSON with automatic review flagging, reviewer tracking, and unique user-question constraints.

**Features:**

- Flexible JSON storage for different answer types
- Automatic review requirement detection
- Review workflow with reviewer assignment
- Audit trail with timestamps
- Answer validation based on question type

## API Endpoints

### Public Endpoints

- `GET /api/marketplace-checklists/categories/` - List checklist categories
- `GET /api/marketplace-checklists/categories/{uuid}/` - Category details
- `GET /api/marketplace-checklists/categories/{uuid}/checklists/` - Checklists in category
- `GET /api/marketplace-checklists/checklists/` - List accessible checklists
- `GET /api/marketplace-checklists/checklists/{uuid}/` - Checklist details
- `GET /api/marketplace-checklists/checklists/{uuid}/questions/` - Get visible questions
- `GET /api/marketplace-checklists/checklists/{uuid}/answers/` - User's answers
- `POST /api/marketplace-checklists/checklists/{uuid}/answers/` - Submit answers

### Admin Endpoints (Staff Only)

- Full CRUD operations on all models
- Bulk answer submission on behalf of users
- Statistical reporting and analytics

### Statistics Endpoints

- `GET /api/marketplace-checklists/stats/{checklist_uuid}/` - Global compliance stats
- `GET /api/marketplace-checklists/projects/{uuid}/stats/` - Project compliance overview
- `GET /api/marketplace-checklists/customers/{uuid}/checklists/{uuid}/stats/` - Customer stats
- `GET /api/marketplace-checklists/users/{uuid}/stats/` - User completion statistics

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

- **Customer Association**: Checklists can be restricted to specific customers
- **Role-based Access**: Questions visible only to users with specific roles
- **Staff Override**: Staff users can view all checklists and submit answers on behalf of others
- **Project-level Permissions**: Integration with Waldur's project permission system

## Validation and Data Integrity

The system includes comprehensive validation:

- **Answer Type Validation**: Ensures answers match expected question types
- **Required Question Enforcement**: Prevents submission of incomplete required questions
- **UUID Validation**: Proper UUID format checking for references
- **Circular Dependency Prevention**: Automatic detection of invalid dependency chains

## Integration with Waldur Marketplace

The checklist system integrates deeply with Waldur's marketplace:

- Questions can be associated with marketplace categories
- Compliance scores feed into project and customer dashboards
- Integration with user roles and permissions
- Statistical reporting for compliance monitoring

## Usage Patterns

### Basic Questionnaire Flow

1. Admin creates checklist with questions
2. Questions are assigned to customer/role combinations
3. Users access relevant checklists based on permissions
4. Users complete questions, with conditional questions appearing dynamically
5. Answers requiring review are flagged automatically
6. Staff review flagged answers and provide approval/feedback

### Compliance Monitoring

1. Organizations track completion rates across projects/customers
2. Statistical dashboards show compliance scores
3. Incomplete questionnaires are identified for follow-up
4. Review workflows ensure quality control

## Technical Implementation

The module follows Waldur's standard architecture patterns:

- **Django Models**: Standard ORM with mixins for common functionality
- **DRF Serializers**: REST API serialization with validation
- **ViewSets**: Standard CRUD operations with custom actions
- **Permissions**: Integration with Waldur's permission system
- **Filtering**: Advanced filtering for admin interfaces
- **Validation**: Comprehensive data validation and business rules

The system is designed for scalability and extensibility, supporting complex compliance scenarios while maintaining ease of use for basic questionnaires.
