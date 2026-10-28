#!/usr/bin/env python3
"""
End-to-end demonstration. Generates synthetic fixtures in ./fixtures/, then
loads each from disk, runs the checks and prints the reports.
No network access; standard library only.

    python demo.py            # compact reports
    python demo.py -v         # include details and sources for every check
"""
import copy
import json
import sys
from pathlib import Path

from dpp_checker import CheckerConfig, print_report, run_compliance_checks

FIXTURES = Path(__file__).parent / "fixtures"
CDS = "https://admin-shell.io/idta/cds/"


def _sem(value):
    return {"type": "ExternalReference", "keys": [{"type": "GlobalReference", "value": value}]}


def _prop(id_short, value, value_type="xs:string"):
    return {
        "idShort": id_short,
        "displayName": [{"language": "en", "text": id_short}],
        "description": [{"language": "en", "text": f"Synthetic description of {id_short}."}],
        "semanticId": _sem(f"{CDS}{id_short}/1"),
        # Example-value qualifier: deliberately NOT treated as a requirement.
        "qualifiers": [{"type": "ExampleValue", "valueType": "xs:string", "value": "n/a"}],
        "modelType": "Property",
        "valueType": value_type,
        "value": value,
    }


def _spec_list(ids):
    return {
        "idShort": "contentSpecificationIds",
        "semanticId": _sem(f"{CDS}contentSpecificationIds/1"),
        "orderRelevant": False,
        "typeValueListElement": "Property",
        "valueTypeListElement": "xs:string",
        "modelType": "SubmodelElementList",
        "value": [{"semanticId": _sem(f"{CDS}contentSpecificationId/1"),
                   "modelType": "Property", "valueType": "xs:string", "value": v} for v in ids],
    }


def build_valid_environment() -> dict:
    sm_dpp = "https://example.com/ids/sm/dpp-metadata/endmill-0001"
    sm_other = "https://example.com/ids/sm/nameplate/endmill-0001"
    elements = [
        _prop("digitalProductPassportId", "https://example.com/dpp/endmill-0001"),
        _prop("uniqueProductIdentifier", "https://example.com/products/endmill-0001"),
        _prop("granularity", "Item"),
        _prop("dppSchemaVersion", "ENXXX:v1.0"),
        _prop("dppStatus", "Active"),
        _prop("lastUpdate", "2026-10-21T15:53:00Z", "xs:dateTime"),
        _prop("economicOperatorId", "gxx:ppp456789"),
        _prop("facilityId", "gxx:xxx987654"),
        _spec_list(["https://admin-shell.io/idta/CarbonFootprint/CarbonFootprint/1/0"]),
    ]
    elements.reverse()          # order must not matter
    return {
        "assetAdministrationShells": [{
            "modelType": "AssetAdministrationShell",
            "id": "https://example.com/ids/aas/endmill-0001",
            "idShort": "SolidCarbideEndMill",
            "assetInformation": {"assetKind": "Instance",
                                 "globalAssetId": "https://example.com/products/endmill-0001"},
            "submodels": [
                {"type": "ModelReference", "keys": [{"type": "Submodel", "value": sm_other}]},
                {"type": "ModelReference", "keys": [{"type": "Submodel", "value": sm_dpp}]},
            ],
        }],
        "submodels": [
            {"modelType": "Submodel", "id": sm_other, "idShort": "Nameplate",
             "submodelElements": [_prop("ManufacturerProductDesignation", "Fictional end mill 6 mm")]},
            {"modelType": "Submodel", "id": sm_dpp, "idShort": "DppMetadata",
             "semanticId": _sem(CDS + "dppMetadata/1"), "submodelElements": elements},
        ],
        "conceptDescriptions": [
            {"modelType": "ConceptDescription", "id": f"{CDS}{n}/1", "idShort": n}
            for n in ("lastUpdate", "dppStatus")],
    }


def _dpp_elements(env):
    return next(s for s in env["submodels"] if s["idShort"] == "DppMetadata")["submodelElements"]


def build_invalid_environment() -> dict:
    env = copy.deepcopy(build_valid_environment())
    els = _dpp_elements(env)
    els[:] = [e for e in els if e["idShort"] != "economicOperatorId"]        # missing mandatory
    for e in els:
        if e["idShort"] == "lastUpdate":
            e["value"] = "2026-02-30T10:00:00Z"                              # invalid calendar date
        elif e["idShort"] == "dppStatus":
            e["value"] = "   "                                               # whitespace only
        elif e["idShort"] == "facilityId":
            e["value"] = 12345                                               # wrong JSON type
        elif e["idShort"] == "granularity":
            e["modelType"] = "MultiLanguageProperty"                         # wrong element type
        elif e["idShort"] == "contentSpecificationIds":
            e["value"].append({"modelType": "Property", "valueType": "xs:string", "value": 7})
    return env


def build_no_timezone_environment() -> dict:
    env = copy.deepcopy(build_valid_environment())
    for e in _dpp_elements(env):
        if e["idShort"] == "lastUpdate":
            e["value"] = "2026-10-21T15:53:00"       # well-formed, but violates timezone profile
    return env


def write_fixtures() -> dict:
    FIXTURES.mkdir(exist_ok=True)
    files = {
        "valid_dpp.json": build_valid_environment(),
        "invalid_dpp.json": build_invalid_environment(),
        "no_timezone_dpp.json": build_no_timezone_environment(),
    }
    for name, env in files.items():
        (FIXTURES / name).write_text(json.dumps(env, indent=2), encoding="utf-8")
    (FIXTURES / "malformed.json").write_text('{"assetAdministrationShells": [', encoding="utf-8")
    return {n: FIXTURES / n for n in list(files) + ["malformed.json"]}


def main() -> int:
    verbose = "-v" in sys.argv
    paths = write_fixtures()
    scenarios = [
        ("1. PASSING CASE", "valid_dpp.json"),
        ("2. FAILING CASE (missing attribute, bad date, wrong types)", "invalid_dpp.json"),
        ("3. VALID TIMESTAMP BUT PROFILE VIOLATION (no timezone)", "no_timezone_dpp.json"),
        ("4. MALFORMED JSON FILE", "malformed.json"),
    ]
    summary = []
    for title, name in scenarios:
        print(f"\n\n##### {title} #####")
        report = run_compliance_checks(str(paths[name]), CheckerConfig())
        print_report(report, verbose=verbose)
        summary.append((name, report.verdict))

    print("\n\nSUMMARY")
    for name, verdict in summary:
        print(f"  {name:<24} {verdict}")
    expected = {"valid_dpp.json": "COMPLIANT"}
    ok = all(v == expected.get(n, "NOT COMPLIANT") for n, v in summary)
    print("Demo outcome as expected." if ok else "UNEXPECTED demo outcome!")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
