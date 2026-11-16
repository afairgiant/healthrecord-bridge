"""Scrub identity and shift dates in a C-CDA document so it can be shared as a test fixture.

Names, addresses, telecoms and identifiers are replaced; every timestamp, and every
date written in text, is moved by the same number of days; embedded base64 content is
replaced with a placeholder; codes, units, structure and narrative tables are left
alone so the converter still exercises the same paths on the output.

The seed is a secret. Identifier hashes and the day shift are HMACs keyed from it, so
anyone who knows the seed can test guesses of the original values against the output.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from lxml import etree

from .document import parse_xml
from .ids import _OID_RE, _UUID_RE
from .xml import HL7_NS, attr, child, find, findall, local_name, q, xsi_type

PATIENT_GIVEN = "Alex"
PATIENT_FAMILY = "Testperson"
PERSON_GIVEN = "Pat"
PERSON_FAMILY = "Clinician"
ORGANIZATION_NAME = "Example Health System"
ADDRESS = (
    ("streetAddressLine", "123 Example St"),
    ("city", "Springfield"),
    ("state", "KS"),
    ("postalCode", "00000"),
    ("country", "US"),
)
PHONE = "tel:+1-555-0100"
EMAIL = "mailto:patient@example.com"
AUTHORITY_NAME = "Example"
EPIC_CUSTOMER_OID = "1.2.840.114350.1.13.0"
NPI_REPLACEMENT = "0000000000"
MRN_REPLACEMENT = "MRN: 00000"
EMAIL_REPLACEMENT = "[email removed]"
PHONE_REPLACEMENT = "555-0100"
BINARY_PLACEHOLDER = base64.b64encode(b"Content removed by hrb anonymize.").decode("ascii")

SUBSTITUTES: frozenset[str] = frozenset(
    {
        PATIENT_GIVEN,
        PATIENT_FAMILY,
        PERSON_GIVEN,
        PERSON_FAMILY,
        ORGANIZATION_NAME,
        AUTHORITY_NAME,
        *(v for _, v in ADDRESS),
        PHONE,
        EMAIL,
        PHONE.split(":", 1)[1],
        EMAIL.split(":", 1)[1],
        NPI_REPLACEMENT,
        MRN_REPLACEMENT,
        EMAIL_REPLACEMENT,
        PHONE_REPLACEMENT,
        BINARY_PLACEHOLDER,
    }
)
"""Every string the scrubber writes; residual checks never report these."""

TS_ATTRIBUTES = frozenset({"value", "low", "high", "center", "time", "effectiveTime"})
NAME_PARTS = ("prefix", "given", "family", "suffix")

_TS_RE = re.compile(r"^(?P<date>\d{8})(?P<rest>\d*(?:\.\d+)?(?:[+-]\d{4})?)$")
_PARTIAL_TS_RE = re.compile(r"^(?P<year>\d{4})(?P<month>\d{2})?(?P<tz>[+-]\d{4})?$")
# Elements whose ``value`` is a timestamp even without an ``xsi:type`` saying so.
_TIME_ELEMENTS = frozenset({"effectiveTime", "time", "birthTime", "deceasedTime"})
_BOUNDS = frozenset({"low", "high", "center"})
_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)  # fmt: skip
_MONTHS = tuple(name[:3] for name in _MONTH_NAMES)
_MONTH_TITLE = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
    r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_MONTH_ANY = f"(?i:{_MONTH_TITLE})"
# Without a day, only a capitalised month counts: "may 1500 mg" is not a date.
_MONTH_CAPS = f"(?:{_MONTH_TITLE}|{_MONTH_TITLE.upper()})"
_YEAR = r"(?:18|19|20|21)\d\d"
_NOT_BEFORE = r"(?<![\d/.-])"
_NOT_AFTER = r"(?!\d|[-/.]\d)"
_ORDINAL = r"(?i:st|nd|rd|th)"
_UNIT_AFTER = r"(?!\s*(?:[µu]?g\b|mg|mcg|kg|m[lL]|[lL]\b|units?\b|IU\b|cc\b|%|mm|cm|mmol|mEq|/))"
# One alternation, so each date is matched, and shifted, exactly once.
_TEXT_DATE_RE = re.compile(
    # 2020-05-14, 2020/05/14, 2020.05.14 (an ISO time may follow)
    rf"{_NOT_BEFORE}(?P<ymd_y>{_YEAR})(?P<ymd_s>[-/.])(?P<ymd_m>\d{{1,2}})(?P=ymd_s)"
    rf"(?P<ymd_d>\d{{1,2}}){_NOT_AFTER}"
    # 05/14/2020, 5-14-20, 05.14.2020 (not 1.5.20, which is a version)
    rf"|{_NOT_BEFORE}(?P<mdy_m>\d{{1,2}})(?P<mdy_s>[-/.])(?P<mdy_d>\d{{1,2}})(?P=mdy_s)"
    rf"(?P<mdy_y>\d{{4}}|(?<!\.)\d{{2}}){_NOT_AFTER}"
    # 2019-05, 2019/05
    rf"|{_NOT_BEFORE}(?P<ym_y>{_YEAR})(?P<ym_s>[-/])(?P<ym_m>0[1-9]|1[0-2]){_NOT_AFTER}"
    # 05/2019
    rf"|{_NOT_BEFORE}(?P<my_m>0?[1-9]|1[0-2])/(?P<my_y>{_YEAR}){_NOT_AFTER}"
    # May 14, 2020 / May 14th 2020 / May-14-2020
    rf"|\b(?P<nd_m>{_MONTH_ANY})(?P<nd_dot>\.?)(?P<nd_s1>\s|-)(?P<nd_d>\d{{1,2}})"
    rf"(?P<nd_o>{_ORDINAL})?(?P<nd_s2>,?\s|-)(?P<nd_y>{_YEAR})\b"
    # 14 March 2023 / 14-May-2020 / 14th May, 2020
    rf"|\b(?P<dn_d>\d{{1,2}})(?P<dn_o>{_ORDINAL})?(?P<dn_s1>\s|-)(?P<dn_m>{_MONTH_ANY})"
    rf"(?P<dn_dot>\.?)(?P<dn_s2>,?\s|-)(?P<dn_y>{_YEAR})\b"
    # March 2023 / MAR 2023
    rf"|\b(?P<mn_m>{_MONTH_CAPS})(?P<mn_dot>\.?)(?P<mn_s>,?\s)(?P<mn_y>{_YEAR})\b"
    # in 2019 / since 2015 / diagnosed 2019 (a bare year only after such a word)
    r"|(?P<yr_pre>\b(?i:in|since|from|until|till|through|during|before|after|circa|around"
    r"|year|diagnosed|dx|onset|started|stopped|quit|born|died)\s+)"
    rf"(?P<yr_y>(?:19|20)\d\d)\b{_NOT_AFTER}{_UNIT_AFTER}"
)
_BIRTH_LABEL_RE = re.compile(
    r"(?:\bDOB|\bD\.O\.B\.?|\bdate of birth|\bbirth\s?date|\bborn(?:\s+on)?)\s*[:#-]?\s*$",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"(?<![\d.])\(?\d{3}\)?[-. ]?\d{3}[-. ]\d{4}(?!\.?\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_MRN_RE = re.compile(r"\bMRN\s*[:#]?\s*[A-Za-z]{0,3}\d+", re.IGNORECASE)
_NPI_RE = re.compile(r"\b(NPI\s*[:#]?\s*)\d{10}\b", re.IGNORECASE)
_LONG_DIGITS_RE = re.compile(r"(?<![\d.])\d{9,}")
_DIGIT_RUN_RE = re.compile(r"(?<!\d)\d{9,}(?!\d)")
_EPIC_CUSTOMER_RE = re.compile(r"1\.2\.840\.114350\.1\.13\.\d+")
_KEPT_AUTHORITIES = frozenset({"EPIC", "EPC"})
_CODE_LABEL_RE = re.compile(
    r"(?:SNOMED(?:[- ]?CT)?|LOINC|RXNORM|CVX|CPT|NDC|HCPCS|ICD-?(?:9|10)(?:-CM)?)\s*[:#]?\s*$",
    re.IGNORECASE,
)

# Elements whose <name> child is a person, an organisation, or must be left alone.
_PERSON_PARENTS = frozenset(
    {
        "patient",
        "assignedPerson",
        "guardianPerson",
        "associatedPerson",
        "relatedPerson",
        "informationRecipient",
        "subject",
    }
)
_ORGANIZATION_PARENTS = frozenset(
    {
        "representedOrganization",
        "representedCustodianOrganization",
        "providerOrganization",
        "guardianOrganization",
        "scopingOrganization",
        "serviceProviderOrganization",
        "receivedOrganization",
        "wholeOrganization",
        "location",
        "playingEntity",
    }
)
_KEEP_NAME_PARENTS = frozenset({"manufacturedMaterial", "labeledDrug", "manufacturerOrganization"})


@dataclass(slots=True)
class AnonymizeReport:
    """What the scrubber changed and what a reviewer should still look at."""

    day_shift: int
    """Days every date moved by. Never written by ``to_dict``: it would undo the shift."""
    ids_rewritten: int = 0
    oids_rewritten: int = 0
    """Epic customer OIDs whose customer number became 0."""
    names_rewritten: int = 0
    telecoms_rewritten: int = 0
    addresses_rewritten: int = 0
    narrative_replacements: int = 0
    dates_in_text_shifted: int = 0
    binary_payloads_removed: int = 0
    """Base64 ``ED`` payloads (scans, images, attachments) replaced with a placeholder."""
    non_xml_body: str | None = None
    """``None`` for a structured body; for a ``nonXMLBody``, ``"file"`` when it was an
    embedded file and was replaced (the output has no clinical content) or ``"text"`` when
    it was plain text and was scrubbed like narrative."""
    residual_findings: list[str] = field(default_factory=list)
    """Text that still looks like an identifier, phone number or email after scrubbing."""

    def to_dict(self) -> dict:
        """Everything except ``day_shift``, so the report can be shared with the output."""
        d = asdict(self)
        del d["day_shift"]
        return d

    def summary(self) -> str:
        """One line for a terminal."""
        return (
            f"ids {self.ids_rewritten}, "
            f"oids {self.oids_rewritten}, "
            f"names {self.names_rewritten}, telecoms {self.telecoms_rewritten}, "
            f"addresses {self.addresses_rewritten}, "
            f"text replacements {self.narrative_replacements}, "
            f"text dates {self.dates_in_text_shifted}, "
            f"binary payloads {self.binary_payloads_removed}, "
            f"residual findings {len(self.residual_findings)}"
        )


def seeded_key(seed: str, purpose: str) -> bytes:
    """A key for one purpose, derived from the seed, so no two purposes share hashes."""
    return hmac.new(seed.encode("utf-8"), purpose.encode("utf-8"), hashlib.sha256).digest()


def keyed_hash(key: bytes, value: str) -> bytes:
    """HMAC-SHA256 of ``value``: without the key, a guess cannot be checked against it."""
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def day_shift_for(seed: str) -> int:
    """Deterministic offset of 30 to 730 days, either sign, derived from the seed."""
    digest = seeded_key(seed, "anonymize/day-shift")
    days = 30 + int.from_bytes(digest[:4], "big") % 701
    return -days if digest[4] & 1 else days


def shift_ts(raw: str, days: int, *, partial: bool = False) -> str | None:
    """Shift the date part of an HL7 TS, keeping its precision; ``None`` if not a TS.

    With ``partial``, year-month and year-only values are shifted too: a year-month
    value moves from the 15th of its month, so any shift of 30 days or more changes the
    month; a year-only value moves by the shift in whole years, at least one, so the
    real year never survives.
    """
    m = _TS_RE.match(raw)
    if m:
        d = m.group("date")
        try:
            shifted = date(int(d[:4]), int(d[4:6]), int(d[6:8])) + timedelta(days=days)
        except (ValueError, OverflowError):
            return None
        return f"{shifted.year:04d}{shifted.month:02d}{shifted.day:02d}{m.group('rest')}"
    m = _PARTIAL_TS_RE.match(raw) if partial and days else None
    if not m:
        return None
    year, month, tz = int(m.group("year")), m.group("month"), m.group("tz") or ""
    try:
        if month is None:
            return f"{_shift_year(year, days):04d}{tz}"
        shifted = _shift_month(year, int(month), days)
    except (ValueError, OverflowError):
        return None
    return f"{shifted.year:04d}{shifted.month:02d}{tz}"


def _shift_month(year: int, month: int, days: int) -> date:
    """A year-month value moves from the 15th, so a shift of 30 days or more changes it."""
    return date(year, month, 15) + timedelta(days=days)


def _shift_year(year: int, days: int) -> int:
    """A year-only value moves by the shift in whole years, at least one."""
    years = round(days / 365) or (1 if days > 0 else -1)
    return date(year + years, 1, 1).year


def _shift_text_date(m: re.Match[str], days: int, *, birth: bool = False) -> str | None:
    """One ``_TEXT_DATE_RE`` match shifted, in its own format; ``None`` if not a real date.

    A day moves by ``days``; a month moves from its 15th; a bare year by whole years. With
    ``birth`` the result is rounded to January 1 (or January), as ``birthTime`` is.
    """
    kind = _date_kind(m)
    g = {k.split("_", 1)[1]: v for k, v in m.groupdict().items() if k.startswith(kind + "_")}

    def moved(year: int, month: int, day: str | None) -> date:
        d = (
            date(year, month, int(day)) + timedelta(days=days)
            if day
            else _shift_month(year, month, days)
        )
        return d.replace(month=1, day=1) if birth else d

    def month_text(d: date) -> str:
        return _month_like(g["m"], g["dot"], d.month) + g["dot"]

    try:
        if kind == "yr":
            return f"{g['pre']}{_shift_year(int(g['y']), days):04d}"
        if kind in {"nd", "dn", "mn"}:
            d = moved(int(g["y"]), _month_number(g["m"]), g.get("d"))
            if kind == "mn":
                return f"{month_text(d)}{g['s']}{d.year:04d}"
            day = f"{d.day}{_ordinal_like(g['o'], d.day)}"
            first, second = (month_text(d), day) if kind == "nd" else (day, month_text(d))
            return f"{first}{g['s1']}{second}{g['s2']}{d.year:04d}"
        y = g["y"]
        year = int(y) if len(y) == 4 else (2000 if int(y) < 70 else 1900) + int(y)
        d = moved(year, int(g["m"]), g.get("d"))
        mm = _pad(d.month, g["m"])
        yy = f"{d.year:04d}" if len(y) == 4 else f"{d.year % 100:02d}"
        if kind == "ymd":
            return f"{yy}{g['s']}{mm}{g['s']}{_pad(d.day, g['d'])}"
        if kind == "mdy":
            return f"{mm}{g['s']}{_pad(d.day, g['d'])}{g['s']}{yy}"
        if kind == "ym":
            return f"{yy}{g['s']}{d.month:02d}"
        return f"{mm}/{yy}"
    except (ValueError, OverflowError):
        return None


def _date_kind(m: re.Match[str]) -> str:
    """Which alternative of ``_TEXT_DATE_RE`` matched: ``ymd``, ``mdy``, ... ``yr``."""
    return (m.lastgroup or "").split("_", 1)[0]


def _looks_like_misread_date(m: re.Match[str]) -> bool:
    """An invalid match worth reporting: a named month, or numbers valid read day-first.

    ``14/03/2023`` is reported (a day-first date the scrubber cannot shift safely);
    ``20/20/20`` (a ratio or score) is not.
    """
    kind = _date_kind(m)
    if kind in {"nd", "dn"}:
        return True
    return kind == "mdy" and 1 <= int(m["mdy_d"]) <= 12 and 1 <= int(m["mdy_m"]) <= 31


def _pad(number: int, like: str) -> str:
    return f"{number:02d}" if len(like) == 2 else str(number)


def _month_number(name: str) -> int:
    return _MONTHS.index(name[:3].title()) + 1


def _month_like(original: str, dot: str, month: int) -> str:
    """Month ``month`` spelled as ``original`` was: full or abbreviated, same case."""
    # "May" is both; written "MAY" it is the DD-MON-YYYY abbreviation.
    full = not dot and original.title() in _MONTH_NAMES and original != "MAY"
    new = _MONTH_NAMES[month - 1] if full else _MONTHS[month - 1]
    if original.isupper():
        return new.upper()
    if original.islower():
        return new.lower()
    return new


def _ordinal_like(original: str | None, day: int) -> str:
    if not original:
        return ""
    suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return suffix.upper() if original.isupper() else suffix


def anonymize(xml_bytes: bytes, *, seed: str | None = None) -> tuple[bytes, AnonymizeReport]:
    """Return the scrubbed document and a report. The same seed gives the same output."""
    root = parse_xml(xml_bytes)
    scrubber = _Scrubber(seed if seed is not None else secrets.token_hex(16))
    scrubber.run(root)
    tree = root.getroottree()
    info = tree.docinfo
    out = etree.tostring(
        tree,
        xml_declaration=True,
        encoding=info.encoding or "UTF-8",
    )
    return out, scrubber.report


def anonymize_file(src: Path, dst: Path, seed: str | None = None) -> AnonymizeReport:
    """Anonymize one XML file into another."""
    out, report = anonymize(Path(src).read_bytes(), seed=seed)
    Path(dst).write_bytes(out)
    return report


class _Scrubber:
    def __init__(self, seed: str) -> None:
        self.id_key = seeded_key(seed, "anonymize/id")
        self.uuid_key = seeded_key(seed, "anonymize/uuid")
        self.report = AnonymizeReport(day_shift=day_shift_for(seed))
        self.tokens: dict[str, str] = {}
        self.files: set[etree._Element] = set()
        """Elements whose content is a file placeholder; text passes leave them alone."""

    def run(self, root: etree._Element) -> None:
        self._remove_binary(root)
        self._collect_tokens(root)
        self._shift_dates(root)
        self._rewrite_ids(root)
        self._rewrite_oids(root)
        self._rewrite_authorities(root)
        self._rewrite_names(root)
        self._rewrite_addresses(root)
        self._rewrite_telecoms(root)
        self._rewrite_narrative(root)
        self._find_residuals(root)

    # ---- hashing ------------------------------------------------------------------

    def _hash(self, original: str) -> str:
        return keyed_hash(self.id_key, original).hex()[:16]

    def _hash_uuid(self, original: str) -> str:
        return str(uuid.UUID(bytes=keyed_hash(self.uuid_key, original)[:16], version=4))

    def _hash_digit_runs(self, original: str) -> str:
        """Replace every run of 9 or more digits with 8 seeded digits."""
        return _DIGIT_RUN_RE.sub(
            lambda m: f"{int(self._hash(m.group(0)), 16) % 10**8:08d}", original
        )

    # ---- embedded binary content ------------------------------------------------------

    def _remove_binary(self, root: etree._Element) -> None:
        """Replace embedded files (scans, images, attachments); nothing in them can be scrubbed.

        An element holds a file when it says ``representation="B64"`` or has a
        ``mediaType`` that is not ``text/*`` (some vendors leave ``representation`` out).
        Its whole content goes, including base64 written after a ``reference`` child;
        thumbnails are dropped, and a ``reference`` stays without its tail.
        """
        body = find(root, "c:component/c:nonXMLBody")
        body_text = child(body, "text")
        dropped: set[etree._Element] = set()
        for el in root.xpath("//*[@representation or @mediaType]"):
            if el in dropped or not _holds_file(el):
                continue
            for c in list(el):
                if isinstance(c.tag, str) and local_name(c) == "reference":
                    c.tail = None
                else:
                    dropped.update(c.iter())
                    el.remove(c)
            el.text = BINARY_PLACEHOLDER
            for name in ("compression", "integrityCheck"):
                el.attrib.pop(name, None)
            self.files.add(el)
            self.report.binary_payloads_removed += 1
        if body is not None:
            self.report.non_xml_body = "file" if body_text in self.files else "text"

    # ---- tokens seen in the structured part ---------------------------------------

    def _collect_tokens(self, root: etree._Element) -> None:
        patient_role = root.find("c:recordTarget/c:patientRole", {"c": HL7_NS})
        if patient_role is not None:
            for name_el in findall(patient_role, "c:patient/c:name"):
                self._collect_person(name_el, PATIENT_GIVEN, PATIENT_FAMILY)
            for id_el in patient_role.findall(q("id")):
                ext = attr(id_el, "extension")
                if ext and len(ext) >= 4:
                    self.tokens.setdefault(ext.lower(), self._hash(ext))
        for name_el in root.iter(q("name")):
            kind = _name_kind(name_el)
            if kind == "person":
                self._collect_person(name_el, PERSON_GIVEN, PERSON_FAMILY)
            elif kind == "organization":
                text = _flat_text(name_el)
                if text:
                    self.tokens.setdefault(text.lower(), ORGANIZATION_NAME)

    def _collect_person(self, name_el: etree._Element, given: str, family: str) -> None:
        parts = [(local_name(c), (c.text or "").strip()) for c in name_el if isinstance(c.tag, str)]
        parts = [(k, v) for k, v in parts if k in NAME_PARTS and v]
        if not parts:
            words = (name_el.text or "").split()
            parts = [("given", w) for w in words[:-1]] + [("family", w) for w in words[-1:]]
        for kind, value in parts:
            replacement = {"given": given, "family": family}.get(kind, "")
            if len(value) >= 2:
                self.tokens.setdefault(value.lower(), replacement)

    # ---- dates ----------------------------------------------------------------------

    def _shift_dates(self, root: etree._Element) -> None:
        days = self.report.day_shift
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            birth = local_name(el) == "birthTime"
            for name, value in el.attrib.items():
                local = etree.QName(name).localname
                if local not in TS_ATTRIBUTES:
                    continue
                raw = value.strip()
                if _TS_RE.match(raw):
                    # A full date anywhere, unless the element is plainly a number.
                    if local == "value" and _value_kind(el) == "number":
                        continue
                    shifted = shift_ts(raw, days)
                elif local == "value" and _PARTIAL_TS_RE.match(raw) and _value_kind(el) == "ts":
                    shifted = shift_ts(raw, days, partial=True)
                else:
                    continue
                if shifted is None:
                    continue
                if birth:
                    shifted = _round_birth(shifted)
                el.set(name, shifted)

    # ---- identifiers ------------------------------------------------------------------

    def _rewrite_ids(self, root: etree._Element) -> None:
        for el in root.iter(q("id"), q("setId")):
            changed = False
            ext = attr(el, "extension")
            if ext:
                el.set("extension", self._hash(ext))
                changed = True
            root_attr = attr(el, "root")
            if root_attr and _UUID_RE.match(root_attr):
                el.set("root", self._hash_uuid(root_attr))
                changed = True
            elif root_attr and not _OID_RE.match(root_attr):
                el.set("root", self._hash(root_attr))
                changed = True
            elif root_attr and _DIGIT_RUN_RE.search(root_attr):
                el.set("root", self._hash_digit_runs(root_attr))
                changed = True
            if changed:
                self.report.ids_rewritten += 1
        self._rewrite_narrative_ids(root)

    def _rewrite_narrative_ids(self, root: etree._Element) -> None:
        """Hash long digit runs in narrative IDs, keeping the references that point at them."""
        renamed: dict[str, str] = {}
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            old = el.get("ID")
            if old and _DIGIT_RUN_RE.search(old):
                new = self._hash_digit_runs(old)
                el.set("ID", new)
                renamed[old] = new
                self.report.ids_rewritten += 1
        if not renamed:
            return
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            for name, value in el.attrib.items():
                if etree.QName(name).localname not in {"value", "href"}:
                    continue
                hash_mark, target = ("#", value[1:]) if value.startswith("#") else ("", value)
                if target in renamed:
                    el.set(name, hash_mark + renamed[target])

    def _rewrite_oids(self, root: etree._Element) -> None:
        """Replace the customer number in Epic OIDs, in attributes and text alike."""

        def sub(value: str) -> str:
            new, k = _EPIC_CUSTOMER_RE.subn(EPIC_CUSTOMER_OID, value)
            self.report.oids_rewritten += k
            return new

        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            for name, value in el.attrib.items():
                if "114350" in value:
                    el.set(name, sub(value))
            if el.text and "114350" in el.text:
                el.text = sub(el.text)
            if el.tail and "114350" in el.tail:
                el.tail = sub(el.tail)

    def _rewrite_authorities(self, root: etree._Element) -> None:
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            value = el.get("assigningAuthorityName")
            if value and value.strip().upper() not in _KEPT_AUTHORITIES:
                el.set("assigningAuthorityName", AUTHORITY_NAME)

    # ---- names, addresses, telecoms ---------------------------------------------------

    def _rewrite_names(self, root: etree._Element) -> None:
        for name_el in list(root.iter(q("name"))):
            kind = _name_kind(name_el)
            if kind is None or _is_empty(name_el):
                continue
            if kind == "organization":
                if _flat_text(name_el) == ORGANIZATION_NAME:
                    continue
                _clear(name_el)
                name_el.text = ORGANIZATION_NAME
            else:
                given, family = (
                    (PATIENT_GIVEN, PATIENT_FAMILY)
                    if kind == "patient"
                    else (PERSON_GIVEN, PERSON_FAMILY)
                )
                _clear(name_el)
                etree.SubElement(name_el, q("given")).text = given
                etree.SubElement(name_el, q("family")).text = family
            self.report.names_rewritten += 1

    def _rewrite_addresses(self, root: etree._Element) -> None:
        for addr_el in root.iter(q("addr")):
            if _is_empty(addr_el):
                continue
            _clear(addr_el)
            for local, value in ADDRESS:
                etree.SubElement(addr_el, q(local)).text = value
            self.report.addresses_rewritten += 1

    def _rewrite_telecoms(self, root: etree._Element) -> None:
        for tel in root.iter(q("telecom")):
            value = attr(tel, "value")
            if not value:
                continue
            scheme = value.split(":", 1)[0].lower() if ":" in value else ""
            replacement = EMAIL if scheme == "mailto" or "@" in value else PHONE
            if value != replacement:
                tel.set("value", replacement)
                self.report.telecoms_rewritten += 1

    # ---- narrative ------------------------------------------------------------------------

    def _rewrite_narrative(self, root: etree._Element) -> None:
        tokens = {k: v for k, v in self.tokens.items() if k != v.lower()}
        token_re = None
        if tokens:
            alternatives = "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True))
            token_re = re.compile(f"(?<![A-Za-z0-9])(?:{alternatives})(?![A-Za-z0-9])", re.I)

        def scrub(text: str, el: etree._Element) -> str:
            n = 0
            if token_re is not None:
                text, k = token_re.subn(lambda m: tokens[m.group(0).lower()], text)
                n += k
                if k:
                    text = re.sub(r" {2,}", " ", re.sub(r" ,", ",", text))
            text, k = _EMAIL_RE.subn(EMAIL_REPLACEMENT, text)
            n += k
            text, k = _PHONE_RE.subn(PHONE_REPLACEMENT, text)
            n += k
            text, k = _MRN_RE.subn(MRN_REPLACEMENT, text)
            n += k
            text, k = _NPI_RE.subn(rf"\g<1>{NPI_REPLACEMENT}", text)
            n += k
            self.report.narrative_replacements += n
            return _TEXT_DATE_RE.sub(lambda m: shift_date(m, el), text)

        def shift_date(m: re.Match[str], el: etree._Element) -> str:
            birth = bool(_BIRTH_LABEL_RE.search(m.string, max(0, m.start() - 30), m.start()))
            shifted = _shift_text_date(m, self.report.day_shift, birth=birth)
            if shifted is not None:
                self.report.dates_in_text_shifted += 1
                return shifted
            if _looks_like_misread_date(m):
                self.report.residual_findings.append(
                    f"unshifted date {m.group(0)!r} in {_where(el)}: {_snippet(m.string)}"
                )
            return m.group(0)

        # Every text node, tail and display name, not only narrative: ST values, desc and
        # display names ("Office Visit 05/14/2020") carry free text too.
        for el in root.iter(etree.Element):
            if el not in self.files and _has_text(el.text):
                el.text = scrub(el.text, el)
            if _has_text(el.tail):
                el.tail = scrub(el.tail, el)
            display = el.get("displayName")
            if _has_text(display):
                el.set("displayName", scrub(display, el))

    def _find_residuals(self, root: etree._Element) -> None:
        codes = {v for el in root.iter(etree.Element) if (v := el.get("code"))}
        # Digit runs inside a hash the scrubber wrote are its own output.
        written = "\0".join(v for v in self.tokens.values() if v not in SUBSTITUTES)
        checks = (("digit run", _LONG_DIGITS_RE), ("email", _EMAIL_RE), ("phone", _PHONE_RE))
        for el in root.iter(etree.Element):
            texts = (None if el in self.files else el.text, el.tail, el.get("displayName"))
            for text in texts:
                if not _has_text(text):
                    continue
                for label, pattern in checks:
                    for m in pattern.finditer(text):
                        found = m.group(0)
                        if found in SUBSTITUTES:
                            continue
                        if label == "digit run" and (
                            found in codes
                            or found in written
                            or _CODE_LABEL_RE.search(text, max(0, m.start() - 40), m.start())
                        ):
                            continue
                        self.report.residual_findings.append(
                            f"{label} {found!r} in {_where(el)}: {_snippet(text)}"
                        )


def _name_kind(name_el: etree._Element) -> str | None:
    """``patient``, ``person``, ``organization`` or ``None`` for names to leave alone."""
    parent = name_el.getparent()
    if parent is None or not isinstance(parent.tag, str):
        return None
    local = local_name(parent)
    if local in _KEEP_NAME_PARENTS:
        return None
    if local == "playingEntity":
        class_code = attr(parent, "classCode") or ""
        if class_code == "MMAT":
            return None
        if class_code == "PSN":
            return "person"
    if local == "patient":
        return "patient"
    if local in _PERSON_PARENTS or any(
        isinstance(c.tag, str) and local_name(c) in NAME_PARTS for c in name_el
    ):
        return "person"
    if local in _ORGANIZATION_PARENTS:
        return "organization"
    return None


def _is_empty(el: etree._Element) -> bool:
    return len(el) == 0 and not _flat_text(el)


def _clear(el: etree._Element) -> None:
    for c in list(el):
        el.remove(c)
    el.text = None


def _flat_text(el: etree._Element) -> str:
    return " ".join(t.strip() for t in el.itertext() if t.strip())


def _value_kind(el: etree._Element) -> str:
    """``"ts"``, ``"number"`` or ``"unknown"`` for what ``el``'s ``value`` attribute holds.

    Full dates are shifted unless the element is plainly a number (it has a ``unit`` or a
    non-timestamp ``xsi:type``); partial dates (``2019``, ``201905``) only when it is
    plainly a timestamp, so a PQ of ``1500`` or an ``IVL_INT`` bound is never a year.
    """
    if el.get("unit") is not None:
        return "number"
    typed = el
    if local_name(el) in _BOUNDS:
        parent = el.getparent()
        if parent is None or not isinstance(parent.tag, str):
            return "unknown"
        typed = parent
    kind = xsi_type(typed)
    if kind:
        return "ts" if "TS" in kind else "number"
    return "ts" if local_name(typed) in _TIME_ELEMENTS else "unknown"


def _has_text(text: str | None) -> bool:
    """Whether ``text`` has anything besides indentation worth scanning."""
    return bool(text) and not text.isspace()


def _holds_file(el: etree._Element) -> bool:
    """Whether ``el`` is an ED carrying an embedded file rather than text."""
    if (attr(el, "representation") or "").upper() == "B64":
        return True
    media = (attr(el, "mediaType") or "").lower()
    if not media or media.startswith("text/"):
        return False
    return bool((el.text or "").strip()) or any(
        (c.tail or "").strip() for c in el if isinstance(c.tag, str)
    )


def _round_birth(ts: str) -> str:
    """Round a shifted birth time to January 1 (or January) of its year."""
    if len(ts) >= 8 and ts[:8].isdigit():
        return ts[:4] + "0101" + ts[8:]
    if len(ts) >= 6 and ts[:6].isdigit():
        return ts[:4] + "01" + ts[6:]
    return ts


def _where(el: etree._Element) -> str:
    section = next(el.iterancestors(q("section")), None)
    if section is None:
        return "header"
    title = section.find(q("title"))
    if title is not None and title.text and title.text.strip():
        return title.text.strip()
    return f"section {attr(section.find(q('code')), 'code') or '?'}"


def _snippet(text: str, width: int = 80) -> str:
    s = " ".join(text.split())
    return s if len(s) <= width else s[: width - 3] + "..."
