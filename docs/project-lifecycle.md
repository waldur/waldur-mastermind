# Project and Resource Lifecycle Management

This document explains the lifecycle of projects and marketplace resources in Waldur, focusing on start/end dates, termination, and state transitions.

## Project Lifecycle

Projects control the overall workspace for resources and define key temporal boundaries.

### Project States and Dates

Projects have two key temporal fields:

- **`start_date`** (optional): When project becomes active for resource provisioning
- **`end_date`** (optional): Inclusive termination date - all project resources scheduled for termination when reached

```python
# Project model fields
start_date = models.DateField(null=True, blank=True)
end_date = models.DateField(
    null=True, blank=True,
    help_text="The date is inclusive. Once reached, all project resource will be scheduled for termination."
)
```

### Project Start Date Impact on Orders

Orders can be blocked by future project start dates. This also affects [invitation processing](./core-concepts/invitations.md#background-processing) where pending invitations wait for project activation:

```mermaid
sequenceDiagram
    participant U as User
    participant O as Order
    participant P as Project
    participant S as System

    U->>O: Create Order
    O->>P: Check project.start_date

    alt Project start_date is future
        O->>S: Set state PENDING_PROJECT
        Note over O,S: Order waits for project activation
        P->>S: Project activated (start_date cleared/reached)
        S->>O: Transition to next approval step
    else Project active or no start_date
        O->>S: Proceed to next approval step
    end
```

### Project End Date Behavior

When a project reaches its `end_date`:

- Property `is_expired` returns `True`
- All project resources are scheduled for termination
- New resource creation is blocked

## Order State Machine

Orders progress through states that include project and start date validation:

```mermaid
stateDiagram-v2
    [*] --> PENDING_CONSUMER : Order created

    PENDING_CONSUMER --> PENDING_PROJECT : Consumer approves & project start date is future
    PENDING_CONSUMER --> PENDING_PROVIDER : Consumer approves & project active
    PENDING_CONSUMER --> PENDING_START_DATE : Consumer approves & no provider review & order start date is future
    PENDING_CONSUMER --> CANCELED : Consumer cancels
    PENDING_CONSUMER --> REJECTED : Consumer rejects

    PENDING_PROJECT --> PENDING_PROVIDER: Project activates & provider review needed
    PENDING_PROJECT --> PENDING_START_DATE: Project activates & no provider review & order start date is future
    PENDING_PROJECT --> EXECUTING: Project activates & ready to process
    PENDING_PROJECT --> CANCELED : Project issues

    PENDING_PROVIDER --> PENDING_START_DATE : Provider approves & order start date is future
    PENDING_PROVIDER --> EXECUTING : Provider approves & ready to process
    PENDING_PROVIDER --> CANCELED : Provider cancels
    PENDING_PROVIDER --> REJECTED : Provider rejects

    PENDING_START_DATE --> EXECUTING : Start date reached
    PENDING_START_DATE --> CANCELED : User cancels

    EXECUTING --> DONE : Processing complete
    EXECUTING --> ERRED : Processing failed

    DONE --> [*]
    ERRED --> [*]
    CANCELED --> [*]
    REJECTED --> [*]
```

### Order State Impacts

| State | Description | Next Actions |
|-------|-------------|--------------|
| **PENDING_PROJECT** | Waiting for project activation | Project `start_date` must be cleared/reached. Also blocks [invitation processing](./core-concepts/invitations.md#background-processing) |
| **PENDING_START_DATE** | Waiting for order start date | Order `start_date` must be reached |
| **EXECUTING** | Resource provisioning active | Processor creates/updates/terminates resource |
| **DONE** | Order completed successfully | Resource state updated, billing triggered |
| **ERRED** | Order failed | Manual intervention required |
| **TERMINAL_STATES** | `{DONE, ERRED, CANCELED, REJECTED}` | No further state transitions |

## Resource Lifecycle

Resources maintain independent lifecycle from orders but are constrained by project boundaries.

### Resource States and Dates

Resources have temporal controls:

- **`end_date`** (optional): Inclusive termination date - resource scheduled for termination when reached
- **`state`**: Current operational state affecting available operations

```python
# Resource model fields
end_date = models.DateField(
    null=True, blank=True,
    help_text="The date is inclusive. Once reached, a resource will be scheduled for termination."
)
```

### Resource State Machine

```mermaid
stateDiagram-v2
    [*] --> CREATING : Order approved & executing

    CREATING --> OK : Provisioning success
    CREATING --> ERRED : Provisioning failed

    OK --> UPDATING : Update requested
    OK --> TERMINATING : Deletion requested or end_date reached

    UPDATING --> OK : Update success
    UPDATING --> ERRED : Update failed

    TERMINATING --> TERMINATED : Deletion success
    TERMINATING --> ERRED : Deletion failed

    ERRED --> OK : Error resolved
    ERRED --> UPDATING : Retry update
    ERRED --> TERMINATING : Force deletion

    TERMINATED --> [*]
```

### Resource End Date Behavior

When a resource reaches its `end_date`:

- Property `is_expired` returns `True`
- Resource is scheduled for termination (transitions to `TERMINATING`)
- Billing stops when termination completes

## Order Processing Flow

This sequence shows how orders create and manage resources:

```mermaid
sequenceDiagram
    participant U as User
    participant O as Order
    participant P as Processor
    participant R as Resource
    participant B as Billing

    U->>O: Create CREATE order
    Note over O: Approval workflow (consumer/provider/project)

    O->>P: process_order() when EXECUTING
    P->>R: Create resource (state: CREATING)
    alt Synchronous processing
        P->>R: Set state OK
        P->>B: Trigger billing for activated resource
        P->>O: Set state DONE
    else Asynchronous processing
        P->>O: Keep state EXECUTING
        Note over P: Backend processing continues
        P->>R: Set state OK when complete
        P->>B: Trigger billing for activated resource
        P->>O: Set state DONE via callback
    end
```

## Termination Flows

### Manual Resource Termination

```mermaid
sequenceDiagram
    participant U as User
    participant O as Order
    participant P as Processor
    participant R as Resource
    participant B as Billing

    U->>O: Create TERMINATE order
    Note over O: Approval workflow

    O->>P: process_order() when EXECUTING
    P->>R: Set state TERMINATING
    P->>R: Execute backend deletion
    alt Success
        P->>R: Set state TERMINATED
        P->>B: Stop billing / create final invoice
        P->>O: Set state DONE
    else Failure
        P->>R: Set state ERRED
        P->>O: Set state ERRED
    end
```

### Automatic Termination (End Date Reached)

```mermaid
sequenceDiagram
    participant S as System Task
    participant R as Resource
    participant O as Order
    participant P as Processor
    participant B as Billing

    S->>R: Check end_date (daily task)

    alt Resource end_date reached
        S->>O: Create automatic TERMINATE order
        O->>P: process_order() (auto-approved)
        P->>R: Set state TERMINATING
        P->>R: Execute backend deletion
        P->>R: Set state TERMINATED
        P->>B: Stop billing / create final invoice
        P->>O: Set state DONE
    end
```

## Key Interaction Points

### Project-Resource Constraints

1. **Resource creation blocked** if project is expired (`project.end_date` reached)
2. **Project end date triggers** termination of all project resources
3. **Project start date blocks** order processing and [invitation processing](./core-concepts/invitations.md#background-processing) until project activates

### Order-Resource Coordination

1. **CREATE orders** generate resources when successfully executed
2. **UPDATE orders** modify existing resource configuration and billing
3. **TERMINATE orders** transition resources through deletion lifecycle
4. **Order failures** leave resources in error states requiring manual intervention

### Billing Integration

1. **Resource activation** (CREATING → OK) triggers initial billing setup
2. **Resource termination** (OK → TERMINATED) stops billing and creates final invoices
3. **Resource end dates** coordinate with billing periods for accurate cost calculation
4. **Order completion** ensures billing state consistency with resource lifecycle

This lifecycle management ensures consistent resource provisioning, proper billing coordination, and controlled termination across the Waldur marketplace ecosystem.
