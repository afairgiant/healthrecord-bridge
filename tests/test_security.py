"""Untrusted input handling."""

from __future__ import annotations

import pytest

from conftest import HANDWRITTEN
from healthrecord_bridge.ccda import (
    CcdaError,
    NotClinicalDocumentError,
    UnsafeXmlError,
    convert,
    convert_bytes,
)


def test_non_clinical_document_root_is_rejected(tmp_path):
    path = tmp_path / "not-cda.xml"
    path.write_text(
        '<?xml version="1.0"?><Bundle xmlns="http://hl7.org/fhir"><type value="x"/></Bundle>'
    )
    with pytest.raises(NotClinicalDocumentError):
        convert(path)


def test_wrong_namespace_is_rejected():
    with pytest.raises(NotClinicalDocumentError):
        convert_bytes(b"<ClinicalDocument/>")


def test_external_entity_is_rejected(tmp_path):
    path = tmp_path / "xxe.xml"
    path.write_text(
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE ClinicalDocument [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
        '<ClinicalDocument xmlns="urn:hl7-org:v3"><title>&xxe;</title></ClinicalDocument>'
    )
    with pytest.raises(UnsafeXmlError):
        convert(path)


def test_internal_entity_and_doctype_are_rejected():
    data = (
        b'<?xml version="1.0"?><!DOCTYPE ClinicalDocument [<!ENTITY a "aaaa">]>'
        b'<ClinicalDocument xmlns="urn:hl7-org:v3"><title>&a;</title></ClinicalDocument>'
    )
    with pytest.raises(UnsafeXmlError):
        convert_bytes(data)
    with pytest.raises(UnsafeXmlError):
        convert_bytes(
            b'<?xml version="1.0"?><!DOCTYPE ClinicalDocument SYSTEM "http://example.invalid/cda.dtd">'
            b'<ClinicalDocument xmlns="urn:hl7-org:v3"/>'
        )


def test_malformed_xml_is_a_ccda_error():
    with pytest.raises(CcdaError):
        convert_bytes(b"<ClinicalDocument xmlns='urn:hl7-org:v3'>")


def test_missing_file():
    with pytest.raises(FileNotFoundError):
        convert("/nonexistent/path/record.xml")


def test_good_document_still_parses():
    bundle, _ = convert_bytes(HANDWRITTEN.read_bytes(), "inline.xml")
    assert len(bundle["entry"]) > 10


def test_referenced_narrative_is_resolved_once(monkeypatch):
    """Entries that point at the same narrative share one resolved string."""
    from healthrecord_bridge.ccda import narrative

    calls = []
    original = narrative.text_of

    def counting_text_of(el):
        calls.append(el)
        return original(el)

    monkeypatch.setattr(narrative, "text_of", counting_text_of)
    entry = (
        '<entry><substanceAdministration classCode="SBADM" moodCode="EVN">'
        '<text><reference value="#sig"/></text><statusCode code="active"/>'
        "<consumable><manufacturedProduct><manufacturedMaterial/>"
        "</manufacturedProduct></consumable></substanceAdministration></entry>"
    )
    doc = (
        '<?xml version="1.0"?><ClinicalDocument xmlns="urn:hl7-org:v3"><id root="1.2.3"/>'
        "<recordTarget><patientRole><patient><name><given>A</given></name></patient>"
        "</patientRole></recordTarget><component><structuredBody><component><section>"
        '<code code="10160-0"/><text><paragraph ID="sig">Take one daily</paragraph></text>'
        + entry * 50
        + "</section></component></structuredBody></component></ClinicalDocument>"
    )
    bundle, _ = convert_bytes(doc.encode())
    texts = [
        e["resource"]["dosage"][0]["text"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "MedicationStatement"
    ]
    assert len(texts) == 50 and set(texts) == {"Take one daily"}
    assert len({id(t) for t in texts}) == 1
    assert len([el for el in calls if el.get("ID") == "sig"]) == 1


def test_v2_table_oid_with_non_ascii_digit_does_not_crash():
    """str.isdigit accepts superscripts that int() rejects."""
    from healthrecord_bridge.ccda.codes import system_uri

    assert system_uri("2.16.840.1.113883.12.²") == "urn:oid:2.16.840.1.113883.12.²"
    assert system_uri("2.16.840.1.113883.12.1") == "http://terminology.hl7.org/CodeSystem/v2-0001"
