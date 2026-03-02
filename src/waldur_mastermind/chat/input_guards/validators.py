"""Pure validation functions for PII patterns."""

import re
from datetime import date

# Country-specific IBAN lengths (ISO 13616)
_IBAN_LENGTHS = {
    "AL": 28,
    "AD": 24,
    "AT": 20,
    "AZ": 28,
    "BH": 22,
    "BY": 28,
    "BE": 16,
    "BA": 20,
    "BR": 29,
    "BG": 22,
    "CR": 22,
    "HR": 21,
    "CY": 28,
    "CZ": 24,
    "DK": 18,
    "DO": 28,
    "TL": 23,
    "EE": 20,
    "FO": 18,
    "FI": 18,
    "FR": 27,
    "GE": 22,
    "DE": 22,
    "GI": 23,
    "GR": 27,
    "GL": 18,
    "GT": 28,
    "HU": 28,
    "IS": 26,
    "IQ": 23,
    "IE": 22,
    "IL": 23,
    "IT": 27,
    "JO": 30,
    "KZ": 20,
    "XK": 20,
    "KW": 30,
    "LV": 21,
    "LB": 28,
    "LI": 21,
    "LT": 20,
    "LU": 20,
    "MT": 31,
    "MR": 27,
    "MU": 30,
    "MC": 27,
    "MD": 24,
    "ME": 22,
    "NL": 18,
    "MK": 19,
    "NO": 15,
    "PK": 24,
    "PS": 29,
    "PL": 28,
    "PT": 25,
    "QA": 29,
    "RO": 24,
    "LC": 32,
    "SM": 27,
    "SA": 24,
    "RS": 22,
    "SC": 31,
    "SK": 24,
    "SI": 19,
    "ES": 24,
    "SE": 24,
    "CH": 21,
    "TN": 24,
    "TR": 26,
    "UA": 29,
    "AE": 23,
    "GB": 22,
    "VA": 22,
    "VG": 24,
}


def validate_estonian_id(code: str) -> bool:
    """Validate an Estonian personal identification code (isikukood).

    Format: GYYMMDDSSSC
    G = gender/century (1-6), YY = year, MM = month, DD = day,
    SSS = sequence, C = checksum.
    """
    if not re.fullmatch(r"[1-6]\d{10}", code):
        return False

    gender_century = int(code[0])
    year_part = int(code[1:3])
    month = int(code[3:5])
    day = int(code[5:7])

    # Determine full year from gender digit
    if gender_century in (1, 2):
        year = 1800 + year_part
    elif gender_century in (3, 4):
        year = 1900 + year_part
    else:
        year = 2000 + year_part

    # Validate date
    try:
        date(year, month, day)
    except ValueError:
        return False

    # Mod-11 checksum with two weight rounds
    weights_1 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 1]
    weights_2 = [3, 4, 5, 6, 7, 8, 9, 1, 2, 3]

    digits = [int(d) for d in code]
    checksum = sum(d * w for d, w in zip(digits[:10], weights_1)) % 11

    if checksum == 10:
        checksum = sum(d * w for d, w in zip(digits[:10], weights_2)) % 11
        if checksum == 10:
            checksum = 0

    return checksum == digits[10]


def validate_iban(iban: str) -> bool:
    """Validate IBAN using ISO 7064 mod-97 algorithm + country-specific length."""
    # Normalize: strip spaces, uppercase
    cleaned = iban.replace(" ", "").upper()

    if len(cleaned) < 5:
        return False

    country = cleaned[:2]
    if not country.isalpha():
        return False

    # Check country-specific length if known
    expected_len = _IBAN_LENGTHS.get(country)
    if expected_len and len(cleaned) != expected_len:
        return False

    # Move first 4 chars to end
    rearranged = cleaned[4:] + cleaned[:4]

    # Convert letters to numbers (A=10, B=11, ..., Z=35)
    numeric = ""
    for ch in rearranged:
        if ch.isdigit():
            numeric += ch
        elif ch.isalpha():
            numeric += str(ord(ch) - ord("A") + 10)
        else:
            return False

    # Mod-97 check
    return int(numeric) % 97 == 1


def luhn_check(number: str) -> bool:
    """Validate a number string using the Luhn algorithm."""
    digits = number.replace(" ", "").replace("-", "")
    if not digits.isdigit() or len(digits) < 2:
        return False

    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d

    return total % 10 == 0


# --- Italian Codice Fiscale ---

# Odd-position (1-indexed: positions 1,3,5,...,15) value table
_CF_ODD = {
    "0": 1,
    "1": 0,
    "2": 5,
    "3": 7,
    "4": 9,
    "5": 13,
    "6": 15,
    "7": 17,
    "8": 19,
    "9": 21,
    "A": 1,
    "B": 0,
    "C": 5,
    "D": 7,
    "E": 9,
    "F": 13,
    "G": 15,
    "H": 17,
    "I": 19,
    "J": 21,
    "K": 2,
    "L": 4,
    "M": 18,
    "N": 20,
    "O": 11,
    "P": 3,
    "Q": 6,
    "R": 8,
    "S": 12,
    "T": 14,
    "U": 16,
    "V": 10,
    "W": 22,
    "X": 25,
    "Y": 24,
    "Z": 23,
}

# Even-position (1-indexed: positions 2,4,6,...,14) value table
_CF_EVEN = {
    "0": 0,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "A": 0,
    "B": 1,
    "C": 2,
    "D": 3,
    "E": 4,
    "F": 5,
    "G": 6,
    "H": 7,
    "I": 8,
    "J": 9,
    "K": 10,
    "L": 11,
    "M": 12,
    "N": 13,
    "O": 14,
    "P": 15,
    "Q": 16,
    "R": 17,
    "S": 18,
    "T": 19,
    "U": 20,
    "V": 21,
    "W": 22,
    "X": 23,
    "Y": 24,
    "Z": 25,
}


def validate_italy_codice_fiscale(code: str) -> bool:
    """Validate Italian Codice Fiscale (tax code) checksum.

    Format: 6 letters + 2 digits + 1 letter + 2 digits + 1 letter + 3 digits + 1 check letter.
    The check letter is computed from odd/even position lookup tables.
    """
    code = code.upper().strip()
    if len(code) != 16:
        return False

    total = 0
    for i, ch in enumerate(code[:15]):
        if i % 2 == 0:  # 0-indexed even = 1-indexed odd
            val = _CF_ODD.get(ch)
        else:
            val = _CF_EVEN.get(ch)
        if val is None:
            return False
        total += val

    expected = chr(ord("A") + total % 26)
    return code[15] == expected


# --- France NIR (numéro de sécurité sociale) ---


def validate_france_nir(code: str) -> bool:
    """Validate French NIR (social security number).

    Format: 15 digits total — 13 significant digits + 2-digit key.
    Key = 97 - (first_13_digits % 97).
    Corsica departments use 2A/2B which are replaced by 19/18 for computation.
    """
    cleaned = code.replace(" ", "").replace(".", "")
    if len(cleaned) != 15:
        return False

    # Handle Corsica: 2A→19, 2B→18 in the department field (positions 5-6, 0-indexed)
    nir_for_calc = cleaned[:15]
    if "A" in nir_for_calc.upper() or "B" in nir_for_calc.upper():
        nir_for_calc = nir_for_calc.upper().replace("2A", "19").replace("2B", "18")

    if not nir_for_calc.isdigit():
        return False

    number_part = int(nir_for_calc[:13])
    key = int(nir_for_calc[13:15])

    return key == 97 - (number_part % 97)


# --- Finland HETU (henkilötunnus) ---

_HETU_CHECK_CHARS = "0123456789ABCDEFHJKLMNPRSTUVWXY"


def validate_finland_hetu(code: str) -> bool:
    """Validate Finnish personal identity code (HETU).

    Format: DDMMYYCZZZQ where C is century sign, ZZZ is individual number, Q is check.
    The check character is computed as (DDMMYYZZZ as 9-digit integer) mod 31,
    looked up in a 31-character table.
    """
    cleaned = code.strip()
    if len(cleaned) != 11:
        return False

    # Extract parts
    date_part = cleaned[:6]
    individual = cleaned[7:10]
    check_char = cleaned[10].upper()

    if not date_part.isdigit() or not individual.isdigit():
        return False

    # Build the 9-digit number: DDMMYY + ZZZ
    nine_digits = int(date_part + individual)
    expected = _HETU_CHECK_CHARS[nine_digits % 31]

    return check_char == expected


# --- Spain DNI / NIE ---

_SPAIN_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"


def validate_spain_dni_nie(code: str) -> bool:
    """Validate Spanish DNI or NIE check letter.

    DNI: 8 digits + letter. Letter = number % 23 → lookup.
    NIE: X/Y/Z + 7 digits + letter. X→0, Y→1, Z→2, then same as DNI.
    """
    cleaned = code.upper().replace(" ", "").replace("-", "")
    if len(cleaned) < 9:
        return False

    first = cleaned[0]
    if first in ("X", "Y", "Z"):
        # NIE
        prefix_map = {"X": "0", "Y": "1", "Z": "2"}
        number_str = prefix_map[first] + cleaned[1:-1]
    elif first.isdigit():
        # DNI
        number_str = cleaned[:-1]
    else:
        return False

    if not number_str.isdigit():
        return False

    number = int(number_str)
    expected_letter = _SPAIN_DNI_LETTERS[number % 23]
    return cleaned[-1] == expected_letter


# --- Poland PESEL ---


def validate_poland_pesel(code: str) -> bool:
    """Validate Polish PESEL number.

    11 digits, weighted checksum: weights [1,3,7,9,1,3,7,9,1,3],
    check digit = (10 - sum%10) % 10.
    """
    cleaned = code.strip()
    if not re.fullmatch(r"\d{11}", cleaned):
        return False

    weights = [1, 3, 7, 9, 1, 3, 7, 9, 1, 3]
    digits = [int(d) for d in cleaned]
    total = sum(d * w for d, w in zip(digits[:10], weights))
    check = (10 - total % 10) % 10

    return check == digits[10]


# --- Germany Steuer-ID ---


def validate_germany_steuer_id(code: str) -> bool:
    """Validate German tax identification number (Steuerliche Identifikationsnummer).

    11 digits, first digit non-zero.
    Structural rule: among the first 10 digits, exactly one digit must appear
    twice (or one digit three times with another digit missing), and the rest
    appear exactly once.
    Check digit: ISO 7064 MOD 11,10.
    """
    cleaned = code.strip()
    if not re.fullmatch(r"[1-9]\d{10}", cleaned):
        return False

    # Structural rule: digit frequency check on first 10 digits
    freq = [0] * 10
    for ch in cleaned[:10]:
        freq[int(ch)] += 1

    # Count how many digits appear 0, 1, 2, 3 times
    zeros = freq.count(0)
    twos = freq.count(2)
    threes = freq.count(3)

    # Valid: exactly one digit appears twice, rest once (zeros=1, twos=1)
    # Or: one digit appears three times, another missing (zeros=2, threes=1)
    if not ((zeros == 1 and twos == 1 and threes == 0) or (zeros == 2 and threes == 1)):
        return False

    # ISO 7064 MOD 11,10 check digit
    product = 10
    for i in range(10):
        total = (int(cleaned[i]) + product) % 10
        if total == 0:
            total = 10
        product = (total * 2) % 11

    check = (11 - product) % 10
    return check == int(cleaned[10])


# --- Czech Republic birth number (Rodné číslo) ---


def validate_czech_birth_number(code: str) -> bool:
    """Validate Czech/Slovak birth number (Rodné číslo).

    Format: YYMMDD/SSSC or YYMMDD/SSS (9-digit pre-1954).
    10-digit numbers (post-1954) must be divisible by 11.
    9-digit numbers pass without mod check.
    Month may have +20 (female), +50 or +70 (Slovak variants).
    """
    cleaned = code.replace("/", "").strip()
    if len(cleaned) not in (9, 10):
        return False
    if not cleaned.isdigit():
        return False

    # Validate date structure (YYMMDD in first 6 digits)
    month = int(cleaned[2:4])
    day = int(cleaned[4:6])
    # Month offsets: +20 female, +50 extra sequence, +70 Slovak female extra
    base_month = month % 50  # strips +50/+70
    if base_month > 20:
        base_month -= 20  # strips +20
    if not (1 <= base_month <= 12):
        return False
    if not (1 <= day <= 31):
        return False

    # 10-digit form: must be divisible by 11
    if len(cleaned) == 10:
        return int(cleaned) % 11 == 0

    # 9-digit form (pre-1954): date validation only, no modular check
    return True


# --- Netherlands BSN (Burgerservicenummer) ---


def validate_netherlands_bsn(code: str) -> bool:
    """Validate Dutch BSN using the elfproef (11-proof).

    8 or 9 digits. Weights [9,8,7,6,5,4,3,2,-1].
    For 8-digit BSN, pad with leading zero.
    The weighted sum must be divisible by 11 and must be > 0.
    """
    cleaned = code.strip()
    if not re.fullmatch(r"\d{8,9}", cleaned):
        return False

    # Pad 8-digit to 9-digit with leading zero
    if len(cleaned) == 8:
        cleaned = "0" + cleaned

    weights = [9, 8, 7, 6, 5, 4, 3, 2, -1]
    digits = [int(d) for d in cleaned]
    total = sum(d * w for d, w in zip(digits, weights))

    return total > 0 and total % 11 == 0


# --- Sweden Personnummer ---


def validate_sweden_personnummer(code: str) -> bool:
    """Validate Swedish personal identity number using Luhn on 10-digit form.

    Input may be YYYYMMDD-NNNN, YYMMDD-NNNN, YYMMDDNNNN, etc.
    Strip century digits and separator to get YYMMDDNNNN (10 digits),
    then apply Luhn algorithm.
    """
    cleaned = code.strip().replace("-", "").replace("+", "")
    if not cleaned.isdigit():
        return False

    # If 12 digits (with century), strip first two
    if len(cleaned) == 12:
        cleaned = cleaned[2:]

    if len(cleaned) != 10:
        return False

    return luhn_check(cleaned)
