"""Shared base class for the D&B Nordic country backends."""

import logging

from waldur_core.onboarding.backends.base import (
    CompanyRegistryBackend,
    ErrorCode,
    ValidationRequest,
    ValidationResult,
)

from .client import DnbError, get_dnb_client, get_dnb_rts_client

logger = logging.getLogger(__name__)


class DnbBaseBackend(CompanyRegistryBackend):
    """Shared skeleton for D&B Nordic backends.

    All four countries authorize via the Nordic Right to Sign (RTS) API:
    ``validate_company`` (overridden in each subclass) delegates to
    ``_validate_company_via_rts``, which decides authorization from the RTS
    signatory lists and then enriches company_data from the Credit Data
    Companies API. How the user is located in the RTS response varies per
    country:

    - SE: company-signatories endpoint, ``{ssn: <personnummer>}`` request;
      response entries carry ``nationalIdentificationNumber``.
    - NO / FI: company-signatories endpoint, ``{name, birthDate}`` request;
      response entries carry structured ``name`` + ``birthDate``.
    - DK: company-level endpoint (``RTS_USE_COMPANY_ENDPOINT``) — no
      per-person request; the user is name-matched against the returned
      signatory lists locally (DK's company-signatories variant requires an
      id or name+address, which onboarding doesn't collect).

    Per-country request/match shape is plugged in via
    ``_build_signatory_payload`` (SE/NO only) and ``_signatory_matches``.

    Subclasses declare the behavioural axes as class attributes: COUNTRY_CODE,
    VALIDATION_METHOD, REGISTRY_NAME, plus the RTS_* / ENRICHMENT_SEGMENTS flags.
    """

    # Subclasses MUST override these three.
    COUNTRY_CODE: str = ""  # Lower-case ISO (se, no, dk, fi)
    VALIDATION_METHOD: str = ""  # enums.ValidationMethod.DNB_*
    REGISTRY_NAME: str = ""  # e.g. "D&B Sweden"

    # RTS request authorityType (DEFAULT | PROCURATION | None). SE/NO send
    # DEFAULT; DK omits it.
    RTS_AUTHORITY_TYPE: str | None = "DEFAULT"

    # Which RTS endpoint to use. False (default, SE/NO) queries
    # company-signatories with a per-person identifier (ssn / name+birthDate).
    # True (DK) queries the company-level endpoint with just the registration
    # number and matches the person against the returned signatory lists
    # locally — DK's company-signatories variant requires an id or name+address
    # per person, which onboarding doesn't collect.
    RTS_USE_COMPANY_ENDPOINT: bool = False

    # Segments for the credit-data enrichment call made on the RTS success
    # path. None => no segments (SE/NO, which return company info by default).
    # DK requests COMPANY_INFORMATION explicitly.
    ENRICHMENT_SEGMENTS: list[str] | None = None

    # When True, a name (or otherwise weak) identifier that matches more than
    # one signatory escalates as AMBIGUOUS_MATCH instead of authorizing the
    # first hit. DK enables this because it can only match by name.
    RTS_AMBIGUITY_GUARD: bool = False

    @classmethod
    def get_supported_countries(cls) -> set[str]:
        return {cls.COUNTRY_CODE.upper()}

    @classmethod
    def get_validation_method(cls) -> str:
        return cls.VALIDATION_METHOD

    @classmethod
    def get_required_fields(cls) -> list[str]:
        return ["legal_person_identifier", "person_identifier"]

    def _unauthorized_message(self, request: ValidationRequest) -> str:
        return (
            f"User is associated with the company but does not have "
            f"signing authority on their own per {self.REGISTRY_NAME}."
        )

    def _not_listed_message(self, request: ValidationRequest) -> str:
        return (
            f"User was not found in {self.REGISTRY_NAME}'s records for this "
            f"company. Check that the personal details match the official "
            f"register entry, or proceed with manual verification."
        )

    def _ambiguous_message(self, request: ValidationRequest) -> str:
        return (
            f"More than one person matched in {self.REGISTRY_NAME}'s records "
            f"for this company, so authorization could not be confirmed "
            f"automatically. Manual verification is required."
        )

    # ------------------------------------------------------------------
    # Right to Sign path (all subclasses delegate here via validate_company).
    # ------------------------------------------------------------------

    def _validate_company_via_rts(self, request: ValidationRequest) -> ValidationResult:
        """RTS-based authorization + credit-data enrichment.

        Sends the user as a single signatory with signTogether=ANY and
        decides authorization from which list they landed in
        (signatories[]=authorized, coSignatories[]/nonSignatories[]=not).
        On success, enriches company_data with credit-data fields the RTS
        payload doesn't expose (address, VAT number, registration date,
        employees). Enrichment failures are non-fatal — RTS has already
        confirmed authorization, so a downstream credit-data outage must
        not flip the result back to NOT_AUTHORIZED.
        """
        try:
            if self.RTS_USE_COMPANY_ENDPOINT:
                # Company-level lookup (DK): no per-person identifier in the
                # request; the queried person is matched against the returned
                # signatory lists locally by `_match_signatory`.
                raw = get_dnb_rts_client().check_company_rts(
                    self.COUNTRY_CODE,
                    request.legal_person_identifier,
                    authority_type=self.RTS_AUTHORITY_TYPE,
                )
            else:
                signatories_payload = [
                    self._build_signatory_payload(request.person_identifier)
                ]
                raw = get_dnb_rts_client().check_rts(
                    self.COUNTRY_CODE,
                    request.legal_person_identifier,
                    signatories=signatories_payload,
                    sign_together="ANY",
                    authority_type=self.RTS_AUTHORITY_TYPE,
                )
        except DnbError as e:
            if e.not_found:
                logger.info(
                    "%s: company %s not found",
                    self.REGISTRY_NAME,
                    request.legal_person_identifier,
                )
                return self._error_result(ErrorCode.COMPANY_NOT_FOUND, str(e))
            logger.error("%s RTS API error: %s", self.REGISTRY_NAME, e)
            return self._error_result(
                ErrorCode.API_ERROR, f"{self.REGISTRY_NAME} API error: {e}"
            )
        except ValueError:
            # Configuration errors (e.g. missing credentials) propagate to the
            # OnboardingValidator, which records them as CONFIGURATION_ERROR.
            raise
        except Exception as e:
            logger.exception("%s unexpected error", self.REGISTRY_NAME)
            return self._error_result(ErrorCode.API_ERROR, f"Unexpected error: {e}")

        status, roles = self._match_signatory(request.person_identifier, raw)
        normalized = self._normalize_rts(raw)

        # RTS is the authoritative signing-rights source: an unambiguous match
        # in signatories[] authorizes the user. The Credit Data Companies call
        # enriches company_data with address/postal/VAT the RTS payload doesn't
        # carry; it is non-fatal (skipped on failure) so a downstream outage or
        # missing credit-data record can't flip a verified result to escalation.
        if status == "authorized":
            enrichment_raw = self._fetch_credit_data_raw(
                request.legal_person_identifier
            )
            enrichment = (
                _extract_credit_data_details(enrichment_raw) if enrichment_raw else {}
            )
            if enrichment:
                normalized.update(enrichment)
            return ValidationResult(
                is_valid=True,
                method_used=self.VALIDATION_METHOD,
                company_data=normalized,
                user_roles=roles,
                raw_response=raw,
                error_code=None,
                error_message=None,
            )
        elif status == "ambiguous":
            # Weak identifier matched several signatories — we can't safely pick
            # one, so escalate with a distinct code for staff/UI.
            error_code = ErrorCode.AMBIGUOUS_MATCH
            error_message = self._ambiguous_message(request)
        elif status == "not_authorized":
            # Person *is* in the company records but in coSig/nonSig — staff
            # review has a concrete starting point (the person + their roles).
            error_code = ErrorCode.NOT_AUTHORIZED
            error_message = self._unauthorized_message(request)
        else:
            # Person isn't in any list — could be wrong DOB, wrong name spelling,
            # genuinely unaffiliated, or the company just has no signers in the
            # current dataset. Surface a distinct code so staff/UI can treat it
            # differently from a clear "not allowed to sign" result.
            error_code = ErrorCode.PERSON_NOT_LISTED
            error_message = self._not_listed_message(request)

        return ValidationResult(
            is_valid=False,
            method_used=self.VALIDATION_METHOD,
            company_data=normalized,
            user_roles=roles,
            raw_response=raw,
            error_code=error_code,
            error_message=error_message,
        )

    def _fetch_credit_data_raw(self, registration_number: str) -> dict | None:
        """Call /companies/{country} and return the raw response (or None).

        Non-fatal: all failures return None and are logged. The caller has
        already been authorized via RTS, so missing enrichment (or a company
        absent from the credit-data dataset) shouldn't reject onboarding — the
        result still verifies, just without the extra company_data fields.
        """
        try:
            # Preserve the no-segments call shape when ENRICHMENT_SEGMENTS is
            # unset so existing SE/NO behaviour (and tests) stay identical.
            if self.ENRICHMENT_SEGMENTS:
                raw = get_dnb_client().get_company(
                    self.COUNTRY_CODE,
                    registration_number,
                    segments=self.ENRICHMENT_SEGMENTS,
                )
            else:
                raw = get_dnb_client().get_company(
                    self.COUNTRY_CODE, registration_number
                )
        except DnbError as e:
            logger.warning(
                "%s enrichment lookup failed for %s: %s",
                self.REGISTRY_NAME,
                registration_number,
                e,
            )
            return None
        except ValueError as e:
            # Credit-data scope may be misconfigured even when RTS works
            # (separate Constance key, separate scope). Log and skip.
            logger.warning(
                "%s enrichment unavailable (configuration): %s", self.REGISTRY_NAME, e
            )
            return None
        except Exception:
            logger.exception(
                "%s unexpected error during enrichment for %s",
                self.REGISTRY_NAME,
                registration_number,
            )
            return None
        return raw

    # ------------------------------------------------------------------
    # Shared normalizers and matchers.
    # ------------------------------------------------------------------

    def _normalize_rts(self, raw: dict) -> dict:
        """RTS payload → company_data (all four RTS backends).

        Surfaces the audit fields a staff reviewer needs at top-level so they
        don't have to grovel through ``raw_response``. All optional fields
        are emitted only when present so downstream consumers (admin views,
        Customer creation) don't have to distinguish "missing" from "empty".
        """
        company = raw.get("company") or {}
        data: dict = {
            "name": company.get("name", ""),
            "legal_person_identifier": company.get("registrationNumber", ""),
            "registry": self.REGISTRY_NAME,
        }

        duns = company.get("duns") or ""
        legal_form = (company.get("legalForm") or {}).get("description") or ""
        operating_status = (company.get("status") or {}).get("operatingStatus") or ""
        signing_authority = raw.get("signingAuthorityDescription") or ""
        interpretation_level = raw.get("interpretationLevel") or ""
        authority_type = raw.get("authorityType") or ""

        if duns:
            data["duns_number"] = duns
        if legal_form:
            data["legal_form"] = legal_form
        if operating_status:
            data["status"] = operating_status
        if signing_authority:
            data["signing_authority"] = signing_authority
        # PARTIAL interpretation means D&B couldn't fully parse the rules —
        # staff should treat the result as advisory.
        if interpretation_level and interpretation_level != "COMPLETE":
            data["interpretation_level"] = interpretation_level
        # PROCURATION queries return a different rule set than DEFAULT;
        # always show which one we asked for so reviewers can correlate.
        if authority_type:
            data["authority_type"] = authority_type

        signing_rules = _format_signing_rules(raw.get("signingRules") or [])
        if signing_rules:
            data["signing_rules"] = signing_rules

        signing_issues = _format_signing_codes(raw.get("signingIssues") or [])
        if signing_issues:
            # COMPANY_INACTIVE etc. — red flags the reviewer needs to see.
            data["signing_issues"] = signing_issues

        signing_infos = _format_signing_codes(raw.get("signingInfos") or [])
        if signing_infos:
            data["signing_infos"] = signing_infos

        period = raw.get("signingAuthorityDescriptionPeriod") or {}
        if isinstance(period, dict):
            start = period.get("startDate") or ""
            end = period.get("endDate") or ""
            if start:
                data["signing_authority_period_start"] = start
            if end:
                data["signing_authority_period_end"] = end

        return data

    # --- Per-country plug points -------------------------------------

    def _build_signatory_payload(self, person_identifier) -> dict:
        """Return the per-signatory dict for the RTS request body.

        Default: the name + birthDate shape the Nordic RTS company-signatories
        endpoint expects for NO and FI (``{"name": {firstName, lastName},
        "birthDate": ...}``). SE overrides this to send ``{"ssn": ...}``. DK
        uses the company-level endpoint and never calls this.
        """
        # `person_identifier` is the dict produced by
        # `get_person_identifier_from_user` / the frontend wizard.
        ident = person_identifier or {}
        return {
            "name": {
                "firstName": str(ident.get("first_name", "") or ""),
                "lastName": str(ident.get("last_name", "") or ""),
            },
            "birthDate": str(ident.get("birth_date", "") or ""),
        }

    def _signatory_matches(self, person_identifier, entry: dict) -> bool:
        """Whether a response signatory entry corresponds to the requested person.

        The RTS API may return all signatories of the company, not just the
        one we queried — match the entry against what we sent so we don't
        authorize the wrong person.

        Default: match on structured ``name`` + ``birthDate`` (NO, FI). SE
        overrides to compare ``nationalIdentificationNumber``; DK overrides to
        compare a flat full-name string.
        """
        ident = person_identifier or {}
        first = _norm(ident.get("first_name"))
        last = _norm(ident.get("last_name"))
        birth = _normalize_date(str(ident.get("birth_date") or ""))
        if not (first and last and birth):
            return False

        name = entry.get("name") or {}
        entry_first = _norm(name.get("firstName"))
        entry_last = _norm(name.get("lastName"))
        entry_birth = _normalize_date(str(entry.get("birthDate") or ""))

        return entry_first == first and entry_last == last and entry_birth == birth

    # --- Shared RTS list traversal -----------------------------------

    def _match_signatory(self, person_identifier, raw: dict) -> tuple[str, list[str]]:
        """Locate the queried person in the RTS response.

        Returns ``(status, roles)`` where ``status`` is:

        - ``"authorized"`` — the person is in ``signatories[]`` and can sign
          per the request's ``signTogether`` semantics.
        - ``"not_authorized"`` — the person is in ``coSignatories[]`` (needs
          a co-signer) or ``nonSignatories[]`` (associated but no signing
          rights). ``roles`` is populated for staff-review context.
        - ``"not_listed"`` — the person isn't in any list. Could be a wrong
          DOB / name spelling, or genuinely unaffiliated. Distinct from
          ``not_authorized`` so the UI can give the user a different
          remediation hint.
        - ``"ambiguous"`` — only when ``RTS_AMBIGUITY_GUARD`` is set: the
          identifier matched more than one ``signatories[]`` entry, so we
          can't safely authorize a single person. Escalates for staff.
        """
        signatory_hits = [
            entry
            for entry in (raw.get("signatories") or [])
            if self._signatory_matches(person_identifier, entry)
        ]
        if self.RTS_AMBIGUITY_GUARD and len(signatory_hits) > 1:
            return "ambiguous", []
        if signatory_hits:
            return "authorized", _extract_roles(signatory_hits[0])

        # Found-but-not-authorized lookups still surface roles to help staff
        # decide on escalation.
        for entry in (raw.get("coSignatories") or []) + (
            raw.get("nonSignatories") or []
        ):
            if self._signatory_matches(person_identifier, entry):
                return "not_authorized", _extract_roles(entry)

        return "not_listed", []

    def _error_result(self, error_code: str, message: str) -> ValidationResult:
        return ValidationResult(
            is_valid=False,
            method_used=self.VALIDATION_METHOD,
            company_data={},
            user_roles=[],
            raw_response={},
            error_code=error_code,
            error_message=message,
        )


def _norm(value) -> str:
    return str(value or "").strip().lower()


def _normalize_date(s: str) -> str:
    # D&B sometimes returns "1980-05-17T00:00:00Z" instead of "1980-05-17";
    # strip everything after the day so both forms compare equal.
    return s.split("T", 1)[0].strip()


def _extract_roles(item: dict) -> list[str]:
    return [
        r.get("description", "")
        for r in (item.get("roles") or [])
        if r.get("description")
    ]


def _format_signing_rules(rules: list) -> list[str]:
    """Flatten ``signingRules[]`` into human-readable strings.

    A rule looks like ``{code: "JOINTLY", signatoryGroups: [{groupType:
    "BOARDMEMBERS", quantity: {type: "ALL", value: 0}}, ...]}`` and the
    Bisnode doc table renders it as e.g. ``JOINTLY (BOARDMEMBERS = ALL)``.
    We follow the same shape so staff reviewers and the published reference
    use the same vocabulary.
    """
    formatted: list[str] = []
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        code = rule.get("code") or ""
        if not code:
            continue
        groups: list[str] = []
        for group in rule.get("signatoryGroups") or []:
            if not isinstance(group, dict):
                continue
            group_type = group.get("groupType") or ""
            quantity = group.get("quantity") or {}
            if isinstance(quantity, dict):
                qtype = quantity.get("type") or ""
                qvalue = quantity.get("value")
                if qtype == "ALL":
                    qty_str = "ALL"
                elif qvalue is not None:
                    qty_str = str(qvalue)
                else:
                    qty_str = qtype
            else:
                qty_str = str(quantity)
            if group_type and qty_str:
                groups.append(f"{group_type} = {qty_str}")
            elif group_type:
                groups.append(group_type)
        if groups:
            formatted.append(f"{code} ({', '.join(groups)})")
        else:
            formatted.append(code)
    return formatted


def _format_signing_codes(items: list) -> list[str]:
    """Flatten ``signingInfos[]`` / ``signingIssues[]`` entries into strings.

    Each entry is ``{code: "COMPANY_INACTIVE", signingAuthorityDescription:
    "..."}``. We surface ``code`` (the categorical signal) plus the
    description when present so the staff-review UI can show both.
    """
    formatted: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        code = item.get("code") or ""
        description = (
            item.get("signingAuthorityDescription") or item.get("description") or ""
        )
        if code and description:
            formatted.append(f"{code}: {description}")
        elif code:
            formatted.append(code)
        elif description:
            formatted.append(description)
    return formatted


def _extract_credit_data_details(raw: dict) -> dict:
    """Pull supplementary company info from a Credit Data Companies response.

    D&B publishes two materially different shapes for /companies/{country}:

    - **SE (flat v2)** — fields at the top level: ``address``,
      ``registrationDate`` (ISO string), ``vatRegistrationNumber``,
      ``numberOfEmployees`` (``{value}`` or int).
    - **NO (nested v3)** — fields under ``companyInformation``:
      ``contactPoints.registeredAddress.streetAddress`` for address,
      ``registrationInformation.registrationDate`` as
      ``{year, month, day}``, ``generalCompanyData.employeeCount`` for
      employees. VAT number is not published, only the boolean
      ``generalCompanyData.registeredInVat``.
    - **DK (nested)** — same ``companyInformation`` container as NO, but the
      date is ``registrationInformation.foundationDate`` and the VAT number is
      published under ``identifiers.vatNumber``.

    The extractor handles both shapes so callers don't need to branch
    on country. Each field is emitted only when present so downstream
    consumers don't have to distinguish "missing" from "empty".
    """
    result: dict = {}
    company_info = raw.get("companyInformation") or {}

    # --- Address ---------------------------------------------------------
    # SE shape first; fall back to NO's nested registeredAddress.
    address = raw.get("address") or {}
    if not address:
        contact = company_info.get("contactPoints") or {}
        registered = contact.get("registeredAddress") or {}
        address = registered.get("streetAddress") or {}
    if isinstance(address, dict):
        street = address.get("streetAddress") or address.get("street") or ""
        city = address.get("town") or address.get("city") or address.get("place") or ""
        postal_code = (
            address.get("postalCode")
            or address.get("postCode")
            or address.get("zipCode")
            or ""
        )
        # Join only the components we actually have so we don't emit
        # stray commas when one part is missing.
        parts = [p for p in (street, city) if p]
        if parts:
            result["address"] = ", ".join(parts)
        if postal_code:
            result["postal"] = postal_code

    # --- Registration date ----------------------------------------------
    # SE returns an ISO string; NO returns {year, month, day} under
    # registrationInformation.registrationDate; DK exposes foundationDate
    # (also {year, month, day}) instead.
    registration_date = raw.get("registrationDate")
    if not registration_date:
        reg_info = company_info.get("registrationInformation") or {}
        registration_date = reg_info.get("registrationDate") or reg_info.get(
            "foundationDate"
        )
    formatted_date = _coerce_date(registration_date)
    if formatted_date:
        result["registration_date"] = formatted_date

    # --- VAT number ------------------------------------------------------
    # SE publishes the number directly; NO only publishes a boolean; DK nests
    # it under companyInformation.identifiers.vatNumber.
    vat_number = raw.get("vatRegistrationNumber") or ""
    if not vat_number:
        vat_number = (company_info.get("identifiers") or {}).get("vatNumber") or ""
    if vat_number:
        result["vat_number"] = vat_number

    # --- Employees -------------------------------------------------------
    employees = raw.get("numberOfEmployees")
    if isinstance(employees, dict) and employees.get("value") is not None:
        result["number_of_employees"] = employees["value"]
    elif isinstance(employees, int):
        result["number_of_employees"] = employees
    else:
        # NO shape
        general = company_info.get("generalCompanyData") or {}
        count = general.get("employeeCount")
        if isinstance(count, int):
            result["number_of_employees"] = count

    return result


def _coerce_date(value) -> str:
    """Normalize a registration date to ``YYYY-MM-DD`` (or shorter when partial).

    Accepts:
    - ISO strings (returned as-is, stripped) — SE shape.
    - ``{year, month, day}`` dicts where any field may be missing — NO shape.
      Falls back gracefully to ``YYYY-MM`` or ``YYYY`` if month/day are absent.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        year = value.get("year")
        month = value.get("month")
        day = value.get("day")
        if year and month and day:
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        if year and month:
            return f"{int(year):04d}-{int(month):02d}"
        if year:
            return f"{int(year):04d}"
    return ""
