import datetime
import hashlib
import logging

import httpx
import openportal
from django.contrib import auth
from django.core.cache import cache
from django.http import JsonResponse
from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import IsAuthenticated

from waldur_auth_social.models import IdentityProvider
from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.authentication import refresh_token, set_user_context
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import get_connected_projects, get_visible_projects
from waldur_core.users.enums import InvitationState
from waldur_mastermind.invoices import models as invoice_models

from . import models, serializers, tasks, utils
from .board import OpenPortalBoard

logger = logging.getLogger(__name__)

User = auth.get_user_model()


def _get_project_spend_info_by_username(request, user, username):
    logger.info(f"api/openportal/monthly_spend request for username: {username}")

    # use the association to find the project_info, from which we can get the project
    project = None
    project_id = None

    # Note that there could be many projects associated with this username
    # in the general case, but we will only return the first one for now
    # as in BriCS we use project-specific user names
    try:
        associations = models.Association.objects.filter(username=username)

        for association in associations:
            if not (user.is_staff or user.is_support):
                if association.user != user:
                    logger.warning(
                        f"User {user} is not the owner of the association {association}"
                    )
                    continue

            if association.has_project_identifier():
                project_id = association.get_project_identifier()

                try:
                    project_info = models.ProjectInfo.objects.filter(
                        shortname=project_id.project
                    ).first()
                except Exception:
                    continue

                if project_info is not None:
                    if project_info.project is not None:
                        project = project_info.project
                        break
    except Exception as e:
        logger.error(f"Error looking up username {username}: {e}")
        response = JsonResponse({"error": "Username not found."})
        response.status_code = status.HTTP_404_NOT_FOUND
        return response

    if project is None:
        logger.error(f"Username {username} not found.")
        response = JsonResponse({"error": "Username not found."})
        response.status_code = status.HTTP_404_NOT_FOUND
        return response

    # get the total credit available for this project
    credit = None

    try:
        project_credit = invoice_models.ProjectCredit.objects.get(project=project)

        if project_credit.value is not None:
            credit = float(project_credit.value)

    except Exception:
        pass

    try:
        end_date = project.end_date.strftime("%Y-%m-%d")
    except Exception:
        end_date = None

    # now calculate the total spend across all OpenPortal allocations
    # for this project
    total_spend = None

    # find any openportal allocations associated with the project
    try:
        allocations = models.Allocation.objects.filter(project=project, is_active=True)

        if allocations:
            total_spend = 0.0

            for allocation in allocations:
                total_spend += float(allocation.node_usage)
    except Exception:
        pass

    data = {
        "projects": [
            {
                "name": str(project.name),
                "identifier": str(project_id),
                "usage": total_spend,
                "limit": credit,
                "end_date": end_date,
            }
        ]
    }

    logger.info(f"project_spend_info({username}) {data}")

    return JsonResponse(data)


def _get_project_spend_info_by_email(request, user, email):
    logger.info(f"api/openportal/monthly_spend request for email: {email}")
    # TODO

    return JsonResponse(None)


def _get_project_spend_info_by_project_id(request, user, project_id):
    logger.info(f"api/openportal/monthly_spend request for project_id: {project_id}")
    # TODO

    return JsonResponse(None)


@extend_schema(exclude=True)
@api_view(["GET"])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def project_spend_info(request):
    """
    Return the monthly spend for the user in the format:

    {
        projects: [
            {
                "name": "Project human name"
                "identifier": "Project identifier"
                "usage": 123.45
                "limit": 205.52
                "end_date": "2025-10-31"
            },
            ...
        ]
    }

    This will either search for projects by local username,
    or by email address, or for the current Waldur user,
    or by the project identifier.

    This returns the current spend, and credit limit for the current month
    for each matching project, as well as the project end date (when the
    credits expire).

    Note that the only staff or support users can query any project.
    Non-staff users can only query the projects to which they belong.
    """
    user = request.user

    if not (user.is_authenticated or user.is_active):
        response = JsonResponse({})
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return response

    username = request.query_params.get("username")

    if username:
        username = str(username).lstrip().rstrip()
        if len(username) == 0:
            username = None

    email = request.query_params.get("email")

    if email:
        email = str(email).lstrip().rstrip()
        if len(email) == 0:
            email = None

    project_id = request.query_params.get("project_id")

    if project_id:
        project_id = str(project_id).lstrip().rstrip()
        if len(project_id) == 0:
            project_id = None

    if username is None and email is None and project_id is None:
        email = user.email

    if username is not None:
        return _get_project_spend_info_by_username(request, user, username=username)
    elif email is not None:
        return _get_project_spend_info_by_email(request, user, email=email)
    elif project_id is not None:
        return _get_project_spend_info_by_project_id(
            request, user, project_id=project_id
        )
    else:
        return JsonResponse(None)


@extend_schema(exclude=True)
@api_view(["GET"])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def customer_spend_info(request):
    user = request.user

    if not (user.is_authenticated or user.is_active):
        response = JsonResponse({})
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return response

    if not (user.is_staff or user.is_support):
        response = JsonResponse({})
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return response

    customer = request.query_params.get("customer")

    if not customer:
        response = JsonResponse({"error": "A customer must be provided."})
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response

    customer = str(customer).lstrip().rstrip()

    if len(customer) == 0:
        response = JsonResponse({"error": "A customer must be provided."})
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response

    # get an optional "use_project_ids" query parameter
    use_project_ids = request.query_params.get("use_project_ids", "false").lower()

    if use_project_ids not in ["true", "false"]:
        response = JsonResponse(
            {"error": "The 'use_project_ids' parameter must be 'true' or 'false'."}
        )
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response

    use_project_ids = use_project_ids == "true"

    # get an optional "start_date" query parameter which is the start month-year
    start_date = request.query_params.get("start_date")

    if start_date:
        start_date = str(start_date).lstrip().rstrip()

        if len(start_date) == 0:
            start_date = None

    if start_date is not None:
        try:
            parts = start_date.split("-")
            start_date = datetime.date(year=int(parts[0]), month=int(parts[1]), day=1)

        except Exception as e:
            response = JsonResponse({"error": str(e)})
            response.status_code = status.HTTP_400_BAD_REQUEST
            return response

    # get an optional "end_date" query parameter which is the end month-year
    end_date = request.query_params.get("end_date")

    if end_date:
        end_date = str(end_date).lstrip().rstrip()

        if len(end_date) == 0:
            end_date = None

    if end_date is not None:
        try:
            parts = end_date.split("-")
            end_date = datetime.date(year=int(parts[0]), month=int(parts[1]), day=1)
            end_date = utils.get_last_day_of_month(end_date)

            if start_date is not None and end_date < start_date:
                response = JsonResponse({"error": "End date must be after start date."})
                response.status_code = status.HTTP_400_BAD_REQUEST
                return response

        except Exception as e:
            response = JsonResponse({"error": str(e)})
            response.status_code = status.HTTP_400_BAD_REQUEST
            return response

    orgs = structure_models.Customer.objects.filter(name=customer)

    if len(orgs) != 1:
        response = JsonResponse({})
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response

    org = orgs[0]

    # get all of the projects in this organisation
    projs = structure_models.Project.objects.filter(customer=org)

    response = {}
    response["customer"] = org.name

    if start_date is not None:
        response["start_date"] = start_date.strftime("%Y-%m-%d")

    if end_date is not None:
        response["end_date"] = end_date.strftime("%Y-%m-%d")

    response["use_project_ids"] = use_project_ids

    projects = {}

    current_year = datetime.date.today().year

    for proj in projs:
        try:
            credit = invoice_models.ProjectCredit.objects.filter(project=proj)[0].value
        except Exception:
            credit = 0

        project = {}
        project["total_allocation"] = float(credit)
        project["total_consumption"] = 0.0
        project["resources"] = []

        # get all of the invoice items for this project - this contains
        # all of the consumption details, and is not deleted when the
        # project is deleted
        try:
            invoice_items = invoice_models.InvoiceItem.objects.filter(project=proj)
        except Exception:
            invoice_items = []

        project_start_date = proj.start_date
        project_end_date = proj.end_date

        if project_start_date is None:
            project_start_date = proj.created.date()

        project["start_date"] = project_start_date.strftime("%Y-%m-%d")

        if project_end_date is not None:
            project["end_date"] = project_end_date.strftime("%Y-%m-%d")

        try:
            project["num_members"] = len(proj.get_users())
        except Exception:
            project["num_members"] = 0

        resources = {}

        for invoice_item in invoice_items:
            usage = float(invoice_item.price)

            if usage == 0:
                continue
            elif usage < 0:
                # this is a credit, so add it to the total allocation
                project["total_allocation"] += abs(usage)
                continue

            # get the name of the resource consumed
            try:
                resource = invoice_item.resource.offering.name.strip()
            except Exception:
                logger.warning(
                    f"Invoice item {invoice_item} has no resource offering - skipping"
                )
                continue

            if resource is None or len(str(resource)) == 0:
                logger.warning(
                    f"Invoice item {invoice_item} has no resource name - skipping"
                )
                continue

            if resource not in resources:
                resources[resource] = {
                    "name": resource,
                    "consumption": [],
                }

            # get the month and year of the usage
            try:
                month = invoice_item.invoice.month
                year = invoice_item.invoice.year
            except Exception:
                logger.warning(
                    f"Invoice item {invoice_item} has no invoice month/year - skipping"
                )
                continue

            if month is None or year is None:
                logger.warning(
                    f"Invoice item {invoice_item} has no invoice month/year - skipping"
                )
                continue

            if month < 1 or month > 12:
                logger.warning(
                    f"Invoice item {invoice_item} has invalid month {month} - skipping"
                )
                continue

            if year < 2000 or year > current_year:
                logger.warning(
                    f"Invoice item {invoice_item} has invalid year {year} - skipping"
                )
                continue

            consumption_date = datetime.date(year=year, month=month, day=1)

            # change the day to the last of the month
            consumption_date = utils.get_last_day_of_month(consumption_date)

            if (
                project_start_date is not None
                and consumption_date < project_start_date
                and usage == 0.0
            ):
                # skip this zero usage if it is before the project start date
                continue

            if start_date is not None and consumption_date < start_date:
                continue

            if end_date is not None and consumption_date > end_date:
                continue

            # have we seen this month/year for this resource? - if so,
            # then we need to add the usage to the existing entry
            found = False

            for entry in resources[resource]["consumption"]:
                if entry["year"] == year and entry["month"] == month:
                    entry["value"] += usage
                    found = True
                    break

            if not found:
                resources[resource]["consumption"].append(
                    {
                        "year": year,
                        "month": month,
                        "value": usage,
                    }
                )

            project["total_consumption"] += usage

        project["resources"] = list(resources.values())
        project["balance"] = project["total_allocation"] - project["total_consumption"]

        project_short_name = str(utils.get_project_shortname(proj))

        project["shortname"] = project_short_name

        if use_project_ids:
            # get the shortname for the project from OpenPortal
            project_name = project_short_name
        else:
            project_name = str(proj.name).strip()

        projects[project_name] = project

    response["projects"] = projects

    response = JsonResponse(response)
    return response


@extend_schema(exclude=True)
@api_view(["GET"])
@authentication_classes([])
@permission_classes([])
def fetch_job(request):
    """
    End-point called by the OpenPortal bridge agent to signal to Waldur
    that new a job has arrived and it needs to be fetched.

    This triggers the code to fetch the job and then submit it for
    processing to one of the backend celery workers.

    As argument, you need to pass in the `job_id` query parameter,
    which must match one of the jobs in the OpenPortal bridge queue.

    If a job is not found, it will return an authorisation error
    (403 Forbidden), thereby preventing "unauthorised" access to
    this end-point.

    If the job is found, it will return a 200 OK response.

    The use of the job-id acts as a bit of authorisation, as the
    job-id is random, and is only known to the OpenPortal bridge agent.
    It is a de-facto secret shared between the OpenPortal bridge agent
    and Waldur, and is not known to any other user or system.
    """

    board = OpenPortalBoard()

    job_id = request.query_params.get("job_id")

    if not job_id:
        response = JsonResponse({})
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return response

    # move to serlialiser django
    job_id = str(job_id).lstrip().rstrip()

    if len(job_id) == 0:
        response = JsonResponse({})
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return response

    # fetch this job from the OpenPortal bridge queue
    try:
        job = board.fetch_job(job_id)
        if job is None:
            response = JsonResponse({})
            response.status_code = status.HTTP_401_UNAUTHORIZED
            return response
    except Exception as e:
        logger.error(f"Error fetching job {job_id}: {e}")
        response = JsonResponse({})
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return response

    job_id = str(job.id).lstrip().rstrip()

    if len(job_id) == 0:
        logger.error(f"Job {job} has no job_id")
        response = JsonResponse({})
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return response

    if job.state != openportal.Status.pending():
        logger.error(f"Job {job_id} is not in PENDING state, but in {job.state}")
        response = JsonResponse({})
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return response

    # create a Job model object for this job
    job_model, created = models.Job.objects.get_or_create(
        job_id=job_id,
        defaults={
            "job_data": job.to_json(),
            "state": models.Job.State.PENDING,
        },
    )

    if not created:
        logger.warning(f"Job {job_id} already exists in the database... re-running?")

    if job_model.state != models.Job.State.PENDING:
        logger.error(f"Job {job_id} is not in PENDING state, but in {job_model.state}")
        response = JsonResponse({})
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return response

    # submit the job for processing
    logger.info(f"Submitting job {job_id} for processing")
    tasks.run_job.delay(core_utils.serialize_instance(job_model))

    response = JsonResponse({})
    response.status_code = status.HTTP_200_OK
    return response


@extend_schema(
    parameters=[
        OpenApiParameter(
            name="q",
            type=str,
            location=OpenApiParameter.QUERY,
            description="Free text search query (email, short_name, project_name, or project_id)",
            required=True,
        ),
    ],
    responses={200: serializers.AccessResponseSerializer(many=True)},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def access_for_email(request):
    """
    Return the level of access available based on an intelligent free text search query.

    This endpoint automatically detects the search type based on the query format:
    - Email addresses (contains @)
    - Short names (alphanumeric between 5 and 20 characters)
    - Project IDs (alphanumeric, full project ID contains dot)
    - Project names (any text)

    The aim of this API call is to allow, e.g. Keycloak, to determine whether
    an identity connected to the specified email address is authorised
    to access Waldur, and is thus allowed to log in.

    It also allows collection of access metadata, e.g. which projects
    a user belongs to, which platform they can access, and what account
    should be used.

    Query parameters:
    - q: Free text search query (required) - can be email, short_name, project_name, or project_id

    Note that this is only available to authenticated users, and a user
    can only query for which they have access (i.e. a staff user can query
    anything, but a non-staff user can only query their own email/short_name
    or projects they belong to)

    The returned JSON object is as follows:

    Single user result:
    {
        "email": email_in_waldur,
        "status": status_in_waldur,
        "short_name": shortname_in_waldur,
        "projects": projects as described below,
        "invited_by": email of the user who invited this person, if invited
        "reason": the reason for any rejection, if status is rejected
    }

    Multiple user result (for project searches):
    [
        {
            "email": email_in_waldur,
            "status": status_in_waldur,
            "short_name": shortname_in_waldur,
            "projects": projects,
            "invited_by": "",
            "reason": ""
        },
        ...
    ]

    Where "projects" is a dictionary as follows, with key/value pairs
    for each project the user with the email can access

    {
        "project-a": {
            "name": "Project A",
            "resources": [
                {
                    "name": "batch.cluster1.example",
                    "username": "user.proj-a"
                },
                {
                    "name": "batch.cluster2.example",
                    "username": "user.proj-a"
                }
            ]
        },
        "project-b": {
            "name": "Project B",
            "resources": [
                {
                    "name": "batch.cluster2.example",
                    "username": "user.proj-b"
                }
            ]
        }
    }

    """
    user = request.user

    if not (user.is_authenticated or user.is_active):
        response = JsonResponse({})
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return response

    # Get the free text search query
    query = request.query_params.get("q")

    # Also support legacy parameters for backwards compatibility
    email = request.query_params.get("email")
    short_name = request.query_params.get("short_name")
    project_name = request.query_params.get("project_name")
    project_id = request.query_params.get("project_id")

    # If no 'q' parameter, check for legacy parameters
    if query is None:
        if email:
            query = email
        elif short_name:
            query = short_name
        elif project_name:
            query = project_name
        elif project_id:
            query = project_id

    if query is None:
        response = JsonResponse(
            {
                "error": "Search query parameter 'q' is required. You can search by email, short_name, project_name, or project_id."
            }
        )
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response

    # Clean and normalize the query
    query = str(query).strip()

    if len(query) == 0:
        response = JsonResponse({"error": "Search query cannot be empty."})
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response

    can_query_all = user.is_staff or user.is_support

    logger.info(
        f"api/openportal/access_for_email request for query='{query}' from {user} ({user.email})"
    )

    # Intelligent search routing based on query format
    return _intelligent_search(user, query, can_query_all)


@extend_schema(exclude=True)
@api_view(["GET"])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def whoami(request):
    user = request.user

    if not (user.is_authenticated or user.is_active):
        response = JsonResponse({})
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return response

    response = JsonResponse(
        {
            "first_name": f"{user.first_name}",
            "last_name": f"{user.last_name}",
            "user_name": f"{user.username}",
            "email": f"{user.email}",
            "date_joined": f"{user.date_joined}",
            "organization": f"{user.organization}",
            "job_title": f"{user.job_title}",
            "phone_number": f"{user.phone_number}",
            "is_staff": f"{user.is_staff}",
        }
    )
    return response


@extend_schema(exclude=True)
@api_view(["GET"])
@authentication_classes([])
@permission_classes([])
def get_api_token(request):
    # Extract OIDC token from Authorisation header
    auth_header = request.headers.get("Authorization", "")

    if not auth_header.lower().startswith("bearer "):
        return JsonResponse(
            {"error": "Authorisation header missing or invalid"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    raw_oidc_token = auth_header.split(" ", 1)[1].strip()

    if not raw_oidc_token:
        return JsonResponse(
            {"error": "Bearer token not provided"}, status=status.HTTP_400_BAD_REQUEST
        )

    provider = IdentityProvider.objects.filter(is_active=True).first()

    discovery_url = provider.discovery_url
    client_id = provider.client_id
    client_secret = provider.client_secret
    user_field = "email"
    cache_timeout = 300.0  # default 5 min

    if not (discovery_url):
        return JsonResponse(
            {"error": "No discovery url found"}, status=status.HTTP_400_BAD_REQUEST
        )

    data_discovery_url = response = httpx.get(
        discovery_url,
        timeout=5.0,
    )
    introspection_url = data_discovery_url.json()["introspection_endpoint"]

    if not (introspection_url and client_id and client_secret):
        return JsonResponse(
            {"error": "OIDC config incomplete"}, status=status.HTTP_400_BAD_REQUEST
        )
    # Use SHA-256 to hash token to avoid very long keys
    cache_key = f"oidc_token:{hashlib.sha256(raw_oidc_token.encode()).hexdigest()}"

    data = cache.get(cache_key)

    if not data:
        try:
            response = httpx.post(
                introspection_url,
                data={"token": raw_oidc_token},
                auth=(client_id, client_secret),
                timeout=5.0,
            )

        except Exception:
            return JsonResponse(
                {"error": "Introspection failed"}, status=status.HTTP_400_BAD_REQUEST
            )

        if response.status_code != 200:
            return JsonResponse({"error": "Introspection endpoint error."})

        data = response.json()
        if not data.get("active"):
            return JsonResponse({"error": "Token is inactive or invalid."})

        cache.set(cache_key, data, timeout=cache_timeout)

    user_identifier = data.get(user_field)

    if not user_identifier:
        return JsonResponse(
            {"error": f"Token missing '{user_field}' field"},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    # GET Waldur user
    user, __ = core_models.User.objects.get_or_create(username=user_identifier)
    set_user_context(user)

    # Check staff access
    user_access = "staff" if user.is_staff else "not a staff"

    # Sync email with Keycloak response email
    email = data.get("email")
    if email and user.email != email:
        user.save(update_fields=["email"])

    # Generate Waldur API token
    waldur_api_token_obj = refresh_token(user)
    waldur_api_token = waldur_api_token_obj.key

    return JsonResponse(
        {"token": waldur_api_token, "user_access": user_access, "user_email": email}
    )


@extend_schema(
    description=(
        "Map OpenPortal destination strings to Waldur Offering objects. "
        "Pass each destination as a repeated 'identifier' query parameter. "
        "Returns a dict keyed by identifier; unknown destinations map to null. "
        "Accessible to all authenticated users."
    ),
    parameters=[
        OpenApiParameter(
            name="identifier",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            many=True,
            description="OpenPortal destination string (repeatable).",
        ),
    ],
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def offering_mapping(request):
    """
    Map OpenPortal destination strings to Waldur Offering objects.

    Chain: destination -> ServiceSettings (options.instance_name)
           -> Offering (scope GenericFK)
    """
    from django.contrib.contenttypes.models import ContentType

    from waldur_mastermind.marketplace import models as marketplace_models

    identifiers = request.query_params.getlist("identifier")
    if not identifiers:
        return JsonResponse({})

    ss_ct = ContentType.objects.get_for_model(structure_models.ServiceSettings)

    # options is a TextField (not a real JSONField) so key-path ORM lookups
    # don't work. Fetch all ServiceSettings backing OpenPortal Allocations and
    # match instance_name in Python — there are very few of these in practice.
    openportal_ss = structure_models.ServiceSettings.objects.filter(
        id__in=models.Allocation.objects.values("service_settings_id")
    )
    instance_name_to_ss = {
        ss.options.get("instance_name"): ss
        for ss in openportal_ss
        if isinstance(ss.options, dict) and ss.options.get("instance_name")
    }

    result = {}
    for identifier in identifiers:
        ss = instance_name_to_ss.get(identifier)

        if ss is None:
            result[identifier] = None
            continue

        offering = marketplace_models.Offering.objects.filter(
            content_type=ss_ct,
            object_id=ss.pk,
        ).first()

        if offering is None:
            result[identifier] = None
            continue

        result[identifier] = {
            "uuid": str(offering.uuid),
            "name": offering.name,
            "description": offering.description,
            "slug": offering.slug,
        }

    return JsonResponse(result)


@extend_schema(
    description=(
        "Map OpenPortal ProjectIdentifier strings to Waldur Project objects. "
        "Pass each identifier as a repeated 'identifier' query parameter. "
        "Returns a dict keyed by identifier; unknown identifiers map to null. "
        "Staff and support see all projects; regular users see only projects "
        "they are a member of."
    ),
    parameters=[
        OpenApiParameter(
            name="identifier",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            many=True,
            description="OpenPortal ProjectIdentifier string (repeatable).",
        ),
    ],
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def project_mapping(request):
    """
    Map OpenPortal ProjectIdentifier strings to Waldur Project objects.

    Chain: Allocation.backend_id == identifier -> Allocation.project
    """
    identifiers = request.query_params.getlist("identifier")
    if not identifiers:
        return JsonResponse({})

    user = request.user

    if user.is_staff or user.is_support:
        accessible_project_ids = None
    else:
        accessible_project_ids = set(get_visible_projects(user))

    result = {}
    for identifier in identifiers:
        allocation = (
            models.Allocation.objects.filter(backend_id=identifier)
            .select_related("project", "project__customer")
            .first()
        )

        if allocation is None:
            result[identifier] = None
            continue

        project = allocation.project

        if (
            accessible_project_ids is not None
            and project.pk not in accessible_project_ids
        ):
            result[identifier] = None
            continue

        result[identifier] = {
            "uuid": str(project.uuid),
            "name": project.name,
            "customer_uuid": str(project.customer.uuid),
            "customer_name": project.customer.name,
        }

    return JsonResponse(result)


@extend_schema(
    description=(
        "Map OpenPortal UserIdentifier strings (or email addresses) to Waldur User objects. "
        "Pass each value as a repeated 'identifier' query parameter. "
        "If the values contain '@' they are treated as email addresses (used for cached "
        "reports from remote portals); otherwise they are treated as UserIdentifier strings "
        "(used for local OpenPortal resources). "
        "Returns a dict keyed by the supplied string; unknown values map to null. "
        "Staff and support see all users; regular users may only look up "
        "users who share a project with them."
    ),
    parameters=[
        OpenApiParameter(
            name="identifier",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            many=True,
            description=(
                "OpenPortal UserIdentifier string or email address (repeatable). "
                "All values in a single request must be the same type."
            ),
        ),
    ],
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def user_mapping(request):
    """
    Map OpenPortal UserIdentifier strings (or email addresses) to Waldur User objects.

    If the supplied identifiers contain '@' they are resolved by email address
    (remote portal reports remap UserIdentifiers to emails via remap_users).
    Otherwise they are resolved via the Association.useridentifier chain.

    Permission: regular users may only resolve identifiers for users on
    projects they themselves belong to.
    """
    identifiers = request.query_params.getlist("identifier")
    if not identifiers:
        return JsonResponse({})

    emails = [i for i in identifiers if "@" in i]
    uid_strings = [i for i in identifiers if "@" not in i]

    user = request.user

    if user.is_staff or user.is_support:
        accessible_uid_user_ids = None
        accessible_email_user_ids = None
    else:
        accessible_project_ids = list(get_visible_projects(user))
        accessible_uid_user_ids = (
            set(
                models.Association.objects.filter(
                    allocation__project_id__in=accessible_project_ids
                ).values_list("user_id", flat=True)
            )
            if uid_strings
            else set()
        )
        # Permission for email lookups via RemoteAssociation — remote-portal
        # users are linked to RemoteAllocation, not local Allocation
        accessible_email_user_ids = (
            set(
                models.RemoteAssociation.objects.filter(
                    allocation__project_id__in=accessible_project_ids,
                    user__isnull=False,
                ).values_list("user_id", flat=True)
            )
            if emails
            else set()
        )

    result = {}

    if emails:
        resolved = utils.resolve_emails(emails)
        if accessible_email_user_ids is not None:
            email_users = {u.email: u for u in User.objects.filter(email__in=emails)}
            for email, user_info in resolved.items():
                if user_info is None:
                    result[email] = None
                    continue
                resolved_user = email_users.get(email)
                if (
                    resolved_user is None
                    or resolved_user.pk not in accessible_email_user_ids
                ):
                    result[email] = None
                    continue
                result[email] = user_info
        else:
            result.update(resolved)

    if uid_strings:
        resolved = utils.resolve_useridentifiers(uid_strings)
        if accessible_uid_user_ids is not None:
            for identifier, user_info in resolved.items():
                if user_info is None:
                    result[identifier] = None
                    continue
                association = (
                    models.Association.objects.filter(useridentifier=identifier)
                    .select_related("user")
                    .first()
                )
                if association is None or association.user is None:
                    result[identifier] = None
                    continue
                if association.user.pk not in accessible_uid_user_ids:
                    result[identifier] = None
                    continue
                result[identifier] = user_info
        else:
            result.update(resolved)

    return JsonResponse(result)

    # Get the free text search query
    query = request.query_params.get("q")

    # Also support legacy parameters for backwards compatibility
    email = request.query_params.get("email")
    short_name = request.query_params.get("short_name")
    project_name = request.query_params.get("project_name")
    project_id = request.query_params.get("project_id")

    # If no 'q' parameter, check for legacy parameters
    if query is None:
        if email:
            query = email
        elif short_name:
            query = short_name
        elif project_name:
            query = project_name
        elif project_id:
            query = project_id

    if query is None:
        response = JsonResponse(
            {
                "error": "Search query parameter 'q' is required. You can search by email, short_name, project_name, or project_id."
            }
        )
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response

    # Clean and normalize the query
    query = str(query).strip()

    if len(query) == 0:
        response = JsonResponse({"error": "Search query cannot be empty."})
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response

    can_query_all = user.is_staff or user.is_support

    logger.info(
        f"api/openportal/access_for_email request for query='{query}' from {user} ({user.email})"
    )

    # Intelligent search routing based on query format
    return _intelligent_search(user, query, can_query_all)


def _intelligent_search(requesting_user, query, can_query_all):
    """
    Intelligently route the search based on query format.
    Tries multiple search strategies and returns the best match.

    Non-staff users can only search by their own email or short_name.
    Staff/support users can search by any criteria.
    """

    # Strategy 1: Check if it's an email (contains @)
    if "@" in query:
        logger.info(f"Detected email format in query: '{query}'")

        # Permission check for non-staff: can only search their own email
        if not can_query_all and requesting_user.email.lower() != query.lower():
            response = JsonResponse(
                {
                    "error": "You can only search by your own email address. Staff users can search for any user."
                }
            )
            response.status_code = status.HTTP_401_UNAUTHORIZED
            return response

        # Try email search
        try:
            result = _search_by_email(requesting_user, query.lower(), can_query_all)
            # If we got a successful response, return it
            if result.status_code == status.HTTP_200_OK:
                return result
        except Exception as e:
            logger.warning(f"Email search failed: {e}")

    # Strategy 2: Check if it looks like a project ID (shortname.portal format or just shortname)
    # NON-STAFF USERS CANNOT SEARCH BY PROJECT
    if "." in query or (len(query) < 10 and " " not in query):
        logger.info(f"Attempting project ID search for query: '{query}'")

        # Permission check: only staff can search by project
        if not can_query_all:
            # This might be a short_name, so don't block yet - let it fall through to short_name search
            logger.info(
                "Non-staff user attempted project search, will try short_name instead"
            )
        else:
            try:
                result = _search_by_project_id(requesting_user, query, can_query_all)
                if result.status_code == status.HTTP_200_OK:
                    # Check if we got actual results (not an error response)
                    import json

                    data = json.loads(result.content)
                    if isinstance(data, list) and len(data) > 0:
                        return result
                    # If single object with projects
                    elif isinstance(data, dict) and data.get("projects"):
                        return result
            except Exception as e:
                logger.info(f"Project ID search didn't match: {e}")

    # Strategy 3: Try short_name search (for usernames)
    # Short names are between 5 and 20 characters, no spaces
    if " " not in query and 5 <= len(query) <= 20:
        logger.info(f"Attempting short_name search for query: '{query}'")
        try:
            result = _search_by_short_name(requesting_user, query, can_query_all)
            if result.status_code == status.HTTP_200_OK:
                import json

                data = json.loads(result.content)
                # Check if we got a real user (not "unknown" status)
                if isinstance(data, dict) and data.get("status") != "unknown":
                    # Additional permission check for non-staff users
                    if not can_query_all:
                        # Verify this is their own short_name
                        try:
                            userinfo = models.UserInfo.objects.get(user=requesting_user)
                            if (
                                userinfo.shortname
                                and userinfo.shortname.lower() != query.lower()
                            ):
                                response = JsonResponse(
                                    {
                                        "error": "You can only search by your own short name. Staff users can search for any user."
                                    }
                                )
                                response.status_code = status.HTTP_401_UNAUTHORIZED
                                return response
                        except models.UserInfo.DoesNotExist:
                            # User doesn't have a short_name, so they can't search by short_name
                            response = JsonResponse(
                                {
                                    "error": "You don't have a short name configured, so you can only search by your email address."
                                }
                            )
                            response.status_code = status.HTTP_401_UNAUTHORIZED
                            return response
                    return result
        except Exception as e:
            logger.info(f"Short name search didn't match: {e}")

    # Strategy 4: Try project name search (broader text search)
    # NON-STAFF USERS CANNOT SEARCH BY PROJECT
    if not can_query_all:
        # Non-staff user trying to search by project name
        response = JsonResponse(
            {
                "error": "You can only search by your own email address or short name. Project searches are only available to staff users.",
                "allowed_searches": ["your_email", "your_short_name"],
            }
        )
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return response

    logger.info(f"Attempting project name search for query: '{query}'")
    try:
        result = _search_by_project_name(requesting_user, query, can_query_all)
        if result.status_code == status.HTTP_200_OK:
            return result
        # If not found, that's okay - we'll return a generic not found
    except Exception as e:
        logger.info(f"Project name search didn't match: {e}")

    # If nothing matched, return a helpful error message
    if can_query_all:
        response = JsonResponse(
            {
                "error": f"No results found for query: '{query}'. Tried searching by email, project ID, short name, and project name.",
                "query": query,
                "searched_types": ["email", "project_id", "short_name", "project_name"],
            }
        )
    else:
        response = JsonResponse(
            {
                "error": f"No results found for query: '{query}'. You can only search by your own email address or short name.",
                "query": query,
                "allowed_searches": ["your_email", "your_short_name"],
            }
        )
    response.status_code = status.HTTP_404_NOT_FOUND
    return response


def _search_by_email(requesting_user, email, can_query_all):
    """Original email search logic extracted into helper function"""
    qs = User.all_objects.all()

    if not can_query_all:
        qs = qs.filter(is_active=True)

    qs = qs.filter(email__iexact=email)

    reason = None
    is_authorised = False
    projects = {}
    short_name_in_waldur = None
    email_in_waldur = None

    for user in qs:
        if user.is_active:
            member_of_projects = structure_models.Project.available_objects.filter(
                id__in=get_connected_projects(user)
            )

            if len(member_of_projects) == 0:
                logger.warning(f"User {user} is not an active member of any projects")
                reason = "User account is not a member of any projects."
                continue

            is_authorised = True
            userinfo, created = models.UserInfo.objects.get_or_create(user=user)
            userinfo.sanitise()
            email_in_waldur = user.email

            if short_name_in_waldur is None:
                if userinfo.shortname is None:
                    logger.warning(f"User {user} has not set their short name")
                    break
                short_name_in_waldur = str(userinfo.shortname).strip()

            projects = _get_user_projects(user)
            break
        elif reason is None:
            reason = "User account is not active"

    if short_name_in_waldur is None:
        short_name_in_waldur = ""

    if is_authorised:
        response = JsonResponse(
            {
                "email": email_in_waldur,
                "status": "active",
                "short_name": short_name_in_waldur,
                "projects": projects,
                "invited_by": "",
                "reason": "",
            }
        )
        logger.info(f"access_for_email({email}, {requesting_user}) {response.content}")
        response.status_code = status.HTTP_200_OK
        return response

    # Check invitations
    from waldur_core.users.models import Invitation

    qs = Invitation.objects.filter(email__iexact=email)
    invited_by = ""

    for invitation in qs:
        if invitation.state in [InvitationState.PENDING, InvitationState.REQUESTED]:
            is_authorised = True
            email_in_waldur = invitation.email
            invited_by = invitation.created_by.full_name
            reason = None
            break
        elif reason is None:
            reason = "Invitation to email is neither pending or requested."

    if is_authorised:
        response = JsonResponse(
            {
                "email": email_in_waldur,
                "status": "invited",
                "short_name": short_name_in_waldur,
                "projects": {},
                "invited_by": invited_by,
                "reason": "",
            }
        )
        logger.info(f"access_for_email({email}, {requesting_user}) {response.content}")
        response.status_code = status.HTTP_200_OK
        return response

    if reason is None:
        reason = "Email address was not found"

    response = JsonResponse(
        {
            "email": email,
            "status": "unknown",
            "short_name": short_name_in_waldur,
            "projects": {},
            "invited_by": "",
            "reason": reason,
        }
    )
    logger.info(f"access_for_email({email}, {requesting_user}) {response.content}")
    response.status_code = status.HTTP_200_OK
    return response


def _search_by_short_name(requesting_user, short_name, can_query_all):
    """Search for user by their short_name (UserInfo.shortname)"""
    try:
        userinfo = models.UserInfo.objects.get(shortname__iexact=short_name)
        user = userinfo.user
    except models.UserInfo.DoesNotExist:
        response = JsonResponse(
            {
                "email": "",
                "status": "unknown",
                "short_name": short_name,
                "projects": {},
                "invited_by": "",
                "reason": "Short name not found",
            }
        )
        response.status_code = status.HTTP_200_OK
        return response

    # Check permissions
    if not can_query_all:
        if user != requesting_user:
            response = JsonResponse({"error": "You can only query your own short_name"})
            response.status_code = status.HTTP_401_UNAUTHORIZED
            return response

    if not user.is_active:
        response = JsonResponse(
            {
                "email": user.email,
                "status": "inactive",
                "short_name": short_name,
                "projects": {},
                "invited_by": "",
                "reason": "User account is not active",
            }
        )
        response.status_code = status.HTTP_200_OK
        return response

    member_of_projects = structure_models.Project.available_objects.filter(
        id__in=get_connected_projects(user)
    )

    if len(member_of_projects) == 0:
        response = JsonResponse(
            {
                "email": user.email,
                "status": "active",
                "short_name": short_name,
                "projects": {},
                "invited_by": "",
                "reason": "User account is not a member of any projects.",
            }
        )
        response.status_code = status.HTTP_200_OK
        return response

    projects = _get_user_projects(user)

    response = JsonResponse(
        {
            "email": user.email,
            "status": "active",
            "short_name": short_name,
            "projects": projects,
            "invited_by": "",
            "reason": "",
        }
    )
    logger.info(
        f"access_for_short_name({short_name}, {requesting_user}) {response.content}"
    )
    response.status_code = status.HTTP_200_OK
    return response


def _search_by_project_name(requesting_user, project_name, can_query_all):
    """Search for all users in a project by project name"""
    logger.info(f"Searching for project with name: '{project_name}'")

    projects_qs = structure_models.Project.available_objects.filter(
        name__iexact=project_name
    )

    logger.info(f"Found {projects_qs.count()} projects matching '{project_name}'")

    if not projects_qs.exists():
        # Try a partial match
        projects_qs = structure_models.Project.available_objects.filter(
            name__icontains=project_name
        )
        logger.info(f"Partial match found {projects_qs.count()} projects")

        if not projects_qs.exists():
            response = JsonResponse(
                {"error": f"Project with name '{project_name}' not found"}
            )
            response.status_code = status.HTTP_404_NOT_FOUND
            return response

    project = projects_qs.first()
    logger.info(f"Using project: {project.name} (ID: {project.id})")

    # Check permissions - non-staff can only query projects they're members of
    if not can_query_all:
        user_projects = structure_models.Project.available_objects.filter(
            id__in=get_connected_projects(requesting_user)
        )
        if project not in user_projects:
            response = JsonResponse(
                {"error": "You can only query projects you are a member of"}
            )
            response.status_code = status.HTTP_401_UNAUTHORIZED
            return response

    # Get all active users in this project using the project's get_users method
    logger.info(f"Getting users for project: {project.name} (ID: {project.id})")

    try:
        # Use the project's built-in method to get all users
        all_project_users = project.get_users()
        logger.info(f"Found {len(all_project_users)} total users in project")

        # Filter for active users only
        users_in_project = [user for user in all_project_users if user.is_active]
        logger.info(f"Found {len(users_in_project)} active users in project")

    except Exception as e:
        logger.error(f"Error getting users from project: {e}")
        users_in_project = []

    if not users_in_project:
        response = JsonResponse(
            {
                "email": "",
                "status": "active",
                "short_name": "",
                "projects": {},
                "invited_by": "",
                "reason": "No active users found in this project",
            }
        )
        response.status_code = status.HTTP_200_OK
        return response

    # Build response with all users
    users_data = []
    for user in users_in_project:
        userinfo, _ = models.UserInfo.objects.get_or_create(user=user)
        short_name = str(userinfo.shortname).strip() if userinfo.shortname else ""

        # Get only projects for this specific user
        projects = _get_user_projects(user)

        users_data.append(
            {
                "email": user.email,
                "status": "active",
                "short_name": short_name,
                "projects": projects,
                "invited_by": "",
                "reason": "",
            }
        )

    # Return array of users for project searches
    response = JsonResponse(users_data, safe=False)
    logger.info(
        f"access_for_project_name({project_name}, {requesting_user}) found {len(users_data)} users"
    )
    response.status_code = status.HTTP_200_OK
    return response


def _search_by_project_id(requesting_user, project_id, can_query_all):
    """Search for all users in a project by project ID (shortname)"""
    # Project ID in OpenPortal format is typically "shortname.portal"
    # Need to look up ProjectInfo by shortname
    try:
        # Try to parse project_id - it might be "proj1.brics" format
        parts = project_id.split(".")
        if len(parts) >= 1:
            project_shortname = parts[0]
        else:
            project_shortname = project_id

        project_info = models.ProjectInfo.objects.filter(
            shortname__iexact=project_shortname
        ).first()

        if not project_info or not project_info.project:
            response = JsonResponse(
                {"error": f"Project with ID '{project_id}' not found"}
            )
            response.status_code = status.HTTP_404_NOT_FOUND
            return response

        project = project_info.project

    except Exception as e:
        logger.error(f"Error finding project with ID {project_id}: {e}")
        response = JsonResponse({"error": f"Project with ID '{project_id}' not found"})
        response.status_code = status.HTTP_404_NOT_FOUND
        return response

    # Check permissions
    if not can_query_all:
        user_projects = structure_models.Project.available_objects.filter(
            id__in=get_connected_projects(requesting_user)
        )
        if project not in user_projects:
            response = JsonResponse(
                {"error": "You can only query projects you are a member of"}
            )
            response.status_code = status.HTTP_401_UNAUTHORIZED
            return response

    # Get all active users in this project using the project's get_users method
    logger.info(f"Getting users for project: {project.name} (ID: {project.id})")

    try:
        # Use the project's built-in method to get all users
        all_project_users = project.get_users()
        logger.info(f"Found {len(all_project_users)} total users in project")

        # Filter for active users only
        users_in_project = [user for user in all_project_users if user.is_active]
        logger.info(f"Found {len(users_in_project)} active users in project")

    except Exception as e:
        logger.error(f"Error getting users from project: {e}")
        users_in_project = []

    if not users_in_project:
        response = JsonResponse(
            {
                "email": "",
                "status": "active",
                "short_name": "",
                "projects": {},
                "invited_by": "",
                "reason": "No active users found in this project",
            }
        )
        response.status_code = status.HTTP_200_OK
        return response

    # Build response with all users
    users_data = []
    for user in users_in_project:
        userinfo, _ = models.UserInfo.objects.get_or_create(user=user)
        short_name = str(userinfo.shortname).strip() if userinfo.shortname else ""

        projects = _get_user_projects(user)

        users_data.append(
            {
                "email": user.email,
                "status": "active",
                "short_name": short_name,
                "projects": projects,
                "invited_by": "",
                "reason": "",
            }
        )

    # Return array of users for project searches
    response = JsonResponse(users_data, safe=False)
    logger.info(
        f"access_for_project_id({project_id}, {requesting_user}) found {len(users_data)} users"
    )
    response.status_code = status.HTTP_200_OK
    return response


def _get_user_projects(user):
    """Extract project information for a user - extracted from original code"""
    projects = {}

    for allocation in utils.get_project_allocations(user):
        if allocation.has_project_identifier():
            project_id = allocation.get_project_identifier()
            project = str(project_id)
            project_short_name = str(project_id.project).strip()
        else:
            backend = allocation.get_backend()
            project_short_name = backend.get_project_shortname(allocation.project)

            if project_short_name is None or len(str(project_short_name).strip()) == 0:
                logger.warning(
                    f"Allocation {allocation} has no project short name - skipping"
                )
                continue

            project_short_name = str(project_short_name).strip()
            portal = backend.portal()

            if portal is None or len(str(portal).strip()) == 0:
                logger.warning(f"Allocation {allocation} has no portal name - skipping")
                continue

            project = f"{project_short_name}.{portal}"
            logger.warning(
                f"{allocation} is missing project identifier - guessing '{project}'"
            )

        destination = str(allocation.get_backend().destination())

        try:
            association = utils.get_association(user=user, allocation=allocation)
            username = association.username
        except models.Association.DoesNotExist:
            logger.warning(f"Association between {user} and {allocation} not found")
            username = None

        if username is None:
            userinfo, _ = models.UserInfo.objects.get_or_create(user=user)
            short_name_in_waldur = (
                str(userinfo.shortname).strip() if userinfo.shortname else None
            )

            if short_name_in_waldur is not None:
                username = f"{short_name_in_waldur}.{project_short_name}"
                logger.warning(
                    f"Guessing username as '{username}' as this is not set for {project}"
                )
            else:
                logger.warning(
                    f"Skipping {project} as username is not set and short name is not set"
                )
                continue

        if project not in projects:
            projects[project] = {
                "name": str(allocation.project.name),
                "resources": [],
            }

        projects[project]["resources"].append(
            {
                "name": destination,
                "username": username,
            }
        )

    return projects
