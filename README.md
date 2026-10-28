# AAS / DPP Metadata Compliance Checker

Checks an AAS JSON environment for a `DppMetadata` submodel as defined in
**IDTA 02099-1 v1.0 (28.05.2026)**. Python 3.8+, standard library only
(optional: `pip install jsonschema` for `--aas-schema`).

## Files
| File | Purpose |
|---|---|
| `dpp_checker.py` | The checker (library + CLI) |
| `demo.py` | Generates fixtures into `fixtures/`, loads them from disk, runs 4 scenarios |
| `fixtures/valid_dpp.json` | Passing case |
| `fixtures/invalid_dpp.json` | Missing attribute, invalid date, wrong types, blank value |
| `fixtures/no_timezone_dpp.json` | Valid timestamp that violates the timezone profile |
| `fixtures/malformed.json` | Truncated JSON |

## Run
```bash
python demo.py            # compact reports for all scenarios
python demo.py -v         # with details and source references
python dpp_checker.py fixtures/valid_dpp.json
python dpp_checker.py my_file.json -v --require-utc
python dpp_checker.py my_file.json --json > report.json
```
Exit code: `0` compliant, `1` not compliant. Options: `--semantic-ids {strict,warn,off}`,
`--require-utc`, `--allow-no-timezone`, `--strict-content-spec`, `--submodel-id ID`, `--aas-schema FILE`.

## Layers (reported separately)
1. **JSON** – readable, UTF-8, valid JSON, root is an object.
2. **AAS structure** – lightweight (root collections, AAS `id`/`assetInformation`, submodel references resolve, DPP submodel resolved without guessing). **This is not full AAS schema validation** unless you pass a versioned schema with `--aas-schema`.
3. **IDTA 02099-1** – mandatory/optional attributes, element types, value types, semanticIds, granularity enumeration, `lastUpdate`, `contentSpecificationIds`.
4. **EN 18223 / EN 18219** – identifier formats. **No built-in rules** (see below); each such check is reported as `SKIP`.

Overall result is `COMPLIANT` only if no check is `FAIL` and validation could run to completion. `WARN` and `SKIP` do not change the verdict, but `SKIP`s are printed.

## Source traceability
Every result carries a `source` (shown with `-v`, included in `--json`).

**Verified** against the published IDTA 02099-1 PDF (v1.0, 28.05.2026):
- Submodel `idShort` `DppMetadata`, semanticId `https://admin-shell.io/idta/cds/dppMetadata/1` (§3.1).
- Attribute table: element types, value types, cardinalities (7 mandatory, `facilityId` 0..1, `contentSpecificationIds` 0..1) and semanticIds `https://admin-shell.io/idta/cds/<name>/1` (§3.1).
- `granularity` enumeration: `Model`, `Item`, `Batch` (§3.1).
- `lastUpdate`: `xs:dateTime`, "Timestamp UTC-based according to ISO 8601-1:2019/Amd1:2022" (§3.1).
- `contentSpecificationIds`: SML, `typeValueListElement=Property`, `valueTypeListElement=xs:string`, item semanticId `.../cds/contentSpecificationId/1`, item cardinality `1..*` (§3.2).
- No schema-version format and no `dppStatus` enumeration are defined (§3.1) – so none is enforced.

**Not verified / not available:**
- **EN 18223:2026 and EN 18219:2026 full texts** (paywalled). Only what IDTA 02099-1 quotes from them is used. No identifier-format rules for `uniqueProductIdentifier`, `economicOperatorId`, `facilityId` are enforced; register your own via `CheckerConfig.identifier_validators` once the rules are confirmed.
- **AAS metamodel clauses** (IDTA-01001): the structural checks reflect general AAS v3 JSON structure; exact version and clause numbers were not verified. IDTA 02099-1 references AAS 3.2 URIs.

## Interpretation decisions (all configurable in `CheckerConfig`)
- **Empty `contentSpecificationIds` list**: the problem statement allows it, IDTA 02099-1 §3.2 says `[1..*]` items. Default: allowed, reported as `WARN`. `--strict-content-spec` makes it a `FAIL`.
- **`lastUpdate` timezone**: required (the spec says UTC-based). Whether non-zero offsets are acceptable is not clear from the spec, so they are accepted unless `--require-utc`.
- **semanticId mismatch**: `FAIL` by default (IDs come from the verified table). Use `--semantic-ids warn|off` if your data uses other semantic IDs. Note the spec's own JSON examples contain inconsistent URL spellings; the checker follows the element tables.
- **Optional attribute present with blank value**: `WARN`; wrong type: `FAIL`.
- **Date-time**: components are split with a pattern, then validated by explicit range and calendar logic (leap years included), offset range `-14:00..+14:00`. Not supported: negative or 5+ digit years, `24:00:00`, leap seconds. Syntax/calendar failures and profile (timezone) failures are separate checks.
- Example-value qualifiers are ignored. Element order is irrelevant; elements are found by `idShort`; duplicates are reported and not silently resolved; multiple DPP submodel candidates produce a failure rather than a guess.

## Not implemented
Full AAS schema validation (only via supplied schema), concept-description content checks, EN 18219 identifier rules, EN 18223 payload/schema rules, content-spec ID resolution, DPP data-carrier or access-control requirements. **This tool is not a certification of compliance with any standard.**

## Extending
Add a function `check_xyz(index, cfg) -> List[CheckResult]` and call it in `run_compliance_checks`. New attributes: add an `AttributeRule` to `ATTRIBUTE_RULES`. New identifier rules:
```python
cfg = CheckerConfig(identifier_validators={"economicOperatorId": lambda v: None if ":" in v else "needs a prefix"})
```
