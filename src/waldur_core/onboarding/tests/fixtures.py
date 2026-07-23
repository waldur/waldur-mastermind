def get_estonian_ariregister_success_response(legal_person_identifier="70000310"):
    """
    Get a successful response from Estonian Äriregister API.

    This represents the response format from:
    https://avaandmed.ariregister.rik.ee/en/open-data-api/rights-representation-all-persons-related-company

    Args:
        legal_person_identifier: Company registration code (default: 70000310)

    Returns:
        dict: Mock API response with company and representatives data
    """
    return {
        "keha": {
            "ettevotjad": {
                "item": [
                    {
                        "ariregistri_kood": int(legal_person_identifier)
                        if isinstance(legal_person_identifier, str)
                        else legal_person_identifier,
                        "staatus": "R",
                        "esindusoiguse_eritingimused": {"item": []},
                        "arinimi": "Registrite ja Infosüsteemide Keskus",
                        "staatus_tekstina": "Entered into the register",
                        "isikud": {
                            "item": [
                                {
                                    "fyysilise_isiku_roll": "ASES",
                                    "ainuesindusoigus_olemas": "JAH",
                                    "isiku_liik": "F",
                                    "fyysilise_isiku_synniaeg": {},
                                    "fyysilise_isiku_kood": "37906094930",
                                    "fyysilise_isiku_roll_tekstina": "Person with right to represent the agency",
                                    "fyysilise_isiku_eesnimi": "Rivo",
                                    "isikukood_riik": "EST",
                                    "isikukoodi_riik_tekstina": "Estonia",
                                    "fyysilise_isiku_perenimi": "Reitmann",
                                },
                                {
                                    "fyysilise_isiku_roll": "KOAS",
                                    "ainuesindusoigus_olemas": "EI",
                                    "isiku_liik": "J",
                                    "fyysilise_isiku_synniaeg": {},
                                    "fyysilise_isiku_kood": "70000898",
                                    "fyysilise_isiku_roll_tekstina": "Superior agency",
                                    "fyysilise_isiku_eesnimi": {},
                                    "isikukood_riik": "EST",
                                    "isikukoodi_riik_tekstina": "Estonia",
                                    "fyysilise_isiku_perenimi": "Justiitsministeerium",
                                },
                            ]
                        },
                        "oiguslik_vorm_tekstina": "Agency of the Executive or other state institution",
                        "oiguslik_vorm": "TRAS",
                    }
                ]
            }
        },
        "paring": {
            "ariregistri_kood": int(legal_person_identifier)
            if isinstance(legal_person_identifier, str)
            else legal_person_identifier,
            "ariregister_parool": "password",
            "ariregister_kasutajanimi": "username",
            "fyysilise_isiku_eesnimi": {},
            "keel": "eng",
            "fyysilise_isiku_koodi_riik": {},
            "ariregister_valjundi_formaat": "json",
            "ariregister_sessioon": {},
            "fyysilise_isiku_kood": {},
            "fyysilise_isiku_perenimi": {},
        },
    }


def get_estonian_ariregister_empty_response():
    """
    Get an empty response from Estonian Äriregister API (company not found).

    Returns:
        dict: Mock API response with no company data
    """
    return {"keha": {"ettevotjad": {"item": []}}}


def get_estonian_ariregister_person_without_authority(civil_number="88888888888"):
    """
    Get a response where person exists but has no representation authority.

    Args:
        civil_number: Personal ID of the person without authority

    Returns:
        dict: Mock API response with person lacking authority
    """
    response = get_estonian_ariregister_success_response()
    # Add a person without authority
    response["keha"]["ettevotjad"]["item"][0]["isikud"]["item"].append(
        {
            "fyysilise_isiku_roll": "MEMBER",
            "ainuesindusoigus_olemas": "EI",
            "isiku_liik": "F",
            "fyysilise_isiku_synniaeg": {},
            "fyysilise_isiku_kood": civil_number,
            "fyysilise_isiku_roll_tekstina": "Board Member",
            "fyysilise_isiku_eesnimi": "Test",
            "isikukood_riik": "EST",
            "isikukoodi_riik_tekstina": "Estonia",
            "fyysilise_isiku_perenimi": "Person",
        }
    )
    return response


# Convenience constants for common test civil numbers
AUTHORIZED_CIVIL_NUMBER = "37906094930"  # Has JAH (yes) for representation
UNAUTHORIZED_CIVIL_NUMBER = "70000898"  # Has EI (no) for representation
NONEXISTENT_CIVIL_NUMBER = "99999999999"  # Not in the list

# Austrian WirtschaftsCompass test constants
AUTHORIZED_PERSON_IDENTIFIER = {
    "first_name": "John",
    "last_name": "Bull",
    "birth_date": "1970-01-01",
}
LEGAL_NAME = "Waldur GmbH"
LEGAL_PERSON_IDENTIFIER = "12345t"


def wico_resolve_register_number():
    """
    Get a successful response from Austrian WirtschaftsCompass API.

    This represents the response format from:
    https://api.wirtschaftscompass.at/organisation/v1/{wico-id}/company-report?load-option=BODIES_INVESTMENTS

    Args:
        legal_person_identifier: Company registration code (default: 56247t)

    Returns:
        dict: Mock API response with company and management data
    """
    return {
        "meta": {},
        "reference": {"wicoId": "076d39c4-a5f2-44ea-8540-50c9fb9c6132"},
        "status": {"registrationStatus": "ACTIVE"},
        "data": {
            "basicData": {
                "masterData": {
                    "registerNumber": {
                        "currentValue": {
                            "validFrom": "2025-10-15",
                            "registerNumber": {
                                "value": LEGAL_PERSON_IDENTIFIER,
                                "register": "AT_FB",
                            },
                        }
                    },
                },
                "communicationData": {
                    "name": {
                        "currentValue": {
                            "validFrom": "2002-11-16",
                            "content": LEGAL_NAME,
                        }
                    },
                },
            },
            "bodiesInvestments": {
                "management": {"management": []},
                "owners": {
                    "owner": [
                        {
                            "type": "GESCHÄFTSFÜHRER",
                            "entityReference": {
                                "structuredName": {
                                    "givenName": AUTHORIZED_PERSON_IDENTIFIER[
                                        "first_name"
                                    ],
                                    "surname": AUTHORIZED_PERSON_IDENTIFIER[
                                        "last_name"
                                    ],
                                },
                                "dateOfBirth": AUTHORIZED_PERSON_IDENTIFIER[
                                    "birth_date"
                                ],
                                "objectType": "NATURAL_PERSON",
                            },
                        }
                    ]
                },
                "derivedLastBeneficialOwners": {"derivedLastBenificialOwner": []},
            },
        },
    }


def wico_fetch_company_profile():
    """
    Get a successful resolve response from Austrian WirtschaftsCompass API.

    This represents the response format from:
    https://api.wirtschaftscompass.at/organisation/v1/resolve?register-number=56247t&register-number-type=FN

    Args:
        legal_person_identifier: Company registration code (for example: 56247t)

    Returns:
        dict: Mock API response with wico-id
    """
    return {"meta": {}, "data": {"wicoId": "076d39c4-a5f2-44ea-8540-50c9fb9c6132"}}


# Swedish Bolagsverket fixtures
BOLAGSVERKET_LEGAL_PERSON_IDENTIFIER = "5560021361"
BOLAGSVERKET_AUTHORIZED_PERSON_IDENTIFIER = "198101032384"
BOLAGSVERKET_UNKNOWN_PERSON_IDENTIFIER = "190001019999"
BOLAGSVERKET_COMPANY_NAME = "Testbolag 4 bokat av SKV Aktiebolag"


def bolagsverket_company_response():
    return [
        {
            "identitet": {
                "typ": {
                    "kod": "ORGANISATIONSNUMMER",
                    "klartext": "Organisationsnummer",
                },
                "identitetsbeteckning": BOLAGSVERKET_LEGAL_PERSON_IDENTIFIER,
            },
            "arende": {
                "arendenummer": "154823/2024",
                "avslutatTidpunkt": "2024-03-27T14:38:11.000+01:00",
            },
            "organisationsnamn": {
                "typ": {"kod": "FORETAGSNAMN", "klartext": "Företagsnamn"},
                "namn": BOLAGSVERKET_COMPANY_NAME,
            },
            "organisationsform": {"kod": "AB", "klartext": "Aktiebolag"},
            "organisationsstatusar": [],
            "funktionarer": [
                {
                    "personnamn": {"fornamn": "FN10003", "efternamn": "EN10003"},
                    "identitet": {
                        "typ": {"kod": "PERSONNUMMER", "klartext": "Personnummer"},
                        "identitetsbeteckning": BOLAGSVERKET_AUTHORIZED_PERSON_IDENTIFIER,
                    },
                    "funktionarsroller": [
                        {"kod": "LE", "klartext": "Styrelseledamot"},
                    ],
                    "postadress": {"postnummer": "85181", "postort": "SUNDSVALL"},
                },
            ],
            "antalValdaFunktionarer": {"ledamoter": 1, "suppleanter": 1},
        }
    ]


# --- Dun & Bradstreet Nordic fixtures ---


def dnb_token_response(
    expires_in: int = 3600, scope: str = "credit_data_companies"
) -> dict:
    """OAuth2 client_credentials response shape from D&B token endpoint."""
    return {
        "access_token": "fake-dnb-access-token",
        "expires_in": expires_in,
        "token_type": "Bearer",
        "scope": scope,
    }


DNB_SE_REGISTRATION_NUMBER = "5560021361"
DNB_SE_AUTHORIZED_PERSONNUMMER = "198101032384"
DNB_SE_UNKNOWN_PERSONNUMMER = "190001019999"
DNB_SE_COMPANY_NAME = "Testbolag D&B AB"


def dnb_rts_sweden_response(
    *,
    in_signatories: bool = True,
    in_co_signatories: bool = False,
    in_non_signatories: bool = False,
    person_pnr: str = DNB_SE_AUTHORIZED_PERSONNUMMER,
    person_name: tuple[str, str] = ("Anna", "Andersson"),
    role_description: str = "Styrelseledamot",
    signing_rules: list | None = None,
    signing_issues: list | None = None,
    signing_infos: list | None = None,
    interpretation_level: str = "COMPLETE",
    authority_type: str = "DEFAULT",
    period_start: str | None = None,
    period_end: str | None = None,
) -> dict:
    """
    Build a Right to Sign response for Sweden, placing the requested
    person in one of signatories[]/coSignatories[]/nonSignatories[].

    Defaults match the "authorized" happy path. Set the in_* flags to
    move the person between categories. Lists not containing the person
    are returned empty so tests stay focused. ``signing_rules`` /
    ``signing_issues`` / ``signing_infos`` accept raw entries in the
    Bisnode shape; the period and interpretation/authority fields default
    to typical sandbox values.
    """
    person_entry = {
        "type": "PERSON",
        "name": {
            "firstName": person_name[0],
            "lastName": person_name[1],
            "fullName": f"{person_name[0]} {person_name[1]}",
        },
        "nationalIdentificationNumber": person_pnr,
        "roles": [{"code": "MA", "description": role_description}],
        "signingRights": [{"ruleId": "r-1", "signatoryGroupId": "r-g-1"}],
    }

    response: dict = {
        "interpretationLevel": interpretation_level,
        "authorityType": authority_type,
        "company": {
            "registrationNumber": DNB_SE_REGISTRATION_NUMBER,
            "duns": "123456789",
            "name": DNB_SE_COMPANY_NAME,
            "registrationNumberSuffix": "",
            "legalForm": {"code": "AB", "scotsCode": "PLC", "description": "AB"},
            "status": {"operatingStatus": "ACTIVE", "operatingStatusScb": "ACTIVE"},
        },
        "signingAuthorityDescription": "Firman tecknas av styrelsen",
        "signingInfos": signing_infos or [],
        "signingIssues": signing_issues or [],
        "signatories": [person_entry] if in_signatories else [],
        "coSignatories": [person_entry] if in_co_signatories else [],
        "nonSignatories": [person_entry] if in_non_signatories else [],
        "signingRules": signing_rules or [],
        "subBusinesses": [],
    }
    if period_start or period_end:
        period: dict = {}
        if period_start:
            period["startDate"] = period_start
        if period_end:
            period["endDate"] = period_end
        response["signingAuthorityDescriptionPeriod"] = period
    return response


def dnb_credit_data_sweden_response(
    *,
    registration_number: str = DNB_SE_REGISTRATION_NUMBER,
    name: str = DNB_SE_COMPANY_NAME,
    street: str = "Storgatan 1",
    city: str = "Stockholm",
    postal_code: str = "11122",
    registration_date: str = "1995-01-15",
    include_address: bool = True,
) -> dict:
    """
    Credit Data Companies API response for a Swedish company.

    Mirrors the shape returned by /companies/se on the Bisnode v2 API.
    The SE backend fetches this only when the RTS lookup already authorized
    the user, to enrich company_data with address/postal and other audit
    fields (registrationDate, numberOfEmployees, vatRegistrationNumber).
    """
    response: dict = {
        "registrationNumber": registration_number,
        "name": name,
        "dunsNumber": "123456789",
        "registrationDate": registration_date,
        "legalForm": {"code": "AB", "description": "Aktiebolag"},
        "status": {"operatingStatus": "ACTIVE"},
        "vatRegistrationNumber": "SE556002136101",
        "numberOfEmployees": {"value": 42},
    }
    if include_address:
        response["address"] = {
            "country": "SE",
            "streetAddress": street,
            "town": city,
            "postalCode": postal_code,
        }
    return response


DNB_NO_REGISTRATION_NUMBER = "987654321"
# Norway's RTS API does not accept fødselsnummer; the request and response
# both key on name + birthDate.
DNB_NO_AUTHORIZED_PERSON = {
    "first_name": "Ola",
    "last_name": "Nordmann",
    "birth_date": "1980-05-17",
}
DNB_NO_UNKNOWN_PERSON = {
    "first_name": "Kari",
    "last_name": "Hansen",
    "birth_date": "1975-02-02",
}
DNB_NO_COMPANY_NAME = "D&B Test Norge AS"


def dnb_rts_norway_response(
    *,
    in_signatories: bool = True,
    in_co_signatories: bool = False,
    in_non_signatories: bool = False,
    first_name: str = "Ola",
    last_name: str = "Nordmann",
    birth_date: str = "1980-05-17",
    role_description: str = "Boardmember",
    signing_rules: list | None = None,
    signing_issues: list | None = None,
    signing_infos: list | None = None,
    interpretation_level: str = "COMPLETE",
    authority_type: str = "DEFAULT",
    period_start: str | None = None,
    period_end: str | None = None,
) -> dict:
    """
    Build a Right to Sign response for Norway, placing the requested
    person in one of signatories[]/coSignatories[]/nonSignatories[].

    Per the NO RTS contract, entries carry structured ``name`` +
    ``birthDate`` (no ``nationalIdentificationNumber``). Defaults match
    the "authorized" happy path. Set the in_* flags to move the person
    between categories. Lists not containing the person are returned
    empty so tests stay focused. ``signing_rules`` / ``signing_issues``
    / ``signing_infos`` accept raw entries in the Bisnode shape.
    """
    person_entry = {
        "type": "PERSON",
        "name": {
            "firstName": first_name,
            "lastName": last_name,
            "fullName": f"{first_name} {last_name}",
        },
        "birthDate": birth_date,
        "roles": [{"code": "LE", "description": role_description}],
        "signingRights": [{"ruleId": "r-1", "signatoryGroupId": "r-g-1"}],
    }

    response: dict = {
        "interpretationLevel": interpretation_level,
        "authorityType": authority_type,
        "company": {
            "registrationNumber": DNB_NO_REGISTRATION_NUMBER,
            "duns": "987654321",
            "name": DNB_NO_COMPANY_NAME,
            "legalForm": {"code": "AS", "scotsCode": "PLC", "description": "AS"},
            "status": {"operatingStatus": "ACTIVE"},
        },
        "signingAuthorityDescription": "Selskapet tegnes av styret",
        "signingInfos": signing_infos or [],
        "signingIssues": signing_issues or [],
        "signatories": [person_entry] if in_signatories else [],
        "coSignatories": [person_entry] if in_co_signatories else [],
        "nonSignatories": [person_entry] if in_non_signatories else [],
        "signingRules": signing_rules or [],
    }
    if period_start or period_end:
        period: dict = {}
        if period_start:
            period["startDate"] = period_start
        if period_end:
            period["endDate"] = period_end
        response["signingAuthorityDescriptionPeriod"] = period
    return response


def dnb_credit_data_norway_response(
    *,
    registration_number: str = DNB_NO_REGISTRATION_NUMBER,
    name: str = DNB_NO_COMPANY_NAME,
    street: str = "Karl Johans gate 1",
    city: str = "Oslo",
    postal_code: str = "0154",
    registration_date_year: int = 1995,
    registration_date_month: int = 1,
    registration_date_day: int = 15,
    employee_count: int = 25,
    include_address: bool = True,
) -> dict:
    """
    Credit Data Companies API response for a Norwegian company.

    Models the **nested v3 shape** D&B actually returns for /companies/no
    (different from Sweden's flat v2 shape — see the sandbox dump). Fields
    live under ``companyInformation`` and dates are ``{year,month,day}``
    dicts, not ISO strings. The NO backend fetches this only when the RTS
    lookup already authorized the user, to enrich company_data with
    address/postal/registration_date/employees. VAT is not published in
    the NO payload (only a boolean ``registeredInVat``).
    """
    company_information: dict = {
        "identifiers": {
            "registrationNumber": registration_number,
            "dunsNumber": "987654321",
        },
        "companyName": {
            "registeredName": {
                "name": name,
                "date": {"year": 2000, "month": 1, "day": 1},
            },
        },
        "legalForm": {
            "code": "LIMITED_COMPANY",
            "description": "Private Limited Company",
            "localCode": "AS",
        },
        "status": {"value": "ACTIVE"},
        "registrationInformation": {
            "registrationDate": {
                "year": registration_date_year,
                "month": registration_date_month,
                "day": registration_date_day,
            },
        },
        "generalCompanyData": {
            "countryCode": "NO",
            "employeeCount": employee_count,
            "registeredInVat": False,
        },
    }
    if include_address:
        company_information["contactPoints"] = {
            "registeredAddress": {
                "streetAddress": {
                    "town": city,
                    "countryCode": "NO",
                    "postalCode": postal_code,
                    "street": street,
                },
            },
        }
    response: dict = {
        "companyInformation": company_information,
    }
    return response


DNB_DK_REGISTRATION_NUMBER = "12345678"
DNB_DK_COMPANY_NAME = "D&B Test Danmark ApS"
# DK matches on name only (no SSN/birthDate, and we don't hold the cvrId).
DNB_DK_PERSON_IDENTIFIER = {
    "first_name": "Lars",
    "last_name": "Jensen",
}
DNB_DK_UNKNOWN_PERSON = {
    "first_name": "Mette",
    "last_name": "Hansen",
}


def dnb_rts_denmark_response(
    *,
    in_signatories: bool = True,
    in_co_signatories: bool = False,
    in_non_signatories: bool = False,
    first_name: str = "Lars",
    last_name: str = "Jensen",
    cvr_id: str = "4000000001",
    role_description: str = "Boardmember",
    signing_rules: list | None = None,
    signing_issues: list | None = None,
    signing_infos: list | None = None,
    interpretation_level: str = "COMPLETE",
    period_start: str | None = None,
    period_end: str | None = None,
) -> dict:
    """Right to Sign response for Denmark.

    Unlike SE/NO, DK entries carry ``name`` as a flat string plus a ``cvrId``
    (no structured name object, no birthDate). The DK request omits
    authorityType, so the response is modelled without one too. Defaults match
    the "authorized" happy path; set the in_* flags to move the person between
    signatories[]/coSignatories[]/nonSignatories[].
    """
    person_entry = {
        "type": "PERSON",
        "name": f"{first_name} {last_name}",
        "cvrId": cvr_id,
        "roles": [{"code": "LE", "description": role_description}],
        "signingRights": [{"ruleId": "r-1", "signatoryGroupId": "r-g-1"}],
    }

    response: dict = {
        "interpretationLevel": interpretation_level,
        "company": {
            "registrationNumber": DNB_DK_REGISTRATION_NUMBER,
            "duns": "222333444",
            "name": DNB_DK_COMPANY_NAME,
            "legalForm": {"code": "APS", "scotsCode": "PLC", "description": "ApS"},
            "status": {"operatingStatus": "ACTIVE"},
        },
        "signingAuthorityDescription": "Selskabet tegnes af en direktør",
        "signingInfos": signing_infos or [],
        "signingIssues": signing_issues or [],
        "signatories": [person_entry] if in_signatories else [],
        "coSignatories": [person_entry] if in_co_signatories else [],
        "nonSignatories": [person_entry] if in_non_signatories else [],
        "signingRules": signing_rules or [],
    }
    if period_start or period_end:
        period: dict = {}
        if period_start:
            period["startDate"] = period_start
        if period_end:
            period["endDate"] = period_end
        response["signingAuthorityDescriptionPeriod"] = period
    return response


def dnb_credit_info_denmark_response(
    *,
    registration_number: str = DNB_DK_REGISTRATION_NUMBER,
    street: str = "Gyngemose Parkvej 50",
    city: str = "Søborg",
    postal_code: str = "2860",
    vat_number: str = "DK12345678",
    foundation_year: int = 2001,
    foundation_month: int = 3,
    foundation_day: int = 14,
    employee_count: int = 12,
    include_address: bool = True,
) -> dict:
    """Credit Information (COMPANY_INFORMATION) response for a Danish company.

    Models the nested ``companyInformation`` shape (like NO) with DK-specific
    paths — VAT under ``identifiers.vatNumber`` and the date as
    ``foundationDate`` — that the DK enrichment call (COMPANY_INFORMATION
    segment) reads to populate address/VAT/registration date/employees.
    """
    company_information: dict = {
        "identifiers": {
            "registrationNumber": registration_number,
            "dunsNumber": "222333444",
            "vatNumber": vat_number,
        },
        "legalForm": {"current": {"code": "LIMITED_COMPANY", "localCode": "ApS"}},
        "status": {"value": "ACTIVE"},
        "registrationInformation": {
            "foundationDate": {
                "year": foundation_year,
                "month": foundation_month,
                "day": foundation_day,
            },
        },
        "generalCompanyData": {"countryCode": "DK", "employeeCount": employee_count},
    }
    if include_address:
        company_information["contactPoints"] = {
            "registeredAddress": {
                "streetAddress": {
                    "town": city,
                    "countryCode": "DK",
                    "postalCode": postal_code,
                    "street": street,
                },
            },
        }

    return {"companyInformation": company_information}


DNB_FI_REGISTRATION_NUMBER = "2345678-9"
DNB_FI_COMPANY_NAME = "D&B Testi Suomi Oy"
# Finland's RTS API takes name + birthDate (no Finnish HETU accepted), like NO.
DNB_FI_AUTHORIZED_PERSON = {
    "first_name": "Matti",
    "last_name": "Virtanen",
    "birth_date": "1979-09-19",
}
DNB_FI_UNKNOWN_PERSON = {
    "first_name": "Liisa",
    "last_name": "Korhonen",
    "birth_date": "1985-03-03",
}


def dnb_rts_finland_response(
    *,
    in_signatories: bool = True,
    in_co_signatories: bool = False,
    in_non_signatories: bool = False,
    first_name: str = "Matti",
    last_name: str = "Virtanen",
    birth_date: str = "1979-09-19",
    role_description: str = "Boardmember",
    signing_rules: list | None = None,
    signing_issues: list | None = None,
    signing_infos: list | None = None,
    interpretation_level: str = "COMPLETE",
    authority_type: str = "DEFAULT",
    period_start: str | None = None,
    period_end: str | None = None,
) -> dict:
    """
    Build a Right to Sign response for Finland, placing the requested
    person in one of signatories[]/coSignatories[]/nonSignatories[].

    Per the FI RTS contract, entries carry structured ``name`` +
    ``birthDate`` (no ``nationalIdentificationNumber``) — identical to
    Norway. Defaults match the "authorized" happy path. Set the in_* flags
    to move the person between categories. Lists not containing the person
    are returned empty so tests stay focused. ``signing_rules`` /
    ``signing_issues`` / ``signing_infos`` accept raw entries in the
    Bisnode shape.
    """
    person_entry = {
        "type": "PERSON",
        "name": {
            "firstName": first_name,
            "lastName": last_name,
            "fullName": f"{first_name} {last_name}",
        },
        "birthDate": birth_date,
        "roles": [{"code": "LE", "description": role_description}],
        "signingRights": [{"ruleId": "r-1", "signatoryGroupId": "r-g-1"}],
    }

    response: dict = {
        "interpretationLevel": interpretation_level,
        "authorityType": authority_type,
        "company": {
            "registrationNumber": DNB_FI_REGISTRATION_NUMBER,
            "duns": "555666777",
            "name": DNB_FI_COMPANY_NAME,
            "legalForm": {"code": "OY", "scotsCode": "PLC", "description": "Oy"},
            "status": {"operatingStatus": "ACTIVE"},
        },
        "signingAuthorityDescription": "Yhtiötä edustaa hallitus",
        "signingInfos": signing_infos or [],
        "signingIssues": signing_issues or [],
        "signatories": [person_entry] if in_signatories else [],
        "coSignatories": [person_entry] if in_co_signatories else [],
        "nonSignatories": [person_entry] if in_non_signatories else [],
        "signingRules": signing_rules or [],
    }
    if period_start or period_end:
        period: dict = {}
        if period_start:
            period["startDate"] = period_start
        if period_end:
            period["endDate"] = period_end
        response["signingAuthorityDescriptionPeriod"] = period
    return response


def dnb_credit_data_finland_response(
    *,
    registration_number: str = DNB_FI_REGISTRATION_NUMBER,
    name: str = DNB_FI_COMPANY_NAME,
    street: str = "Mannerheimintie 1",
    city: str = "Helsinki",
    postal_code: str = "00100",
    registration_date_year: int = 1998,
    registration_date_month: int = 6,
    registration_date_day: int = 1,
    employee_count: int = 18,
    include_address: bool = True,
) -> dict:
    """
    Credit Data Companies API response for a Finnish company.

    Models the **nested v3 shape** D&B returns for /companies/fi (same
    container as NO). The FI backend fetches this only when the RTS lookup
    already authorized the user, to enrich company_data with
    address/postal/registration_date/employees.
    """
    company_information: dict = {
        "identifiers": {
            "registrationNumber": registration_number,
            "dunsNumber": "555666777",
            "vatNumber": "FI23456789",
        },
        "companyName": {
            "registeredName": {
                "name": name,
                "date": {"year": 2000, "month": 1, "day": 1},
            },
        },
        "legalForm": {
            "code": "LIMITED_COMPANY",
            "description": "Private Limited Company",
            "localCode": "OY",
        },
        "status": {"value": "ACTIVE"},
        "registrationInformation": {
            "registrationDate": {
                "year": registration_date_year,
                "month": registration_date_month,
                "day": registration_date_day,
            },
        },
        "generalCompanyData": {
            "countryCode": "FI",
            "employeeCount": employee_count,
            "registeredInVat": True,
        },
    }
    if include_address:
        company_information["contactPoints"] = {
            "registeredAddress": {
                "streetAddress": {
                    "town": city,
                    "countryCode": "FI",
                    "postalCode": postal_code,
                    "street": street,
                },
            },
        }
    return {"companyInformation": company_information}
