# Message templates

## waldur_core.structure

### notifications_profile_changes_operator_subject.txt (waldur_core.structure)

``` txt
Owner details have been updated
```

### notifications_profile_changes.html (waldur_core.structure)

``` html
User {{user.full_name}} (id={{ user.id }}) profile has been updated:

{% for f in fields %}
    {{ f.name }} from {{ f.old_value }} to {{ f.new_value }}{% if not forloop.last %}, {% else %}.{% endif %}
{% endfor %}
```

### change_email_request_subject.txt (waldur_core.structure)

``` txt
Verify new email address.
```

### notifications_profile_changes_operator_message.html (waldur_core.structure)

``` html
Owner of
{% for o in organizations %}
    {{ o.name }} {% if o.abbreviation %} ({{ o.abbreviation }}){% endif %}{% if not forloop.last %}, {% endif %}
{% endfor %}

{{user.full_name}} (id={{ user.id }}) has changed

{% for f in fields %}
    {{ f.name }} from {{ f.old_value }} to {{ f.new_value }}{% if not forloop.last %}, {% else %}.{% endif %}
{% endfor %}
```

### structure_role_granted_message.txt (waldur_core.structure)

``` txt
Role {{ permission.role }}  for {{ structure }} has been granted.
```

### structure_role_granted_subject.txt (waldur_core.structure)

``` txt
Role granted.
```

### change_email_request_message.html (waldur_core.structure)

``` html
<p>To confirm the change of email address from {{ request.user.email }} to {{ request.email }}, follow the <a href="{{ link }}">link</a>.</p>
```

### change_email_request_message.txt (waldur_core.structure)

``` txt
To confirm the change of email address from {{ request.user.email }} to {{ request.email }}, follow the {{ link }}.
```

### notifications_profile_changes_operator_message.txt (waldur_core.structure)

``` txt
Owner of
{% for o in organizations %}
    {{ o.name }} {% if o.abbreviation %} ({{ o.abbreviation }}){% endif %}{% if not forloop.last %}, {% endif %}
{% endfor %}

{{user.full_name}} (id={{ user.id }}) has changed

{% for f in fields %}
    {{ f.name }} from {{ f.old_value }} to {{ f.new_value }}{% if not forloop.last %}, {% else %}.{% endif %}
{% endfor %}
```

### structure_role_granted_message.html (waldur_core.structure)

``` html
<p>Role {{ permission.role }}  for {{ structure }} has been granted.</p>
```

## waldur_core.users

### invitation_approved_subject.txt (waldur_core.users)

``` txt
Account has been created
```

### invitation_created_message.html (waldur_core.users)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Invitation to {{ name }} {{ type }}</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    {{ sender }} has invited you to join {{ name }} {{ type }} in {{ role }} role.<br>
    Please visit <a href="{{ link }}">this page</a> to sign up and accept your invitation.
    Please note: this invitation expires at {{ invitation.get_expiration_time|date:'d.m.Y H:i' }}!
</p>
<p>
    {{ extra_invitation_text }}
</p>
</body>
</html>
```

### permission_request_submitted_message.html (waldur_core.users)

``` html
<html>
<head lang="en">
  <meta charset="UTF-8">
  <title>Permission request has been submitted.</title>
</head>
<body>
<p>
  Hello!
</p>
<p>
  User {{ permission_request.created_by }} with email {{ permission_request.created_by.email }} created permission request for {{ permission_request.invitation }}.
</p>
<p>
  Please visit the <a href="{{ requests_link }}">link</a> to approve or reject permission request.
</p>
</body>
</html>
```

### invitation_approved_message.txt (waldur_core.users)

``` txt
Hello!

{{ sender }} has invited you to join {{ name }} {{ type }} in {{ role }} role.
Please visit the link below to sign up and accept your invitation:
{{ link }}

Your credentials are as following.

Username is {{ username }}

Your password is {{ password }}
```

### invitation_created_message.txt (waldur_core.users)

``` txt
Hello!

{{ sender }} has invited you to join {{ name }} {{ type }} in {{ role }} role.
Please visit the link below to sign up and accept your invitation:
{{ link }}
{{ extra_invitation_text }}
```

### invitation_expired_message.html (waldur_core.users)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Invitation to {{ invitation.email }} has expired</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    An invitation to {{ invitation.email }} has expired <br>
    An invitation to {{ invitation.email }} has expired at {{ invitation.get_expiration_time|date:'d.m.Y H:i' }}.

</p>
</body>
</html>
```

### invitation_approved_message.html (waldur_core.users)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Account has been created</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    {{ sender }} has invited you to join {{ name }} {{ type }} in {{ role }} role.<br>
    Please visit <a href="{{ link }}">this page</a> to sign up and accept your invitation.
</p>
<p>
  Your credentials are as following.
</p>
<p>
  Your username is {{ username }}
</p>
<p>
  Your password is {{ password }}
</p>
</body>
</html>
```

### invitation_expired_subject.txt (waldur_core.users)

``` txt
Invitation has expired
```

### invitation_expired_message.txt (waldur_core.users)

``` txt
Hello!

An invitation to {{ invitation.email }} has expired.
This invitation expires at {{ invitation.get_expiration_time|date:'d.m.Y H:i' }}.
```

### invitation_rejected_message.txt (waldur_core.users)

``` txt
Hello!

The following invitation has been rejected.

Full name: {{ invitation.full_name }}

Target: {{ name }} {{ type }}

Role: {{ role }}
```

### invitation_rejected_subject.txt (waldur_core.users)

``` txt
Invitation has been rejected
```

### invitation_created_subject.txt (waldur_core.users)

``` txt
{% if reminder %}
REMINDER: Invitation to {{ name }} {{ type }}
{% else %}
Invitation to {{ name }} {{ type }}
{% endif %}
```

### permission_request_submitted_subject.txt (waldur_core.users)

``` txt
Permission request has been submitted.
```

### invitation_requested_message.txt (waldur_core.users)

``` txt
Hello!

{{ sender }} has created invitation request for the following user
to join {{ name }} {{ type }} in {{ role }} role.

{% if invitation.civil_number %}
Civil number: {{ invitation.civil_number }}
{% endif %}

{% if invitation.phone_number %}
Phone number: {{ invitation.phone_number }}
{% endif %}

E-mail: {{ invitation.email }}

{% if invitation.full_name %}
Full name: {{ invitation.full_name }}
{% endif %}

{% if invitation.native_name %}
Native name: {{ invitation.native_name }}
{% endif %}

{% if invitation.organization %}
Organization: {{ invitation.organization }}
{% endif %}

{% if invitation.job_title %}
Job title: {{ invitation.job_title }}
{% endif %}

Please visit the link below to approve invitation: {{ approve_link }}

Alternatively, you may reject invitation: {{ reject_link }}
```

### invitation_rejected_message.html (waldur_core.users)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Invitation to {{ name }} {{ type }}</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
  The following invitation has been rejected.
</p>

<p>
  Full name: {{ invitation.full_name }}
</p>

<p>
  Target: {{ name }} {{ type }}
</p>

<p>
  Role: {{ role }}
</p>
</body>
</html>
```

### permission_request_submitted_message.txt (waldur_core.users)

``` txt
Hello!

User {{ permission_request.created_by }} with email {{ permission_request.created_by.email }} created permission request for {{ permission_request.invitation }}.

Please visit the link below to approve or reject permission request: {{ requests_link }}.
```

### invitation_requested_message.html (waldur_core.users)

``` html
<html>
<head lang="en">
  <meta charset="UTF-8">
  <title>Invitation request</title>
</head>
<body>
<p>
  Hello!
</p>
<p>
  {{ sender }} has created invitation request for the following user
  to join {{ name }} {{ type }} in {{ role }} role.
</p>

{% if invitation.civil_number %}
  <p>
    Civil number: {{ invitation.civil_number }}
  </p>
{% endif %}

{% if invitation.phone_number %}
  <p>
    Phone number: {{ invitation.phone_number }}
  </p>
{% endif %}

<p>
  E-mail: {{ invitation.email }}
</p>

{% if invitation.full_name %}
  <p>
    Full name: {{ invitation.full_name }}
  </p>
{% endif %}

{% if invitation.native_name %}
  <p>
    Native name: {{ invitation.native_name }}
  </p>
{% endif %}

{% if invitation.organization %}
  <p>
    Organization: {{ invitation.organization }}
  </p>
{% endif %}

{% if invitation.job_title %}
  <p>
    Job title: {{ invitation.job_title }}
  </p>
{% endif %}

<p>
  Please <a href="{{ approve_link }}">approve</a> or <a href="{{ reject_link }}">reject</a> invitation.
</p>
</body>
</html>
```

### invitation_requested_subject.txt (waldur_core.users)

``` txt
Invitation request
```

## waldur_core.logging

### email.html (waldur_core.logging)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Notifications from waldur_core</title>
</head>
<body>
<ul>
    {% for event in events %}
    <li>
        {{ event.message }}
        <div>{{ event.created|date:"M d H:i e"}}</div>
    </li>
    {% endfor %}
</ul>
</body>
</html>
```

## waldur_mastermind.booking

### notification_message.txt (waldur_mastermind.booking)

``` txt
Hello!

Please do not forget about upcoming booking:
{% for resource in resources %}
    {{ resource.name }}{% if not forloop.last %}, {% endif %}
{% endfor %}.
```

### notification_message.html (waldur_mastermind.booking)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Reminder about upcoming booking.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    Please do not forget about upcoming booking: <br />
    {% for resource in resources %}
        {{ resource.name }}
        {% if not forloop.last %}
            <br />
        {% endif %}
    {% endfor %}
</p>
</body>
</html>
```

### notification_subject.txt (waldur_mastermind.booking)

``` txt
Reminder about upcoming booking.
```

## waldur_mastermind.invoices

### monthly_invoicing_reports.html (waldur_mastermind.invoices)

``` html
{% load i18n %}
{% load static %}
{% load humanize %}
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <style type="text/css">
        {% include "./style.css" %}
    </style>
</head>
<body>
<h2>{% trans 'Fixed price contracts:' %}</h2>
{% if contracts %}
    <table class="invoice-table">
        <thead>
            <tr>
                <th></th>
                <th>{% trans 'Organization' %}</th>
                <th>{% trans 'Contract end date' %}</th>
                <th>{% trans 'Till the end of contract. [days]' %}</th>
                <th>{% trans 'Contract sum' %}</th>
                <th>{% trans 'Payment sum' %}</th>
            </tr>
        </thead>
        <tbody>
            {% for contract in contracts %}
                <tr>
                    <th>{{ forloop.counter }}</th>
                    <td>{{ contract.name }}</td>
                    <td>{{ contract.end|date:"Y-m-d"|default_if_none:"" }}</td>
                    <td {% if contract.end_date_alarm %} class="text-danger" {% endif %}>{{ contract.till_end|default_if_none:"" }}</td>
                    <td {% if contract.payments_alarm %} class="text-danger" {% endif %}>
                        {{ contract.contract_sum|default_if_none:0|floatformat:"2"|intcomma }}
                    </td>
                    <td {% if contract.payments_alarm %} class="text-danger" {% endif %}>
                        {{ contract.payments_sum|default_if_none:0|floatformat:"2"|intcomma }}
                    </td>
                </tr>
            {% endfor %}
        </tbody>
    </table>
{% else %}
    <p>{% trans 'Contracts do not exist.' %}</p>
{% endif %}

<h2>{% blocktrans %}Invoices for month {{ month }}-{{ year }}:{% endblocktrans %}</h2>

<table class="invoice-table">
    <thead>
        <tr>
            <th></th>
            <th>{% trans 'Organization' %}</th>
            <th>{% trans 'Invoice date' %}</th>
            <th>{% trans 'Invoice sum' %}</th>
        </tr>
    </thead>
    <tbody>
        {% for invoice in invoices %}
            <tr>
                <th>{{ forloop.counter }}</th>
                <td>{% if invoice.customer.abbreviation %}
                        {{ invoice.customer.abbreviation }}
                    {% else %}
                        {{ invoice.customer.name }}
                    {% endif %}</td>
                <td>{{ invoice.invoice_date|date:"Y-m-d" }}</td>
                <td>{{ invoice.total|floatformat:"2"|intcomma }}</td>
            </tr>
        {% endfor %}
    </tbody>
</table>

</body>
</html>
```

### report_body.txt (waldur_mastermind.invoices)

``` txt
Attached is an accounting report for {{ month }}/{{ year }}.
```

### invoice.html (waldur_mastermind.invoices)

``` html
{% load i18n %}
{% load humanize %}

<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8">
    <title>Invoice</title>
    <style type="text/css">
      {% include "waldur_core/font.css" %}
      {% include "./style.css" %}
    </style>
  </head>
  <body>
      {% if deployment_logo %}
      <div id="logo">
        <img src="data:image/png;base64,{{ deployment_logo }}">
      </div>
      {% endif %}
      <h1>{% trans "Invoice No." %} {{ invoice.number|upper }}</h1>
      <br>
      <div class="text-right">
          <strong>{% trans "Invoice date" %}:</strong> {% if invoice.invoice_date %}
        {{ invoice.invoice_date|date:"Y-m-d" }} {% else %} {% trans "Pending" %} {% endif %}<br/>
          {% if invoice.due_date %}<strong>{% trans "Due date" %}:</strong> {{ invoice.due_date|date:"Y-m-d" }}<br/>{% endif %}
          <strong>{% trans "Invoice period" %}:</strong> {{ invoice.year }}-{{ invoice.month }}<br/>
      </div>

      <div>
        <h3 class="name">From</h3>
        <div><strong>{{ issuer_details.company }}</strong></div>
        <div>{{ issuer_details.address }}</div>
        <div>{{ issuer_details.country }}, {{ issuer_details.postal }}</div>
        <div><abbr>P:</abbr> ({{ issuer_details.phone.country_code }}) {{ issuer_details.phone.national_number }}</div>
        <div>{{ issuer_details.bank }}, {{ issuer_details.account }}</div>
        <div><abbr>{% trans "VAT" %}:</abbr>{{ issuer_details.vat_code }}</div>
        <div>{{ issuer_details.email }}</div>
      </div>
      <div>
        <h3 class="name">To</h3>
        <div><strong>{{ invoice.customer.name }}</strong></div>

        {% if invoice.customer.address %}
          <div>{{ invoice.customer.address }}</div>
        {% endif %}

        {% if invoice.customer.country and invoice.customer.postal %}
          <div>{{ invoice.customer.country }}, {{ invoice.customer.postal }}</div>
        {% endif %}

        {% if invoice.customer.phone_number %}
          <div><abbr>P:</abbr> {{ invoice.customer.phone_number }}</div>
        {% endif %}

        {% if invoice.customer.bank_name and invoice.customer.bank_account %}
          <div>{{ invoice.customer.bank_name }}, {{ invoice.customer.bank_account }}</div>
        {% endif %}

        {% if customer.vat_code %}
          <div><abbr>{% trans "VAT" %}:</abbr> {{ customer.vat_code }}</div>
        {% endif %}

        <div>{{ invoice.customer.email }}</div>

      </div>
      <div class="m-t">
        <table class="invoice-table">
          <tr>
            <th>Item</th>
            <th>Quantity</th>
            <th>Unit price</th>
            <th>Total price</th>
          </tr>

          {% regroup items|dictsort:"project_name" by project_name as project_list %}
          {% for project in project_list %}
          <tr>
              <td colspan="4"><h3>{{ project.grouper }}</h3></td>
          </tr>
            {% for item in project.list %}
              <tr>
                <td>
                  <strong>{{ item.name }}</strong>
                  <div>
                    <small>
                      {% trans "Start time" %}: {{ item.start | date:"Y-m-d H:i" }}.
                      {% trans "End time" %}: {{ item.end | date:"Y-m-d H:i" }}.
                    </small>
                  </div>
                </td>
                <td>{{ item.quantity }}</td>
                <td>{{ currency }} {{ item.unit_price | floatformat:2 | intcomma }}</td>
                <td>{{ currency }} {{ item.total | floatformat:2 | intcomma }}</td>
              </tr>
            {% endfor %}
          {% endfor %}
        </table>
      </div>
      <table class="m-t invoice-total">
        <tr>
          <td><strong>{% trans "Subtotal" %}</strong></td>
          <td>{{ currency }} {{ invoice.price | floatformat:2 | intcomma}}</td>
        </tr>
        {% if invoice.tax %}
          <tr>
            <td><strong>{% trans "VAT" %}</strong></td>
            <td>{{ currency }} {{ invoice.tax | floatformat:2 | intcomma}}</td>
          </tr>
        {% endif %}
        <tr>
          <td><strong>{% trans "TOTAL" %}</strong></td>
          <td>{{ currency }} {{ invoice.total | floatformat:2 | intcomma}}</td>
      </tr>
      </table>
  </body>
</html>
```

### notification_message.txt (waldur_mastermind.invoices)

``` txt
Hello,

Please follow the link below to see {{ customer }}'s accounting information for {{ month }}/{{ year }}:
{{ link }}
```

### upcoming_ends_notification_message.txt (waldur_mastermind.invoices)

``` txt
Hello,

this is a reminder that {{ organization_name }}'s fixed price contract {{ contract_number }} is ending on {{ end }}.
```

### upcoming_ends_notification_subject.txt (waldur_mastermind.invoices)

``` txt
{{ organization_name }}'s fixed price contract {{ contract_number }} is coming to an end
```

### report_subject.txt (waldur_mastermind.invoices)

``` txt
Waldur accounting report for {{ month }}/{{ year }}
```

### notification_message.html (waldur_mastermind.invoices)

``` html
<html xmlns="http://www.w3.org/1999/html">
<head lang="en">
    <meta charset="UTF-8">
    <title>{{ customer }}'s invoice for {{ month }}/{{ year }}</title>
</head>
<body>
<p>
    Dear Sir or Madam,
</p>
<p>
    Attached is invoice for services consumed by {{ customer }}'s during {{ month }}/{{ year }}.
</p>
</body>
</html>
```

### upcoming_ends_notification_message.html (waldur_mastermind.invoices)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>{{ organization_name }}'s fixed price contract {{ contract_number }} is coming to an end.</title>
</head>
<body>
<p>
    Hello,
    <br/>
    this is a reminder that {{ organization_name }}'s fixed price contract {{ contract_number }} is ending on {{ end }}.
</p>
</body>
</html>
```

### notification_subject.txt (waldur_mastermind.invoices)

``` txt
{{ customer }}'s invoice for {{ month }}/{{ year }}
```

## waldur_mastermind.marketplace

### notification_about_project_ending_subject.txt (waldur_mastermind.marketplace)

``` txt
{% if count_projects > 1 %}Your {{ count_projects }} projects{% else %} Project{% endif %} will be deleted on {{ end_date|date:'d/m/Y' }}.
```

### marketplace_resource_update_limits_succeeded_message.txt (waldur_mastermind.marketplace)

``` txt
Hello!

Following request from {{ order_user }}, resource {{ resource_name }} limits have been updated from:
    {{ resource_old_limits }}
to:
    {{ resource_limits }}.

{% if support_email or support_phone %}
If you have any additional questions, please contact support.
{% if support_email %}
Email: {{ support_email }}
{% endif %}
{% if support_phone %}
Phone: {{ support_phone }}
{% endif %}
{% endif %}
```

### tos_reconsent_required_subject.txt (waldur_mastermind.marketplace)

``` txt
Action required: Updated Terms of Service for {{ offering.name }}
```

### marketplace_resource_update_limits_succeeded_subject.txt (waldur_mastermind.marketplace)

``` txt
Resource {{ resource_name }} limits have been updated.
```

### notification_about_stale_resources_message.txt (waldur_mastermind.marketplace)

``` txt
Hello!

We noticed that you have stale resources that have not cost you anything for the last 3 months.
Perhaps some of them are not needed any more?

The resource names are:
{% for resource in resources %}
    {{ resource.resource.name }} {{ resource.resource_url }}
{% endfor %}
Thank you!
```

### marketplace_plan_template.txt (waldur_mastermind.marketplace)

``` txt
Plan: {{ plan.name }}{% for component in components %}
{{component.name}}; amount: {{component.amount}}; price: {{component.price|floatformat }};
{% endfor %}
```

### notification_usages_message.txt (waldur_mastermind.marketplace)

``` txt
Hello!

Please do not forget to add usage for the resources you provide:
{% regroup resources by offering as offering_list %}{% for offering in offering_list %}
{{forloop.counter}}. {{ offering.grouper.name }}:{% for resource in offering.list %}
    - {{ resource.name }}
{% endfor %}{% endfor %}
You can submit resource usage via API or do it manually at {{ public_resources_url }}.
```

### notify_consumer_about_pending_order_subject.txt (waldur_mastermind.marketplace)

``` txt
A new order by {{ order.created_by.get_full_name }} is waiting for approval.
```

### marketplace_resource_termination_scheduled_staff_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Resource {{ resource.name }} termination has been scheduled.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    The resource you have - <a href="{{ resource_url }}">{{ resource.name }}</a> has not been used for the past 3 months. {{ user.full_name }} has scheduled termination of that resource on {{ resource.end_date|date:"SHORT_DATE_FORMAT" }}.
    If you feel that you still want to keep it, please <a href="{{ resource_url }}"></a>remove the resource end date</a>.
</p>
</body>
</html>
```

### marketplace_resource_update_succeeded_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Resource {{ resource_name }} has been updated.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    Following request from {{ order_user }}, resource {{ resource_name }} has been updated.
</p>
{% if resource_old_plan %}
<p>
    The plan has been changed from {{ resource_old_plan }} to {{ resource_plan }}.
</p>
{% endif %}
{% if support_email or support_phone %}
<p>
    If you have any additional questions, please contact support.
</p>
{% if support_email %}
<p>
    Email: {{ support_email }}
</p>
{% endif %}
{% if support_phone %}
<p>
    Phone: {{ support_phone }}
</p>
{% endif %}
{% endif %}
</body>
</html>
```

### marketplace_resource_update_failed_subject.txt (waldur_mastermind.marketplace)

``` txt
Resource {{ resource_name }} update has failed.
```

### notify_provider_about_pending_order_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>A new order by {{ order.created_by.get_full_name }} is waiting for approval.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    Please visit <a href="{{ order_url }}">{{ site_name }}</a> to find out more details.
</p>
</body>
</html>
```

### tos_reconsent_required_message.txt (waldur_mastermind.marketplace)

``` txt
Hello {{ user.full_name }},

The Terms of Service for {{ offering.name }} have been updated from version {{ old_version }} to version {{ new_version }}.

You need to review and re-accept the updated Terms of Service to continue accessing this offering.

View updated Terms of Service: {{ terms_of_service_link }}

To manage your consents, please visit your profile:
{{ tos_management_url }}

Thank you for your attention to this matter.

{{ site_name }} Team
```

### marketplace_resource_terminate_failed_subject.txt (waldur_mastermind.marketplace)

``` txt
Resource {{ resource_name }} deletion has failed.
```

### marketplace_resource_update_succeeded_message.txt (waldur_mastermind.marketplace)

``` txt
Hello!

Following request from {{ order_user }}, resource {{ resource_name }} has been updated.

{% if resource_old_plan %}
The plan has been changed from {{ resource_old_plan }} to {{ resource_plan }}.
{% endif %}

{% if support_email or support_phone %}
If you have any additional questions, please contact support.
{% if support_email %}
Email: {{ support_email }}
{% endif %}
{% if support_phone %}
Phone: {{ support_phone }}
{% endif %}
{% endif %}
```

### notification_to_user_that_order_been_rejected_subject.txt (waldur_mastermind.marketplace)

``` txt
Your order to {{ order_type }} a resource {{ order.resource.name }} has been rejected.
```

### marketplace_resource_update_failed_message.txt (waldur_mastermind.marketplace)

``` txt
Hello!

Resource {{ resource_name }} update has failed.
```

### notification_about_stale_resources_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Reminder about stale resources.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    We noticed that you have stale resources that have not cost you anything for the last 3 months. <br />
    Perhaps some of them are not needed any more?<br />

    The resource names are:
    <ul>
        {% for resource in resources %}
            <li><a href='{{ resource.resource_url }}'>{{ resource.resource.name }}</a></li>
        {% endfor %}
    </ul>
    Thank you!
</p>
</body>
</html>
```

### marketplace_resource_create_failed_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Resource {{ resource_name }} creation has failed.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    Resource {{ resource_name }} creation has failed.
</p>
</body>
</html>
```

### notify_consumer_about_pending_order_message.txt (waldur_mastermind.marketplace)

``` txt
Hello!

A new order by {{ order.created_by.get_full_name }} is waiting for approval.
```

### marketplace_resource_create_succeeded_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Resource {{ resource_name }} has been created.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    Resource {{ resource_name }} has been created.
</p>
</body>
</html>
```

### marketplace_resource_update_limits_failed_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Resource {{ resource_name }} limits update has failed.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    Resource {{ resource_name }} limits update has failed.
</p>
</body>
</html>
```

### marketplace_resource_update_limits_succeeded_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Resource {{ resource_name }} limits have been updated.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    Following request from {{ order_user }}, resource {{ resource_name }} limits have been updated from:<br>
    <blockquote>
        {{ resource_old_limits }}
    </blockquote>
    to:
    <blockquote>
        {{ resource_limits }}
    </blockquote>
</p>
{% if support_email or support_phone %}
<p>
    If you have any additional questions, please contact support.
</p>
{% if support_email %}
<p>
    Email: {{ support_email }}
</p>
{% endif %}
{% if support_phone %}
<p>
    Phone: {{ support_phone }}
</p>
{% endif %}
{% endif %}
</body>
</html>
```

### tos_consent_required_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head>
    <meta charset="UTF-8">
</head>
<body>
    <p>Hello {{ user.full_name }},</p>

    <p>You have been granted access to <strong>{{ offering.name }}</strong>, which requires you to accept the <a href="{{ terms_of_service_link }}" style="color: #007bff; text-decoration: underline;">Terms of Service</a>.</p>

    <p>Before you can use this offering, please review and accept the Terms of Service.</p>

    <p>
        <a href="{{ tos_management_url }}" style="display: inline-block; padding: 10px 20px; background-color: #28a745; color: white; text-decoration: none; border-radius: 5px;">
            Manage ToS Consents
        </a>
    </p>

    <p>Once you've accepted, you can access all resources from this offering through your project dashboard.</p>

    <p>
        Thank you,<br>
        {{ site_name }} Team
    </p>
</body>
</html>
```

### marketplace_resource_update_failed_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Resource {{ resource_name }} update has failed.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    Resource {{ resource_name }} update has failed.
</p>
</body>
</html>
```

### notification_about_resource_ending_message.txt (waldur_mastermind.marketplace)

``` txt
Dear {{ user.full_name }},

Termination date of your {{ resource.name }} is approaching and it will be deleted{% if delta == 1 %} tomorrow {% else %} in {{ delta }} days{% endif %}.
If you are aware of that, then no actions are needed from your side.
If you need to update resource end date, please update it in resource details {{ resource_url }}.

Thank you!
```

### notification_about_project_ending_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Projects will be deleted.</title>
</head>
<body>
<p>Hello {{ user.full_name }}!</p>
<p>The following projects are ending {% if delta == 1 %} tomorrow {% else %} in {{ delta }} days{% endif %}:</p>
<ul>
{% for project in projects %}
    <li><a href="{{ project.url }}">{{ project.name }}</a></li>
{% endfor %}
</ul>
<p>
    End of the project will lead to termination of all resources in the project. <br />
    If you are aware of that, then no actions are needed from your side. <br />
    If you need to update project end date, please update it in project details.
</p>
<p>Thank you!</p>
</body>
</html>
```

### marketplace_resource_create_failed_subject.txt (waldur_mastermind.marketplace)

``` txt
Resource {{ resource_name }} creation has failed.
```

### marketplace_resource_update_limits_failed_subject.txt (waldur_mastermind.marketplace)

``` txt
Resource {{ resource_name }} limits update has failed.
```

### marketplace_resource_terminate_failed_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Resource {{ resource_name }} deletion has failed.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    Resource {{ resource_name }} deletion has failed.
</p>
</body>
</html>
```

### notification_usages_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Reminder about missing usage reports.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>Please do not forget to add usage for the resources you provide:</p>
{% regroup resources by offering as offering_list %}

<ol>
{% for offering in offering_list %}
    <li>
        {{ offering.grouper.name }}:
        <ul>
            {% for resource in offering.list %}
            <li>{{ resource.name }}</li>
            {% endfor %}
        </ul>
    </li>
{% endfor %}
</ol>

<p>
    You can submit resource usage via API or do it <a href='{{ public_resources_url }}'>manually</a>.
</p>
</body>
</html>
```

### marketplace_resource_terminate_succeeded_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Resource {{ resource_name }} has been deleted.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    Resource {{ resource_name }} has been deleted.
</p>
</body>
</html>
```

### marketplace_resource_termination_scheduled_staff_subject.txt (waldur_mastermind.marketplace)

``` txt
Resource {{ resource.name }} termination has been scheduled.
```

### notification_about_stale_resources_subject.txt (waldur_mastermind.marketplace)

``` txt
Reminder about stale resources.
```

### notify_provider_about_pending_order_subject.txt (waldur_mastermind.marketplace)

``` txt
A new order by {{ order.created_by.get_full_name }} is waiting for approval.
```

### marketplace_resource_terminate_succeeded_subject.txt (waldur_mastermind.marketplace)

``` txt
Resource {{ resource_name }} has been deleted.
```

### marketplace_resource_termination_scheduled_message.txt (waldur_mastermind.marketplace)

``` txt
Hello!

The resource you have - {{ resource.name }} has not been used for the past 3 months. {{ user.full_name }} has scheduled termination of that resource on {{ resource.end_date|date:"SHORT_DATE_FORMAT" }}. If you feel that you still want to keep it, please remove the resource end date {{ resource_url }}.
```

### tos_consent_required_message.txt (waldur_mastermind.marketplace)

``` txt
Hello {{ user.full_name }},

You have been granted access to {{ offering.name }}, which requires you to accept the Terms of Service.

Before you can use this offering, please review and accept the Terms of Service:

Terms of Service: {{ terms_of_service_link }}

To manage your ToS consents, please visit your profile:
{{ tos_management_url }}

Once you've accepted, you can access all resources from this offering through your project dashboard.

Thank you,
{{ site_name }} Team
```

### notification_about_resource_ending_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Resource {{ resource.name }} will be deleted.</title>
</head>
<body>
<p>Dear {{ user.full_name }},</p>

<p>
    Termination date of your {{ resource.name }} is approaching and it will be
    deleted{% if delta == 1 %} tomorrow {% else %} in {{ delta }} days{% endif %}.<br />
    If you are aware of that, then no actions are needed from your side. <br />
    If you need to update resource end date, please update it in resource details {{ resource_url }}.
</p>

<p>Thank you!</p>
</body>
</html>
```

### notification_about_project_ending_message.txt (waldur_mastermind.marketplace)

``` txt
Hello {{ user.full_name }}!

The following projects are ending {% if delta == 1 %} tomorrow {% else %} in {{ delta }} days{% endif %}:

{% for project in projects %}
    - {{ project.name }} ({{ project.url }})
{% endfor %}

End of the project will lead to termination of all resources in the project.
If you are aware of that, then no actions are needed from your side.
If you need to update project end date, please update it in project details.

Thank you!
```

### notification_to_user_that_order_been_rejected_message.txt (waldur_mastermind.marketplace)

``` txt
Hello!

Your order {{ link }} to {{ order_type }} a resource {{ order.resource.name }} has been rejected.
```

### notification_about_resource_ending_subject.txt (waldur_mastermind.marketplace)

``` txt
Resource {{ resource.name }} will be deleted.
```

### marketplace_resource_update_succeeded_subject.txt (waldur_mastermind.marketplace)

``` txt
Resource {{ resource_name }} has been updated.
```

### marketplace_resource_update_limits_failed_message.txt (waldur_mastermind.marketplace)

``` txt
Hello!

Resource {{ resource_name }} limits update has failed.
```

### marketplace_resource_termination_scheduled_subject.txt (waldur_mastermind.marketplace)

``` txt
Resource {{ resource.name }} termination has been scheduled.
```

### tos_reconsent_required_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head>
    <meta charset="UTF-8">
</head>
<body>
    <p>Hello {{ user.full_name }},</p>

    <p>The Terms of Service for <strong>{{ offering.name }}</strong> have been updated from version <strong>{{ old_version }}</strong> to version <strong>{{ new_version }}</strong>.</p>

    <p>You need to review and re-accept the updated Terms of Service to continue accessing this offering.</p>

    <p><a href="{{ terms_of_service_link }}" style="color: #007bff;">View Updated Terms of Service</a></p>

    <p>
        <a href="{{ tos_management_url }}" style="display: inline-block; padding: 10px 20px; background-color: #28a745; color: white; text-decoration: none; border-radius: 5px;">
            Manage ToS Consents
        </a>
    </p>

    <p>Thank you for your attention to this matter.</p>

    <p>
        {{ site_name }} Team
    </p>
</body>
</html>
```

### notify_consumer_about_pending_order_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>A new order by {{ order.created_by.get_full_name }} is waiting for approval.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    Please visit <a href="{{ order_link }}">{{ site_name }}</a> to find out more details.
</p>
</body>
</html>
```

### marketplace_resource_terminate_failed_message.txt (waldur_mastermind.marketplace)

``` txt
Hello!

Resource {{ resource_name }} deletion has failed.
```

### marketplace_resource_termination_scheduled_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Resource {{ resource.name }} termination has been scheduled.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    The resource you have - <a href="{{ resource_url }}">{{ resource.name }}</a> has not been used for the past 3 months. {{ user.full_name }} has scheduled termination of that resource on {{ resource.end_date|date:"SHORT_DATE_FORMAT" }}.
    If you feel that you still want to keep it, please <a href="{{ resource_url }}"></a>remove the resource end date</a>.
</p>
</body>
</html>
```

### notification_to_user_that_order_been_rejected_message.html (waldur_mastermind.marketplace)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Your order has been rejected.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    Your <a href="{{ link }}">order</a> to {{ order_type }} a resource {{ order.resource.name }} has been rejected.
</p>
</body>
</html>
```

### marketplace_resource_create_failed_message.txt (waldur_mastermind.marketplace)

``` txt
Hello!

Resource {{ resource_name }} creation has failed.
```

### tos_consent_required_subject.txt (waldur_mastermind.marketplace)

``` txt
Action required: Accept Terms of Service for {{ offering.name }}
```

### marketplace_resource_termination_scheduled_staff_message.txt (waldur_mastermind.marketplace)

``` txt
Hello!

The resource you have - {{ resource.name }} has not been used for the past 3 months. {{ user.full_name }} has scheduled termination of that resource on {{ resource.end_date|date:"SHORT_DATE_FORMAT" }}. If you feel that you still want to keep it, please remove the resource end date {{ resource_url }}.
```

### marketplace_resource_create_succeeded_subject.txt (waldur_mastermind.marketplace)

``` txt
Resource {{ resource_name }} has been created.
```

### marketplace_resource_terminate_succeeded_message.txt (waldur_mastermind.marketplace)

``` txt
Hello!

Resource {{ resource_name }} has been deleted.
```

### notify_provider_about_pending_order_message.txt (waldur_mastermind.marketplace)

``` txt
Hello!

A new order by {{ order.created_by.get_full_name }} is waiting for approval.
```

### marketplace_resource_create_succeeded_message.txt (waldur_mastermind.marketplace)

``` txt
Hello!

Resource {{ resource_name }} has been created.
```

### notification_usages_subject.txt (waldur_mastermind.marketplace)

``` txt
Reminder about missing usage reports.
```

## waldur_mastermind.marketplace_remote

### notification_about_pending_project_updates_message.txt (waldur_mastermind.marketplace_remote)

``` txt
Hello!

We noticed that you have pending project update requests.
Perhaps you would like to have a look at them?

The project is:
    {{ project_update_request.project.name }} {{ project_url }}
Thank you!
```

### notification_about_pending_project_updates_subject.txt (waldur_mastermind.marketplace_remote)

``` txt
Reminder about pending project updates.
```

### notification_about_project_details_update_message.txt (waldur_mastermind.marketplace_remote)

``` txt
Hello!

We would like to notify you about recent updates in project details.
Perhaps you would like to have a look at them?

The project is:
    {{ new_name }} {{ project_url }}

    Details after the update are below:
        {% if new_description %}
            Old description: {{ old_description }}
            New description: {{ new_description }}
        {% endif %}

        {% if new_name %}
            Old name: {{ old_name }}
            New name: {{ new_name }}
        {% endif %}

        {% if new_end_date %}
           Old end date: {{ old_end_date }}
           New end date: {{ new_end_date }}
        {% endif %}

        {% if new_oecd_fos_2007_code %}
           Old OECD FOS 2007 code: {{ old_oecd_fos_2007_code }}
           New OECD FOS 2007 code: {{ new_oecd_fos_2007_code }}
        {% endif %}

        {% if new_is_industry %}
           Old is_industry: {{ old_is_industry }}
           New is_industry: {{ new_is_industry }}
        {% endif %}

    Reviewed by: {{ reviewed_by }}
Thank you!
```

### notification_about_project_details_update_message.html (waldur_mastermind.marketplace_remote)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>A notification about project details update.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    We would like to notify you about recent updates in project details.<br />
    Perhaps you would like to have a look at them?<br />

    The project is:
    <ul>
        <li><a href='{{ project_url }}'>{{ new_name }}</a></li>
    </ul>

    <div>
        Details after the update are below:
        <ul>
            {% if new_description %}
                <li>Old description: {{ old_description }}</li>
                <li>New description: {{ new_description }}</li>
            {% endif %}

            {% if new_name %}
                <li>Old name: {{ old_name }}</li>
                <li>New name: {{ new_name }}</li>
            {% endif %}

            {% if new_end_date %}
                <li>Old end date: {{ old_end_date }}</li>
                <li>New end date: {{ new_end_date }}</li>
            {% endif %}

            {% if new_oecd_fos_2007_code %}
                <li>Old OECD FOS 2007 code: {{ old_oecd_fos_2007_code }}</li>
                <li>New OECD FOS 2007 code: {{ new_oecd_fos_2007_code }}</li>
            {% endif %}

            {% if new_is_industry %}
                <li>Old is_industry: {{ old_is_industry }}</li>
                <li>New is_industry: {{ new_is_industry }}</li>
            {% endif %}
            <li>Reviewed by: {{ reviewed_by }}</li>
        </ul>

    </div>
    Thank you!
</p>
</body>
</html>
```

### notification_about_pending_project_updates_message.html (waldur_mastermind.marketplace_remote)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Reminder about pending project updates.</title>
</head>
<body>
<p>
    Hello!
</p>
<p>
    We noticed that you have pending project update requests.<br />
    Perhaps you would like to have a look at them?<br />

    The project is:
    <ul>
        <li><a href='{{ project_url }}'>{{ project_update_request.project.name }}</a></li>
    </ul>
    Thank you!
</p>
</body>
</html>
```

### notification_about_project_details_update_subject.txt (waldur_mastermind.marketplace_remote)

``` txt
A notification about project details update.
```

## waldur_mastermind.marketplace_support

### create_project_membership_update_issue.txt (waldur_mastermind.marketplace_support)

``` txt
User: {{user.first_name}} {{user.last_name}} (e-mail: {{user.email}}, username: {{user.username}}).
Project: {{project}} ({{ project_url }}).

Service offerings:
{% for offering in offerings %} {{offering}}
    {% if offering.offering_user %}Offering user: {{offering.offering_user.username}}
    {% else %}
    Username not available.
    {% endif %}
    {% if offering.resources %}Resources:
        {% for resource in offering.resources %}- name: {{resource.name}}, backend ID: {{resource.backend_id}}, link: {{resource.get_homeport_link}}
        {% endfor %}
    {% endif %}
{% endfor %}
```

### terminate_resource_template.txt (waldur_mastermind.marketplace_support)

``` txt
{% load waldur_marketplace %}[Terminate resource {{order.resource.scope.name}}|{{request_url}}].
{% plan_details order.resource.plan %}
Marketplace resource UUID: {{order.resource.uuid.hex}}
```

### update_resource_template.txt (waldur_mastermind.marketplace_support)

``` txt
[Switch plan for resource {{order.resource.scope.name}}|{{request_url}}].
Switch from {{order.resource.plan.name}} plan to {{order.plan.name}}.
Marketplace resource UUID: {{order.resource.uuid.hex}}
```

### create_resource_template.txt (waldur_mastermind.marketplace_support)

``` txt
{% load waldur_marketplace %}[Order|{{order_url}}].
Provider: {{order.offering.customer.name}}
Resource UUID: {{resource.uuid}}
Resource name: {{resource.name}}
Plan details:
    {% plan_details order.plan %}
Full name: {{order.created_by.full_name|default:"none"}}
Civil code: {{order.created_by.civil_number|default:"none"}}
Email: {{order.created_by.email}}
```

### update_limits_template.txt (waldur_mastermind.marketplace_support)

``` txt
[Update limits for resource {{order.resource.scope.name}}|{{request_url}}].
Marketplace resource UUID: {{order.resource.uuid.hex}}
Old limits: {{ old_limits }}.
New limits: {{ new_limits }}.
```

## waldur_mastermind.proposal

### round_opening_for_reviewers_subject.txt (waldur_mastermind.proposal)

``` txt
New review round opening: {{ call_name }}
```

### review_rejected_subject.txt (waldur_mastermind.proposal)

``` txt
Alert: review assignment rejected for {{ proposal_name }}
```

### proposal_decision_for_reviewer_message.txt (waldur_mastermind.proposal)

``` txt
Dear {{ reviewer_name }},

A decision has been made on the proposal "{{ proposal_name }}" in call "{{ call_name }}" that you reviewed.

Decision details:
- Proposal: {{ proposal_name }}
- Decision: {{ proposal_state }}
- Decision date: {{ decision_date }}

{% if proposal_state == "rejected" and rejection_reason %}Reason: {{ rejection_reason }}{% endif %}

Thank you for your valuable contribution to the review process. Your expert assessment helped inform this decision.

View proposal: {{ proposal_url }}

This is an automated message from {{ site_name }}. Please do not reply to this email.
```

### proposal_state_changed_message.txt (waldur_mastermind.proposal)

``` txt
Dear {{ proposal_creator_name }},

The state of your proposal "{{ proposal_name }}" in call "{{ call_name }}" has been updated.

State change:
- Previous state: {{ previous_state }}
- New state: {{ new_state }}
- Updated on: {{ update_date }}

{% if new_state == 'accepted' %}
Project created: {{ project_name }}
Allocation start date: {{ allocation_date }}
Duration: {{ duration }} days

Allocated resources:
{% for resource in allocated_resources %}
{{ forloop.counter }}. {{ resource.name }} - {{ resource.provider_name }} - {{ resource.plan_name }} - Provisioned
{% empty %}
No resources allocated yet.
{% endfor %}
{% endif %}

{% if new_state == 'rejected' %}
Feedback: {{ rejection_feedback }}
{% endif %}

{% if new_state == 'submitted' %}
Your proposal has been successfully submitted and will be reviewed according to the review process for this call. You will receive further notifications as your proposal progresses through the review process.
{% endif %}

{% if new_state == 'in_review' %}
Your proposal is now under review. Reviewers will evaluate your proposal based on the criteria specified in the call. This process may take {{ review_period }} days according to the round's review period.
{% endif %}

{% if new_state == 'accepted' %}
Congratulations! Your proposal has been accepted. Resources have been allocated based on your request and a new project has been created. You can access your project by clicking the link below.
{% endif %}

{% if new_state == 'rejected' %}
We regret to inform you that your proposal has not been accepted at this time. Please review any feedback provided above. You may have the opportunity to submit a revised proposal in future rounds.
{% endif %}

View Proposal: {{ proposal_url }}
{% if new_state == 'accepted' and project_url %}
View Project: {{ project_url }}
{% endif %}

This is an automated message from the {{ site_name }}. Please do not reply to this email.
```

### new_review_submitted_message.html (waldur_mastermind.proposal)

``` html
<html>
<head>
    <meta charset="UTF-8">
    <title>Review Submitted</title>
</head>
<body>
    <p>Dear call manager,</p>

    <p>A review has been submitted for proposal "{{ proposal_name }}" in call "{{ call_name }}".</p>

    <p>
        <strong>Review summary:</strong><br>
        - Reviewer: {{ reviewer_name }}<br>
        - Submission date: {{ review_date }}<br>
        - Score: {{ score }}/{{ max_score }}
    </p>

    <p>
        <strong>Review Progress:</strong><br>
        - Submitted reviews: {{ submitted_reviews }}<br>
        - Pending reviews: {{ pending_reviews }}<br>
        - Rejected reviews: {{ rejected_reviews }}<br>
    </p>

    <p>
        You can view the full review details at:<br>
        <a href="{{ review_url }}">{{ review_url }}</a>
    </p>

    <p>
        This is an automated message from the {{ site_name }}. Please do not reply to this email.
    </p>
</body>
</html>
```

### round_closing_for_managers_message.html (waldur_mastermind.proposal)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>Round closed</title>
</head>
<body>
    <p>Dear call manager,</p>

    <p>The round "{{ round_name }}" for call "{{ call_name }}" has now closed.</p>

    <h4>Round summary:</h4>
    <ul>
        <li><strong>Total proposals submitted:</strong> {{ total_proposals }}</li>
        <li><strong>Start date:</strong> {{ start_date }}</li>
        <li><strong>Closed date:</strong> {{ close_date }}</li>
    </ul>

    <p>Based on the review strategy selected for this round ({{ review_strategy }}), the system has:</p>
    <ul>
        <li>Set all draft proposals to "canceled" state</li>
        <li>Moved all submitted proposals to "in_review" state</li>
        <li>Created {{ total_reviews }} review assignments</li>
    </ul>

    <p>You can view the round details and manage proposals by visiting: <a href="{{ round_url }}">{{ round_url }}</a></p>

    <p>
        This is an automated message from the {{ site_name }}. Please do not reply to this email.
    </p>
</body>
</html>
```

### new_proposal_submitted_message.html (waldur_mastermind.proposal)

``` html
<html>
<head>
    <meta charset="UTF-8">
</head>
<body>
    <p>Dear call manager,</p>

    <p>A new proposal has been submitted to the call "{{ call_name }}".</p>

    <p>
        <strong>Proposal details:</strong><br>
        - Name: {{ proposal_name }}<br>
        - Submitted by: {{ proposal_creator_name }}<br>
        - Submission date: {{ submission_date }}<br>
        - Round: {{ round_name }}
    </p>

    <p>
        You can review this proposal by visiting the following URL:<br>
        <a href="{{ proposal_url }}">{{ proposal_url }}</a>
    </p>

    <p>
        This is an automated message from the {{ site_name }}. Please do not reply to this email.
    </p>
</body>
</html>
```

### proposal_state_changed_subject.txt (waldur_mastermind.proposal)

``` txt
Proposal state update: {{ proposal_name }} - {{ new_state }}
```

### proposal_decision_for_reviewer_subject.txt (waldur_mastermind.proposal)

``` txt
Decision made: Proposal {{ proposal_state }} - {{ proposal_name }}
```

### reviews_complete_subject.txt (waldur_mastermind.proposal)

``` txt
All reviews complete for proposal: {{ proposal_name }}
```

### reviews_complete_message.txt (waldur_mastermind.proposal)

``` txt
Dear call manager,

All required reviews have been completed for proposal "{{ proposal_name }}" in call "{{ call_name }}".

Review summary:
- Proposal: {{ proposal_name }}
- Submitted by: {{ submitter_name }}
- Number of submitted reviews: {{ reviews_count }}
- Average score: {{ average_score }}/5

Review details:
{% for r in reviews %}{{ forloop.counter }}. {{ r.reviewer_name }} - {{ r.score }}/5 - {{ r.submitted_at|date:"Y-m-d H:i" }}
{% empty %}No individual reviews available.
{% endfor %}
ACTION REQUIRED: Please review the evaluation and make a decision on this proposal.

Review & decide: {{ proposal_url }}

This is an automated message from the {{ site_name }}. Please do not reply to this email.
```

### proposal_cancelled_subject.txt (waldur_mastermind.proposal)

``` txt
Proposal canceled: {{ proposal_name }}
```

### new_review_submitted_message.txt (waldur_mastermind.proposal)

``` txt
Dear call manager,

A review has been submitted for proposal "{{ proposal_name }}" in call "{{ call_name }}".

Review summary:
- Reviewer: {{ reviewer_name }}
- Submission date: {{ submission_date }}
- Score: {{ score }}/{{ max_score }}

Review Progress:
- Submitted reviews: {{ submitted_reviews }}
- Pending reviews: {{ pending_reviews }}
- Rejected reviews: {{ rejected_reviews }}

You can view the full review details at:
{{ review_url }}

This is an automated message from the {{ site_name }}. Please do not reply to this email.
```

### requested_offering_decision_message.txt (waldur_mastermind.proposal)

``` txt
Dear call manager,

The provider has {{ decision }} the request to include offering "{{ offering_name }}" in call "{{ call_name }}".

Offering details:
- Offering: {{ offering_name }}
- Provider: {{ provider_name }}
- Decision Date: {{ decision_date }}
- State: {{ decision }}

{% if decision == "accepted" %}This offering is now available for selection in proposals submitted to this call.{% endif %}

{% if decision == "canceled" %}You may need to look for alternative offerings or contact the provider directly for more information about their decision.{% endif %}

You can view the call details and manage offerings by visiting:
{{ call_url }}

This is an automated message from {{ site_name }}. Please do not reply to this email.
```

### review_assigned_subject.txt (waldur_mastermind.proposal)

``` txt
New review assignment: {{ proposal_name }}
```

### round_opening_for_reviewers_message.html (waldur_mastermind.proposal)

``` html
<html>
<head>
    <meta charset="UTF-8">
    <title>New round opening</title>
</head>
<body>
    <p>Dear {{ reviewer_name }},</p>

    <p>A new review round is opening for call "<strong>{{ call_name }}</strong>" where you are registered as a reviewer.</p>

    <h4>Round details:</h4>
    <ul>
      <li><strong>Round:</strong> {{ round_name }}</li>
      <li><strong>Submission period:</strong> {{ start_date }} to {{ end_date }}</li>
    </ul>

    <p>You may be assigned proposals to review once they are submitted. Please ensure your availability during the review period.</p>

    <p>If you anticipate any conflicts or periods of unavailability during this time, please notify the call manager as soon as possible.</p>
    <p>View call details: <a href="{{ call_url }}">{{ call_url }}</a></p>

    <p><em>This is an automated message from the {{ site_name }}. Please do not reply to this email.</em></p>

</body>
</html>
```

### reviews_complete_message.html (waldur_mastermind.proposal)

``` html
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Reviews completed</title>
  </head>
  <body>
    <p>Dear call manager,</p>

    <p>
      All required reviews have been completed for proposal
      "<strong>{{ proposal_name }}</strong>" in call
      "<strong>{{ call_name }}</strong>".
    </p>

    <h3>Review summary</h3>
    <ul>
      <li><strong>Proposal:</strong> {{ proposal_name }}</li>
      <li><strong>Submitted by:</strong> {{ submitter_name }}</li>
      <li><strong>Number of submitted reviews:</strong> {{ reviews_count }}</li>
      <li><strong>Average score:</strong> {{ average_score }}/5</li>
    </ul>

    <h3>Review details</h3>
    <ol>
      {% for r in reviews %}
      <li>
        <strong>{{ r.reviewer_name }}</strong>
        &nbsp;-&nbsp;{{ r.score }}/5
        &nbsp;-&nbsp;{{ r.submitted_at|date:"Y-m-d H:i" }}
      </li>
      {% empty %}
      <li>No individual reviews available.</li>
      {% endfor %}
    </ol>

    <p>
      ACTION REQUIRED: Please review the evaluation and make a decision on this proposal.
    </p>

    <p>
      <a href="{{ proposal_url }}">
        {{ proposal_url }}
      </a>
    </p>

    <p>
      This is an automated message from the {{ site_name }}. Please do not reply to this email.
    </p>
  </body>
</html>
```

### review_rejected_message.html (waldur_mastermind.proposal)

``` html
<html>
<head>
    <meta charset="UTF-8">
    <title>Reviewer Assignment Rejected</title>
</head>
<body>
    <p>Dear call manager,</p>

    <p>A reviewer has rejected their assignment to review proposal "{{ proposal_name }}" in call "{{ call_name }}".</p>

    <p>
        <strong>Assignment details:</strong><br>
        - Reviewer: {{ reviewer_name }}<br>
        - Assigned date: {{ assign_date }}<br>
        - Rejected date: {{ rejection_date }}
    </p>

    <p>
        <strong style="color: #d9534f;">ACTION REQUIRED:</strong> Please assign a new reviewer to maintain the minimum required number of reviews for this proposal.
    </p>

    <p>
        <strong>Review Progress:</strong><br>
        - Submitted reviews: {{ submitted_reviews }}<br>
        - Pending reviews: {{ pending_reviews }}<br>
        - Rejected reviews: {{ rejected_reviews }}<br>
    </p>

    <p>
        You can assign a new reviewer by visiting:<br>
        <a href="{{ create_review_link }}">{{ create_review_link }}</a>
    </p>

    <p>
        This is an automated message from the {{ site_name }}. Please do not reply to this email.
    </p>
</body>
</html>
```

### review_assigned_message.txt (waldur_mastermind.proposal)

``` txt
Dear {{ reviewer_name }},

You have been assigned to review a proposal in call "{{ call_name }}".

Proposal details:
- Proposal name: {{ proposal_name }}
- Submitted by: {{ proposal_creator_name }}
- Date submitted: {{ submission_date }}
- Review deadline: {{ review_deadline }}

Please log in to the platform to review the proposal. You can accept or reject this review assignment by visiting:

{{ link_to_reviews_list }}

If you accept this assignment, you'll be able to access the full proposal content and submit your review.

This is an automated message from {{ site_name }}. Please do not reply to this email.
```

### requested_offering_decision_message.html (waldur_mastermind.proposal)

``` html
<html>
<head>
    <meta charset="UTF-8">
    <title>Offering request {{ decision }}</title>
</head>
<body>
    <p>Dear call manager,</p>

    <p>The provider has <strong>{{ decision }}</strong> the request to include offering "<strong>{{ offering_name }}</strong>" in call "<strong>{{ call_name }}</strong>".</p>

    <p><strong>Offering details:</strong></p>
    <ul>
        <li><strong>Offering:</strong> {{ offering_name }}</li>
        <li><strong>Provider:</strong> {{ provider_name }}</li>
        <li><strong>Decision Date:</strong> {{ decision_date }}</li>
        <li><strong>State:</strong> {{ decision }}</li>
    </ul>

    {% if decision == "accepted" %}
    <p>This offering is now available for selection in proposals submitted to this call.</p>
    {% endif %}

    {% if decision == "canceled" %}
    <p>You may need to look for alternative offerings or contact the provider directly for more information about their decision.</p>
    {% endif %}

    <p>You can view the call details and manage offerings by visiting:<br>
    <a href="{{ call_url }}">{{ call_url }}</a></p>

    <p><em>This is an automated message from {{ site_name }}. Please do not reply to this email.</em></p>
</body>
</html>
```

### round_opening_for_reviewers_message.txt (waldur_mastermind.proposal)

``` txt
Dear {{ reviewer_name }},

A new review round is opening for call "{{ call_name }}" where you are registered as a reviewer.

Round details:
- Round: {{ round_name }}
- Submission period: {{ start_date }} to {{ end_date }}

You may be assigned proposals to review once they are submitted. Please ensure your availability during the review period.

If you anticipate any conflicts or periods of unavailability during this time, please notify the call manager as soon as possible.

View call details: {{ call_url }}

This is an automated message from the {{ site_name }}. Please do not reply to this email.
```

### round_closing_for_managers_message.txt (waldur_mastermind.proposal)

``` txt
Dear call manager,

The round "{{ round_name }}" for call "{{ call_name }}" has now closed.

Round summary:
- Total proposals submitted: {{ total_proposals }}
- Start date: {{ start_date }}
- Closed date: {{ close_date }}

Based on the review strategy selected for this round ({{ review_strategy }}), the system has:
- Set all draft proposals to "canceled" state
- Moved all submitted proposals to "in_review" state
- Created {{ total_reviews }} review assignments

You can view the round details and manage proposals by visiting:
{{ round_url }}

This is an automated message from {{ site_name }}. Please do not reply to this email.
```

### new_proposal_submitted_subject.txt (waldur_mastermind.proposal)

``` txt
New proposal submitted: {{ proposal_name }}
```

### proposal_cancelled_message.html (waldur_mastermind.proposal)

``` html
<html>
<head>
    <meta charset="UTF-8">
    <title>Proposal Canceled</title>
</head>
<body>
    <p>Dear {{ proposal_creator_name }},</p>

    <p>Your proposal "{{ proposal_name }}" in call "{{ call_name }}" has been canceled.</p>

    <p>
        <strong>Cancellation details:</strong><br>
        - Proposal: {{ proposal_name }}<br>
        - Cancelation date: {{ cancellation_date }}<br>
        - Reason for cancellation: Round closure/The submission deadline has passed and the proposal was not finalized
    </p>

    <p>All draft proposals are automatically canceled when a round closes. This ensures that only fully submitted proposals proceed to the review stage.</p>

    <p>
        You can still view your proposal by visiting:<br>
        <a href="{{ proposal_url }}">{{ proposal_url }}</a>
    </p>

    <p>If you would like to resubmit your proposal, please check for upcoming rounds in this call or other relevant calls.</p>

    <p>
        This is an automated message from the {{ site_name }}. Please do not reply to this email.
    </p>
</body>
</html>
```

### new_proposal_submitted_message.txt (waldur_mastermind.proposal)

``` txt
Dear call manager,

A new proposal has been submitted to the call "{{ call_name }}".

Proposal details:
- Name: {{ proposal_name }}
- Submitted by: {{ proposal_creator_name }}
- Submission date: {{ submission_date }}
- Round: {{ round_name }}

You can review this proposal by visiting the following URL:
{{ proposal_url }}

This is an automated message from the {{ site_name }}. Please do not reply to this email.
```

### review_rejected_message.txt (waldur_mastermind.proposal)

``` txt
Dear call manager,

A reviewer has rejected their assignment to review proposal "{{ proposal_name }}" in call "{{ call_name }}".

Assignment details:
- Reviewer: {{ reviewer_name }}
- Assigned date: {{ assign_date }}
- Rejected date: {{ rejection_date }}

ACTION REQUIRED: Please assign a new reviewer to maintain the minimum required number of reviews for this proposal.

Review Progress:
- Submitted reviews: {{ submitted_reviews }}
- Pending reviews: {{ pending_reviews }}
- Rejected reviews: {{ rejected_reviews }}

You can assign a new reviewer by visiting:
{{ create_review_link }}

This is an automated message from the {{ site_name }}. Please do not reply to this email.
```

### round_closing_for_managers_subject.txt (waldur_mastermind.proposal)

``` txt
Round closed: {{ round_name }} - {{ call_name }}
```

### proposal_state_changed_message.html (waldur_mastermind.proposal)

``` html
<html>
<head>
    <meta charset="UTF-8">
    <title>Proposal Status Update</title>
    <style>
        body {
            color: #333;
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
        }
        .header {
            margin-bottom: 20px;
        }
        .state-change {
            background-color: #f9f9f9;
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 20px;
        }
        .message-box {
            padding: 15px;
            margin: 15px 0;
        }
        .footer {
            margin-top: 30px;
            color: #777;
            border-top: 1px solid #eee;
            padding-top: 10px;
        }
    </style>
</head>
<body>
    <div class="header">
        <p>Dear {{ proposal_creator_name }},</p>
        <p>The state of your proposal "<strong>{{ proposal_name }}</strong>" in call "<strong>{{ call_name }}</strong>" has been updated.</p>
    </div>

    <div class="state-change">
        <h3>State change:</h3>
        <ul>
            <li><strong>Previous state:</strong> {{ previous_state }}</li>
            <li><strong>New state:</strong> {{ new_state }}</li>
            <li><strong>Updated on:</strong> {{ update_date }}</li>
        </ul>

        {% if new_state == 'accepted' %}
        <ul>
            <li><strong>Project created:</strong> {{ project_name }}</li>
            <li><strong>Allocation start date:</strong> {{ allocation_date }}</li>
            <li><strong>Duration:</strong> {{ duration }} days</li>
        </ul>
        <div>
            <h4>Allocated resources:</h4>
            {% for resource in allocated_resources %}
            <div>
                <strong>{{ forloop.counter }}.</strong> {{ resource.name }} - {{ resource.provider_name }} - {{ resource.plan_name }} - Provisioned
            </div>
            {% empty %}
            <p><em>No resources allocated yet.</em></p>
            {% endfor %}
        </div>
        {% endif %}

        {% if new_state == 'rejected' %}
        <p><strong>Feedback:</strong> {{ rejection_feedback }}</p>
        {% endif %}
    </div>

    <div class="message-box">
        {% if new_state == 'submitted' %}
        <p>Your proposal has been successfully submitted and will be reviewed according to the review process for this call. You will receive further notifications as your proposal progresses through the review process.</p>
        {% endif %}

        {% if new_state == 'in_review' %}
        <p>Your proposal is now under review. Reviewers will evaluate your proposal based on the criteria specified in the call. This process may take {{ review_period }} days according to the round's review period.</p>
        {% endif %}

        {% if new_state == 'accepted' %}
        <p>Congratulations! Your proposal has been accepted. Resources have been allocated based on your request and a new project has been created. You can access your project by clicking the link below.</p>
        {% endif %}

        {% if new_state == 'rejected' %}
        <p>We regret to inform you that your proposal has not been accepted at this time. Please review any feedback provided above. You may have the opportunity to submit a revised proposal in future rounds.</p>
        {% endif %}
    </div>

    <a href="{{ proposal_url }}">View Proposal</a>
    <br>
    {% if new_state == 'accepted' and project_url %}
    <a href="{{ project_url }}">View Project</a>
    {% endif %}

    <div class="footer">
        <p>This is an automated message from the {{ site_name }}. Please do not reply to this email.</p>
    </div>
</body>
</html>
```

### new_review_submitted_subject.txt (waldur_mastermind.proposal)

``` txt
Review submitted for proposal: {{ proposal_name }}
```

### requested_offering_decision_subject.txt (waldur_mastermind.proposal)

``` txt
Offering request {{ decision }}: {{ offering_name }}
```

### proposal_decision_for_reviewer_message.html (waldur_mastermind.proposal)

``` html
<html>
<head>
    <meta charset="UTF-8">
    <title>Proposal {{ proposal_state }}</title>
</head>
<body>
    <p>Dear {{ reviewer_name }},</p>

    <p>A decision has been made on the proposal "<strong>{{ proposal_name }}</strong>" in call "<strong>{{ call_name }}</strong>" that you reviewed.</p>

    <p><strong>Decision details:</strong></p>
    <ul>
        <li><strong>Proposal:</strong> {{ proposal_name }}</li>
        <li><strong>Decision:</strong> {{ proposal_state }}</li>
        <li><strong>Decision date:</strong> {{ decision_date }}</li>
    </ul>

    {% if proposal_state == "rejected" and rejection_reason %}
    <p><strong>Reason:</strong> {{ rejection_reason }}</p>
    {% endif %}

    <p>Thank you for your valuable contribution to the review process. Your expert assessment helped inform this decision.</p>

    <p>View proposal: <a href="{{ proposal_url }}">{{ proposal_url }}</a></p>

    <p><em>This is an automated message from {{ site_name }}. Please do not reply to this email.</em></p>
</body>
</html>
```

### review_assigned_message.html (waldur_mastermind.proposal)

``` html
<html>
<head>
    <meta charset="UTF-8">
</head>
<body>
<p>Dear {{ reviewer_name }},</p>

<p>You have been assigned to review a proposal in call "<strong>{{ call_name }}</strong>".</p>

<p><strong>Proposal details:</strong></p>
<ul>
    <li><strong>Proposal name:</strong> {{ proposal_name }}</li>
    <li><strong>Submitted by:</strong> {{ proposal_creator_name }}</li>
    <li><strong>Date submitted:</strong> {{ submission_date }}</li>
    <li><strong>Review deadline:</strong> {{ review_deadline }}</li>
</ul>

<p>Please log in to the platform to review the proposal. You can accept or reject this review assignment by visiting:</p>

<a href="{{ link_to_reviews_list }}">{{ link_to_reviews_list }}</a>

<p>If you accept this assignment, you'll be able to access the full proposal content and submit your review.</p>

<p><em>This is an automated message from {{ site_name }}. Please do not reply to this email.</em></p>
</body>
</html>
```

### proposal_cancelled_message.txt (waldur_mastermind.proposal)

``` txt
Dear {{ proposal_creator_name }},

Your proposal "{{ proposal_name }}" in call "{{ call_name }}" has been canceled.

Cancellation details:
- Proposal: {{ proposal_name }}
- Cancellation date: {{ cancellation_date }}
- Reason for cancellation: Round closure/The submission deadline has passed and the proposal was not finalized

All draft proposals are automatically canceled when a round closes. This ensures that only fully submitted proposals proceed to the review stage.

You can still view your proposal by visiting:
{{ proposal_url }}

If you would like to resubmit your proposal, please check for upcoming rounds in this call or other relevant calls.

This is an automated message from the {{ site_name }}. Please do not reply to this email.
```

## waldur_mastermind.support

### notification_comment_updated_message.txt (waldur_mastermind.support)

``` txt
Hello!

The comment has been updated. Please go to {{issue_url}} to see it.
```

### notification_comment_updated_message.html (waldur_mastermind.support)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>The comment has been updated ({{ issue.key }})</title>
</head>
<body>
<p>
    {{ comment.author.name }} updated comment.
</p>
<p>
    <a href="{{ issue_url }}">[{{ issue.key }}] {{ issue.summary }}</a>
</p>
<p>
    Old comment:
</p>
<p>
    {{ old_description|safe }}
</p>
<p>
    New comment:
</p>
<p>
    {{ description|safe }}
</p>
</body>
</html>
```

### description.txt (waldur_mastermind.support)

``` txt
{{issue.description}}

Additional Info:
{% if issue.customer %}- Organization: {{issue.customer.name}}{% endif %}
{% if issue.project %}- Project: {{issue.project.name}}{% endif %}
{% if issue.resource %}
    {% if issue.resource.service_settings %}
        {% if issue.resource.service_settings.type %}- Service type: {{issue.resource.service_settings.type}}{% endif %}
        - Offering name: {{ issue.resource.service_settings.name }}
        - Offering provided by: {{ issue.resource.service_settings.customer.name }}
    {% endif %}
    - Affected resource: {{issue.resource}}
    - Backend ID: {{issue.resource.backend_id}}
{% endif %}
- Site name: {{ settings.WALDUR_CORE.SITE_NAME }}
- Site URL: {{ config.HOMEPORT_URL }}
```

### summary.txt (waldur_mastermind.support)

``` txt
{% if issue.customer.abbreviation %}{{issue.customer.abbreviation}}: {% endif %}{{issue.summary}}
```

### notification_issue_updated_subject.txt (waldur_mastermind.support)

``` txt
Updated issue: {{issue.key}} {{issue.summary}}
```

### notification_comment_added_message.html (waldur_mastermind.support)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>The issue you have created ({{ issue.key }}) has a new comment</title>
</head>
<body>
<p>
    {% if is_system_comment %}
        Added a new comment.
    {% else %}
        {{ comment.author.name }} added a new comment.
    {% endif %}
</p>
<p>
    <a href="{{ issue_url }}">[{{ issue.key }}] {{ issue.summary }}</a>
</p>
<div>
    {{ description|safe }}
</div>
</body>
</html>
```

### notification_issue_feedback_message.html (waldur_mastermind.support)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>The issue you have ({{ issue.key }}) has been updated</title>

    <style>
        * {
          font-family: sans-serif;
        }
        .rating {
          unicode-bidi: bidi-override;
          direction: rtl;
          width: 500px;
        }
        .rating > a {
          display: inline-block;
          position: relative;
          width: 1.1em;
          text-decoration: none;
          font-size: xx-large;
          color: rgba(180, 179, 178, 1);
        }
        .rating > a:hover:before,
        .rating > a:hover ~ a:before {
           content: "\2605";
           position: absolute;
           color: rgba(224, 194, 75, 1);
        }
    </style>

</head>
<body>
<p>Hello, {{issue.caller.full_name}}!</p>
<p>We would like to hear your feedback regarding your recent experience with support for
    <a href='{{issue_url}}'>{{ issue.summary }}</a>.
</p>
<p>Click the stars below to provide your feedback:</p>
<div class="rating">
    {% for link in feedback_links reversed %}
        <a href='{{link.link}}'>☆</a>
    {% endfor %}
</div>
</body>
</html>
```

### notification_issue_feedback_message.txt (waldur_mastermind.support)

``` txt
Hello, {{issue.caller.full_name}}!

We would like to hear your feedback regarding your recent experience with support for {{issue_url}}.

Click on the evaluations below to provide the feedback.

{% for link in feedback_links%}
    {{link.label}}: {{link.link}}
{% endfor %}
```

### notification_comment_added_message.txt (waldur_mastermind.support)

``` txt
Hello!

The issue you have created has a new comment. Please go to {{issue_url}} to see it.
```

### notification_issue_feedback_subject.txt (waldur_mastermind.support)

``` txt
Please share your feedback: {{issue.key}} {{issue.summary}}
```

### notification_comment_added_subject.txt (waldur_mastermind.support)

``` txt
The issue ({{ issue.key }}) you have created has a new comment
```

### notification_issue_updated_message.txt (waldur_mastermind.support)

``` txt
Hello!

The issue you have has been updated.

{% if changed.status %}
Status has been changed from {{ changed.status }} to {{ issue.status }}.
{% endif %}
{% if changed.description %}
Description has been changed from {{ changed.description }} to {{ issue.description }}.
{% endif %}
{% if changed.summary %}
Summary has been changed from {{ changed.summary }} to {{ issue.summary }}.
{% endif %}
{% if changed.priority %}
Priority has been changed from {{ changed.priority }} to {{ issue.priority }}.
{% endif %}

Please go to {{issue_url}} to see it.
```

### notification_issue_updated_message.html (waldur_mastermind.support)

``` html
<html>
<head lang="en">
    <meta charset="UTF-8">
    <title>The issue you have ({{ issue.key }}) has been updated</title>
</head>
<body>
<p>
    Hello!
</p>
{% if changed.status %}
<p>
    Status has been changed from <strong>{{ changed.status }}</strong> to <strong>{{ issue.status }}</strong>.
</p>
{% endif %}
{% if old_description %}
<p>
    Description has been changed from <strong>{{ old_description|safe }}</strong> to <strong>{{ description|safe }}</strong>.
</p>
{% endif %}
{% if changed.summary %}
<p>
    Summary has been changed from <strong>{{ changed.summary }}</strong> to <strong>{{ issue.summary }}</strong>.
</p>
{% endif %}
{% if changed.priority %}
<p>
    Priority has been changed from <strong>{{ changed.priority }}</strong> to <strong>{{ issue.priority }}</strong>.
</p>
{% endif %}
<p>
    Please visit <a href="{{ issue_url }}">{{ site_name }}</a> to find out more details.
</p>
</body>
</html>
```

### notification_comment_updated_subject.txt (waldur_mastermind.support)

``` txt
Issue {{ issue.key }}. The comment has been updated
```

