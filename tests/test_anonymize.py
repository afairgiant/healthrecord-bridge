"""The anonymizer removes identity and shifts dates without changing what the converter sees."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import date
from pathlib import Path

import pytest
from lxml import etree

from conftest import (
    HANDWRITTEN,
    NS,
    PDF_B64,
    XDM_SOURCE_XML,
    non_xml_body_document,
    run_cli,
)
from healthrecord_bridge.ccda import convert_bytes
from healthrecord_bridge.ccda.anonymize import (
    BINARY_PLACEHOLDER,
    anonymize,
    anonymize_file,
    day_shift_for,
    shift_ts,
)
from healthrecord_bridge.cli import main

FIXTURES = [HANDWRITTEN, XDM_SOURCE_XML]
EPIC_CUSTOMER = b"1.2.840.114350.1.13.999"
PERFORMER = b"""
  <documentationOf>
    <serviceEvent classCode="PCPR">
      <performer typeCode="PRF">
        <assignedEntity>
          <id root="2.16.840.1.113883.4.6" extension="1234567893" assigningAuthorityName="NPI"/>
          <id root="1.2.840.114350.1.13.999.2.7.8.688883.3677179739" assigningAuthorityName="EPIC"/>
          <id root="1.2.840.114350.1.13.999.2.7.8.688883.2" assigningAuthorityName="EPC"/>
          <code code="207R00000X" codeSystem="2.16.840.1.113883.6.101">
            <originalText>Internal Medicine</originalText>
            <translation code="32" codeSystem="1.2.840.114350.1.72.1.7.7.10.688867.4160"/>
            <translation code="17" codeSystem="1.2.840.114350.1.13.999.2.7.10.836982.1050"/>
          </code>
          <addr nullFlavor="UNK"><city>Boston</city></addr>
          <telecom nullFlavor="UNK" value="tel:+1-617-555-0199"/>
          <assignedPerson><name nullFlavor="UNK">Rosa Attending</name></assignedPerson>
          <representedOrganization classCode="ORG">
            <name nullFlavor="UNK">Big City Brigham</name>
            <addr nullFlavor="UNK"/>
          </representedOrganization>
        </assignedEntity>
      </performer>
    </serviceEvent>
  </documentationOf>
  <documentationOf>"""


def with_performer(extra: bytes = b"") -> bytes:
    data = HANDWRITTEN.read_bytes().replace(b"  <documentationOf>", PERFORMER, 1)
    return data.replace(b'<item ID="enc2">Hospital admission</item>', extra) if extra else data


def performer_entity(root: etree._Element) -> etree._Element:
    return root.find(".//c:performer[@typeCode='PRF']/c:assignedEntity", NS)


PATIENT_TOKENS = {
    HANDWRITTEN: ["Jane", "Quinn", "Sampleton", "jane.sampleton", "913-555", "Overland Park"],
    XDM_SOURCE_XML: ["Letha284", "Haag279", "Robel Vale", "Bedford"],
}


def ts_values(root: etree._Element) -> list[tuple[str, str]]:
    """(local tag, value) for every ``value`` attribute that looks like a TS."""
    out = []
    for el in root.iter():
        v = el.get("value")
        if v and re.match(r"^\d{8}", v):
            out.append((etree.QName(el).localname, v))
    return out


@pytest.fixture(params=FIXTURES, ids=lambda p: p.stem[:24])
def pair(request) -> tuple[Path, bytes, bytes]:
    src = request.param
    original = src.read_bytes()
    out, _ = anonymize(original, seed="fixture-seed")
    return src, original, out


def test_day_shift_range_and_sign():
    seen = {day_shift_for(str(i)) for i in range(200)}
    assert all(30 <= abs(d) <= 730 for d in seen)
    assert any(d < 0 for d in seen) and any(d > 0 for d in seen)
    assert day_shift_for("x") == day_shift_for("x")


def test_shift_ts_keeps_precision():
    assert shift_ts("20240315", 10) == "20240325"
    assert shift_ts("20240315143000-0500", -15) == "20240229143000-0500"
    assert shift_ts("20240315143000.123+0000", 1) == "20240316143000.123+0000"
    assert shift_ts("2024", 1) is None
    assert shift_ts("120", 1) is None
    assert shift_ts("20241399", 1) is None


def test_dates_shift_consistently(pair):
    _, original, out = pair
    shift = day_shift_for("fixture-seed")
    before = ts_values(etree.fromstring(original))
    after = ts_values(etree.fromstring(out))
    assert len(before) == len(after)
    for (tag_b, b), (tag_a, a) in zip(before, after, strict=True):
        assert tag_a == tag_b
        assert len(a) == len(b)
        assert a[8:] == b[8:]
        assert a != b
        if tag_b != "birthTime":
            assert _days(a[:8]) - _days(b[:8]) == shift
    root_before = etree.fromstring(original)
    root_after = etree.fromstring(out)
    lo_b = root_before.find(".//c:serviceEvent/c:effectiveTime/c:low", NS).get("value")
    hi_b = root_before.find(".//c:serviceEvent/c:effectiveTime/c:high", NS).get("value")
    lo_a = root_after.find(".//c:serviceEvent/c:effectiveTime/c:low", NS).get("value")
    hi_a = root_after.find(".//c:serviceEvent/c:effectiveTime/c:high", NS).get("value")
    assert _days(hi_a[:8]) - _days(lo_a[:8]) == _days(hi_b[:8]) - _days(lo_b[:8])
    assert lo_a != lo_b


def test_birth_date_is_january_first(pair):
    _, original, out = pair
    before = etree.fromstring(original).find(".//c:patient/c:birthTime", NS).get("value")
    after = etree.fromstring(out).find(".//c:patient/c:birthTime", NS).get("value")
    assert after[4:8] == "0101"
    assert len(after) == len(before)
    assert after[8:] == before[8:]


def test_no_patient_token_remains(pair):
    src, _, out = pair
    text = out.decode("utf-8").lower()
    for token in PATIENT_TOKENS[src]:
        assert token.lower() not in text, token
    root = etree.fromstring(out)
    name = root.find(".//c:recordTarget//c:patient/c:name", NS)
    assert [c.text for c in name] == ["Alex", "Testperson"]
    assert name.find("c:prefix", NS) is None and name.find("c:suffix", NS) is None
    addr = root.find(".//c:patientRole/c:addr", NS)
    assert addr.find("c:city", NS).text == "Springfield"
    assert addr.find("c:postalCode", NS).text == "00000"
    for tel in root.iterfind(".//c:telecom", NS):
        assert tel.get("value") in {None, "tel:+1-555-0100", "mailto:patient@example.com"}
    for person in root.iterfind(".//c:assignedPerson/c:name", NS):
        assert [c.text for c in person] == ["Pat", "Clinician"]
    for org in root.iterfind(".//c:representedCustodianOrganization/c:name", NS):
        assert org.text == "Example Health System"


def test_ids_change_and_repeat_consistently(pair):
    _, original, out = pair
    before = [
        (e.get("root"), e.get("extension"))
        for e in etree.fromstring(original).iter("{urn:hl7-org:v3}id")
    ]
    after = [
        (e.get("root"), e.get("extension"))
        for e in etree.fromstring(out).iter("{urn:hl7-org:v3}id")
    ]
    assert len(before) == len(after)
    mapping: dict[tuple, tuple] = {}
    for b, a in zip(before, after, strict=True):
        if b == (None, None):
            assert a == b
            continue
        assert a != b
        if b[1]:
            assert a[1] != b[1] and re.fullmatch(r"[0-9a-f]{16}", a[1])
        assert mapping.setdefault(b, a) == a
    assert len(set(mapping.values())) == len(mapping)
    doc_id = etree.fromstring(out).find("c:id", NS)
    assert doc_id.get("extension") == mapping[before[0]][1]


def test_codes_and_structure_unchanged(pair):
    _, original, out = pair
    rb, ra = etree.fromstring(original), etree.fromstring(out)

    def codes(root):
        return [
            (e.get("code"), (e.get("codeSystem") or "").replace(".1.13.999.", ".1.13.0."))
            for e in root.iter()
            if e.get("code")
        ]

    assert codes(ra) == codes(rb)
    assert len(codes(ra)) == original.count(b" code=")
    assert [e.tag for e in ra.iterfind(".//c:section", NS)] == [
        e.tag for e in rb.iterfind(".//c:section", NS)
    ]
    assert len(ra.findall(".//c:text//c:tr", NS)) == len(rb.findall(".//c:text//c:tr", NS))
    units_b = [e.get("unit") for e in rb.iter() if e.get("unit")]
    units_a = [e.get("unit") for e in ra.iter() if e.get("unit")]
    assert units_a == units_b
    pq_b = [e.get("value") for e in rb.iter("{urn:hl7-org:v3}value") if e.get("unit")]
    pq_a = [e.get("value") for e in ra.iter("{urn:hl7-org:v3}value") if e.get("unit")]
    assert pq_a == pq_b


def test_output_still_converts(pair):
    _, original, out = pair
    bundle_b, report_b = convert_bytes(original, "before.xml")
    bundle_a, report_a = convert_bytes(out, "after.xml")
    assert report_a.resource_counts == report_b.resource_counts
    assert len(bundle_a["entry"]) == len(bundle_b["entry"])
    patient = next(
        e["resource"] for e in bundle_a["entry"] if e["resource"]["resourceType"] == "Patient"
    )
    assert patient["name"][0]["family"] == "Testperson"
    assert patient["birthDate"].endswith("-01-01")


def test_report_has_no_residual_findings(pair):
    _, original, _ = pair
    _, report = anonymize(original, seed="fixture-seed")
    assert report.residual_findings == []
    assert report.ids_rewritten > 0
    assert report.names_rewritten > 0
    assert report.addresses_rewritten > 0
    d = report.to_dict()
    assert "day_shift" not in d  # sharing the report must not undo the date shift
    assert set(d) == {
        "ids_rewritten",
        "oids_rewritten",
        "names_rewritten",
        "telecoms_rewritten",
        "addresses_rewritten",
        "narrative_replacements",
        "dates_in_text_shifted",
        "binary_payloads_removed",
        "non_xml_body",
        "residual_findings",
    }


def test_same_seed_same_bytes_and_different_seed_differs():
    data = HANDWRITTEN.read_bytes()
    a, _ = anonymize(data, seed="one")
    b, _ = anonymize(data, seed="one")
    c, _ = anonymize(data, seed="two")
    assert a == b
    assert a != c
    assert a.startswith(b"<?xml version='1.0' encoding='UTF-8'?>")


def test_narrative_phone_email_and_mrn_are_scrubbed():
    data = HANDWRITTEN.read_bytes().replace(
        b'<item ID="enc2">Hospital admission</item>',
        b'<item ID="enc2">Hospital admission, call (913) 555-0147 or jane@example.org, '
        b"MRN: 1234567, dose 500 mg, SSN-like 123456789</item>",
    )
    out, report = anonymize(data, seed="s")
    item = etree.fromstring(out).find(".//c:item[@ID='enc2']", NS).text
    assert "555-0147" not in item and "jane@example.org" not in item
    assert "MRN: 1234567" not in item
    assert "dose 500 mg" in item
    assert report.narrative_replacements >= 3
    assert any("123456789" in f for f in report.residual_findings)
    assert any("Encounters" in f for f in report.residual_findings)


def test_null_flavor_names_and_addresses_are_scrubbed():
    out, report = anonymize(with_performer(), seed="s")
    text = out.decode("utf-8")
    for token in ("Big City Brigham", "Rosa", "Attending", "Boston", "617-555"):
        assert token not in text, token
    entity = performer_entity(etree.fromstring(out))
    org = entity.find("c:representedOrganization/c:name", NS)
    assert org.get("nullFlavor") == "UNK" and org.text == "Example Health System"
    assert len(entity.find("c:representedOrganization/c:addr", NS)) == 0
    person = entity.find("c:assignedPerson/c:name", NS)
    assert person.get("nullFlavor") == "UNK"
    assert [c.text for c in person] == ["Pat", "Clinician"]
    addr = entity.find("c:addr", NS)
    assert addr.get("nullFlavor") == "UNK" and addr.find("c:city", NS).text == "Springfield"
    assert entity.find("c:telecom", NS).get("value") == "tel:+1-555-0100"
    assert report.names_rewritten >= 2


def test_epic_customer_oids_are_rewritten():
    data = with_performer(
        b'<item ID="enc2">Order 1.2.840.114350.1.13.999.2.7.2.798268.1455851590 placed</item>'
    )
    out, report = anonymize(data, seed="s")
    text = out.decode("utf-8")
    assert "114350.1.13.999" not in text
    assert "1.2.840.114350.1.72.1.7.7.10.688867.4160" in text
    assert report.oids_rewritten == data.count(EPIC_CUSTOMER)
    root = etree.fromstring(out)
    assert root.find("c:id", NS).get("root") == "1.2.840.114350.1.13.0.2.7.8.688883.9999"
    entity = performer_entity(root)
    ids = entity.findall("c:id", NS)
    assert re.fullmatch(r"1\.2\.840\.114350\.1\.13\.0\.2\.7\.8\.688883\.\d{8}", ids[1].get("root"))
    assert ids[2].get("root") == "1.2.840.114350.1.13.0.2.7.8.688883.2"
    systems = [t.get("codeSystem") for t in entity.findall("c:code/c:translation", NS)]
    assert systems[1] == "1.2.840.114350.1.13.0.2.7.10.836982.1050"
    item = root.find(".//c:item[@ID='enc2']", NS).text
    assert item == "Order 1.2.840.114350.1.13.0.2.7.2.798268.1455851590 placed"
    assert "oids" in report.summary()


def test_assigning_authority_names():
    out, _ = anonymize(with_performer(), seed="s")
    ids = performer_entity(etree.fromstring(out)).findall("c:id", NS)
    assert [i.get("assigningAuthorityName") for i in ids] == ["Example", "EPIC", "EPC"]


def test_npi_in_narrative_is_scrubbed():
    data = with_performer(
        b'<item ID="enc2">Seen by Dr. X (NPI: 1234567890), covering NPI 9876543210.</item>'
    )
    out, report = anonymize(data, seed="s")
    item = etree.fromstring(out).find(".//c:item[@ID='enc2']", NS).text
    assert item == "Seen by Dr. X (NPI: 0000000000), covering NPI 0000000000."
    assert report.narrative_replacements >= 2
    assert report.residual_findings == []


def test_residual_check_ignores_substitutes():
    data = with_performer(
        b'<item ID="enc2">Alex Testperson, Pat Clinician, Example Health System, '
        b"123 Example St, Springfield, patient@example.com, 555-0100, MRN: 00000, "
        b"NPI 0000000000, [email removed]</item>"
    )
    _, report = anonymize(data, seed="s")
    assert report.residual_findings == []


def test_name_tokens_are_replaced_outside_narrative():
    data = HANDWRITTEN.read_bytes().replace(
        b"  <custodian>",
        b'  <author><time value="20240315"/><assignedAuthor><id root="1.2.3"/>'
        b"<assignedAuthoringDevice><manufacturerModelName>Epic for Jane Sampleton"
        b"</manufacturerModelName><softwareName>Sampletonic 2</softwareName>"
        b"</assignedAuthoringDevice></assignedAuthor></author>\n  <custodian>",
    )
    out, _ = anonymize(data, seed="s")
    root = etree.fromstring(out)
    assert root.find(".//c:manufacturerModelName", NS).text == "Epic for Alex Testperson"
    assert root.find(".//c:softwareName", NS).text == "Sampletonic 2"


def test_organization_words_are_not_patient_tokens():
    data = HANDWRITTEN.read_bytes().replace(
        b"<name>Example Health System</name>\n      </providerOrganization>",
        b"<name>Care Partners Clinic</name>\n      </providerOrganization>",
    )
    out, _ = anonymize(data, seed="s")
    root = etree.fromstring(out)
    assert root.find("c:title", NS).text == "Continuity of Care Document"
    assert root.find(".//c:providerOrganization/c:name", NS).text == "Example Health System"
    assert b"Partners" not in out


def test_record_numbers_in_narrative_ids_are_hashed():
    data = (
        HANDWRITTEN.read_bytes()
        .replace(b'ID="enc2"', b'ID="enc_5790225540"')
        .replace(b'value="#enc2"', b'value="#enc_5790225540"')
    )
    out, report = anonymize(data, seed="s")
    again, _ = anonymize(data, seed="s")
    assert out == again
    assert b"5790225540" not in out
    root = etree.fromstring(out)
    ids = {el.get("ID") for el in root.iter() if el.get("ID")}
    new = next(i for i in ids if i.startswith("enc_"))
    assert re.fullmatch(r"enc_\d{8}", new)
    assert root.find(".//c:encounter/c:text/c:reference", NS).get("value") in {
        f"#{new}",
        "#enc1",
    }
    refs = {r.get("value").lstrip("#") for r in root.iter("{urn:hl7-org:v3}reference")}
    assert refs <= ids
    _, plain = anonymize(HANDWRITTEN.read_bytes(), seed="s")
    assert report.ids_rewritten == plain.ids_rewritten + 1


def test_cli_anonymize(tmp_path, capsys):
    out = tmp_path / "anon.xml"
    rep = tmp_path / "report.json"
    rc = main(["anonymize", str(HANDWRITTEN), str(out), "--seed", "cli", "--report", str(rep)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "residual findings 0" in captured.out
    assert "seed" not in captured.out and captured.err == ""  # a given seed is not echoed
    expected = anonymize_file(HANDWRITTEN, tmp_path / "again.xml", seed="cli")
    assert out.read_bytes() == (tmp_path / "again.xml").read_bytes()
    assert json.loads(rep.read_text()) == expected.to_dict()


def test_cli_anonymize_warns_on_residuals(tmp_path, capsys):
    src = tmp_path / "in.xml"
    src.write_bytes(
        HANDWRITTEN.read_bytes().replace(b"Hospital admission", b"Hospital admission 987654321")
    )
    rc = main(["anonymize", str(src), str(tmp_path / "out.xml"), "--seed", "s"])
    assert rc == 0
    assert "warning" in capsys.readouterr().err


def test_cli_anonymize_error(tmp_path, capsys):
    bad = tmp_path / "bad.xml"
    bad.write_text("<nope/>")
    rc = main(["anonymize", str(bad), str(tmp_path / "out.xml")])
    assert rc == 1
    assert "error" in capsys.readouterr().err


def _days(yyyymmdd: str) -> int:
    return date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8])).toordinal()


# ---- partial-precision timestamps ---------------------------------------------------------

ST_VALUE = b'<value xsi:type="ST">Specimen slightly hemolyzed</value>'
MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_FULL = [
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
]  # fmt: skip


def with_value(value: bytes) -> bytes:
    """The handwritten fixture with the lab annotation's ST value replaced."""
    data = HANDWRITTEN.read_bytes()
    assert data.count(ST_VALUE) == 1
    return data.replace(ST_VALUE, value)


def lab_value(out: bytes) -> etree._Element:
    for obs in etree.fromstring(out).iter(f"{{{NS['c']}}}observation"):
        code = obs.find("c:code", NS)
        if code is not None and code.get("code") == "48767-8":
            return obs.find("c:value", NS)
    raise AssertionError("annotation observation not found")


def annotate(text: str, seed: str = "fixture-seed"):
    """Anonymize the fixture with ``text`` as the annotation's ST value."""
    out, report = anonymize(with_value(f'<value xsi:type="ST">{text}</value>'.encode()), seed=seed)
    return lab_value(out).text, report


def shifted_date(d: date, seed: str = "fixture-seed") -> date:
    return date.fromordinal(d.toordinal() + day_shift_for(seed))


@pytest.fixture(scope="module")
def baseline():
    """The report for the unmodified fixture, to compare counts against."""
    return anonymize(HANDWRITTEN.read_bytes(), seed="fixture-seed")[1]


@pytest.mark.parametrize("days", [30, 182, 183, 400, 730, -30, -182, -730])
def test_shift_ts_year_only_always_moves(days):
    shifted = int(shift_ts("2019", days, partial=True))
    assert (shifted > 2019) == (days > 0)
    assert abs(shifted - 2019) == max(1, round(abs(days) / 365))


@pytest.mark.parametrize("days", [30, 31, -30, -31, 365, -730])
@pytest.mark.parametrize("month", ["201901", "201902", "201912"])
def test_shift_ts_year_month_always_moves(month, days):
    shifted = shift_ts(month, days, partial=True)
    got = date.fromordinal(date(int(month[:4]), int(month[4:]), 15).toordinal() + days)
    assert shifted == f"{got.year:04d}{got.month:02d}" != month


def test_shift_ts_partial_only_when_asked():
    assert shift_ts("2019", 400) is None
    assert shift_ts("201905+0500", 31, partial=True) == "201906+0500"
    assert shift_ts("201913", 31, partial=True) is None


def test_partial_birth_and_effective_times_are_shifted():
    data = with_value(ST_VALUE).replace(
        b'<birthTime value="19840722"/>', b'<birthTime value="1984"/>'
    )
    data = re.sub(
        rb'<effectiveTime value="20240301073000-0600"/>(\s+' + re.escape(ST_VALUE) + rb")",
        rb'<effectiveTime value="202403"/>\1',
        data,
    )
    assert b'value="202403"' in data and b'<birthTime value="1984"/>' in data
    out, report = anonymize(data, seed="fixture-seed")
    birth = etree.fromstring(out).find(".//c:patient/c:birthTime", NS).get("value")
    assert re.fullmatch(r"\d{4}", birth) and birth != "1984"
    effective = lab_value(out).getparent().find("c:effectiveTime", NS).get("value")
    assert effective == shift_ts("202403", day_shift_for("fixture-seed"), partial=True)
    assert effective != "202403"
    assert report.residual_findings == []


@pytest.mark.parametrize(
    "value",
    [
        b'<value xsi:type="PQ" value="1985" unit="mL"/>',
        b'<value xsi:type="INT" value="2019"/>',
        b'<value xsi:type="IVL_INT"><low value="2019"/><high value="202012"/></value>',
        b'<value xsi:type="INT" value="20190101"/>',
        b'<value xsi:type="PQ" value="20190101" unit="/uL"/>',
    ],
)
def test_numbers_that_look_like_dates_are_not_shifted(value):
    out, _ = anonymize(with_value(value), seed="fixture-seed")
    before = etree.fromstring(value.replace(b"xsi:type", b"type"))
    after = lab_value(out)
    assert [e.get("value") for e in before.iter()] == [e.get("value") for e in after.iter()]


# ---- dates in text --------------------------------------------------------------------------


def test_text_dates_are_shifted_in_their_own_format(baseline):
    got, report = annotate(
        "Seen on 03/14/2023, again 3/1/23, drawn 2023-03-14T10:00, "
        "reviewed Mar 14, 2023 and March 2023, signed 14 March 2023."
    )
    d = shifted_date(date(2023, 3, 14))
    first = shifted_date(date(2023, 3, 1))
    mid = shifted_date(date(2023, 3, 15))
    assert got == (
        f"Seen on {d.month:02d}/{d.day:02d}/{d.year}, "
        f"again {first.month}/{first.day}/{first.year % 100:02d}, "
        f"drawn {d.isoformat()}T10:00, "
        f"reviewed {MONTH_ABBR[d.month - 1]} {d.day}, {d.year} "
        f"and {MONTH_FULL[mid.month - 1]} {mid.year}, "
        f"signed {d.day} {MONTH_FULL[d.month - 1]} {d.year}."
    )
    assert report.dates_in_text_shifted == baseline.dates_in_text_shifted + 6
    assert report.residual_findings == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("05-14-2020", "{m:02d}-{d:02d}-{y}"),
        ("2020/05/14", "{y}/{m:02d}/{d:02d}"),
        ("05.14.2020", "{m:02d}.{d:02d}.{y}"),
        ("May 14th, 2020", "{B} {d}{th}, {y}"),
        ("14-May-2020", "{d}-{B}-{y}"),
        ("14-MAY-2020", "{d}-{BU}-{y}"),
        ("June 5, 2020", "{B5} {d5}, {y5}"),
    ],
)
def test_more_day_formats_are_shifted(text, expected):
    got, report = annotate(f"Seen {text}.")
    d = shifted_date(date(2020, 5, 14))
    d5 = shifted_date(date(2020, 6, 5))
    th = "th" if 10 <= d.day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(d.day % 10, "th")
    fields = {
        "y": d.year, "m": d.month, "d": d.day, "th": th,
        "B": MONTH_FULL[d.month - 1], "BU": MONTH_ABBR[d.month - 1].upper(),
        "B5": MONTH_FULL[d5.month - 1], "d5": d5.day, "y5": d5.year,
    }  # fmt: skip
    assert got == f"Seen {expected.format(**fields)}."
    assert report.residual_findings == []


@pytest.mark.parametrize(
    ("text", "fmt"),
    [("05/2019", "{m:02d}/{y}"), ("2019-05", "{y}-{m:02d}"), ("May 2019", "{B} {y}")],
)
def test_month_year_formats_are_shifted(text, fmt):
    got, _ = annotate(f"Since {text}")
    mid = shifted_date(date(2019, 5, 15))
    assert got == "Since " + fmt.format(y=mid.year, m=mid.month, B=MONTH_FULL[mid.month - 1])


def test_bare_year_after_a_date_word_is_shifted():
    got, _ = annotate("Diagnosed 2019, in 2018 too; platelets 2019 and in 2000 mL")
    year = int(shift_ts("2019", day_shift_for("fixture-seed"), partial=True))
    assert got.startswith(f"Diagnosed {year}, in {year - 1} too;")
    assert got.endswith("platelets 2019 and in 2000 mL")


@pytest.mark.parametrize(
    "text", ["may 1500 mg", "Take 1/2 tab", "Visual acuity 20/20/20", "version 1.5.20"]
)
def test_text_that_is_not_a_date_is_left_alone_and_not_reported(text, baseline):
    got, report = annotate(text)
    assert got == text
    assert report.dates_in_text_shifted == baseline.dates_in_text_shifted
    assert report.residual_findings == []


def test_birth_date_in_text_is_rounded_like_birth_time():
    got, _ = annotate("DOB 07/22/1984; date of birth: July 22, 1984")
    year = shifted_date(date(1984, 7, 22)).year
    assert got == f"DOB 01/01/{year}; date of birth: January 1, {year}"


def test_text_dates_in_narrative_follow_the_shift():
    data = with_performer(b'<item ID="enc2">Seen on 03/14/2023</item>')
    out, _ = anonymize(data, seed="fixture-seed")
    item = etree.fromstring(out).find(".//c:item[@ID='enc2']", NS).text
    d = shifted_date(date(2023, 3, 14))
    assert item == f"Seen on {d.month:02d}/{d.day:02d}/{d.year}"


def with_display_name(suffix: str) -> bytes:
    return HANDWRITTEN.read_bytes().replace(
        b'displayName="Annotation comment"', f'displayName="Annotation comment {suffix}"'.encode()
    )


def test_dates_in_display_names_are_shifted():
    out, report = anonymize(with_display_name("05/14/2020"), seed="fixture-seed")
    d = shifted_date(date(2020, 5, 14))
    code = lab_value(out).getparent().find("c:code", NS)
    assert code.get("displayName") == f"Annotation comment {d.month:02d}/{d.day:02d}/{d.year}"
    assert report.residual_findings == []


def test_day_first_date_is_a_residual_finding():
    got, report = annotate("Seen on 14/03/2023", seed="s")
    assert got == "Seen on 14/03/2023"
    assert any("unshifted date '14/03/2023'" in f for f in report.residual_findings)
    _, report = anonymize(with_display_name("14/03/2023"), seed="s")
    assert any("14/03/2023" in f for f in report.residual_findings)


# ---- identifiers outside narrative ------------------------------------------------------------


def test_phone_email_mrn_in_structured_text_are_scrubbed():
    got, report = annotate("Callback 555-987-6543 or lab@clinic.org, MRN: 7654321", seed="s")
    assert "555-987-6543" not in got and "lab@clinic.org" not in got
    assert "7654321" not in got
    assert report.residual_findings == []


def test_residual_check_covers_structured_text():
    got, report = annotate("Accession 9876543210", seed="s")
    assert "9876543210" in got
    assert any("9876543210" in f for f in report.residual_findings)


# ---- keyed hashes and the seed ---------------------------------------------------------------


def test_id_hashes_are_not_plain_sha256_of_seed_and_value():
    out, _ = anonymize(HANDWRITTEN.read_bytes(), seed="s")
    assert b"LAB-7001-2" not in out
    plain = hashlib.sha256(b"sLAB-7001-2").hexdigest()[:16]
    assert plain.encode() not in out


def test_id_hashes_depend_on_the_seed_as_a_key():
    a, _ = anonymize(HANDWRITTEN.read_bytes(), seed="s")
    b, _ = anonymize(HANDWRITTEN.read_bytes(), seed="sL")
    ext = re.compile(rb'extension="([0-9a-f]{16})"')
    assert set(ext.findall(a)).isdisjoint(ext.findall(b))


def test_cli_prints_a_random_seed_to_stderr_only(tmp_path, capsys):
    assert main(["anonymize", str(HANDWRITTEN), str(tmp_path / "o.xml")]) == 0
    captured = capsys.readouterr()
    assert "hrb: seed" not in captured.out
    assert "hrb: seed" in captured.err and "keep it private" in captured.err


# ---- embedded files ----------------------------------------------------------------------------


def test_base64_ed_values_are_replaced():
    value = (
        '<value xsi:type="ED" mediaType="application/pdf" representation="B64" '
        f'compression="DF" integrityCheck="abc">{PDF_B64}</value>'
    ).encode()
    out, report = anonymize(with_value(value), seed="s")
    el = lab_value(out)
    assert PDF_B64.encode() not in out
    assert el.get("representation") == "B64" and el.get("mediaType") == "application/pdf"
    assert el.get("compression") is None and el.get("integrityCheck") is None
    assert base64.b64decode(el.text) == b"Content removed by hrb anonymize."
    assert report.binary_payloads_removed == 1
    assert report.non_xml_body is None
    assert report.residual_findings == []


def test_base64_after_a_reference_child_is_removed():
    value = (
        '<value xsi:type="ED" mediaType="application/pdf" representation="B64">'
        f'<reference value="doc.pdf"/>{PDF_B64}'
        '<thumbnail mediaType="image/png" representation="B64">iVBORw0KGgo=</thumbnail>'
        "</value>"
    ).encode()
    out, report = anonymize(with_value(value), seed="s")
    assert PDF_B64.encode() not in out and b"iVBORw0KGgo" not in out
    el = lab_value(out)
    assert el.text == BINARY_PLACEHOLDER
    assert [etree.QName(c).localname for c in el] == ["reference"]
    assert el[0].get("value") == "doc.pdf" and el[0].tail is None
    assert report.binary_payloads_removed == 1


@pytest.mark.parametrize("representation", [b' representation="B64"', b""])
def test_non_xml_body_file_is_replaced_and_warned_about(representation, tmp_path, capsys):
    data = non_xml_body_document().replace(b' representation="B64"', representation)
    out, report = anonymize(data, seed="s")
    assert PDF_B64.encode() not in out
    assert report.non_xml_body == "file" and report.binary_payloads_removed == 1
    rc, err = run_cli(capsys, tmp_path, "anonymize", data)
    assert rc == 0 and "replaced with a placeholder" in err


def test_plain_text_non_xml_body_is_scrubbed_not_called_removed(tmp_path, capsys):
    data = non_xml_body_document().replace(
        b'<text mediaType="application/pdf" representation="B64">' + PDF_B64.encode(),
        b'<text mediaType="text/plain">Jane Sampleton seen 03/14/2023, call 913-555-0147',
    )
    out, report = anonymize(data, seed="s")
    assert report.non_xml_body == "text"
    assert b"Sampleton" not in out and b"913-555-0147" not in out and b"03/14/2023" not in out
    _, err = run_cli(capsys, tmp_path, "anonymize", data)
    assert "plain text" in err and "replaced with a placeholder" not in err
