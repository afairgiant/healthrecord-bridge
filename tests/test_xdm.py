"""Zip / IHE XDM input."""

from __future__ import annotations

import copy
import zipfile

import pytest

from conftest import HANDWRITTEN, XDM_SOURCE_XML, XDM_ZIP, by_type
from healthrecord_bridge.ccda import NoDocumentsFoundError, convert, xdm
from healthrecord_bridge.ccda.xdm import iter_xml_members


def _strip_source_entity(bundle: dict) -> dict:
    out = copy.deepcopy(bundle)
    for e in out["entry"]:
        if e["resource"]["resourceType"] == "Provenance":
            e["resource"].pop("entity")
    return out


def test_xdm_zip_matches_bare_xml():
    from_zip, zip_report = convert(XDM_ZIP)
    from_xml, xml_report = convert(XDM_SOURCE_XML)
    assert _strip_source_entity(from_zip) == _strip_source_entity(from_xml)
    prov = by_type(from_zip)["Provenance"][0]
    assert prov["entity"][0]["what"]["display"] == (
        "mychart_export_letha284.zip:MachineReadable_XDMFormat/IHE_XDM/SUBSET01/DOC0001.XML"
    )
    assert zip_report.resource_counts == xml_report.resource_counts
    assert len(zip_report.documents) == 1


def test_member_selection_skips_metadata_and_prefers_ihe_xdm(tmp_path):
    path = tmp_path / "mixed.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("INDEX.HTM", "<html/>")
        zf.writestr("README.TXT", "x")
        zf.writestr("other/stray.xml", "<x/>")
        zf.writestr("IHE_XDM/SUBSET01/METADATA.XML", "<m/>")
        zf.writestr("IHE_XDM/SUBSET01/DOC0002.XML", "<b/>")
        zf.writestr("IHE_XDM/SUBSET01/DOC0001.xml", "<a/>")
    with zipfile.ZipFile(path) as zf:
        names = [m.filename for m in iter_xml_members(zf)]
    assert names == ["IHE_XDM/SUBSET01/DOC0001.xml", "IHE_XDM/SUBSET01/DOC0002.XML"]


def test_plain_zip_with_ccda_anywhere(tmp_path):
    path = tmp_path / "plain.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("exports/2024/record.xml", HANDWRITTEN.read_bytes())
    bundle, _ = convert(path)
    assert by_type(bundle)["Patient"][0]["name"][0]["family"] == "Sampleton"


def test_zip_without_xml_is_rejected(tmp_path):
    path = tmp_path / "empty.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("INDEX.HTM", "<html/>")
    with pytest.raises(NoDocumentsFoundError):
        convert(path)


def _two_document_zip(tmp_path):
    path = tmp_path / "two.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.xml", HANDWRITTEN.read_bytes())
        zf.writestr("b.xml", HANDWRITTEN.read_bytes())
    return path


@pytest.mark.parametrize(
    ("limit", "value", "message"),
    [
        ("MAX_DOCUMENTS", 1, "2 XML documents, over the 1 limit"),
        ("MAX_TOTAL_BYTES", 1000, "bytes of XML, over the 1000 byte limit"),
        ("MAX_MEMBER_BYTES", 1000, r"a.xml is"),
    ],
)
def test_zip_limits_are_checked_before_extraction(tmp_path, monkeypatch, limit, value, message):
    path = _two_document_zip(tmp_path)
    monkeypatch.setattr(xdm, limit, value)
    opened = []
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda *a, **k: opened.append(a))
    with pytest.raises(ValueError, match=message):
        list(xdm.iter_documents(path))
    assert not opened  # nothing was decompressed


def test_bare_xml_file_has_the_same_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(xdm, "MAX_MEMBER_BYTES", 1000)
    with pytest.raises(ValueError, match=r"record.xml is over the 1000 byte limit"):
        convert(_copy(tmp_path))


def _copy(tmp_path):
    path = tmp_path / "record.xml"
    path.write_bytes(HANDWRITTEN.read_bytes())
    return path
