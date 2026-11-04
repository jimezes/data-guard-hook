"""
Detectors for "real data" in tabular files.

To add a new kind of field, append a Detector to DETECTORS at the bottom.
Each detector can flag a column in two ways:

  1. Header match:  the column name matches `header` (a regex, checked against
     a normalised header: lowercase, spaces/hyphens -> underscores) and the
     column holds non-placeholder values.
  2. Value match:   `value` (a function str -> bool) returns True for at least
     `min_hits` cells in a column, AND for at least `min_ratio` of that column's
     non-empty cells, AND the matching cells are varied enough (`min_distinct`
     = distinct matches / matches), since real people rarely repeat, AND (if
     set) `column_check` accepts the list of matching values as a whole. Use
     it for properties no single cell reveals, e.g. "these dates span decades".

Either way is enough. Leave `header` or `value` as None to use just one.

Some personal data only shows across columns or rows (a first name and a
surname in two columns; one person stored as "field,value" rows). Those are
found by the functions in TABLE_DETECTORS at the bottom, which see the whole
table.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Optional
import unicodedata


def norm_header(h: str) -> str:
    """Lowercase, strip accents, spaces/hyphens/dots -> underscores, and split
    camelCase (common in JSON): "dateOfBirth" -> "date_of_birth"."""
    h = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", h.strip())
    h = unicodedata.normalize("NFKD", h)
    h = "".join(ch for ch in h if not unicodedata.combining(ch))
    return re.sub(r"[\s\-./]+", "_", h.strip().lower()).strip("_")


@dataclass
class Detector:
    name: str                                   # shown to the user, e.g. "Email addresses"
    header: Optional[str] = None                # regex on normalised column name
    value: Optional[Callable[[str], bool]] = None
    min_hits: int = 3
    min_ratio: float = 0.5
    min_distinct: float = 0.5
    column_check: Optional[Callable[[list[str]], bool]] = None
    # (column header, all headers in the table) -> True to skip this detector
    # for the column, e.g. "city" columns are never person names. Headers are
    # normalised. Should never skip a header that clearly names this field.
    skip_column: Optional[Callable[[str, list[str]], bool]] = None

    def header_matches(self, header: str) -> bool:
        return bool(self.header) and re.search(self.header, header) is not None


# --------------------------------------------------------------------------- #
# Placeholder / dummy values: these never count as real data.
# --------------------------------------------------------------------------- #
_PLACEHOLDER = re.compile(
    r"^(test\w*|dummy|sample|example|foo|bar|baz|lorem.*|ipsum|n/?a|none|null|"
    r"tbd|xxx+|\*+|-+|\?+|john doe|jane doe|john smith|jane smith|"
    r"(test|demo|dummy|sample|example|fake) ?(user|person|customer|client|account|name)\s*\d*|"
    r"first ?name|last ?name|name|email|phone|address)$",
    re.I,
)
_PLACEHOLDER_EMAIL_DOMAIN = re.compile(
    r"@(example\.(com|org|net)|test\.\w+|localhost|invalid|mailinator\.com)$", re.I
)


# Shared team mailboxes (support@, sales@ ...) belong to no single person.
_ROLE_EMAIL = re.compile(
    r"^(info|support|help|helpdesk|sales|billing|accounts?|invoices?|finance|press|media|"
    r"careers|jobs|hr|recruiting|admin|administrator|contact|hello|office|team|marketing|"
    r"enquiries|inquiries|orders|service|customerservice|webmaster|postmaster|hostmaster|"
    r"abuse|security|privacy|legal|compliance|it|ops|dev|noreply|no-reply|donotreply|"
    r"do-not-reply|notifications?|newsletter|reception|bookings|reservations)"
    r"([.+_-][a-z0-9]+)?@", re.I)


def is_role_email(v: str) -> bool:
    return bool(_ROLE_EMAIL.match(v.strip()))


def is_placeholder(v: str) -> bool:
    v = v.strip()
    if not v or _PLACEHOLDER.match(v):
        return True
    if _PLACEHOLDER_EMAIL_DOMAIN.search(v) or is_role_email(v):
        return True
    return False


# --------------------------------------------------------------------------- #
# Value checks
# --------------------------------------------------------------------------- #
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


# "@" and "." spelled out: as "[at]" and "[dot]", as "(at)", or as plain "at" and "dot"
_BR_AT = r"\s*[\[({<]\s*at\s*[\])}>]\s*"
_BR_DOT = r"\s*[\[({<]\s*dot\s*[\])}>]\s*"
_WORD_DOT = r"\s+dot\s+"
_OBF_EMAILS = (
    # bracketed "at"; the domain may use any kind of dot
    re.compile(rf"([A-Za-z0-9._%+-]+){_BR_AT}([A-Za-z0-9-]+(?:(?:{_BR_DOT}|{_WORD_DOT}|\.)[A-Za-z0-9-]+)+)", re.I),
    # plain " at " only counts together with a spelled-out "dot" (else "arrived at 10.30")
    re.compile(rf"([A-Za-z0-9._%+-]+)\s+at\s+([A-Za-z0-9-]+(?:(?:{_BR_DOT}|{_WORD_DOT})[A-Za-z0-9-]+)+)", re.I),
)


def find_email(v: str) -> Optional[str]:
    """The first email address in v, undoing [at]/[dot] style obfuscation."""
    m = _EMAIL.search(v)
    if m:
        return m.group(0)
    for rx in _OBF_EMAILS:
        m = rx.search(v)
        if m:
            domain = re.sub(rf"{_BR_DOT}|{_WORD_DOT}", ".", m.group(2), flags=re.I)
            if re.search(r"\.[A-Za-z]{2,}$", domain):
                return f"{m.group(1)}@{domain}"
    return None


def looks_like_email(v: str) -> bool:
    e = find_email(v)
    return bool(e) and not _PLACEHOLDER_EMAIL_DOMAIN.search(e) and not is_role_email(e)


_PHONE_CHARS = re.compile(r"^\+?[\d\s().\-/]{7,22}$")


def is_isbn13(digits: str) -> bool:
    """13 digits, 978/979 prefix and a valid ISBN-13 check digit."""
    if len(digits) != 13 or not digits.startswith(("978", "979")):
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits[:12]))
    return (10 - total % 10) % 10 == int(digits[12])


# National ID numbers with a fixed, checkable shape. Others (passport, tax IDs)
# vary too much by country, so they are detected by header only.
_SSN_SHAPE = re.compile(r"^\d{3}-\d{2}-\d{4}$")
_NINO = re.compile(r"^(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]\d{6}[A-D]$")


def is_greek_afm(v: str) -> bool:
    """Greek tax number (ΑΦΜ): 9 digits, last one a mod-11 check digit."""
    v = v.strip()
    if not re.fullmatch(r"\d{9}", v) or v == "000000000":
        return False
    total = sum(int(d) << (8 - i) for i, d in enumerate(v[:8]))
    return total % 11 % 10 == int(v[8])


def is_greek_amka(v: str) -> bool:
    """Greek social security number (ΑΜΚΑ): 11 digits, birth date ddmmyy + Luhn."""
    v = v.strip()
    if not re.fullmatch(r"\d{11}", v):
        return False
    try:
        datetime.strptime(v[:6], "%d%m%y")
    except ValueError:
        return False
    return luhn_ok(v)


def looks_like_national_id(v: str) -> bool:
    v = v.strip().upper()
    if _SSN_SHAPE.match(v):                       # US SSN, with its invalid ranges excluded
        area, group, serial = v.split("-")
        return area not in ("000", "666") and area[0] != "9" and group != "00" and serial != "0000"
    if is_greek_afm(v) or is_greek_amka(v):
        return True
    return bool(_NINO.match(v.replace(" ", "")))  # UK National Insurance number


def looks_like_ipv4(v: str) -> bool:
    parts = v.strip().split(".")
    if len(parts) != 4 or not all(p.isdigit() and len(p) <= 3 for p in parts):
        return False
    nums = [int(p) for p in parts]
    # all(<=255) is an IP; all(<10) is far more often a version number
    return all(n <= 255 for n in nums) and max(nums) >= 10


_IPV6 = re.compile(r"^(?=.*:.*:)[0-9a-f:]{2,39}$", re.I)


def looks_like_ip(v: str) -> bool:
    v = v.strip()
    if looks_like_ipv4(v):
        return True
    # IPv6: hex groups with "::" compression or all 8 groups (rules out "12:30")
    return bool(_IPV6.match(v)) and ("::" in v or v.count(":") == 7)


_MAC = re.compile(r"^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.I)


def looks_like_mac(v: str) -> bool:
    return bool(_MAC.match(v.strip()))


_CARD_SHAPE = re.compile(r"^\d[\d \-]{11,22}\d$")
# Issuer prefixes: Visa 4, Mastercard 51-55/2221-2720, Amex 34/37, Discover 6011/644-649/65, JCB 35
_CARD_PREFIX = re.compile(r"^(4|5[1-5]|2[2-7]|3[47]|6011|64[4-9]|65|35)")


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def looks_like_card(v: str) -> bool:
    v = v.strip()
    if not _CARD_SHAPE.match(v):
        return False
    digits = re.sub(r"\D", "", v)
    return 13 <= len(digits) <= 19 and bool(_CARD_PREFIX.match(digits)) and luhn_ok(digits)


# GPS coordinates precise enough to point at a home (4+ decimals = ~10 m).
_COORD = r"-?\d{1,3}\.\d{4,}"
_COORD_PAIR = re.compile(rf"^\(?\s*({_COORD})\s*[,;/ ]\s*({_COORD})\s*\)?$")


def looks_like_precise_coordinate(v: str) -> bool:
    v = v.strip()
    m = _COORD_PAIR.match(v)
    if m:
        return abs(float(m.group(1))) <= 90 and abs(float(m.group(2))) <= 180
    return bool(re.fullmatch(_COORD, v)) and abs(float(v)) <= 180


# North American numbers written as bare digits: 10 digits, or 11 with a leading 1.
# Area code and exchange both start 2-9 and are not N11 (411, 911 ...).
_NANP = re.compile(r"^1?([2-9]\d{2})([2-9]\d{2})\d{4}$")


def looks_like_bare_nanp(v: str) -> bool:
    m = _NANP.match(v.strip())
    return bool(m) and m.group(1)[1:] != "11" and m.group(2)[1:] != "11"


def not_in_sequence(values: list[str]) -> bool:
    """IDs are usually issued in order; real phone lists are not."""
    nums = [int(v) for v in values]
    return nums != sorted(nums) and nums != sorted(nums, reverse=True)


_ID_LIKE_HEADER = re.compile(
    r"(^|_)(id|key|ref|reference|code|sku|serial|account|acct|order|invoice|ticket|txn|"
    r"transaction|isbn|ean|upc|gtin|barcode|amount|total|price|qty|count|timestamp|epoch|ts)($|_)")


def looks_like_phone(v: str) -> bool:
    v = v.strip()
    if not _PHONE_CHARS.match(v):
        return False
    digits = re.sub(r"\D", "", v)
    if re.fullmatch(r"69\d{8}", v):    # Greek mobile written as bare digits
        return len(set(digits)) > 2
    if not 9 <= len(digits) <= 15:
        return False
    if len(set(digits)) <= 2:          # 000000000, 1111111111 ...
        return False
    if is_isbn13(digits):              # a valid ISBN is a book, not a phone
        return False
    if _SSN_SHAPE.match(v) or looks_like_ipv4(v):   # SSN-shaped or IP-shaped, not a phone
        return False
    # Bare digit runs are too often IDs; require a phone-ish shape.
    has_sep = bool(re.search(r"[\s().\-/]", v))
    return v.startswith(("+", "00", "0")) or has_sep


_UP = "A-ZÀ-ÖØ-ÞΑ-ΩΆ-Ώ"
_LO = "a-zà-öø-ÿα-ωά-ώ'’"
_CAP = rf"[{_UP}][{_LO}]+"                                   # one capitalised word
# Also O'/D'/Mc/Mac prefixes and double-barrelled names
_NAME_WORD = rf"(?:[OD][’']|Mc|Mac)?{_CAP}(?:-(?:[OD][’']|Mc|Mac)?{_CAP})*"
# Lower-case particles inside a name: "van der", "de la", "bin"
_PARTICLE = r"(?:van|von|der|den|de|del|della|di|da|do|dos|das|du|la|le|bin|binti|ibn|al|el|ten|ter|y|zu)"
_FULL_NAME = re.compile(rf"^{_NAME_WORD}(?:\s+(?:{_PARTICLE}\s+)*{_NAME_WORD}){{1,4}}$")
_CAPS_WORD = rf"[{_UP}]{{2,}}(?:[-'’][{_UP}]+)*"
_FULL_NAME_CAPS = re.compile(rf"^{_CAPS_WORD}(?:\s+{_CAPS_WORD}){{1,3}}$")    # a full name in capitals
_SINGLE_NAME = re.compile(rf"^{_NAME_WORD}$")


# Common given names (accents stripped, lower case). Only used to recognise
# names written without capitals, where the shape alone
# can't tell a person from "blue widget". Extend freely.
COMMON_FIRST_NAMES = frozenset("""
aaron abdul abdullah adam adrian ahmed aisha alan albert alberto alejandro aleksandar alex alexander
alexandra alexandros alexis alfredo ali alice alicia alina alan amanda amelia amir amy ana anastasia
andrea andreas andrew andrei angel angela angeliki angelos anil anita ann anna anne annie anthony
antonio antonis apostolos arjun arthur ashley athanasios ava barbara beatriz ben benjamin bernard
beth betty bianca bob brandon brenda brian bruno camila camille carl carla carlos carmen carol
caroline catherine cecilia charles charlie charlotte chen chloe chris christian christina
christine christos claire clara claudia colin connor daniel daniela danielle david deborah
debra deepak defne dennis despina diana diego dimitra dimitris dimitrios dmitri dominic donald
donna dorothy dylan ed edward eirini elena eleni eli elias elif elijah elisa elizabeth ella
ellen emily emma emmanuel eric erik eva evangelia evangelos evelyn fatima federico felix fernando
filipe fiona francesca francesco francisco frank gabriel gabriela gary george georgia georgios
gerald giorgos giovanni giulia giuseppe grace greg gregory hannah hans harry hassan heather helen
helena henry hiroshi hugo ian ibrahim ilias ingrid ioanna ioannis irene isaac isabel isabella
ivan jack jacob james jamie jan jane janet jason javier jean jennifer jessica jiang jim joan joanna
joao joe john jonathan jordan jorge jose joseph joshua juan julia julian julie justin karen
karim kate katerina katherine kathleen kevin konstantinos kostas kristina kumar kwame laura
lauren lea leo leon leonardo liam lily linda lisa liu lorenzo louis lucas lucia lucy luis luka
lukas maite manuel marco margaret maria marie marina mario mark marta martin mary mateo matteo
matthew max maximilian mehmet melissa mia michael michail michelle miguel mikhail mohamed
mohammed muhammad nadia nancy natalia natasha nathan nicholas nicola nicole nikolaos nikos nina
noah nora oliver olivia omar oscar pablo panagiotis paola patricia patrick paul paula pedro peter
petros philip pierre pieter priya rachel rafael rahul raj ramon raymond rebecca richard robert
roberto rosa ruby ryan sakura samuel sandra sara sarah sean sebastian sergei sergio seán sharon
siobhan sofia sophia sophie spyros stavros stefan stefanos stella stephanie stephen steven susan
takumi tatiana teresa thanasis theo theodoros thomas tim timothy tom tomas valentina vanessa
vasileios vasilis victor victoria vincent virginia wei william xavier yannis yiannis yusuf
zainab zoe μαρια ελενη νικος γιωργος κατερινα δημητρης σοφια κωστας γιαννης
""".split())


def _fold(w: str) -> str:
    w = unicodedata.normalize("NFKD", w.lower())
    return "".join(ch for ch in w if not unicodedata.combining(ch))


_ANY_CASE_WORD = re.compile(r"^[^\W\d_][^\W\d_'’\-]*(?:[-'’][^\W\d_]+)*$")


def looks_like_full_name(v: str) -> bool:
    v = v.strip()
    if _FULL_NAME.match(v) or _FULL_NAME_CAPS.match(v):
        return True
    # Any capitalisation, when it starts with a known first name
    words = v.split()
    return (2 <= len(words) <= 4 and all(_ANY_CASE_WORD.match(w) for w in words)
            and _fold(words[0]) in COMMON_FIRST_NAMES)


def looks_like_single_name(v: str) -> bool:
    return bool(_SINGLE_NAME.match(v.strip()))


_STREET_SUFFIX = (
    r"street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|way|court|ct|"
    r"place|pl|square|sq|terrace|highway|hwy|parkway|pkwy|"
    r"gardens|gdns|mews|close|crescent|cres|grove|walk|rise|quay|wharf|alley|circle|cir|"
    r"plaza|esplanade|embankment|"
    r"strasse|straße|str|weg|gasse|platz|rue|via|calle|odos|"
    r"οδός|οδος|οδ|λεωφ\w*|πλατε[ίι]\w*|πλ|πάροδος|παροδος|πεζόδρομος|πεζοδρομος"
)
_ADDRESS = re.compile(
    rf"(\b\d{{1,5}}[a-z]?\s+(\w+\s+){{0,4}}({_STREET_SUFFIX})\b\.?)"     # number, street name, street word
    rf"|(\b(\w+\s*)?({_STREET_SUFFIX})\.?\s+\w+(\s+\w+)*\s+\d{{1,5}}[a-z]?\b)"  # street word, street name, number
    rf"|(\b\w*({_STREET_SUFFIX})\.?\s+\d{{1,5}}[a-z]?\b)",                # name ending in a street word, number
    re.I,
)


_PO_BOX = re.compile(r"\b(p\.?\s?o\.?\s?box|post\s+office\s+box|postfach|apartado|bo[iî]te\s+postale|"
                     r"τ\.?\s?θ\.?)\s*\d+", re.I)
# Postcodes with a distinctive shape (case-sensitive): UK and Canadian formats
_POSTCODE = re.compile(r"\b([A-PR-UWYZ][A-HK-Y]?\d[A-Z\d]?\s\d[ABD-HJLNP-UW-Z]{2}"
                       r"|[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z]\s\d[ABCEGHJ-NPRSTV-Z]\d)\b")


# Greek postcode (5 digits, with or without a space) followed by a Greek town, or after "Τ.Κ."
_GR_POSTCODE = re.compile(r"\b[1-8]\d{2}\s?\d{2},?\s+[Α-ΩΆ-Ώ][α-ωά-ώϊϋΐΰ]+"
                          r"|\bτ\.?\s?κ\.?:?\s*[1-8]\d{2}\s?\d{2}\b", re.I)
# Greek streets are usually named in the genitive with no street word:
# just the street name and a house number. Matched at the start
# of the cell, and only when the last word has a genitive ending, so
# "Κουτί 12" or "Μάρτιος 2024" don't count.
_GR_STREET_NO_WORD = re.compile(
    r"^((?:[Α-ΩΆ-Ώ][α-ωά-ώϊϋΐΰ]+\.?\s+){0,2}([Α-ΩΆ-Ώ][α-ωά-ώϊϋΐΰ]+))\s+\d{1,3}[α-ω]?(?=$|[\s,])")
_GR_GENITIVE = ("ου", "ους", "ας", "ης", "ων", "ως")


def _greek_street_without_word(v: str) -> bool:
    m = _GR_STREET_NO_WORD.match(v.strip())
    return bool(m) and _fold(m.group(2)).endswith(_GR_GENITIVE)


def looks_like_address(v: str) -> bool:
    return bool(_ADDRESS.search(v) or _PO_BOX.search(v) or _POSTCODE.search(v)
                or _GR_POSTCODE.search(v) or _greek_street_without_word(v))


# IBAN: ISO 13616 length per country + ISO 7064 mod-97 checksum.
_IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22,
    "BH": 22, "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22,
    "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24, "FI": 18, "FO": 18, "FR": 27,
    "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28, "HR": 21, "HU": 28,
    "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20,
    "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MD": 24,
    "ME": 22, "MK": 19, "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15, "PK": 24,
    "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "SA": 24, "SC": 31,
    "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25, "SV": 28, "TL": 23, "TN": 24,
    "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
}
_IBAN_SHAPE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")


def looks_like_iban(v: str) -> bool:
    s = re.sub(r"[\s\-]", "", v).upper()
    if not _IBAN_SHAPE.match(s):
        return False
    expected = _IBAN_LENGTHS.get(s[:2])
    if expected and len(s) != expected:
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)   # A=10 ... Z=35
    return int(digits) % 97 == 1


# Dates of birth: a parsable past date per cell, plus a column-level sanity
# check (ages spread over years, typical adult/child ages) so that order dates,
# invoice dates etc. don't trigger.
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y",
                 "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y")


def parse_date(v: str) -> Optional[date]:
    v = v.strip()
    v = re.split(r"[T ]\d{1,2}:\d{2}", v)[0].strip()        # drop "00:00:00" parts
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def looks_like_past_date(v: str) -> bool:
    d = parse_date(v)
    return d is not None and date(1900, 1, 1) <= d <= date.today()


def dates_look_like_birthdates(values: list[str]) -> bool:
    today = date.today()
    ages = [(today - d).days / 365.25 for d in map(parse_date, values) if d]
    if len(ages) < 3:
        return False
    return 16 <= statistics.median(ages) <= 100 and max(ages) - min(ages) >= 10


# --------------------------------------------------------------------------- #
# Column context: headers that say "this is not that kind of data"
# --------------------------------------------------------------------------- #
_PERSON_NAME_HEADER = (r"^(full|contact|customer|person|client|employee|first|given|fore|"
                       r"middle|last|family|sur|patient|user|member|account_holder|owner)_?name$"
                       r"|^(surname|fname|lname)$")
# Columns that hold names of places, things or organisations, not people.
_NOT_PERSON_HEADER = re.compile(
    r"(^|_)(city|town|village|country|region|state|province|county|location|place|venue|"
    r"site|store|shop|branch|street|product|item|sku|title|book|company|organi[sz]ation|"
    r"org|business|brand|vendor|supplier|manufacturer|category|department|dept|team|"
    r"project|model|service|plan|campaign|event|course|colou?r|shade|paint)(_?name)?($|_)")
def not_a_person_column(col: str, headers: list[str]) -> bool:
    if re.search(_PERSON_NAME_HEADER, col):
        return False                                     # clearly about a person
    # "city", "product_name" ... A bare "name" is NOT skipped, even in a product
    # catalogue: a product list prompting once is better than missing customers.
    return bool(_NOT_PERSON_HEADER.search(col))


_PRODUCT_CODE_HEADER = re.compile(
    r"(^|_)(isbn|ean|gtin|upc|barcode|sku|serial(_?no)?|part_?(no|number)|mpn|asin|"
    r"tracking(_?no)?|order_?(id|no))($|_)")


def not_a_phone_column(col: str, headers: list[str]) -> bool:
    return bool(_PRODUCT_CODE_HEADER.search(col))


# A date column named for something else (founding, orders, hiring ...) is not
# a birth date, however its values are spread.
_OTHER_DATE_HEADER = re.compile(
    r"(^|_)(founded|established|incorporated|created|updated|modified|opened|closed|"
    r"launched|released|published|built|registered|signup|sign_up|order(ed)?|invoice(d)?|"
    r"ship(ped|ping)?|deliver(y|ed)|due|start|end|expir(y|es|ation)|hired?|joined|since|"
    r"renewal|purchase(d)?|payment|paid|last_(seen|login|restocked)|restocked|timestamp)($|_)")


# "email_address", "ip_address", "mac_address", "web_address" ... are not postal.
_NOT_POSTAL_HEADER = re.compile(
    r"(^|_)(e?_?mail|ip|ipv4|ipv6|mac|web|url|website|wallet|btc|eth|crypto|server|host|"
    r"bluetooth|hardware|device)(_|$)")


def not_a_postal_column(col: str, headers: list[str]) -> bool:
    return bool(_NOT_POSTAL_HEADER.search(col))


def not_a_birth_date_column(col: str, headers: list[str]) -> bool:
    return bool(_OTHER_DATE_HEADER.search(col))


# --------------------------------------------------------------------------- #
# Registry: add new detectors here.
# --------------------------------------------------------------------------- #
DETECTORS: list[Detector] = [
    Detector(
        name="Email addresses",
        header=r"(^|_)e?_?mail(_address)?($|_)",
        value=looks_like_email,
        min_hits=2,
        min_ratio=0.1,       # emails buried in a "notes" column still count
    ),
    Detector(
        name="Telephone numbers",
        header=(r"(^|_)(phone|tel|telephone|telefon|telefono|mobile|mobil|handy|cell|fax|msisdn|"
                r"whatsapp|landline|τηλεφωνο|τηλ|κινητο|σταθερο|contact_?(no|num|number))(_?(no|num|number))?($|_)"),
        value=looks_like_phone,
        min_hits=3,
        min_ratio=0.5,
        skip_column=not_a_phone_column,
    ),
    Detector(
        name="First names",
        header=r"^(first|given|fore|middle)_?name$|^(fname|given|forename|vorname|prenom|nombre|ονομα)$",
        value=None,          # single words are too ambiguous without a header
    ),
    Detector(
        name="Surnames",
        header=r"^(last|family|sur)_?name$|^(surname|lname|family|nachname|apellidos?|επωνυμο)$",
        value=None,
    ),
    Detector(
        name="Full names",
        header=r"^(full_?|contact_?|customer_?|person_?|client_?|employee_?)?name$|^ονοματεπωνυμο$",
        value=looks_like_full_name,
        min_hits=5,
        min_ratio=0.8,       # strict: "Blue Widget" also looks like a name
        skip_column=not_a_person_column,
    ),
    Detector(
        name="Postal addresses",
        header=(r"(^|_)(address|addr|street|postal_?code|post_?code|zip(_?code)?|"
                r"διευθυνση|διευθ|οδος|τ_?κ|ταχ(υδρομικος)?_?κωδ(ικας)?)($|_)"),
        value=looks_like_address,
        min_hits=3,
        min_ratio=0.5,
        skip_column=not_a_postal_column,
    ),
    Detector(
        name="Bank account numbers (IBAN)",
        header=r"(^|_)(iban|bank_?account(_no|_number)?|account_iban)($|_)",
        value=looks_like_iban,
        min_hits=1,          # a valid mod-97 checksum is strong evidence
        min_ratio=0.1,
    ),
    Detector(
        name="Dates of birth",
        header=(r"(^|_)(dob|d_o_b|date_of_birth|birth_?date|birth_?day|born(_on)?|"
                r"geburtsdatum|fecha_de_nacimiento|date_de_naissance|"
                r"ημερομηνια_γεννησης|ημ_γεννησης)($|_)"),
        value=looks_like_past_date,
        column_check=dates_look_like_birthdates,
        min_hits=3,
        min_ratio=0.8,
        min_distinct=0.3,
        skip_column=not_a_birth_date_column,
    ),
    Detector(
        name="National ID numbers",
        header=(r"(^|_)(ssn|social_?security(_?(no|num|number))?|nino|ni_?(no|number)|"
                r"national_?(id|insurance)(_?(no|number))?|amka|afm|tin|tax_?(id|no|number)|"
                r"passport(_?(no|num|number))?|id_?card(_?(no|number))?|dni|nie|nif|"
                r"codice_?fiscale|steuer_?id|bsn|personnummer|cpr|pesel|aadhaar|"
                r"αφμ|α_φ_μ|αμκα|α_μ_κ_α|αδτ|α_δ_τ|αρ(ιθμος)?_?ταυτοτητας)($|_)"),
        value=looks_like_national_id,
        min_hits=3,
        min_ratio=0.5,
    ),
    Detector(
        name="IP addresses",
        header=r"(^|_)(ip|ipv4|ipv6|ip_?addr(ess)?|remote_?addr|client_?ip|src_?ip|dst_?ip|source_?ip)($|_)",
        value=looks_like_ip,
        min_hits=3,
        min_ratio=0.5,
    ),
    Detector(
        name="MAC addresses",
        header=r"(^|_)(mac|mac_?addr(ess)?|bssid)($|_)",
        value=looks_like_mac,
        min_hits=3,
        min_ratio=0.5,
    ),
    Detector(
        # Bare 10/11-digit numbers under a header that says nothing ("contact",
        # "col3"). Strict, because ID columns look the same: 90% must be valid
        # North American numbers, nearly all different, and not in sequence.
        name="Telephone numbers",
        header=None,
        value=looks_like_bare_nanp,
        min_hits=5,
        min_ratio=0.9,
        min_distinct=0.9,
        column_check=not_in_sequence,
        skip_column=lambda col, _: bool(_ID_LIKE_HEADER.search(col)),
    ),
    Detector(
        name="Ages / years of birth",
        header=(r"^((customer|patient|client|member|employee|user|person|respondent|child|student|"
                r"applicant|participant)_)?(age|age_years|age_in_years|age_group|age_range|age_band)$"
                r"|^(birth_?year|year_of_birth|yob|jahrgang|geburtsjahr|ηλικια|ετος_γεννησης)$"),
        value=None,
    ),
    Detector(
        name="Payment card numbers",
        header=(r"(^|_)(card_?(no|num|number)|credit_?card(_?(no|num|number))?|debit_?card|"
                r"cc_?(no|num|number)|pan|cvv2?|cvc2?|card_?verification(_?code)?)($|_)"),
        value=looks_like_card,           # issuer prefix + Luhn checksum
        min_hits=2,
        min_ratio=0.5,
    ),
    Detector(
        # GDPR Art. 9 special-category data and criminal records. Header only:
        # the values ("Asthma", "Christian") say nothing on their own.
        name="Sensitive data (health, ethnicity, beliefs...)",
        header=(r"^(race|faith|beliefs?)$|(^|_)(diagnos[ie]s|medical(_\w+)?|health_(status|condition|record|"
                r"data|issues?|notes?)|medications?|prescriptions?|disabilit(y|ies)|allerg(y|ies)|"
                r"blood_?type|hiv(_status)?|pregnan(t|cy)|mental_health|ethnicity|ethnic_origin|"
                r"religion|religious_\w+|sexual_?orientation|sexuality|political_\w+|"
                r"trade_?union(_\w+)?|union_member(ship)?|biometrics?|genetics?|dna|fingerprints?|"
                r"criminal(_\w+)?|convictions?)($|_)"),
        value=None,
    ),
    Detector(
        name="Vehicle identifiers",
        header=(r"(^|_)(licen[cs]e_?plate|number_?plate|plate(_?(no|num|number))?|"
                r"vehicle_?reg(istration)?(_?(no|number))?|car_?reg(istration)?|vin)($|_)"),
        value=None,
    ),
    Detector(
        name="Precise locations (GPS)",
        header=None,
        value=looks_like_precise_coordinate,
        min_hits=3,
        min_ratio=0.8,
        # only columns named as coordinates: plenty of other numbers have 4+ decimals
        skip_column=lambda col, _: not re.search(
            r"(^|_)(lat|latitude|lng|lon|longitude|coords?|coordinates|gps|geo_?location|latlng|latlon)($|_)", col),
    ),
]


# --------------------------------------------------------------------------- #
# Safe labels for the notice. Column names are taken from the first row, but
# in a file without a header row that row is a person (a name, an
# email address). The notice must never repeat personal data, so such labels
# become "column 1", "column 2" ...
# --------------------------------------------------------------------------- #
_STRONG_VALUE_CHECKS = (looks_like_email, looks_like_phone, looks_like_iban, looks_like_card,
                        looks_like_national_id, looks_like_ip, looks_like_mac, looks_like_address,
                        looks_like_precise_coordinate, looks_like_past_date, looks_like_bare_nanp)


def _known_header(cell: str) -> bool:
    key = norm_header(cell)
    return any(d.header_matches(key) for d in DETECTORS)


def looks_like_data(cell: str) -> bool:
    """True if a would-be column name could itself be personal data."""
    cell = cell.strip()
    if not cell or _known_header(cell):
        return False                       # "First Name", "Email", "Home Address" ...
    if any(check(cell) for check in _STRONG_VALUE_CHECKS):
        return True
    if find_email(cell) or len(cell) > 60:
        return True
    # A name that starts with a common first name.
    # Other name-shaped headers ("Order Date") are judged in guard.scan_table
    # against the values of their own column.
    words = cell.split()
    return bool(words) and _fold(words[0]) in COMMON_FIRST_NAMES and (
        looks_like_full_name(cell) or (len(words) == 1 and looks_like_single_name(cell)))


def safe_labels(labels: list[str]) -> list[str]:
    """Labels to show for a table's columns. If any cell of the header row
    holds strong personal data (an email, phone, IBAN ...), the row is a
    record, not a header: every label becomes "column N". Otherwise only the
    cells that look like a name are replaced."""
    strong = any(any(check(c.strip()) for check in _STRONG_VALUE_CHECKS) or find_email(c)
                 for c in labels if c.strip() and not _known_header(c))
    return [f"column {i + 1}" if strong or looks_like_data(c) else c for i, c in enumerate(labels)]


def safe_row_key(key: str, index: int) -> str:
    """How to name a key/value row in the notice: the field name if it is a
    plain one ("email"), else its position."""
    k = key.strip()
    if len(k) <= 30 and re.fullmatch(r"[^\W\d][\w .\-]*", k) and not looks_like_data(k):
        return f"row '{k}'"
    return f"row {index + 1}"


# --------------------------------------------------------------------------- #
# Table detectors: personal data that shows only across columns or rows.
# Each gets (headers, columns) where columns[i] is the list of non-empty
# values of column i, and returns [(detector name, [column labels], reason)].
# --------------------------------------------------------------------------- #
TableFinding = tuple[str, list[str], str]


def split_name_columns(headers: list[str], columns: list[list[str]]) -> list[TableFinding]:
    """Two neighbouring columns of single capitalised words that vary like names
    do: a first name and a surname kept apart ("given","family"), or a file
    with no header row at all."""
    def namey(i: int) -> bool:
        real = [v for v in columns[i] if not is_placeholder(v)]
        if len(real) < 3 or _NOT_PERSON_HEADER.search(norm_header(headers[i])):
            return False
        if sum(map(looks_like_single_name, real)) / len(real) < 0.8:
            return False
        # Names vary; categories ("Hardware", "Sales") repeat.
        need = 0.5 if len(real) <= 50 else 0.2
        return len(set(real)) / len(real) >= need

    ok = [namey(i) for i in range(len(columns))]
    return [("Full names", [headers[i], headers[i + 1]], "split across two columns")
            for i in range(len(columns) - 1) if ok[i] and ok[i + 1]]


def key_value_rows(headers: list[str], columns: list[list[str]], rows: list[list[str]]) -> list[TableFinding]:
    """A record stored vertically ("field,value" rows: a name row, an
    email row ...). A column of field names is read as headers, and each
    row counts only if its value also passes that detector's value check, so
    a data dictionary ("email,string,Customer email") stays clean."""
    out: list[TableFinding] = []
    matching: dict[str, list[Detector]] = {}         # cell text -> detectors whose header it names

    def detectors_named_by(cell: str) -> list[Detector]:
        if cell not in matching:
            key = norm_header(cell) if len(cell) <= 80 else ""
            matching[cell] = [d for d in DETECTORS if key and d.header_matches(key)]
        return matching[cell]

    for ki in range(max((len(r) for r in rows), default=0)):
        confirmed, labels, named = 0, [], 0
        for n, r in enumerate(rows):
            if n == 200 and not named:
                break                    # 200 rows without a single field name: not a key column
            if ki >= len(r):
                continue
            dets = detectors_named_by(r[ki])
            if not dets:
                continue
            named += 1
            others = [c.strip() for j, c in enumerate(r) if j != ki and c.strip() and not is_placeholder(c)]
            for det in dets:
                if not others:
                    continue
                if det.value is not None and any(det.value(o) for o in others):
                    confirmed += 1
                    labels.append((det.name, safe_row_key(r[ki], n)))
                elif det.value is None and any(looks_like_single_name(o) or looks_like_full_name(o) for o in others) \
                        and det.name in ("First names", "Surnames"):
                    labels.append((det.name, safe_row_key(r[ki], n)))
        if confirmed >= 2:
            seen = dict.fromkeys(labels)
            out += [(name, [where], "key/value layout") for name, where in seen]
    return out
