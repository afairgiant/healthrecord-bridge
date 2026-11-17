"""Assemble and merge FHIR R4 ``Bundle`` resources of type ``collection``."""

from __future__ import annotations


def make_bundle(resources: list[dict]) -> dict:
    """Wrap resources in a collection Bundle with ``urn:uuid:`` fullUrls.

    ``Bundle.total`` is deliberately absent: FHIR only allows it on searchset and
    history bundles.
    """
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"fullUrl": f"urn:uuid:{r['id']}", "resource": r} for r in resources],
    }


def merge_bundles(bundles: list[dict]) -> dict:
    """Merge collection Bundles, deduplicating by (resourceType, id); later entries win.

    Order is first-seen order, so a resource replaced by a later document keeps
    its original position in the output.
    """
    return make_bundle(dedupe([e["resource"] for b in bundles for e in b.get("entry", [])]))


def dedupe(resources: list[dict]) -> list[dict]:
    """One resource per (resourceType, id): the later wins, in first-seen position."""
    merged: dict[tuple[str, str], dict] = {}
    for r in resources:
        merged[(r["resourceType"], r["id"])] = r
    return list(merged.values())


def resources_by_type(bundle: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for entry in bundle.get("entry", []):
        r = entry["resource"]
        out.setdefault(r["resourceType"], []).append(r)
    return out
