#!/usr/bin/env python3
"""
dpp_checker.py - Compliance checker for AAS JSON files carrying DPP metadata.

Validation layers (kept separate; a file may pass one and fail another):
  1. JSON            - readable, syntactically valid JSON, root is an object
  2. AAS structure   - lightweight structural checks (NOT a full AAS schema
                       validation unless a JSON schema file is supplied)
  3. IDTA 02099-1    - DppMetadata submodel, v1.0 (28.05.2026)
  4. EN 18223 / 18219 - identifier formats. NOT IMPLEMENTED by default: the
                       standard texts were not available to the implementer.
                       Pluggable via CheckerConfig.identifier_validators.

Python standard library only (optional: `jsonschema` for --aas-schema).

This tool is *compatible with* the stated specifications for the rules it
implements. It is NOT a certification tool. See README.md for coverage.
"""
from __future__ import annotations

import argparse
import calendar
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"

LAYER_JSON = "1 JSON"
LAYER_AAS = "2 AAS structure"
LAYER_IDTA = "3 IDTA 02099-1"
LAYER_EN = "4 EN 18223 / EN 18219"
LAYERS = (LAYER_JSON, LAYER_AAS, LAYER_IDTA, LAYER_EN)

CAT_INPUT = "INPUT_FILE"
CAT_STRUCT = "STRUCTURAL"
CAT_MAND = "MANDATORY"
CAT_TYPE = "TYPE"
CAT_DT = "DATETIME"
CAT_OPT = "OPTIONAL"
CAT_SEM = "SEMANTIC"
CAT_VALUE = "VALUE"
CAT_INTERNAL = "INTERNAL"

SRC_IDTA = "IDTA 02099-1 v1.0 (28.05.2026)"
SRC_IDTA_SM = SRC_IDTA + ", section 3.1"
SRC_IDTA_SML = SRC_IDTA + ", section 3.2"
SRC_AAS = "AAS metamodel (IDTA-01001); exact version/clause not verified"
SRC_EN = ("EN 18223:2026 / EN 18219:2026 - full text NOT available; "
          "only as quoted in " + SRC_IDTA_SM)
SRC_PROBLEM = "Problem statement (project requirement, not a standard)"

_CDS = "https://admin-shell.io/idta/cds/"  # semanticId namespace, IDTA 02099-1


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class CheckResult:
    check_id: str
    name: str
    layer: str
    category: str
    location: str
    status: str                      # PASS | FAIL | WARN | SKIP
    message: str
    expected: Optional[str] = None
    actual: Optional[str] = None
    source: str = ""


@dataclass(frozen=True)
class AttributeRule:
    """One row of the IDTA 02099-1 element table (section 3.1)."""
    id_short: str
    model_type: str
    value_type: Optional[str]        # None for lists
    mandatory: bool                  # cardinality 1 (True) or 0..1 (False)
    semantic_id: str
    kind: str = "scalar"             # scalar | datetime | list


ATTRIBUTE_RULES: Tuple[AttributeRule, ...] = (
    AttributeRule("digitalProductPassportId", "Property", "xs:string", True,
                  _CDS + "digitalProductPassportId/1"),
    AttributeRule("uniqueProductIdentifier", "Property", "xs:string", True,
                  _CDS + "uniqueProductIdentifier/1"),
    AttributeRule("granularity", "Property", "xs:string", True,
                  _CDS + "granularity/1"),
    AttributeRule("dppSchemaVersion", "Property", "xs:string", True,
                  _CDS + "dppSchemaVersion/1"),
    AttributeRule("dppStatus", "Property", "xs:string", True,
                  _CDS + "dppStatus/1"),
    AttributeRule("lastUpdate", "Property", "xs:dateTime", True,
                  _CDS + "lastUpdate/1", kind="datetime"),
    AttributeRule("economicOperatorId", "Property", "xs:string", True,
                  _CDS + "economicOperatorId/1"),
    AttributeRule("facilityId", "Property", "xs:string", False,
                  _CDS + "facilityId/1"),
    AttributeRule("contentSpecificationIds", "SubmodelElementList", None, False,
                  _CDS + "contentSpecificationIds/1", kind="list"),
)
RULES_BY_ID = {r.id_short: r for r in ATTRIBUTE_RULES}

CONTENT_SPEC_ITEM_SEMANTIC_ID = _CDS + "contentSpecificationId/1"


@dataclass
class CheckerConfig:
    """All rules that depend on interpretation are configurable here."""
    required_root_collections: Tuple[str, ...] = (
        "assetAdministrationShells", "submodels", "conceptDescriptions")
    # Without these two the file cannot be inspected safely -> stop early.
    essential_collections: Tuple[str, ...] = (
        "assetAdministrationShells", "submodels")

    # How the DPP submodel is identified.
    submodel_id: Optional[str] = None                 # explicit submodel `id`
    submodel_id_short: str = "DppMetadata"            # IDTA 02099-1, 3.1
    submodel_semantic_id: str = _CDS + "dppMetadata/1"  # IDTA 02099-1, 3.1

    # semanticId verification: "strict" -> FAIL, "warn" -> WARN, "off".
    semantic_id_policy: str = "strict"

    # lastUpdate profile. IDTA 02099-1 says "Timestamp UTC-based according to
    # ISO 8601-1:2019/Amd1:2022" -> a timezone designator is required. Whether
    # a non-zero offset is acceptable is not verifiable -> require_utc=False.
    require_timezone: bool = True
    require_utc: bool = False

    # IDTA 02099-1 defines Model/Item/Batch for granularity (section 3.1).
    allowed_granularity: Optional[Tuple[str, ...]] = ("Model", "Item", "Batch")
    # Spec: no enumeration for dppStatus (only example "Active") -> None.
    allowed_status: Optional[Tuple[str, ...]] = None

    # Problem statement allows an empty list; IDTA 02099-1 3.2 says the list
    # elements are [1..*]. True = allow (reported as WARN), False = FAIL.
    allow_empty_content_spec_list: bool = True

    # idShort -> function(value) -> error message or None.
    # Intended for EN 18219 rules once the text is available.
    identifier_validators: Dict[str, Callable[[str], Optional[str]]] = field(
        default_factory=dict)

    # Optional JSON schema for full AAS validation (needs `jsonschema`).
    aas_schema_path: Optional[str] = None


@dataclass
class ComplianceReport:
    path: str
    results: List[CheckResult] = field(default_factory=list)
    validation_complete: bool = True
    incomplete_reason: str = ""

    @property
    def verdict(self) -> str:
        if not self.validation_complete or any(r.status == FAIL for r in self.results):
            return "NOT COMPLIANT"
        return "COMPLIANT"

    def layer_verdict(self, layer: str) -> str:
        rs = [r for r in self.results if r.layer == layer]
        if any(r.status == FAIL for r in rs):
            return "FAIL"
        if any(r.status in (PASS, WARN) for r in rs):
            return "PASS"
        return "NOT EVALUATED"


@dataclass
class ElementIndex:
    elements: Dict[str, dict] = field(default_factory=dict)
    duplicates: Dict[str, int] = field(default_factory=dict)   # idShort -> count


@dataclass
class DateTimeAnalysis:
    errors: List[str] = field(default_factory=list)   # syntax / calendar problems
    offset_error: Optional[str] = None
    has_timezone: Optional[bool] = None
    is_utc: Optional[bool] = None
    parsed: Optional[datetime] = None

    @property
    def well_formed(self) -> bool:
        return not self.errors and self.offset_error is None


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _jtype(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, str):
        return "string"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, dict):
        return "object"
    if isinstance(v, list):
        return "array"
    return type(v).__name__


def safe_repr(v: Any, limit: int = 80) -> str:
    try:
        s = json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        s = repr(v)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _is_nonblank_str(v: Any) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _dict_items(seq: Any) -> List[Tuple[int, dict]]:
    if not isinstance(seq, list):
        return []
    return [(i, x) for i, x in enumerate(seq) if isinstance(x, dict)]


def reference_values(ref: Any) -> List[str]:
    """All key values of an AAS Reference (empty list if malformed)."""
    if not isinstance(ref, dict) or not isinstance(ref.get("keys"), list):
        return []
    return [k["value"] for k in ref["keys"]
            if isinstance(k, dict) and isinstance(k.get("value"), str)]


def _R(check_id, name, layer, category, location, status, message,
       expected=None, actual=None, source="") -> CheckResult:
    return CheckResult(check_id, name, layer, category, location, status,
                       message, expected, actual, source)


def _summarise(problems: List[str], limit: int = 5) -> str:
    shown = "; ".join(problems[:limit])
    more = len(problems) - limit
    return shown + (f"; ... and {more} more" if more > 0 else "")


# --------------------------------------------------------------------------
# Check 1: file format
# --------------------------------------------------------------------------
def load_json_file(path: str) -> Tuple[Any, List[CheckResult]]:
    """Read and parse the file. Returns (data_or_None, results)."""
    out: List[CheckResult] = []
    loc = str(path)

    def fail(cid, name, msg, **kw):
        out.append(_R(cid, name, LAYER_JSON, CAT_INPUT, loc, FAIL, msg,
                      source=SRC_PROBLEM, **kw))
        return None, out

    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        return fail("JSON-READ", "File is readable", f"Cannot read file: {exc}")
    out.append(_R("JSON-READ", "File is readable", LAYER_JSON, CAT_INPUT, loc,
                  PASS, f"Read {len(raw)} bytes.", source=SRC_PROBLEM))

    try:
        text = raw.decode("utf-8-sig")      # tolerate a UTF-8 BOM
    except UnicodeDecodeError as exc:
        return fail("JSON-ENCODING", "File is UTF-8 text",
                    f"File is not valid UTF-8: {exc}", expected="UTF-8")

    def _reject_constant(name):
        raise ValueError(f"non-standard JSON constant {name!r}")

    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        detail = (f"line {exc.lineno}, column {exc.colno}: {exc.msg}"
                  if isinstance(exc, json.JSONDecodeError) else str(exc))
        return fail("JSON-SYNTAX", "File is syntactically valid JSON",
                    f"Invalid JSON at {detail}.")
    except RecursionError:
        return fail("JSON-SYNTAX", "File is syntactically valid JSON",
                    "JSON nesting is too deep to parse safely.")
    out.append(_R("JSON-SYNTAX", "File is syntactically valid JSON", LAYER_JSON,
                  CAT_INPUT, loc, PASS, "JSON parsed successfully.",
                  source=SRC_PROBLEM))
    return data, out


def validate_root_structure(data: Any, cfg: CheckerConfig) -> Tuple[List[CheckResult], bool]:
    """Returns (results, can_continue)."""
    out: List[CheckResult] = []
    if not isinstance(data, dict):
        out.append(_R("ROOT-OBJECT", "Root is a JSON object", LAYER_JSON,
                      CAT_INPUT, "$", FAIL,
                      "Root value must be a JSON object (AAS environment).",
                      expected="object", actual=_jtype(data), source=SRC_PROBLEM))
        return out, False
    out.append(_R("ROOT-OBJECT", "Root is a JSON object", LAYER_JSON, CAT_INPUT,
                  "$", PASS, "Root is a JSON object.", source=SRC_PROBLEM))

    can_continue = True
    for name in cfg.required_root_collections:
        loc, cid = f"$.{name}", f"ROOT-{name}"
        value = data.get(name, KeyError)
        if value is KeyError:
            out.append(_R(cid, f"Root collection '{name}' exists", LAYER_AAS,
                          CAT_STRUCT, loc, FAIL, "Required root collection is missing.",
                          expected="array of objects", actual="absent",
                          source=SRC_PROBLEM))
        elif not isinstance(value, list):
            out.append(_R(cid, f"Root collection '{name}' exists", LAYER_AAS,
                          CAT_STRUCT, loc, FAIL, "Root collection must be an array.",
                          expected="array of objects", actual=_jtype(value),
                          source=SRC_PROBLEM))
        else:
            bad = [i for i, x in enumerate(value) if not isinstance(x, dict)]
            if bad:
                out.append(_R(cid, f"Root collection '{name}' exists", LAYER_AAS,
                              CAT_STRUCT, loc, FAIL,
                              f"Array items at index {bad[:5]} are not objects.",
                              expected="array of objects", source=SRC_PROBLEM))
            else:
                out.append(_R(cid, f"Root collection '{name}' exists", LAYER_AAS,
                              CAT_STRUCT, loc, PASS,
                              f"Array of {len(value)} object(s).", source=SRC_PROBLEM))
        ok = isinstance(value, list)
        if name in cfg.essential_collections and not ok:
            can_continue = False
    return out, can_continue


# --------------------------------------------------------------------------
# Layer 2: AAS structure (lightweight) and optional schema validation
# --------------------------------------------------------------------------
def referenced_submodel_ids(data: dict) -> List[str]:
    ids: List[str] = []
    for _, aas in _dict_items(data.get("assetAdministrationShells")):
        refs = aas.get("submodels")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            keys = ref.get("keys") if isinstance(ref, dict) else None
            if isinstance(keys, list):
                ids += [k["value"] for k in keys if isinstance(k, dict)
                        and k.get("type") == "Submodel" and isinstance(k.get("value"), str)]
    return ids


def validate_aas_structure(data: dict) -> List[CheckResult]:
    out: List[CheckResult] = []
    shells = _dict_items(data.get("assetAdministrationShells"))
    loc = "$.assetAdministrationShells"
    if not shells:
        out.append(_R("AAS-PRESENT", "At least one AAS exists", LAYER_AAS, CAT_STRUCT,
                      loc, FAIL, "No Asset Administration Shell object found.",
                      expected=">= 1 object", actual="0", source=SRC_PROBLEM))
        return out

    id_problems, info_problems, ref_problems = [], [], []
    for i, aas in shells:
        if not _is_nonblank_str(aas.get("id")):
            id_problems.append(f"[{i}] 'id' missing or not a non-empty string")
        if not isinstance(aas.get("assetInformation"), dict):
            info_problems.append(f"[{i}] 'assetInformation' missing or not an object")
        refs = aas.get("submodels")
        if refs is not None and not isinstance(refs, list):
            ref_problems.append(f"[{i}] 'submodels' is {_jtype(refs)}, expected array")
        elif isinstance(refs, list):
            for j, ref in enumerate(refs):
                if not reference_values(ref):
                    ref_problems.append(f"[{i}].submodels[{j}] is not a valid reference")

    for cid, nm, probs in (("AAS-ID", "Each AAS has an 'id'", id_problems),
                           ("AAS-ASSETINFO", "Each AAS has 'assetInformation'", info_problems),
                           ("AAS-SUBMODEL-REFS", "AAS submodel references are well-formed", ref_problems)):
        out.append(_R(cid, nm, LAYER_AAS, CAT_STRUCT, loc,
                      FAIL if probs else PASS,
                      _summarise(probs) if probs else f"OK for {len(shells)} AAS object(s).",
                      source=SRC_AAS))

    sm_ids = {s.get("id") for _, s in _dict_items(data.get("submodels"))}
    dangling = [v for v in referenced_submodel_ids(data) if v not in sm_ids]
    out.append(_R("AAS-REF-RESOLVE", "AAS submodel references resolve", LAYER_AAS,
                  CAT_STRUCT, loc, FAIL if dangling else PASS,
                  ("Unresolved submodel reference(s): " + _summarise(dangling))
                  if dangling else "All submodel references resolve to a submodel.",
                  source=SRC_AAS))

    sm_problems = [f"[{i}] 'id' missing or not a non-empty string"
                   for i, s in _dict_items(data.get("submodels"))
                   if not _is_nonblank_str(s.get("id"))]
    out.append(_R("AAS-SUBMODEL-ID", "Each submodel has an 'id'", LAYER_AAS, CAT_STRUCT,
                  "$.submodels", FAIL if sm_problems else PASS,
                  _summarise(sm_problems) if sm_problems else "OK.", source=SRC_AAS))
    return out


def check_aas_schema(data: Any, cfg: CheckerConfig) -> List[CheckResult]:
    """Optional full AAS JSON-schema validation (versioned schema supplied by user)."""
    cid, nm = "AAS-SCHEMA", "AAS JSON-schema validation"
    if not cfg.aas_schema_path:
        return [_R(cid, nm, LAYER_AAS, CAT_STRUCT, "$", SKIP,
                   "No AAS JSON schema supplied (--aas-schema); full AAS schema "
                   "validation was NOT performed.", source=SRC_AAS)]
    try:
        import jsonschema                       # optional dependency
        from jsonschema import validators
    except ImportError:
        return [_R(cid, nm, LAYER_AAS, CAT_INTERNAL, "$", FAIL,
                   "A schema was requested but 'jsonschema' is not installed "
                   "(pip install jsonschema).", source=SRC_AAS)]
    try:
        schema = json.loads(Path(cfg.aas_schema_path).read_text(encoding="utf-8"))
        validator = validators.validator_for(schema)(schema)
        errors = sorted(validator.iter_errors(data), key=lambda e: list(map(str, e.path)))
    except (OSError, ValueError, jsonschema.SchemaError) as exc:
        return [_R(cid, nm, LAYER_AAS, CAT_INTERNAL, "$", FAIL,
                   f"Could not run schema validation: {exc}", source=SRC_AAS)]
    if errors:
        probs = ["/".join(map(str, e.path)) + ": " + e.message[:100] for e in errors]
        return [_R(cid, nm, LAYER_AAS, CAT_STRUCT, "$", FAIL,
                   f"{len(errors)} schema violation(s): " + _summarise(probs, 3),
                   source=SRC_AAS + f"; schema file {cfg.aas_schema_path}")]
    return [_R(cid, nm, LAYER_AAS, CAT_STRUCT, "$", PASS,
               "Document validates against the supplied schema.",
               source=SRC_AAS + f"; schema file {cfg.aas_schema_path}")]


# --------------------------------------------------------------------------
# Locating the DPP submodel and indexing its elements
# --------------------------------------------------------------------------
def find_dpp_submodel(data: dict, cfg: CheckerConfig) -> Tuple[Optional[dict], List[CheckResult]]:
    """Resolve the DppMetadata submodel without silently guessing."""
    out: List[CheckResult] = []
    cid, nm, loc = "DPP-SUBMODEL", "DPP metadata submodel can be resolved", "$.submodels"
    submodels = [s for _, s in _dict_items(data.get("submodels"))]

    def matches(s: dict) -> bool:
        if cfg.submodel_id is not None:
            return s.get("id") == cfg.submodel_id
        return (s.get("idShort") == cfg.submodel_id_short
                or cfg.submodel_semantic_id in reference_values(s.get("semanticId")))

    candidates = [s for s in submodels if matches(s)]
    if not candidates:
        what = (f"id={cfg.submodel_id!r}" if cfg.submodel_id else
                f"idShort={cfg.submodel_id_short!r} or semanticId={cfg.submodel_semantic_id!r}")
        out.append(_R(cid, nm, LAYER_AAS, CAT_STRUCT, loc, FAIL,
                      "No DPP metadata submodel found.", expected=what,
                      actual=f"{len(submodels)} submodel(s), none matching",
                      source=SRC_IDTA_SM))
        return None, out

    referenced = set(referenced_submodel_ids(data))
    ref_cands = [s for s in candidates if s.get("id") in referenced]
    pool = ref_cands if ref_cands else candidates
    if len(pool) > 1:
        ids = [safe_repr(s.get("id"), 60) for s in pool]
        out.append(_R(cid, nm, LAYER_AAS, CAT_STRUCT, loc, FAIL,
                      f"{len(pool)} candidate DPP submodels found; refusing to pick one. "
                      "Set CheckerConfig.submodel_id to disambiguate.",
                      expected="exactly 1", actual=", ".join(ids), source=SRC_IDTA_SM))
        return None, out

    chosen = pool[0]
    out.append(_R(cid, nm, LAYER_AAS, CAT_STRUCT, loc, PASS,
                  f"Resolved submodel id={safe_repr(chosen.get('id'), 70)}.",
                  source=SRC_IDTA_SM))
    out.append(_R("DPP-SUBMODEL-REF", "DPP submodel is referenced by an AAS", LAYER_AAS,
                  CAT_STRUCT, loc, PASS if ref_cands else FAIL,
                  "Referenced from an AAS 'submodels' list." if ref_cands else
                  "Submodel exists but no AAS references it.", source=SRC_AAS))
    return chosen, out


def _semantic_result(cid: str, name: str, loc: str, node: Any, expected: str,
                     cfg: CheckerConfig, source: str) -> Optional[CheckResult]:
    if cfg.semantic_id_policy == "off":
        return None
    bad = FAIL if cfg.semantic_id_policy == "strict" else WARN
    sid = node.get("semanticId") if isinstance(node, dict) else None
    vals = reference_values(sid)
    if expected in vals:
        return _R(cid, name, LAYER_IDTA, CAT_SEM, loc, PASS, "semanticId matches.",
                  source=source)
    msg = "semanticId is missing or malformed." if not vals else "semanticId does not match."
    return _R(cid, name, LAYER_IDTA, CAT_SEM, loc, bad, msg, expected=expected,
              actual=safe_repr(vals) if vals else "absent", source=source)


def check_submodel_identity(submodel: dict, cfg: CheckerConfig) -> List[CheckResult]:
    out: List[CheckResult] = []
    loc = "DppMetadata submodel"
    ok = submodel.get("idShort") == cfg.submodel_id_short
    out.append(_R("DPP-IDSHORT", "Submodel idShort", LAYER_IDTA, CAT_STRUCT, loc,
                  PASS if ok else FAIL,
                  "idShort matches." if ok else "idShort differs from specification.",
                  expected=cfg.submodel_id_short, actual=safe_repr(submodel.get("idShort")),
                  source=SRC_IDTA_SM))
    r = _semantic_result("DPP-SEMANTIC", "Submodel semanticId", loc, submodel,
                         cfg.submodel_semantic_id, cfg, SRC_IDTA_SM)
    if r:
        out.append(r)
    return out


def extract_submodel_elements(submodel: dict) -> Tuple[ElementIndex, List[CheckResult]]:
    """Index elements by idShort (never by position); detect duplicates."""
    out: List[CheckResult] = []
    idx = ElementIndex()
    loc = "submodels[DppMetadata].submodelElements"
    raw = submodel.get("submodelElements")
    if not isinstance(raw, list):
        out.append(_R("ELEM-LIST", "submodelElements is an array", LAYER_IDTA, CAT_STRUCT,
                      loc, FAIL, "'submodelElements' missing or not an array.",
                      expected="array", actual=_jtype(raw), source=SRC_AAS))
        return idx, out

    counts: Dict[str, int] = {}
    malformed = []
    for i, el in enumerate(raw):
        if not isinstance(el, dict) or not _is_nonblank_str(el.get("idShort")):
            malformed.append(i)
            continue
        counts[el["idShort"]] = counts.get(el["idShort"], 0) + 1
        idx.elements.setdefault(el["idShort"], el)
    idx.duplicates = {k: v for k, v in counts.items() if v > 1}

    out.append(_R("ELEM-LIST", "submodelElements is an array", LAYER_IDTA, CAT_STRUCT, loc,
                  FAIL if malformed else PASS,
                  (f"Items at index {malformed[:5]} are not objects with a non-empty idShort."
                   if malformed else f"{len(raw)} element(s) indexed by idShort."),
                  source=SRC_AAS))
    out.append(_R("ELEM-DUPLICATES", "idShort values are unique", LAYER_IDTA, CAT_STRUCT, loc,
                  FAIL if idx.duplicates else PASS,
                  ("Duplicate idShort values: " + _summarise([f"{k} x{v}" for k, v in idx.duplicates.items()]))
                  if idx.duplicates else "No duplicate idShort values.", source=SRC_AAS))
    return idx, out


# --------------------------------------------------------------------------
# Check 2: mandatory attributes
# --------------------------------------------------------------------------
def _has_value(el: dict) -> bool:
    v = el.get("value")
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    if isinstance(v, (list, dict)):
        return len(v) > 0
    return True


def check_mandatory_attributes(index: ElementIndex, cfg: CheckerConfig) -> List[CheckResult]:
    out: List[CheckResult] = []
    for rule in (r for r in ATTRIBUTE_RULES if r.mandatory):
        a = rule.id_short
        loc = f"submodelElements[idShort={a}]"
        mk = lambda st, msg, **kw: out.append(_R(  # noqa: E731
            f"MAND-{a}", f"Mandatory attribute '{a}' present", LAYER_IDTA, CAT_MAND,
            loc, st, msg, source=SRC_IDTA_SM + " (cardinality 1)", **kw))
        if a in index.duplicates:
            mk(FAIL, "Ambiguous: duplicate idShort.", actual=f"x{index.duplicates[a]}")
        elif a not in index.elements:
            mk(FAIL, "Mandatory attribute is missing.", expected="element present with a non-empty value",
               actual="absent")
        elif not _has_value(index.elements[a]):
            v = index.elements[a].get("value", "<no 'value' key>")
            mk(FAIL, "Element exists but its value is missing, null, empty or whitespace-only.",
               expected="non-empty value", actual=safe_repr(v))
        else:
            mk(PASS, "Present with a non-empty value.")
    return out


# --------------------------------------------------------------------------
# Check 3 + 5: structure, element types, data types, optional attributes
# --------------------------------------------------------------------------
def validate_content_specification_ids(element: dict, cfg: CheckerConfig) -> List[CheckResult]:
    """SML of Property/xs:string items. Items are objects; extract nested values."""
    out: List[CheckResult] = []
    a = "contentSpecificationIds"
    loc = f"submodelElements[idShort={a}]"

    def add(cid, nm, cat, st, msg, where=loc, **kw):
        out.append(_R(f"{cid}-{a}", nm, LAYER_IDTA, cat, where, st, msg,
                      source=SRC_IDTA_SML, **kw))

    tvle = element.get("typeValueListElement")
    add("TYPE-listElementType", "SML typeValueListElement", CAT_STRUCT,
        PASS if tvle == "Property" else FAIL,
        "List element type is Property." if tvle == "Property" else "List must contain Property elements.",
        expected="Property", actual=safe_repr(tvle))
    vtle = element.get("valueTypeListElement")
    if vtle is None:
        add("TYPE-listValueType", "SML valueTypeListElement", CAT_TYPE, WARN,
            "valueTypeListElement not declared (specification sets xs:string).", expected="xs:string",
            actual="absent")
    else:
        add("TYPE-listValueType", "SML valueTypeListElement", CAT_TYPE,
            PASS if vtle == "xs:string" else FAIL,
            "Declared list value type is xs:string." if vtle == "xs:string" else "Wrong list value type.",
            expected="xs:string", actual=safe_repr(vtle))

    items = element.get("value")
    if items is None:
        items = []
    if not isinstance(items, list):
        add("TYPE-listValue", "SML value is an array", CAT_TYPE, FAIL,
            "SubmodelElementList 'value' must be an array of Property objects.",
            expected="array of objects", actual=_jtype(items))
        return out
    add("TYPE-listValue", "SML value is an array", CAT_TYPE, PASS, f"Array with {len(items)} item(s).")

    if not items:
        st = WARN if cfg.allow_empty_content_spec_list else FAIL
        add("OPT-listNotEmpty", "SML has at least one item", CAT_OPT, st,
            "List is empty. Allowed by the project requirements; IDTA 02099-1 3.2 gives the "
            "item cardinality as [1..*]." if st == WARN else
            "List is empty but at least one item is required (IDTA 02099-1 3.2, [1..*]).",
            expected=">= 1 item (IDTA)", actual="0 items")
        return out

    problems: List[str] = []
    sem_bad: List[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"[{i}] is {_jtype(item)}, expected object")
            continue
        if item.get("modelType") != "Property":
            problems.append(f"[{i}] modelType={safe_repr(item.get('modelType'), 30)}, expected Property")
        vt = item.get("valueType")
        if vt != "xs:string":
            problems.append(f"[{i}] valueType={safe_repr(vt, 30)}, expected xs:string")
        v = item.get("value")
        if not isinstance(v, str):
            problems.append(f"[{i}] value is {_jtype(v)}, expected string")
        elif v.strip() == "":
            problems.append(f"[{i}] value is empty/whitespace")
        if cfg.semantic_id_policy != "off" and CONTENT_SPEC_ITEM_SEMANTIC_ID not in reference_values(item.get("semanticId")):
            sem_bad.append(f"[{i}]")
    add("TYPE-listItems", "SML items are Property/xs:string with non-empty string values",
        CAT_TYPE, FAIL if problems else PASS,
        _summarise(problems) if problems else f"All {len(items)} item(s) are valid.",
        where=loc + ".value", expected="Property, xs:string, non-empty string value")
    if cfg.semantic_id_policy != "off":
        add("SEM-listItems", "SML item semanticId", CAT_SEM,
            (FAIL if cfg.semantic_id_policy == "strict" else WARN) if sem_bad else PASS,
            f"Items {sem_bad[:8]} lack semanticId {CONTENT_SPEC_ITEM_SEMANTIC_ID}." if sem_bad
            else "All items carry the specified semanticId.", where=loc + ".value",
            expected=CONTENT_SPEC_ITEM_SEMANTIC_ID)
    return out


def check_attribute_types(index: ElementIndex, cfg: CheckerConfig) -> List[CheckResult]:
    out: List[CheckResult] = []
    for rule in ATTRIBUTE_RULES:
        a = rule.id_short
        el = index.elements.get(a)
        if el is None:
            continue                                   # absence handled by MAND / OPT checks
        loc = f"submodelElements[idShort={a}]"
        src = SRC_IDTA_SM

        def add(cid, nm, cat, st, msg, **kw):
            out.append(_R(f"{cid}-{a}", nm, LAYER_IDTA, cat, loc, st, msg, source=src, **kw))

        if a in index.duplicates:
            add("TYPE-modelType", "Element type", CAT_STRUCT, SKIP, "Not evaluated: duplicate idShort.")
            continue

        mt = el.get("modelType")
        if mt != rule.model_type:
            add("TYPE-modelType", "Element type (modelType)", CAT_STRUCT, FAIL,
                "Structural error: wrong AAS element type.", expected=rule.model_type, actual=safe_repr(mt))
            continue
        add("TYPE-modelType", "Element type (modelType)", CAT_STRUCT, PASS,
            f"modelType is {rule.model_type}.")

        if rule.kind == "list":
            out.extend(validate_content_specification_ids(el, cfg))
            continue

        vt = el.get("valueType")
        add("TYPE-valueType", "Declared valueType", CAT_TYPE,
            PASS if vt == rule.value_type else FAIL,
            "Declared valueType is correct." if vt == rule.value_type else
            "Type error: declared valueType is wrong or missing.",
            expected=rule.value_type, actual=safe_repr(vt) if vt is not None else "absent")

        v = el.get("value")
        if v is None:
            add("TYPE-value", "Actual value representation", CAT_TYPE, SKIP,
                "No value to type-check (see mandatory/optional check).")
        elif isinstance(v, str):
            add("TYPE-value", "Actual value representation", CAT_TYPE, PASS, "Value is a JSON string.")
        else:
            add("TYPE-value", "Actual value representation", CAT_TYPE, FAIL,
                "Type error: value must be a JSON string.", expected="string",
                actual=f"{_jtype(v)}: {safe_repr(v, 40)}")
    return out


def check_optional_attributes(index: ElementIndex, cfg: CheckerConfig) -> List[CheckResult]:
    """Check 5: absence is fine; presence is judged by the type checks above."""
    out: List[CheckResult] = []
    for rule in (r for r in ATTRIBUTE_RULES if not r.mandatory):
        a = rule.id_short
        loc = f"submodelElements[idShort={a}]"
        mk = lambda st, msg, **kw: out.append(_R(  # noqa: E731
            f"OPT-{a}", f"Optional attribute '{a}'", LAYER_IDTA, CAT_OPT, loc, st, msg,
            source=SRC_IDTA_SM + " (cardinality 0..1)", **kw))
        if a in index.duplicates:
            mk(FAIL, "Ambiguous: duplicate idShort.")
        elif a not in index.elements:
            mk(PASS, "Absent - allowed (optional); not a failure.")
        elif rule.kind == "scalar" and not _has_value(index.elements[a]):
            mk(WARN, "Present but has no/blank value; omit the element or supply a value.",
               actual=safe_repr(index.elements[a].get("value", "<no 'value' key>")))
        else:
            mk(PASS, "Present; structure/type results are reported by the TYPE checks.")
    return out


# --------------------------------------------------------------------------
# Check 4: date-time
# --------------------------------------------------------------------------
_DT_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?", re.ASCII)


def validate_datetime(value: str, cfg: CheckerConfig) -> DateTimeAnalysis:
    """
    Analyse an xs:dateTime-style string. Uses a pattern only to split components;
    validity is decided by explicit range/calendar logic (leap years included).
    Not supported: negative/5+ digit years, hour 24:00:00, leap seconds.
    """
    res = DateTimeAnalysis()
    m = _DT_RE.fullmatch(value)
    if not m:
        res.errors.append("does not match YYYY-MM-DDThh:mm:ss[.fff][Z|+hh:mm]")
        return res
    y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    frac, tz = m.group(7), m.group(8)

    if y < 1:
        res.errors.append("year 0000 is not supported")
    if not 1 <= mo <= 12:
        res.errors.append(f"month {mo:02d} out of range 01-12")
    elif y >= 1 and not 1 <= d <= calendar.monthrange(y, mo)[1]:
        res.errors.append(f"day {d:02d} invalid for {y:04d}-{mo:02d} "
                          f"(max {calendar.monthrange(y, mo)[1]})")
    elif d < 1:
        res.errors.append("day 00 out of range")
    if h > 23:
        res.errors.append(f"hour {h:02d} out of range 00-23")
    if mi > 59:
        res.errors.append(f"minute {mi:02d} out of range 00-59")
    if s > 59:
        res.errors.append(f"second {s:02d} out of range 00-59")

    res.has_timezone = tz is not None
    tzinfo = None
    if tz == "Z":
        tzinfo, res.is_utc = timezone.utc, True
    elif tz:
        sign = -1 if tz[0] == "-" else 1
        oh, om = int(tz[1:3]), int(tz[4:6])
        if om > 59 or oh > 14 or (oh == 14 and om != 0):
            res.offset_error = f"offset {tz} outside -14:00..+14:00 or minutes > 59"
        else:
            tzinfo = timezone(sign * timedelta(hours=oh, minutes=om))
            res.is_utc = (oh == 0 and om == 0)
    elif tz is None:
        res.is_utc = None

    if res.well_formed:
        micro = int((frac[1:] + "000000")[:6]) if frac else 0
        res.parsed = datetime(y, mo, d, h, mi, s, micro, tzinfo=tzinfo)
    return res


def check_datetime(index: ElementIndex, cfg: CheckerConfig) -> List[CheckResult]:
    out: List[CheckResult] = []
    a = "lastUpdate"
    loc = f"submodelElements[idShort={a}].value"
    src = SRC_IDTA_SM + " (lastUpdate: UTC-based ISO 8601 timestamp; AAS type xs:dateTime)"

    def add(cid, nm, cat, st, msg, **kw):
        out.append(_R(f"DT-{a}-{cid}", nm, LAYER_IDTA, cat, loc, st, msg, source=src, **kw))

    el = index.elements.get(a)
    if el is None or a in index.duplicates:
        add("string", "lastUpdate is a string", CAT_DT, SKIP,
            "Not evaluated: element absent or ambiguous (see mandatory checks).")
        return out
    v = el.get("value")
    if not isinstance(v, str):
        add("string", "lastUpdate is a string", CAT_DT, FAIL, "Value must be a JSON string.",
            expected="string", actual=f"{_jtype(v)}: {safe_repr(v, 40)}")
        for cid, nm in (("wellformed", "Well-formed date-time"), ("offset", "Timezone offset valid"),
                        ("profile", "Profile: timezone requirement")):
            add(cid, nm, CAT_DT, SKIP, "Not evaluated: value is not a string.")
        return out
    add("string", "lastUpdate is a string", CAT_DT, PASS, "Value is a JSON string.")

    an = validate_datetime(v, cfg)
    if an.errors:
        add("wellformed", "Well-formed date-time (syntax and calendar)", CAT_DT, FAIL,
            "Invalid timestamp: " + "; ".join(an.errors) + ".",
            expected="e.g. 2026-10-21T15:53:00Z", actual=safe_repr(v))
    else:
        add("wellformed", "Well-formed date-time (syntax and calendar)", CAT_DT, PASS,
            "Valid date and time components.", actual=safe_repr(v))

    if an.errors and an.has_timezone is None:        # syntax failure: nothing more to say
        add("offset", "Timezone offset valid", CAT_DT, SKIP, "Not evaluated: timestamp not well-formed.")
        add("profile", "Profile: timezone requirement", CAT_DT, SKIP,
            "Not evaluated: timestamp not well-formed.")
        return out
    if an.has_timezone:
        add("offset", "Timezone offset valid", CAT_DT, FAIL if an.offset_error else PASS,
            an.offset_error or "Timezone designator is valid.")
    else:
        add("offset", "Timezone offset valid", CAT_DT, SKIP, "No timezone designator present.")

    if an.errors:
        add("profile", "Profile: timezone requirement", CAT_DT, SKIP,
            "Not evaluated: timestamp not well-formed.")
        return out
    problems = []
    if cfg.require_timezone and not an.has_timezone:
        problems.append("timezone designator (Z or +hh:mm) is required but absent")
    if cfg.require_utc and an.has_timezone and not an.is_utc:
        problems.append("UTC (Z or +00:00) is required")
    add("profile", "Profile: timezone requirement", CAT_DT, FAIL if problems else PASS,
        ("Valid timestamp but violates the profile: " + "; ".join(problems) + ".") if problems
        else "Satisfies the configured timezone profile.",
        expected=("timezone-aware" + (", UTC" if cfg.require_utc else "")) if cfg.require_timezone else "any",
        actual=safe_repr(v))
    return out


# --------------------------------------------------------------------------
# Semantic IDs, value rules, identifier rules
# --------------------------------------------------------------------------
def check_semantic_ids(index: ElementIndex, cfg: CheckerConfig) -> List[CheckResult]:
    out: List[CheckResult] = []
    for rule in ATTRIBUTE_RULES:
        el = index.elements.get(rule.id_short)
        if el is None or rule.id_short in index.duplicates:
            continue
        r = _semantic_result(f"SEM-{rule.id_short}", f"semanticId of '{rule.id_short}'",
                             f"submodelElements[idShort={rule.id_short}].semanticId", el,
                             rule.semantic_id, cfg, SRC_IDTA_SM)
        if r:
            out.append(r)
    return out


def check_value_rules(index: ElementIndex, cfg: CheckerConfig) -> List[CheckResult]:
    """Enumerations - only where a verified definition exists or one is configured."""
    out: List[CheckResult] = []
    for a, allowed, src in (("granularity", cfg.allowed_granularity, SRC_IDTA_SM + " (GranularityEnum)"),
                            ("dppStatus", cfg.allowed_status, "User configuration (no enumeration in IDTA 02099-1)")):
        el = index.elements.get(a)
        if allowed is None or el is None or a in index.duplicates:
            continue
        v = el.get("value")
        if not isinstance(v, str) or not v.strip():
            continue                                  # reported by mandatory/type checks
        out.append(_R(f"VAL-{a}", f"Allowed values for '{a}'", LAYER_IDTA, CAT_VALUE,
                      f"submodelElements[idShort={a}].value", PASS if v in allowed else FAIL,
                      "Value is permitted." if v in allowed else "Value is not in the permitted set (case-sensitive).",
                      expected=" | ".join(allowed), actual=safe_repr(v), source=src))
    return out


def check_identifier_rules(index: ElementIndex, cfg: CheckerConfig) -> List[CheckResult]:
    """Layer 4: EN 18219 formats. No built-in rules (text unavailable)."""
    out: List[CheckResult] = []
    for a in ("uniqueProductIdentifier", "economicOperatorId", "facilityId"):
        el = index.elements.get(a)
        if el is None or a in index.duplicates or not isinstance(el.get("value"), str):
            continue
        loc = f"submodelElements[idShort={a}].value"
        fn = cfg.identifier_validators.get(a)
        if fn is None:
            out.append(_R(f"ID-{a}", f"EN 18219 identifier format of '{a}'", LAYER_EN, CAT_VALUE, loc,
                          SKIP, "Not evaluated: EN 18219:2026 rules are not built in (text not available); "
                                "register a validator in CheckerConfig.identifier_validators.",
                          source=SRC_EN))
            continue
        try:
            err = fn(el["value"])
        except Exception as exc:                     # a user validator must not crash the run
            err = f"validator raised {type(exc).__name__}: {exc}"
        out.append(_R(f"ID-{a}", f"EN 18219 identifier format of '{a}'", LAYER_EN, CAT_VALUE, loc,
                      FAIL if err else PASS, err or "Identifier accepted by configured validator.",
                      actual=safe_repr(el["value"]), source="User-configured validator"))
    return out


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def _skip_layers(layers, reason: str) -> List[CheckResult]:
    return [_R(f"NOTRUN-{i}", f"Layer '{l}' not run", l, CAT_INPUT, "-", SKIP, reason)
            for i, l in enumerate(layers)]


def run_compliance_checks(path: str, cfg: Optional[CheckerConfig] = None) -> ComplianceReport:
    cfg = cfg or CheckerConfig()
    rep = ComplianceReport(path=str(path))
    try:
        data, res = load_json_file(path)
        rep.results += res
        if data is None:
            rep.validation_complete = False
            rep.incomplete_reason = "File could not be read or parsed."
            rep.results += _skip_layers((LAYER_AAS, LAYER_IDTA, LAYER_EN), rep.incomplete_reason)
            return rep

        res, ok = validate_root_structure(data, cfg)
        rep.results += res
        if not ok:
            rep.validation_complete = False
            rep.incomplete_reason = "Root structure cannot be safely inspected."
            rep.results += _skip_layers((LAYER_IDTA, LAYER_EN), rep.incomplete_reason)
            return rep

        rep.results += validate_aas_structure(data)
        rep.results += check_aas_schema(data, cfg)

        sm, res = find_dpp_submodel(data, cfg)
        rep.results += res
        if sm is None:
            rep.validation_complete = False
            rep.incomplete_reason = "DPP metadata submodel could not be resolved."
            rep.results += _skip_layers((LAYER_IDTA, LAYER_EN), rep.incomplete_reason)
            return rep

        rep.results += check_submodel_identity(sm, cfg)
        index, res = extract_submodel_elements(sm)
        rep.results += res
        rep.results += check_mandatory_attributes(index, cfg)
        rep.results += check_attribute_types(index, cfg)
        rep.results += check_datetime(index, cfg)
        rep.results += check_optional_attributes(index, cfg)
        rep.results += check_semantic_ids(index, cfg)
        rep.results += check_value_rules(index, cfg)
        rep.results += check_identifier_rules(index, cfg)
    except Exception as exc:                          # last-resort safety net
        rep.validation_complete = False
        rep.incomplete_reason = f"Internal error: {type(exc).__name__}: {exc}"
        rep.results.append(_R("INTERNAL", "Checker ran without internal error", LAYER_JSON,
                              CAT_INTERNAL, "-", FAIL, rep.incomplete_reason))
    return rep


# --------------------------------------------------------------------------
# Reporting (I/O kept separate from validation)
# --------------------------------------------------------------------------
def print_report(rep: ComplianceReport, verbose: bool = False, out=None) -> None:
    out = out or sys.stdout
    w = lambda s="": print(s, file=out)  # noqa: E731
    bar = "=" * 78
    w(bar)
    w("AAS / DPP METADATA COMPLIANCE REPORT")
    w(f"File: {rep.path}")
    w(bar)
    for layer in LAYERS:
        rs = [r for r in rep.results if r.layer == layer]
        w(f"\n[Layer {layer}]  -> {rep.layer_verdict(layer)}")
        for r in rs:
            head = f"  [{r.status}] {r.check_id} | {r.category} | {r.name}"
            if r.status in (PASS, SKIP) and not verbose:
                w(f"{head}\n         {r.message}" if r.status == SKIP else head)
                continue
            w(head)
            w(f"         location: {r.location}")
            w(f"         result:   {r.message}")
            if r.expected is not None:
                w(f"         expected: {r.expected}")
            if r.actual is not None:
                w(f"         actual:   {r.actual}")
            if verbose and r.source:
                w(f"         source:   {r.source}")

    counts = {s: sum(1 for r in rep.results if r.status == s) for s in (PASS, FAIL, WARN, SKIP)}
    w("\n" + "-" * 78)
    w("Failures by category: " + (", ".join(
        f"{c}={n}" for c in (CAT_INPUT, CAT_MAND, CAT_STRUCT, CAT_TYPE, CAT_DT, CAT_SEM, CAT_VALUE, CAT_OPT, CAT_INTERNAL)
        if (n := sum(1 for r in rep.results if r.status == FAIL and r.category == c))) or "none"))
    w(f"Totals: PASS={counts[PASS]}  FAIL={counts[FAIL]}  WARN={counts[WARN]}  SKIP={counts[SKIP]}")
    if not rep.validation_complete:
        w(f"VALIDATION INCOMPLETE: {rep.incomplete_reason}")
    w(f"OVERALL RESULT: {rep.verdict}")
    if rep.verdict == "COMPLIANT":
        w("(with respect to the implemented checks only - this is not a certification; "
          "SKIPped checks were not evaluated)")
    w(bar)


def report_to_dict(rep: ComplianceReport) -> dict:
    return {"path": rep.path, "verdict": rep.verdict,
            "validation_complete": rep.validation_complete,
            "incomplete_reason": rep.incomplete_reason,
            "results": [asdict(r) for r in rep.results]}


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Check an AAS JSON file with DPP metadata.")
    p.add_argument("file", help="path to the AAS JSON file")
    p.add_argument("-v", "--verbose", action="store_true", help="show details and sources for every check")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.add_argument("--semantic-ids", choices=("strict", "warn", "off"), default="strict")
    p.add_argument("--require-utc", action="store_true", help="only accept Z / +00:00")
    p.add_argument("--allow-no-timezone", action="store_true", help="do not require a timezone")
    p.add_argument("--strict-content-spec", action="store_true",
                   help="empty contentSpecificationIds list is a FAIL (IDTA [1..*])")
    p.add_argument("--submodel-id", help="explicit id of the DPP submodel")
    p.add_argument("--aas-schema", help="AAS JSON schema file (requires 'jsonschema')")
    a = p.parse_args(argv)

    cfg = CheckerConfig(semantic_id_policy=a.semantic_ids, require_utc=a.require_utc,
                        require_timezone=not a.allow_no_timezone,
                        allow_empty_content_spec_list=not a.strict_content_spec,
                        submodel_id=a.submodel_id, aas_schema_path=a.aas_schema)
    rep = run_compliance_checks(a.file, cfg)
    if a.json:
        print(json.dumps(report_to_dict(rep), indent=2, ensure_ascii=False))
    else:
        print_report(rep, verbose=a.verbose)
    return 0 if rep.verdict == "COMPLIANT" else 1


if __name__ == "__main__":
    sys.exit(main())
