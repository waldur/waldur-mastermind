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
