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

Individual questions with configurable types, ordering, conditional user guidance, and review trigger logic based on answer values.

**Question Types:**

- **Boolean**: Yes/No/N/A responses
- **Single Select**: Choose one option from a list
- **Multi Select**: Choose multiple options from a list
- **Text Input**: Short text responses
- **Text Area**: Long text responses
- **Number**: Numeric input with optional min/max validation constraints
- **Date**: Date selection
- **File**: File upload (planned)

**Features:**

- Conditional visibility based on dependencies
- Review triggering based on answer values
- Conditional user guidance display
- Required/optional questions
- Ordered display

#### NUMBER Question Type Validation

NUMBER type questions support optional validation constraints for form generation and server-side validation:

- **min_value**: Minimum allowed numeric value (decimal field with 4 decimal places)
- **max_value**: Maximum allowed numeric value (decimal field with 4 decimal places)
- **Validation**: Server-side validation rejects answers outside the specified range
- **UI Integration**: Min/max values are exposed through serializers for client-side form constraints
- **Format Support**: Accepts both integer and floating-point numbers

**Example API Usage:**

```http
POST /api/checklists-admin-questions/
Content-Type: application/json

{
  "description": "Enter project budget (in thousands)",
  "question_type": "number",
  "checklist": "http://localhost:8000/api/checklists-admin/{checklist_uuid}/",
  "required": true,
  "min_value": "1.0",
  "max_value": "10000.0",
  "user_guidance": "Budget should be in thousands of dollars (e.g., 100 for $100,000)",
  "order": 1
}
```

**Validation Scenarios:**

- Budget ranges (e.g., $1K - $10M)
- Percentages (0-100)
- Age ranges (18-100)
- Scientific measurements with decimal precision
- Counts and quantities with natural limits

### QuestionOption

Multiple choice options for select-type questions with ordering support. Provides the available choices for single-select and multi-select questions.

### QuestionDependency

Conditional visibility logic - questions can depend on other questions' answers with circular dependency prevention. This enables dynamic questionnaires that adapt based on user responses.

**Operators supported:**

- `equals`: Exact match
- `not_equals`: Not equal to
- `contains`: Text contains substring
- `in`: Value exists in list
- `not_in`: Value does not exist in list

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

- `GET /api/checklists-admin-categories/` - List checklist categories
- `GET /api/checklists-admin-categories/{uuid}/` - Category details

### Admin Endpoints (Staff Only)

- `GET /api/checklists-admin/` - List checklists (staff only)
- `POST /api/checklists-admin/` - Create checklist (staff only)
- `GET /api/checklists-admin/{uuid}/` - Checklist details (staff only)
- `PUT/PATCH /api/checklists-admin/{uuid}/` - Update checklist (staff only)
- `DELETE /api/checklists-admin/{uuid}/` - Delete checklist (staff only)
- `GET /api/checklists-admin/{uuid}/questions/` - List checklist questions (staff only)

- `GET /api/checklists-admin-questions/` - List all questions (staff only)
- `POST /api/checklists-admin-questions/` - Create question (staff only)
- `GET /api/checklists-admin-questions/{uuid}/` - Question details (staff only)
- `PUT/PATCH /api/checklists-admin-questions/{uuid}/` - Update question (staff only)
- `DELETE /api/checklists-admin-questions/{uuid}/` - Delete question (staff only)

- `GET /api/checklists-admin-question-options/` - List question options (staff only)
- `POST /api/checklists-admin-question-options/` - Create option (staff only)
- `GET /api/checklists-admin-question-dependencies/` - List question dependencies (staff only)
- `POST /api/checklists-admin-question-dependencies/` - Create question dependency (staff only)
- Full CRUD operations on question options and dependencies

### Integration via ViewSet Mixins

The core checklist module provides ViewSet mixins for integration into other apps:

**UserChecklistMixin** - For end users filling checklists:

- `GET /{app}/{uuid}/checklist/` - Get checklist questions with user's answers
- `GET /{app}/{uuid}/completion_status/` - Get completion status
- `POST /{app}/{uuid}/submit_answers/` - Submit answers (including answer removal)

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

## Answer Management

### Answer Submission and Updates

Users can submit, update, and remove answers through the `submit_answers` endpoint:

```http
POST /api/{app}/{uuid}/submit_answers/
Content-Type: application/json

[
  {
    "question_uuid": "123e4567-e89b-12d3-a456-426614174000",
    "answer_data": "New answer value"
  },
  {
    "question_uuid": "456e7890-e12b-34c5-d678-901234567890",
    "answer_data": null  // Remove existing answer
  }
]
```

### Answer Removal

Users can remove their answers by submitting `null` as the `answer_data` value. This performs a hard deletion of the answer record and automatically:

- **Recalculates completion percentage** - Removed answers no longer count toward completion
- **Updates completion status** - Required questions with removed answers mark checklist as incomplete
- **Updates review requirements** - Removing answers that triggered reviews clears the review flag
- **Maintains audit trail** - Through Answer model timestamps before deletion

**Key Features:**

- **Safe operations**: Attempting to remove non-existent answers succeeds without errors
- **Mixed operations**: Single request can create, update, and remove answers simultaneously
- **Validation bypass**: Null values skip validation since they indicate removal intent
- **Status synchronization**: Completion and review status automatically updated after changes

**Example - Mixed Operations:**

```http
POST /api/proposals/{uuid}/submit_answers/
[
  {"question_uuid": "q1-uuid", "answer_data": true},        // Create/update
  {"question_uuid": "q2-uuid", "answer_data": null},        // Remove
  {"question_uuid": "q3-uuid", "answer_data": "New text"}   // Create/update
]
```

## Review Workflow

Questions can be configured to trigger reviews based on answers:

1. **Automatic Review Triggers**: Specific answer values trigger review requirements
2. **Always Review**: Questions that always require review regardless of answer
3. **Review Assignment**: Staff can be assigned to review flagged answers
4. **Review Notes**: Internal notes and approval tracking

## Configuring Conditional Visibility via REST API

The checklist system supports sophisticated conditional logic through two mechanisms: **Question Dependencies** (for question visibility) and **Conditional User Guidance** (for guidance text display). Both use the same flexible operator-based system.

### Supported Operators

All conditional logic supports these operators, with specific question type compatibility:

- `equals` - Exact match
  - **Compatible with**: NUMBER, DATE, BOOLEAN question types
  - **Example**: Check if boolean answer is `true`, or if number equals `100`

- `not_equals` - Not equal to
  - **Compatible with**: NUMBER, DATE, BOOLEAN question types
  - **Example**: Check if boolean answer is not `false`, or if number is not `0`

- `contains` - Text contains substring
  - **Compatible with**: TEXT_INPUT, TEXT_AREA question types
  - **Example**: Check if text answer contains "sensitive" or "export"
  - **Note**: Case-sensitive matching

- `in` - Value exists in list
  - **Compatible with**: SINGLE_SELECT, MULTI_SELECT question types
  - **Example**: Check if selected option is one of `["high", "critical", "urgent"]`
  - **Note**: For single-select, checks if the selected value is in the condition list
  - **Note**: For multi-select, checks if any selected value is in the condition list

- `not_in` - Value does not exist in list
  - **Compatible with**: SINGLE_SELECT, MULTI_SELECT question types
  - **Example**: Check if selected option is not one of `["low", "minimal"]`
  - **Note**: For single-select, checks if the selected value is not in the condition list
  - **Note**: For multi-select, checks if none of the selected values are in the condition list

### Question Dependencies (Conditional Visibility)

Configure questions to show/hide based on answers to other questions.

#### Creating a Question Dependency

```http
POST /api/checklists-admin-question-dependencies/
Content-Type: application/json

{
  "question": "http://localhost:8000/api/checklists-admin-questions/{dependent_question_uuid}/",
  "depends_on_question": "http://localhost:8000/api/checklists-admin-questions/{trigger_question_uuid}/",
  "required_answer_value": "yes",
  "operator": "equals"
}
```

#### Example Scenarios

**1. Show cloud questions only if user selects "cloud" deployment:**

```http
POST /api/checklists-admin-question-dependencies/
{
  "question": "http://localhost:8000/api/checklists-admin-questions/{cloud_provider_question_uuid}/",
  "depends_on_question": "http://localhost:8000/api/checklists-admin-questions/{deployment_type_question_uuid}/",
  "required_answer_value": "cloud",
  "operator": "equals"
}
```

**2. Show security questions if user indicates sensitive data:**

```http
POST /api/checklists-admin-question-dependencies/
{
  "question": "http://localhost:8000/api/checklists-admin-questions/{security_measures_question_uuid}/",
  "depends_on_question": "http://localhost:8000/api/checklists-admin-questions/{has_sensitive_data_question_uuid}/",
  "required_answer_value": true,
  "operator": "equals"
}
```

**3. Show budget questions for high-value options:**

```http
POST /api/checklists-admin-question-dependencies/
{
  "question": "http://localhost:8000/api/checklists-admin-questions/{budget_approval_question_uuid}/",
  "depends_on_question": "http://localhost:8000/api/checklists-admin-questions/{project_category_question_uuid}/",
  "required_answer_value": ["enterprise", "large_scale"],
  "operator": "in"
}
```

### Conditional User Guidance

Configure guidance text to appear based on user answers.

#### Creating a Question with Always-Visible Guidance

```http
POST /api/checklists-admin-questions/
Content-Type: application/json

{
  "description": "Does your project handle personal data?",
  "question_type": "boolean",
  "checklist": "http://localhost:8000/api/checklists-admin/{checklist_uuid}/",
  "user_guidance": "Personal data includes names, emails, addresses, and any identifiable information.",
  "always_show_guidance": true,
  "order": 1,
  "required": true
}
```

#### Creating a Question with Conditional Guidance

```http
POST /api/checklists-admin-questions/
Content-Type: application/json

{
  "description": "What type of deployment will you use?",
  "question_type": "single_select",
  "checklist": "http://localhost:8000/api/checklists-admin/{checklist_uuid}/",
  "user_guidance": "Since you selected cloud deployment, ensure you have reviewed our cloud security guidelines and compliance requirements.",
  "always_show_guidance": false,
  "guidance_answer_value": "cloud",
  "guidance_operator": "equals",
  "order": 2,
  "required": true
}
```

#### Updating Conditional Guidance

```http
PATCH /api/checklists-admin-questions/{question_uuid}/
Content-Type: application/json

{
  "user_guidance": "Updated guidance text for enterprise projects",
  "always_show_guidance": false,
  "guidance_answer_value": "enterprise",
  "guidance_operator": "equals"
}
```

#### Example Scenarios

**1. Show compliance guidance only for "Yes" answers:**

```http
POST /api/checklists-admin-questions/
{
  "description": "Will you be processing EU citizen data?",
  "question_type": "boolean",
  "user_guidance": "Since you're processing EU data, you must comply with GDPR requirements. Please review our GDPR compliance checklist.",
  "always_show_guidance": false,
  "guidance_answer_value": true,
  "guidance_operator": "equals"
}
```

**2. Show warning guidance for multiple selections:**

```http
POST /api/checklists-admin-questions/
{
  "description": "Which data types will you collect?",
  "question_type": "multi_select",
  "user_guidance": "You've selected multiple sensitive data types. Additional security measures and approvals may be required.",
  "always_show_guidance": false,
  "guidance_answer_value": ["personal_data", "financial_data", "health_data"],
  "guidance_operator": "in"
}
```

**3. Show budget guidance for high-value project categories:**

```http
POST /api/checklists-admin-questions/
{
  "description": "What is your project category?",
  "question_type": "single_select",
  "user_guidance": "Enterprise and large-scale projects require additional financial approvals. Please prepare detailed budget documentation.",
  "always_show_guidance": false,
  "guidance_answer_value": ["enterprise", "large_scale"],
  "guidance_operator": "in"
}
```

### Complex Scenarios

#### Multi-Level Dependencies

Create cascading question visibility:

```http
# First level: Show cloud questions if deployment is cloud
POST /api/checklists-admin-question-dependencies/
{
  "question": "http://localhost:8000/api/checklists-admin-questions/{cloud_provider_question_uuid}/",
  "depends_on_question": "http://localhost:8000/api/checklists-admin-questions/{deployment_type_question_uuid}/",
  "required_answer_value": "cloud",
  "operator": "equals"
}

# Second level: Show AWS-specific questions if provider is AWS
POST /api/checklists-admin-question-dependencies/
{
  "question": "http://localhost:8000/api/checklists-admin-questions/{aws_region_question_uuid}/",
  "depends_on_question": "http://localhost:8000/api/checklists-admin-questions/{cloud_provider_question_uuid}/",
  "required_answer_value": "aws",
  "operator": "equals"
}
```

#### Combined Review Triggers and Guidance

Configure a question that both shows guidance and triggers reviews:

```http
POST /api/checklists-admin-questions/
{
  "description": "Does your application handle financial transactions?",
  "question_type": "boolean",
  "required": true,

  // Conditional guidance
  "user_guidance": "Financial transaction handling requires PCI DSS compliance and additional security reviews.",
  "always_show_guidance": false,
  "guidance_answer_value": true,
  "guidance_operator": "equals",

  // Review trigger (same condition)
  "review_answer_value": true,
  "operator": "equals",
  "always_requires_review": false
}
```

### API Response Examples

When questions are retrieved through user-facing endpoints, conditional logic is automatically applied:

**Question with visible guidance:**

```json
{
  "uuid": "123e4567-e89b-12d3-a456-426614174000",
  "description": "What is your deployment type?",
  "question_type": "single_select",
  "user_guidance": "Since you selected cloud deployment, review our cloud security guidelines.",
  "existing_answer": {
    "answer_data": "cloud"
  },
  "question_options": [
    {"uuid": "...", "label": "On-premise", "order": 1},
    {"uuid": "...", "label": "Cloud", "order": 2},
    {"uuid": "...", "label": "Hybrid", "order": 3}
  ]
}
```

**Question with hidden guidance (condition not met):**

```json
{
  "uuid": "123e4567-e89b-12d3-a456-426614174000",
  "description": "What is your deployment type?",
  "question_type": "single_select",
  "user_guidance": null,
  "existing_answer": {
    "answer_data": "on-premise"
  },
  "question_options": [...]
}
```

## Configuring Review Triggers and User Guidance

Beyond conditional visibility, questions can be configured with **review triggers** (to flag answers for staff review) and **conditional user guidance** (to show context-sensitive help text). Both features use the same operator system for maximum flexibility.

### Review Trigger Configuration

Review triggers automatically flag specific answers for staff review, enabling compliance workflows and quality control.

#### Basic Review Trigger Setup

**1. Always Require Review:**

```http
POST /api/checklists-admin-questions/
Content-Type: application/json

{
  "description": "Will this project involve processing personal data?",
  "question_type": "boolean",
  "checklist": "http://localhost:8000/api/checklists-admin/{checklist_uuid}/",
  "required": true,
  "always_requires_review": true,
  "order": 1
}
```

**2. Conditional Review Trigger:**

```http
POST /api/checklists-admin-questions/
Content-Type: application/json

{
  "description": "What type of data will you be processing?",
  "question_type": "multi_select",
  "checklist": "http://localhost:8000/api/checklists-admin/{checklist_uuid}/",
  "required": true,
  "always_requires_review": false,
  "review_answer_value": ["personal_data", "financial_data", "health_data"],
  "operator": "in",
  "order": 2
}
```

#### Review Trigger Scenarios

**1. Security Review for High-Risk Projects:**

```http
POST /api/checklists-admin-questions/
{
  "description": "What is your project's risk level?",
  "question_type": "single_select",
  "review_answer_value": ["high", "critical"],
  "operator": "in",
  "always_requires_review": false
}
```

**2. Budget Review for Large Expenditures:**

```http
POST /api/checklists-admin-questions/
{
  "description": "Select your budget range:",
  "question_type": "single_select",
  "review_answer_value": "over_100k",
  "operator": "equals",
  "always_requires_review": false
}
```

**3. Compliance Review for Specific Text Content:**

```http
POST /api/checklists-admin-questions/
{
  "description": "Describe your data handling procedures:",
  "question_type": "text_area",
  "review_answer_value": "export",
  "operator": "contains",
  "always_requires_review": false
}
```

**4. Multiple Review Conditions:**

```http
POST /api/checklists-admin-questions/
{
  "description": "Which compliance frameworks apply?",
  "question_type": "multi_select",
  "review_answer_value": ["gdpr", "hipaa", "sox", "pci_dss"],
  "operator": "in",
  "always_requires_review": false
}
```

### Advanced User Guidance Configuration

User guidance provides contextual help that appears based on user answers, improving completion rates and data quality.

#### Static vs Conditional Guidance

**1. Static Guidance (Always Visible):**

```http
POST /api/checklists-admin-questions/
{
  "description": "Enter your project start date:",
  "question_type": "date",
  "user_guidance": "The project start date should be when actual development work begins, not when planning started.",
  "always_show_guidance": true
}
```

**2. Conditional Guidance (Answer-Dependent):**

```http
POST /api/checklists-admin-questions/
{
  "description": "Will you be using cloud services?",
  "question_type": "boolean",
  "user_guidance": "Since you're using cloud services, please ensure you review our cloud security checklist and obtain necessary approvals before proceeding.",
  "always_show_guidance": false,
  "guidance_answer_value": true,
  "guidance_operator": "equals"
}
```

#### User Guidance Scenarios

**1. Regulatory Guidance for EU Users:**

```http
POST /api/checklists-admin-questions/
{
  "description": "Which regions will your service operate in?",
  "question_type": "multi_select",
  "user_guidance": "Since you selected EU regions, you must comply with GDPR. Please review our GDPR compliance guide and ensure you have a lawful basis for processing personal data.",
  "always_show_guidance": false,
  "guidance_answer_value": ["eu", "uk"],
  "guidance_operator": "in"
}
```

**2. Technical Guidance for Specific Technologies:**

```http
POST /api/checklists-admin-questions/
{
  "description": "Which technologies will you use?",
  "question_type": "multi_select",
  "user_guidance": "Since you're using AI/ML technologies, additional ethical review and bias testing may be required. Please consult with our AI Ethics team.",
  "always_show_guidance": false,
  "guidance_answer_value": ["machine_learning", "artificial_intelligence", "deep_learning"],
  "guidance_operator": "in"
}
```

**3. Process Guidance for Complex Workflows:**

```http
POST /api/checklists-admin-questions/
{
  "description": "How many users will access this system?",
  "question_type": "single_select",
  "user_guidance": "For enterprise-scale deployments, you'll need to complete additional capacity planning and load testing requirements. Please coordinate with the Infrastructure team.",
  "always_show_guidance": false,
  "guidance_answer_value": ["1000_plus", "enterprise"],
  "guidance_operator": "in"
}
```

**4. Warning Guidance for Risk Factors:**

```http
POST /api/checklists-admin-questions/
{
  "description": "Will you be integrating with external systems?",
  "question_type": "boolean",
  "user_guidance": "⚠️ External integrations require security review and may need additional authentication mechanisms. Please document all external connections and data flows.",
  "always_show_guidance": false,
  "guidance_answer_value": true,
  "guidance_operator": "equals"
}
```

### Combined Review and Guidance Workflows

Configure questions that both provide guidance and trigger reviews for comprehensive workflows.

#### Example: Financial Transaction Handling

```http
POST /api/checklists-admin-questions/
Content-Type: application/json

{
  "description": "Will your application process financial transactions?",
  "question_type": "boolean",
  "checklist": "http://localhost:8000/api/checklists-admin/{checklist_uuid}/",
  "required": true,
  "order": 5,

  // User guidance for "Yes" answers
  "user_guidance": "Financial transaction processing requires PCI DSS compliance. Please review our payment processing guidelines and ensure all credit card data is properly secured.",
  "always_show_guidance": false,
  "guidance_answer_value": true,
  "guidance_operator": "equals",

  // Review trigger for the same condition
  "review_answer_value": true,
  "operator": "equals",
  "always_requires_review": false
}
```

#### Example: Multi-Condition Security Workflow

```http
POST /api/checklists-admin-questions/
{
  "description": "Select all data types you'll be handling:",
  "question_type": "multi_select",
  "required": true,

  // Guidance for any sensitive data
  "user_guidance": "You've selected sensitive data types. Additional security measures, encryption, and audit logging will be required. Please coordinate with the Security team early in your project.",
  "always_show_guidance": false,
  "guidance_answer_value": ["personal_data", "financial_data", "health_data", "confidential"],
  "guidance_operator": "in",

  // Review trigger for high-risk combinations
  "review_answer_value": ["financial_data", "health_data"],
  "operator": "in",
  "always_requires_review": false
}
```

### Updating Existing Questions

#### Adding Review Triggers to Existing Questions

```http
PATCH /api/checklists-admin-questions/{question_uuid}/
Content-Type: application/json

{
  "review_answer_value": ["high_risk", "critical"],
  "operator": "in",
  "always_requires_review": false
}
```

#### Modifying User Guidance

```http
PATCH /api/checklists-admin-questions/{question_uuid}/
Content-Type: application/json

{
  "user_guidance": "Updated guidance text with new requirements and procedures.",
  "always_show_guidance": false,
  "guidance_answer_value": "enterprise",
  "guidance_operator": "equals"
}
```

#### Removing Conditions

```http
PATCH /api/checklists-admin-questions/{question_uuid}/
Content-Type: application/json

{
  "always_requires_review": false,
  "review_answer_value": [],
  "operator": "equals",
  "always_show_guidance": true,
  "guidance_answer_value": [],
  "guidance_operator": "equals"
}
```

### API Response Examples for Review and Guidance

#### Question with Active Guidance (User View)

```json
{
  "uuid": "123e4567-e89b-12d3-a456-426614174000",
  "description": "Will you be using cloud services?",
  "question_type": "boolean",
  "required": true,
  "user_guidance": "Since you're using cloud services, please ensure you review our cloud security checklist.",
  "existing_answer": {
    "uuid": "answer-uuid",
    "answer_data": true,
    "requires_review": false,
    "created": "2024-01-15T10:30:00Z"
  }
}
```

#### Question with Review Flag (Reviewer View)

```json
{
  "uuid": "123e4567-e89b-12d3-a456-426614174000",
  "description": "What type of data will you process?",
  "question_type": "multi_select",
  "required": true,
  "user_guidance": "You've selected sensitive data types. Additional security measures will be required.",
  "existing_answer": {
    "uuid": "answer-uuid",
    "answer_data": ["personal_data", "financial_data"],
    "requires_review": true,
    "created": "2024-01-15T10:30:00Z"
  },
  "operator": "in",
  "review_answer_value": ["personal_data", "financial_data", "health_data"],
  "always_requires_review": false
}
```

#### Completion Status with Review Summary

```json
{
  "uuid": "completion-uuid",
  "is_completed": true,
  "completion_percentage": 100.0,
  "requires_review": true,
  "review_trigger_summary": [
    {
      "question": "What type of data will you process?",
      "answer": ["personal_data", "financial_data"],
      "trigger_value": ["personal_data", "financial_data", "health_data"],
      "operator": "in"
    },
    {
      "question": "Will your application process payments?",
      "answer": true,
      "trigger_value": true,
      "operator": "equals"
    }
  ],
  "reviewed_by": null,
  "reviewed_at": null,
  "review_notes": ""
}
```

### Best Practices

#### Review Trigger Design

1. **Clear Criteria**: Use specific, unambiguous trigger conditions
2. **Risk-Based**: Focus triggers on high-risk or compliance-critical answers
3. **Consistent Operators**: Use the same operators across similar question types
4. **Documentation**: Include internal notes about why specific answers trigger reviews

#### User Guidance Best Practices

1. **Actionable**: Provide specific next steps, not just information
2. **Contextual**: Tailor guidance to the specific answer given
3. **Timely**: Show guidance when users need it most
4. **Resource Links**: Include references to relevant documentation or contacts

#### Workflow Integration

1. **Progressive Disclosure**: Use conditional visibility with guidance to reduce cognitive load
2. **Layered Validation**: Combine client-side guidance with server-side review triggers
3. **Clear Feedback**: Ensure users understand when answers will be reviewed
4. **Review Efficiency**: Design triggers to minimize false positives for reviewers

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
