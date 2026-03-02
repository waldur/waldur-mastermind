import unittest

from waldur_mastermind.chat.input_guards.validators import (
    luhn_check,
    validate_czech_birth_number,
    validate_estonian_id,
    validate_finland_hetu,
    validate_france_nir,
    validate_germany_steuer_id,
    validate_iban,
    validate_italy_codice_fiscale,
    validate_netherlands_bsn,
    validate_poland_pesel,
    validate_spain_dni_nie,
    validate_sweden_personnummer,
)


class EstonianIDValidatorTest(unittest.TestCase):
    """Tests for Estonian personal ID code (isikukood) validation."""

    def test_valid_male_born_1987(self):
        # gender 3 (male, 1900s), 1987-06-18, seq 123, check 7
        self.assertTrue(validate_estonian_id("38706181237"))

    def test_valid_female_born_2000(self):
        # gender 6 (female, 2000s), 2000-01-01, seq 123, check 3
        self.assertTrue(validate_estonian_id("60001011233"))

    def test_valid_male_born_1800s(self):
        # gender 1 (male, 1800s), 1875-01-01, seq 123, check 5
        self.assertTrue(validate_estonian_id("17501011235"))

    def test_invalid_checksum(self):
        # Change last digit to make checksum wrong
        self.assertFalse(validate_estonian_id("38706181230"))

    def test_invalid_month(self):
        # Month 13 is invalid
        self.assertFalse(validate_estonian_id("38713181234"))

    def test_invalid_day(self):
        # Day 32 is invalid
        self.assertFalse(validate_estonian_id("38706321234"))

    def test_invalid_gender_digit(self):
        # Gender digit 0 is invalid
        self.assertFalse(validate_estonian_id("08706181234"))

    def test_invalid_gender_digit_7(self):
        # Gender digit 7 is invalid
        self.assertFalse(validate_estonian_id("78706181234"))

    def test_too_short(self):
        self.assertFalse(validate_estonian_id("3870618123"))

    def test_too_long(self):
        self.assertFalse(validate_estonian_id("387061812345"))

    def test_non_numeric(self):
        self.assertFalse(validate_estonian_id("3870618123a"))

    def test_empty_string(self):
        self.assertFalse(validate_estonian_id(""))

    def test_february_29_leap_year(self):
        # 2000 is a leap year, so Feb 29 is valid, check 9
        self.assertTrue(validate_estonian_id("50002291239"))

    def test_february_29_non_leap_year(self):
        # 2001 is not a leap year, so Feb 29 is invalid
        self.assertFalse(validate_estonian_id("50102291232"))


class IBANValidatorTest(unittest.TestCase):
    """Tests for IBAN validation (ISO 7064 mod-97)."""

    def test_valid_estonian_iban(self):
        self.assertTrue(validate_iban("EE382200221020145685"))

    def test_valid_estonian_iban_with_spaces(self):
        self.assertTrue(validate_iban("EE38 2200 2210 2014 5685"))

    def test_valid_german_iban(self):
        self.assertTrue(validate_iban("DE89370400440532013000"))

    def test_valid_uk_iban(self):
        self.assertTrue(validate_iban("GB29NWBK60161331926819"))

    def test_valid_french_iban(self):
        self.assertTrue(validate_iban("FR7630006000011234567890189"))

    def test_invalid_check_digits(self):
        # Changed check digits from 38 to 39
        self.assertFalse(validate_iban("EE392200221020145685"))

    def test_invalid_country_length(self):
        # EE should be 20 chars, this is 19
        self.assertFalse(validate_iban("EE38220022102014568"))

    def test_too_short(self):
        self.assertFalse(validate_iban("EE38"))

    def test_empty_string(self):
        self.assertFalse(validate_iban(""))

    def test_lowercase_is_accepted(self):
        self.assertTrue(validate_iban("ee382200221020145685"))

    def test_invalid_characters(self):
        self.assertFalse(validate_iban("EE38!200221020145685"))

    def test_non_alpha_country(self):
        self.assertFalse(validate_iban("12382200221020145685"))


class LuhnCheckTest(unittest.TestCase):
    """Tests for Luhn algorithm validation."""

    def test_valid_visa(self):
        self.assertTrue(luhn_check("4111111111111111"))

    def test_valid_mastercard(self):
        self.assertTrue(luhn_check("5500000000000004"))

    def test_valid_amex(self):
        self.assertTrue(luhn_check("378282246310005"))

    def test_valid_with_spaces(self):
        self.assertTrue(luhn_check("4111 1111 1111 1111"))

    def test_valid_with_dashes(self):
        self.assertTrue(luhn_check("4111-1111-1111-1111"))

    def test_invalid_number(self):
        self.assertFalse(luhn_check("4111111111111112"))

    def test_single_digit(self):
        self.assertFalse(luhn_check("4"))

    def test_empty_string(self):
        self.assertFalse(luhn_check(""))

    def test_non_numeric(self):
        self.assertFalse(luhn_check("411111111111111a"))

    def test_all_zeros(self):
        # 00 passes Luhn (sum = 0, mod 10 = 0)
        self.assertTrue(luhn_check("00"))


class ItalyCodiceFiscaleValidatorTest(unittest.TestCase):
    """Tests for Italian Codice Fiscale (tax code) validation."""

    def test_valid_codice_fiscale(self):
        self.assertTrue(validate_italy_codice_fiscale("RSSMRA85T10A562S"))

    def test_valid_lowercase(self):
        self.assertTrue(validate_italy_codice_fiscale("rssmra85t10a562s"))

    def test_invalid_check_letter(self):
        # Change check letter from S to A
        self.assertFalse(validate_italy_codice_fiscale("RSSMRA85T10A562A"))

    def test_wrong_length(self):
        self.assertFalse(validate_italy_codice_fiscale("RSSMRA85T10A562"))

    def test_too_long(self):
        self.assertFalse(validate_italy_codice_fiscale("RSSMRA85T10A562SS"))

    def test_empty_string(self):
        self.assertFalse(validate_italy_codice_fiscale(""))


class FranceNIRValidatorTest(unittest.TestCase):
    """Tests for French NIR (social security number) validation."""

    def test_valid_nir(self):
        # key = 97 - (1850275108123 % 97) = 32
        self.assertTrue(validate_france_nir("185027510812332"))

    def test_invalid_key(self):
        # Change key from 32 to 33
        self.assertFalse(validate_france_nir("185027510812333"))

    def test_corsica_2a_department(self):
        # Corsica department 2A, key = 97 - (1851975108123 % 97) = 13
        self.assertTrue(validate_france_nir("1852A7510812313"))

    def test_wrong_length(self):
        self.assertFalse(validate_france_nir("18502751081233"))

    def test_non_numeric(self):
        # Non-Corsica letters should fail
        self.assertFalse(validate_france_nir("18502X510812332"))

    def test_empty_string(self):
        self.assertFalse(validate_france_nir(""))


class FinlandHETUValidatorTest(unittest.TestCase):
    """Tests for Finnish HETU (personal identity code) validation."""

    def test_valid_hetu(self):
        # 131052-308T: (131052308 % 31) = index for 'T'
        self.assertTrue(validate_finland_hetu("131052-308T"))

    def test_invalid_check_char(self):
        # Change check from T to A
        self.assertFalse(validate_finland_hetu("131052-308A"))

    def test_wrong_length(self):
        self.assertFalse(validate_finland_hetu("131052-30T"))

    def test_non_numeric_date(self):
        self.assertFalse(validate_finland_hetu("13A052-308T"))

    def test_empty_string(self):
        self.assertFalse(validate_finland_hetu(""))


class SpainDNINIEValidatorTest(unittest.TestCase):
    """Tests for Spanish DNI and NIE validation."""

    def test_valid_dni(self):
        # 12345678 % 23 = index for 'Z'
        self.assertTrue(validate_spain_dni_nie("12345678Z"))

    def test_valid_nie_x_prefix(self):
        # X -> 0, so 01234567 % 23 = index for 'L'
        self.assertTrue(validate_spain_dni_nie("X1234567L"))

    def test_invalid_dni_letter(self):
        # Wrong letter for 12345678
        self.assertFalse(validate_spain_dni_nie("12345678A"))

    def test_invalid_nie_letter(self):
        self.assertFalse(validate_spain_dni_nie("X1234567A"))

    def test_too_short(self):
        self.assertFalse(validate_spain_dni_nie("1234567Z"))

    def test_empty_string(self):
        self.assertFalse(validate_spain_dni_nie(""))


class PolandPESELValidatorTest(unittest.TestCase):
    """Tests for Polish PESEL validation."""

    def test_valid_pesel(self):
        self.assertTrue(validate_poland_pesel("44051401359"))

    def test_invalid_check_digit(self):
        # Change last digit
        self.assertFalse(validate_poland_pesel("44051401350"))

    def test_wrong_length(self):
        self.assertFalse(validate_poland_pesel("4405140135"))

    def test_non_numeric(self):
        self.assertFalse(validate_poland_pesel("4405140135a"))

    def test_empty_string(self):
        self.assertFalse(validate_poland_pesel(""))


class GermanySteuerIDValidatorTest(unittest.TestCase):
    """Tests for German Steuerliche Identifikationsnummer validation."""

    def test_valid_steuer_id(self):
        self.assertTrue(validate_germany_steuer_id("65929970489"))

    def test_invalid_check_digit(self):
        # Change last digit from 9 to 0
        self.assertFalse(validate_germany_steuer_id("65929970480"))

    def test_invalid_digit_frequency(self):
        # All same digits — violates the structural rule
        self.assertFalse(validate_germany_steuer_id("11111111118"))

    def test_leading_zero(self):
        # First digit must be non-zero
        self.assertFalse(validate_germany_steuer_id("05929970489"))

    def test_wrong_length(self):
        self.assertFalse(validate_germany_steuer_id("6592997048"))

    def test_empty_string(self):
        self.assertFalse(validate_germany_steuer_id(""))


class CzechBirthNumberValidatorTest(unittest.TestCase):
    """Tests for Czech/Slovak birth number (Rodné číslo) validation."""

    def test_valid_10_digit(self):
        # 9001011008 is divisible by 11, month=01, day=01
        self.assertTrue(validate_czech_birth_number("9001011008"))

    def test_valid_10_digit_with_slash(self):
        self.assertTrue(validate_czech_birth_number("900101/1008"))

    def test_valid_9_digit(self):
        # Pre-1954 form, month=01, day=01
        self.assertTrue(validate_czech_birth_number("530101123"))

    def test_valid_female_month_offset(self):
        # Month 21 = January + 20 (female), day=01
        self.assertTrue(validate_czech_birth_number("9021011010"))

    def test_invalid_month_zero(self):
        self.assertFalse(validate_czech_birth_number("9000011008"))

    def test_invalid_month_13(self):
        # Month 13 is invalid (13 % 50 = 13, not > 20, so 13 > 12)
        self.assertFalse(validate_czech_birth_number("9013011008"))

    def test_invalid_day_zero(self):
        self.assertFalse(validate_czech_birth_number("9001001008"))

    def test_invalid_day_32(self):
        self.assertFalse(validate_czech_birth_number("9001321008"))

    def test_invalid_mod_11(self):
        # 9001011009 is not divisible by 11
        self.assertFalse(validate_czech_birth_number("9001011009"))

    def test_wrong_length(self):
        self.assertFalse(validate_czech_birth_number("90010110"))

    def test_non_numeric(self):
        self.assertFalse(validate_czech_birth_number("900101100a"))

    def test_empty_string(self):
        self.assertFalse(validate_czech_birth_number(""))


class NetherlandsBSNValidatorTest(unittest.TestCase):
    """Tests for Dutch BSN (Burgerservicenummer) elfproef validation."""

    def test_valid_9_digit_bsn(self):
        self.assertTrue(validate_netherlands_bsn("111222333"))

    def test_valid_9_digit_bsn_2(self):
        self.assertTrue(validate_netherlands_bsn("123456782"))

    def test_valid_8_digit_bsn(self):
        # 8-digit gets padded with leading zero
        self.assertTrue(validate_netherlands_bsn("10000021"))

    def test_invalid_elfproef(self):
        self.assertFalse(validate_netherlands_bsn("123456789"))

    def test_wrong_length_7_digits(self):
        self.assertFalse(validate_netherlands_bsn("1234567"))

    def test_wrong_length_10_digits(self):
        self.assertFalse(validate_netherlands_bsn("1234567890"))

    def test_non_numeric(self):
        self.assertFalse(validate_netherlands_bsn("12345678a"))

    def test_empty_string(self):
        self.assertFalse(validate_netherlands_bsn(""))


class SwedenPersonnummerValidatorTest(unittest.TestCase):
    """Tests for Swedish personnummer (Luhn on 10-digit form) validation."""

    def test_valid_10_digit(self):
        self.assertTrue(validate_sweden_personnummer("8112289874"))

    def test_valid_12_digit(self):
        # Century digits stripped before Luhn check
        self.assertTrue(validate_sweden_personnummer("198112289874"))

    def test_valid_with_dash(self):
        self.assertTrue(validate_sweden_personnummer("811228-9874"))

    def test_invalid_luhn(self):
        # Change last digit to break Luhn
        self.assertFalse(validate_sweden_personnummer("8112289875"))

    def test_wrong_length(self):
        self.assertFalse(validate_sweden_personnummer("81122898"))

    def test_non_numeric(self):
        self.assertFalse(validate_sweden_personnummer("811228987a"))

    def test_empty_string(self):
        self.assertFalse(validate_sweden_personnummer(""))
