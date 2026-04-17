import logging
from http import HTTPStatus as status

from django.contrib import auth
from django.http import JsonResponse
from drf_spectacular.utils import (
    OpenApiParameter,
    extend_schema,
)
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import IsAuthenticated

from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import get_connected_projects
from waldur_core.users.enums import InvitationState

from . import models, serializers, tasks, utils
from . import op as openportal
from .board import OpenPortalBoard

logger = logging.getLogger(__name__)

User = auth.get_user_model()


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
        response.status_code = status.UNAUTHORIZED
        return response

    # move to serlialiser django
    job_id = str(job_id).lstrip().rstrip()

    if len(job_id) == 0:
        response = JsonResponse({})
        response.status_code = status.UNAUTHORIZED
        return response

    # fetch this job from the OpenPortal bridge queue
    try:
        job = board.fetch_job(job_id)
        if job is None:
            response = JsonResponse({})
            response.status_code = status.UNAUTHORIZED
            return response
    except Exception as e:
        logger.error(f"Error fetching job {job_id}: {e}")
        response = JsonResponse({})
        response.status_code = status.UNAUTHORIZED
        return response

    job_id = str(job.id).lstrip().rstrip()

    if len(job_id) == 0:
        logger.error(f"Job {job} has no job_id")
        response = JsonResponse({})
        response.status_code = status.UNAUTHORIZED
        return response

    if job.state != openportal.Status.pending():
        logger.error(f"Job {job_id} is not in PENDING state, but in {job.state}")
        response = JsonResponse({})
        response.status_code = status.UNAUTHORIZED
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
        response.status_code = status.UNAUTHORIZED
        return response

    # submit the job for processing
    logger.info(f"Submitting job {job_id} for processing")
    tasks.run_job.delay(core_utils.serialize_instance(job_model))

    response = JsonResponse({})
    response.status_code = status.OK
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
        response.status_code = status.UNAUTHORIZED
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
        response.status_code = status.BAD_REQUEST
        return response

    # Clean and normalize the query
    query = str(query).strip()

    if len(query) == 0:
        response = JsonResponse({"error": "Search query cannot be empty."})
        response.status_code = status.BAD_REQUEST
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
            response.status_code = status.FORBIDDEN
            return response

        # Try email search
        try:
            result = _search_by_email(requesting_user, query.lower(), can_query_all)
            # If we got a successful response, return it
            if result.status_code == status.OK:
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
                if result.status_code == status.OK:
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
            if result.status_code == status.OK:
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
                                response.status_code = status.FORBIDDEN
                                return response
                        except models.UserInfo.DoesNotExist:
                            # User doesn't have a short_name, so they can't search by short_name
                            response = JsonResponse(
                                {
                                    "error": "You don't have a short name configured, so you can only search by your email address."
                                }
                            )
                            response.status_code = status.FORBIDDEN
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
        response.status_code = status.FORBIDDEN
        return response

    logger.info(f"Attempting project name search for query: '{query}'")
    try:
        result = _search_by_project_name(requesting_user, query, can_query_all)
        if result.status_code == status.OK:
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
    response.status_code = status.NOT_FOUND
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
        response.status_code = status.OK
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
        response.status_code = status.OK
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
    response.status_code = status.OK
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
        response.status_code = status.OK
        return response

    # Check permissions
    if not can_query_all:
        if user != requesting_user:
            response = JsonResponse({"error": "You can only query your own short_name"})
            response.status_code = status.FORBIDDEN
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
        response.status_code = status.OK
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
        response.status_code = status.OK
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
    response.status_code = status.OK
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
            response.status_code = status.NOT_FOUND
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
            response.status_code = status.FORBIDDEN
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
        response.status_code = status.OK
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
    response.status_code = status.OK
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
            response.status_code = status.NOT_FOUND
            return response

        project = project_info.project

    except Exception as e:
        logger.error(f"Error finding project with ID {project_id}: {e}")
        response = JsonResponse({"error": f"Project with ID '{project_id}' not found"})
        response.status_code = status.NOT_FOUND
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
            response.status_code = status.FORBIDDEN
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
        response.status_code = status.OK
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
    response.status_code = status.OK
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
