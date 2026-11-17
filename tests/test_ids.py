"""Deterministic ids and identifiers."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from pathlib import Path

from conftest import HANDWRITTEN, SYNTHEA, by_type
from healthrecord_bridge.ccda import NAMESPACE_HRB, convert, convert_many
from healthrecord_bridge.ccda.ids import CdaId, content_id, fallback_id, identifier, resource_id


def test_namespace_is_fixed():
    assert uuid.uuid5(uuid.NAMESPACE_DNS, "healthrecord-bridge") == NAMESPACE_HRB
    assert str(NAMESPACE_HRB) == "c56c34af-5015-509c-ba3d-34930e66992b"


def test_resource_id_formula():
    cid = CdaId("1.2.3", "ABC")
    assert resource_id("Observation", cid) == str(
        uuid.uuid5(NAMESPACE_HRB, "Observation|1.2.3|ABC")
    )
    assert resource_id("Observation", CdaId("1.2.3")) == str(
        uuid.uuid5(NAMESPACE_HRB, "Observation|1.2.3|")
    )
    assert resource_id("Condition", cid) != resource_id("Observation", cid)
    content = ("http://loinc.org|2345-7", "20260301")
    assert resource_id("Observation", cid, *content) == str(
        uuid.uuid5(NAMESPACE_HRB, "Observation|1.2.3|ABC|@http://loinc.org|2345-7|20260301")
    )
    assert resource_id("Observation", cid, *content) != resource_id("Observation", cid)
    doc = CdaId("9.9", "DOC")
    assert fallback_id("Patient", doc, "recordTarget") == str(
        uuid.uuid5(NAMESPACE_HRB, "Patient|9.9|DOC|recordTarget")
    )
    assert content_id("Observation", "Patient/p", "29762-2", "http://loinc.org|1-8", "2024") == (
        str(
            uuid.uuid5(
                NAMESPACE_HRB, "Observation|#content|Patient/p|29762-2|http://loinc.org|1-8|2024"
            )
        )
    )


def test_identifier_shapes():
    assert identifier(CdaId("2.16.840.1.113883.19.5", "X1")) == {
        "system": "urn:oid:2.16.840.1.113883.19.5",
        "value": "X1",
    }
    assert identifier(CdaId("2.16.840.1.113883.19.5")) == {
        "system": "urn:ietf:rfc:3986",
        "value": "urn:oid:2.16.840.1.113883.19.5",
    }
    assert identifier(CdaId("5e09b6b3-5091-40f1-83ec-422879429da9")) == {
        "system": "urn:ietf:rfc:3986",
        "value": "urn:uuid:5e09b6b3-5091-40f1-83ec-422879429da9",
    }


def test_ids_are_deterministic_across_runs(fixture_path):
    first, _ = convert(fixture_path)
    second, _ = convert(fixture_path)
    assert first == second
    ids = [e["resource"]["id"] for e in first["entry"]]
    assert len(ids) == len(set(ids)), "ids must be unique within a bundle"
    resources = [e["resource"] for e in first["entry"]]
    # Epic reuses one CDA id on several observations; only the first keeps the plain id.
    seen = Counter(
        (r["resourceType"], str(r["identifier"][0])) for r in resources if "identifier" in r
    )
    for r in resources:
        if "identifier" in r and seen[(r["resourceType"], str(r["identifier"][0]))] == 1:
            cid = r["identifier"][0]
            if cid["system"] == "urn:ietf:rfc:3986":
                root = cid["value"].split(":", 2)[2]
                expected = resource_id(r["resourceType"], CdaId(root))
            else:
                root = cid["system"].split(":", 2)[2]
                expected = resource_id(r["resourceType"], CdaId(root, cid["value"]))
            if r["resourceType"] not in {"DocumentReference", "Provenance"}:
                assert r["id"] == expected, r["resourceType"]


def test_meta_source_names_the_document():
    bundle, _ = convert(HANDWRITTEN)
    for e in bundle["entry"]:
        assert e["resource"]["meta"]["source"] == (
            "urn:healthrecord-bridge:ccda:1.2.840.114350.1.13.999.2.7.8.688883.9999:CCD-2024-000042"
        )


def test_merge_dedupes_and_later_wins():
    bundle, report = convert_many([HANDWRITTEN, HANDWRITTEN])
    single, _ = convert(HANDWRITTEN)
    assert len(bundle["entry"]) == len(single["entry"])
    assert len(report.documents) == 2
    assert report.resource_counts["Patient"] == 2  # per-document counts are summed
    assert len(by_type(bundle)["Patient"]) == 1


def test_merge_distinct_documents():
    bundle, _ = convert_many(SYNTHEA[:2])
    a, _ = convert(SYNTHEA[0])
    b, _ = convert(SYNTHEA[1])
    assert len(bundle["entry"]) == len(a["entry"]) + len(b["entry"])
    assert len(by_type(bundle)["Patient"]) == 2


FIXTURES = Path(__file__).parent / "fixtures"


def test_fixture_ids_are_stable():
    """Every fixture's resource ids, as released: a change here changes ids for users."""
    expected = json.loads((FIXTURES / "expected_ids.json").read_text(encoding="utf-8"))
    for name, ids in expected.items():
        bundle, _ = convert(FIXTURES / name)
        got = sorted(
            f"{e['resource']['resourceType']}/{e['resource']['id']}" for e in bundle["entry"]
        )
        assert got == ids, name


NO_ID_DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <templateId root="2.16.840.1.113883.10.20.22.1.2"/>
  <id root="1.2.3.4" extension="{doc}"/>
  <code code="34133-9" codeSystem="2.16.840.1.113883.6.1"/>
  <effectiveTime value="20260301"/>
  <recordTarget><patientRole><id root="1.2.3.4.5" extension="P-1"/>
    <patient><name><family>Testperson</family></name></patient></patientRole></recordTarget>
  <component><structuredBody><component><section>
    <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
    {entries}
  </section></component></structuredBody></component>
</ClinicalDocument>
"""


def no_id_lab(code: str, when: str = "20260301") -> str:
    return (
        '<entry><observation classCode="OBS" moodCode="EVN">'
        f'<code code="{code}" codeSystem="2.16.840.1.113883.6.1"/>'
        f'<statusCode code="completed"/><effectiveTime value="{when}"/>'
        '<value xsi:type="PQ" value="5" unit="mg/dL"/></observation></entry>'
    )


def write(tmp_path, doc: str, entries: str, section: str = "30954-2"):
    path = tmp_path / f"{doc}.xml"
    text = NO_ID_DOCUMENT.format(doc=doc, entries=entries).replace("30954-2", section)
    path.write_text(text, encoding="utf-8")
    return path


def test_entry_without_an_id_keeps_its_id_across_documents(tmp_path):
    first = write(tmp_path, "DOC-1", no_id_lab("2345-7"))
    # A newer export: another document id, another position, the same result.
    second = write(tmp_path, "DOC-2", no_id_lab("718-7") + no_id_lab("2345-7"))
    one, _ = convert(first)
    merged, _ = convert_many([first, second])
    observations = by_type(merged)["Observation"]
    assert len(observations) == 2  # the glucose result is one resource, not two
    glucose = next(o for o in observations if o["code"]["coding"][0]["code"] == "2345-7")
    assert glucose["id"] == by_type(one)["Observation"][0]["id"]


def test_entries_without_ids_are_keyed_by_content(tmp_path):
    path = write(
        tmp_path,
        "DOC-1",
        no_id_lab("2345-7")
        + no_id_lab("2345-7", "20260302")  # another day
        + no_id_lab("2345-7")  # identical: the same entry listed twice
        + no_id_lab("718-7"),
    )
    bundle, _ = convert(path)
    types = by_type(bundle)
    patient = f"Patient/{types['Patient'][0]['id']}"

    def expected(code, when="20260301"):
        return content_id(
            "Observation", patient, "30954-2", f"http://loinc.org|{code}", when, "5 mg/dL"
        )

    assert [o["id"] for o in types["Observation"]] == [
        expected("2345-7"),
        expected("2345-7", "20260302"),
        expected("718-7"),
    ]


def test_entries_with_nothing_to_key_on_stay_within_their_document(tmp_path):
    """No id, code or time: two documents' encounters must not merge into one."""
    blank = '<entry><encounter classCode="ENC" moodCode="EVN"/></entry>'
    paths = [write(tmp_path, doc, blank * 2, "46240-8") for doc in ("DOC-1", "DOC-2")]
    merged, _ = convert_many(paths)
    ids = [e["id"] for e in by_type(merged)["Encounter"]]
    assert ids == [
        fallback_id("Encounter", CdaId("1.2.3.4", doc), "46240-8", f"#{n}")
        for doc in ("DOC-1", "DOC-2")
        for n in (1, 2)
    ]
