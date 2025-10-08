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
