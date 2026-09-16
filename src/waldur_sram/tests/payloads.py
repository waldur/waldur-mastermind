"""SCIM payloads shaped exactly like SRAM's (SURFscz/SBS ``server/scim``).

The builders follow ``create_user_template`` / ``create_group_template``,
including SBS's habit of replacing ``None`` with ``""``.
"""

import base64
import uuid

from waldur_sram.mapping import SRAM_GROUP_EXTENSION_URN, SRAM_USER_EXTENSION_URN

SCOPE = "@test.sram.surf.nl"
CORE_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
CORE_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"

KEY1 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJJ8hP1eFBxBCWiUaB5vsLAvFaYjs0zQ0gWOltsWd8LI key1@example.com"
KEY2 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH4VKrindOs5cEW4vv2NZfGAB6A1/tuYmOXv2emFcuSC key2@example.com"


def b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def sram_user(
    username="roger",
    given_name="Roger",
    family_name="Doe",
    email=None,
    ssh_keys=(),
    affiliation="member@example.org",
    external_id=None,
    active=True,
):
    external_id = external_id or f"{uuid.uuid4()}{SCOPE}"
    return {
        "schemas": [CORE_USER, SRAM_USER_EXTENSION_URN],
        "externalId": external_id,
        "userName": username,
        "name": {"givenName": given_name, "familyName": family_name},
        "displayName": f"{given_name} {family_name}",
        "active": active,
        "emails": [{"value": email or f"{username}@example.org", "primary": True}],
        "x509Certificates": [{"value": b64(key)} for key in ssh_keys],
        SRAM_USER_EXTENSION_URN: {
            "eduPersonScopedAffiliation": affiliation,
            "eduPersonUniqueId": f"{username}@test.sram.surf.nl",
            "voPersonExternalAffiliation": "",
            "voPersonExternalId": f"{username}@example.org",
            "sramInactiveDays": 0,
        },
    }


def sram_group(
    display_name="Research",
    urn="uuc:research",
    member_ids=(),
    labels=(),
    external_id=None,
    description="",
):
    external_id = external_id or f"{uuid.uuid4()}{SCOPE}"
    extension = {"description": description, "urn": urn}
    if labels:
        extension["labels"] = sorted(labels)
    if urn.count(":") == 1:
        extension["links"] = [
            {"name": "sbs_url", "value": "http://localhost:8080/collaborations/x"},
            {"name": "logo", "value": ""},
        ]
    return {
        "schemas": [CORE_GROUP, SRAM_GROUP_EXTENSION_URN],
        "externalId": external_id,
        "displayName": display_name,
        "members": [
            {"value": member, "display": member, "$ref": f"http://sbs/Users/{member}"}
            for member in sorted(member_ids)
        ],
        SRAM_GROUP_EXTENSION_URN: extension,
    }
