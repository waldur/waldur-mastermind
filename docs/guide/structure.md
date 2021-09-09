# Customers, projects, services, resources and users

Waldur is a service for sharing resources across projects. It is based
on the delegation model where a customer can allocate certain users to
perform technical or non-technical actions in the projects.

## Glossary

### User

An account in Waldur belonging to a person or a robot. A user can
belong to project or customer that can grant him different roles.

### Customer

A standalone entity. Represents a company or a department.
It can belong to a domain, which is mentioned in `domain` field of `Customer` model.

### Customer owner

A role of the user that allows her to represent a corresponding
customer. In this role, a user can create new projects, register
resources, as well as allocate them to the projects.

### Customer support

A person who has read-only access to all users, projects and resources in a supported organization.

### Customer service manager

A person who can manage organization's offerings and approve or reject orders for resources.

### Division

A set of customers grouped by some type.
Multiple divisions can organize a hierarchy.

### Provider

An entity that represents account in an external service provider.

  Private providers - providers that are available and manageable within a specific organization.
  Shared providers - global providers that are available for all organizations.

### Resource

An entity within a project and a provider. Represents cloud resource.
Examples: instance in AWS, droplet in DigitalOcean.

### Service settings

Represents an account of particular cloud service, for example, AWS or
OpenStack. Account credentials must provide full
access to service API. It is possible to mark service settings as
`shared` and they will be automatically connected to all customers.

### Service property

Represents any properties of cloud service usually used for a
resource provisioning. For example: image and flavor in OpenStack.

### General service property

Represents any property of a service that is not connected to
service settings.

### Project

A project is an entity within a customer. Project
aggregates and isolates resources. A customer owner can allow usage
of certain clouds within a project - defining what resource pools
project administrators can use.

### Project administrator

A project role responsible for the day-to-day technical operations
within a project. Limited access to project management and billing.

### Project manager

An optional non-technical role that a customer can use to delegate
management of certain projects to selected users. Project manager
can create new projects and manage administrators within a scope of
a certain project.

### Project member

A person who has the right to consume the resources allocated to a project.

### Resource

A resource is a provisioned entity of a service, for example, a VM
in OpenStack or AWS. Each resource belongs to a particular project.
