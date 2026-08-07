from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

try:
    from neo4j import GraphDatabase
except Exception:  # pragma: no cover
    GraphDatabase = None

app = FastAPI(title="odoo-fg-factory", version="0.3.0")

cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:5179,http://127.0.0.1:5179").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in cors_origins if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ARTIFACT_ROOT = Path(os.getenv("ARTIFACT_ROOT", "/app/artifacts"))
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "/app/storage"))
GENERATED_ADDONS_ROOT = Path(os.getenv("GENERATED_ADDONS_ROOT", "/app/generated-addons"))
CUSTOM_ADDONS_ROOT = Path(os.getenv("CUSTOM_ADDONS_ROOT", "/app/custom_addons"))
ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
GENERATED_ADDONS_ROOT.mkdir(parents=True, exist_ok=True)
CUSTOM_ADDONS_ROOT.mkdir(parents=True, exist_ok=True)

DEFAULT_PHASES = ["P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7"]
DEFAULT_APPS = ["sales", "inventory", "manufacturing", "mrp_planning", "quality", "purchase"]


class CountSummary(BaseModel):
    nodes: int = 0
    relationships: int = 0
    dangling_relationships: int = 0
    standard_models: int = 0
    domain_value_links: int = 0
    later_phase_links: int = 0
    external_anchors: int = 0
    app_count: int = 0
    bundle_count: int = 0
    scenario_count: int = 0
    odoo_models: int = 0
    odoo_views: int = 0
    odoo_menus: int = 0
    demo_data_records: int = 0


class PhaseStatus(BaseModel):
    phase_key: str
    label: str
    status: str
    app_key: str | None = None
    prompt_exported: bool = False
    json_imported: bool = False
    normalized: bool = False
    cypher_built: bool = False
    neo4j_applied: bool = False
    odoo_generated: bool = False
    odoo_applied: bool = False
    demo_data_loaded: bool = False
    count_summary: CountSummary = Field(default_factory=CountSummary)
    warnings: list[str] = Field(default_factory=list)


class ImportSummary(BaseModel):
    import_id: str
    filename: str
    import_type: Literal["json", "zip"]
    phase: str | None = None
    status: str
    ready_for_neo4j_import: bool
    saved_path: str
    extracted_dir: str | None = None
    normalized_json_path: str | None = None
    manifest_path: str | None = None
    progress_path: str | None = None
    count_summary: CountSummary
    phase_statuses: list[PhaseStatus] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    links: dict[str, str] = Field(default_factory=dict)


class ApplyRequest(BaseModel):
    import_id: str
    dry_run: bool = True


class Neo4jApplyResult(BaseModel):
    import_id: str
    dry_run: bool
    status: str
    node_count: int
    relationship_count: int
    dangling_relationship_count: int = 0
    applied_node_count: int = 0
    applied_relationship_count: int = 0
    skipped_relationship_count: int = 0
    label_counts: dict[str, int] = Field(default_factory=dict)
    relationship_type_counts: dict[str, int] = Field(default_factory=dict)
    neo4j_uri: str | None = None
    applied_at: str | None = None
    browser_url: str | None = None
    verify_cypher: dict[str, str] = Field(default_factory=dict)


class YFilesPayload(BaseModel):
    view: str
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json_bytes(data: bytes) -> dict[str, Any]:
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc


def _load_json_path(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON file {path.name}: {exc}") from exc


def _extract_payload(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    neo = payload.get("neo4j_import_payload") or {}
    nodes = neo.get("nodes") or payload.get("nodes") or []
    rels = neo.get("relationships") or payload.get("relationships") or []
    if not isinstance(nodes, list) or not isinstance(rels, list):
        raise HTTPException(status_code=400, detail="Expected neo4j_import_payload.nodes/relationships lists")
    return nodes, rels


def _node_key(node: dict[str, Any]) -> str | None:
    return node.get("node_key") or node.get("id") or node.get("key")


def _rel_key(rel: dict[str, Any]) -> str:
    return rel.get("relationship_key") or rel.get("id") or f"rel::{uuid4()}"


def _from_key(rel: dict[str, Any]) -> str | None:
    return rel.get("from_node_key") or rel.get("source") or rel.get("from")


def _to_key(rel: dict[str, Any]) -> str | None:
    return rel.get("to_node_key") or rel.get("target") or rel.get("to")


def _labels(node: dict[str, Any]) -> list[str]:
    labels = node.get("labels") or node.get("node_labels") or []
    if isinstance(labels, str):
        labels = [labels]
    return [str(x) for x in labels]


def _node_type(node: dict[str, Any]) -> str:
    labels = _labels(node)
    for label in ["App", "Bundle", "Scenario", "OdooStandardModel", "DomainValueAnchor", "LaterPhaseConcept", "ExternalArea"]:
        if label in labels:
            return label
    return labels[0] if labels else "FGNode"




def _neo4j_property_value(value: Any) -> Any:
    """Convert arbitrary JSON values into Neo4j-safe property values.

    Neo4j properties may be scalar values or arrays of scalar values. Nested
    dictionaries / lists of objects are preserved as JSON strings so graph apply
    does not fail while keeping the source metadata inspectable.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        primitive_items = []
        primitive_only = True
        for item in value:
            if item is None:
                continue
            if isinstance(item, (str, int, float, bool)):
                primitive_items.append(item)
            else:
                primitive_only = False
                break
        if primitive_only:
            return primitive_items
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _neo4j_safe_props(props: dict[str, Any] | None) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in dict(props or {}).items():
        if not key:
            continue
        safe[str(key)] = _neo4j_property_value(value)
    return safe


_GRAPH_NODE_STRUCTURAL_KEYS = {
    "node_key",
    "id",
    "key",
    "labels",
    "node_labels",
    "properties",
}

_GRAPH_REL_STRUCTURAL_KEYS = {
    "relationship_key",
    "edge_key",
    "id",
    "from_node_key",
    "to_node_key",
    "source",
    "target",
    "from",
    "to",
    "relationship_type",
    "type",
    "properties",
}


def _infer_p3_support_master_model_from_node_key(node_key: str | None) -> str | None:
    """Infer a stable demo support model name from a P3 support-master node key."""
    if not node_key or not isinstance(node_key, str):
        return None
    parts = node_key.split("::")
    if len(parts) >= 3 and parts[0] == "support_master":
        app_key = re.sub(r"[^a-z0-9_]+", "_", parts[1].lower()).strip("_")
        master_key = re.sub(r"[^a-z0-9_]+", "_", parts[-1].lower()).strip("_")
        if app_key and master_key:
            return f"x_fg_p3_{app_key}_{master_key}"
    if len(parts) >= 2 and parts[0] == "support_master_model":
        model = parts[-1].strip()
        return model or None
    return None


def _enrich_p3_support_master_props(node: dict[str, Any], props: dict[str, Any]) -> dict[str, Any]:
    """Add mechanical aliases for P3 support-master nodes."""
    labels = set(_labels(node))
    if "P3SupportMasterDefinition" not in labels:
        return props

    node_key = str(props.get("node_key") or _node_key(node) or "")
    model = (
        props.get("model")
        or props.get("support_model")
        or props.get("target_model")
        or props.get("relation_model")
        or _infer_p3_support_master_model_from_node_key(node_key)
    )
    name = (
        props.get("name")
        or props.get("support_master_name")
        or props.get("candidate_name_ja")
        or props.get("label_ja")
        or props.get("display_name")
    )
    purpose = props.get("purpose") or props.get("business_role_ja") or "P3デモ用の簡易マスタ"

    if model:
        props.setdefault("model", model)
        props.setdefault("support_model", model)
        props.setdefault("technical_name", model)
    if name:
        props.setdefault("name", name)
        props.setdefault("support_master_name", name)
        props.setdefault("display_name", name)
    if purpose:
        props.setdefault("purpose", purpose)
    props.setdefault("support_master_kind", "p3_demo_support_master")
    props.setdefault("mechanical_alias_enriched", True)
    return props


def _graph_node_props(node: dict[str, Any]) -> dict[str, Any]:
    """Preserve explicit and top-level graph node business attributes."""
    props: dict[str, Any] = {}
    explicit = node.get("properties")
    if isinstance(explicit, dict):
        props.update(explicit)
    for key, value in node.items():
        if key in _GRAPH_NODE_STRUCTURAL_KEYS:
            continue
        props.setdefault(key, value)
    node_key = _node_key(node)
    if node_key:
        props.setdefault("node_key", node_key)
    return _enrich_p3_support_master_props(node, props)


def _graph_rel_props(rel: dict[str, Any]) -> dict[str, Any]:
    """Preserve explicit and top-level graph relationship metadata."""
    props: dict[str, Any] = {}
    explicit = rel.get("properties")
    if isinstance(explicit, dict):
        props.update(explicit)
    for key, value in rel.items():
        if key in _GRAPH_REL_STRUCTURAL_KEYS:
            continue
        props.setdefault(key, value)
    return props


def _neo4j_safe_labels(labels: list[str] | None) -> list[str]:
    safe_labels: list[str] = []
    for label in labels or []:
        safe = str(label).replace("`", "").strip()
        if safe and safe not in safe_labels:
            safe_labels.append(safe)
    if "FGNode" not in safe_labels:
        safe_labels.insert(0, "FGNode")
    return safe_labels


def _neo4j_merge_node_cypher(labels: list[str]) -> str:
    """MERGE by the constrained FGNode key only, then add labels.

    Do not MERGE using all labels (e.g. FGNode:Bundle) because an existing
    FGNode with the same node_key but without the extra label would violate the
    FGNode(node_key) uniqueness constraint. This is what caused the observed
    ConstraintValidationFailed error.
    """
    extra_labels = [x for x in labels if x != "FGNode"]
    if extra_labels:
        label_expr = ":".join("`" + x + "`" for x in extra_labels)
        return f"MERGE (n:FGNode {{node_key: $node_key}}) SET n += $props SET n:{label_expr} RETURN n.node_key AS node_key"
    return "MERGE (n:FGNode {node_key: $node_key}) SET n += $props RETURN n.node_key AS node_key"


def _calc_dangling(nodes: list[dict[str, Any]], rels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    node_keys = {_node_key(n) for n in nodes if _node_key(n)}
    return [r for r in rels if _from_key(r) not in node_keys or _to_key(r) not in node_keys]


def _slug_label(key: str) -> str:
    raw = key.split("::", 1)[-1]
    return raw.replace("_", " ").strip().title()


def _append_unique_node(nodes: list[dict[str, Any]], node: dict[str, Any]) -> bool:
    key = _node_key(node)
    if not key:
        return False
    if any(_node_key(n) == key for n in nodes):
        return False
    nodes.append(node)
    return True


def _append_unique_rel(rels: list[dict[str, Any]], rel: dict[str, Any]) -> bool:
    key = _rel_key(rel)
    if any(_rel_key(r) == key for r in rels):
        return False
    rels.append(rel)
    return True


def _repair_context_nodes(payload: dict[str, Any]) -> dict[str, Any]:
    """Materialize Bundle/Scenario context nodes missing from normalized P1 payload.

    ChatGPT-generated P1 packs may contain BUNDLE_USES_MODEL relationships
    whose from_node_key is bundle::<key> but omit the Bundle node itself.
    This repair step adds those context nodes and useful App/Scenario links so
    the payload can be safely imported into Neo4j and displayed in yFiles.
    """
    repaired = json.loads(json.dumps(payload, ensure_ascii=False))
    neo = repaired.setdefault("neo4j_import_payload", {})
    nodes = neo.setdefault("nodes", [])
    rels = neo.setdefault("relationships", [])

    node_keys = {_node_key(n) for n in nodes if _node_key(n)}
    added_nodes = 0
    added_rels = 0

    bundle_meta: dict[str, dict[str, Any]] = {}
    scenario_meta: dict[str, dict[str, Any]] = {}
    scenario_bundle_pairs: set[tuple[str, str, str | None]] = set()
    scenario_model_pairs: set[tuple[str, str, str | None]] = set()

    for usage in repaired.get("app_model_usage") or []:
        app_key = usage.get("app_key")
        model_key = usage.get("standard_model_key") or (f"odoo_model::{usage.get('odoo_model')}" if usage.get("odoo_model") else None)
        for b in usage.get("used_in_bundles") or []:
            bkey = b.get("bundle_key")
            if bkey:
                bundle_meta.setdefault(
                    f"bundle::{bkey}",
                    {
                        "bundle_key": bkey,
                        "bundle_name_ja": b.get("bundle_name_ja") or _slug_label(bkey),
                        "app_key": app_key,
                        "phase": "P1_STANDARD_REPLACEMENT_NORMALIZED",
                    },
                )
                if app_key:
                    scenario_bundle_pairs.add(("", f"bundle::{bkey}", app_key))
        for sc in usage.get("used_in_scenarios") or []:
            skey = sc.get("scenario_key")
            if skey:
                scenario_meta.setdefault(
                    f"scenario::{skey}",
                    {
                        "scenario_key": skey,
                        "scenario_name_ja": sc.get("scenario_name_ja") or _slug_label(skey),
                        "app_key": app_key,
                        "phase": "P1_STANDARD_REPLACEMENT_NORMALIZED",
                    },
                )
                if model_key:
                    scenario_model_pairs.add((f"scenario::{skey}", model_key, app_key))
                for b in usage.get("used_in_bundles") or []:
                    bkey = b.get("bundle_key")
                    if bkey:
                        scenario_bundle_pairs.add((f"scenario::{skey}", f"bundle::{bkey}", app_key))

    # Also infer missing bundle/scenario nodes directly from relationships.
    for rel in rels:
        for key in [_from_key(rel), _to_key(rel)]:
            if not key:
                continue
            props = rel.get("properties") or {}
            if key.startswith("bundle::"):
                bkey = key.split("::", 1)[1]
                bundle_meta.setdefault(
                    key,
                    {
                        "bundle_key": props.get("bundle_key") or bkey,
                        "bundle_name_ja": props.get("bundle_name_ja") or _slug_label(bkey),
                        "app_key": props.get("app_key"),
                        "phase": "P1_STANDARD_REPLACEMENT_NORMALIZED",
                    },
                )
            elif key.startswith("scenario::"):
                skey = key.split("::", 1)[1]
                scenario_meta.setdefault(
                    key,
                    {
                        "scenario_key": props.get("scenario_key") or skey,
                        "scenario_name_ja": props.get("scenario_name_ja") or _slug_label(skey),
                        "app_key": props.get("app_key"),
                        "phase": "P1_STANDARD_REPLACEMENT_NORMALIZED",
                    },
                )

    for key, props in sorted(bundle_meta.items()):
        if key not in node_keys:
            if _append_unique_node(nodes, {"node_key": key, "labels": ["Bundle"], "properties": props}):
                node_keys.add(key)
                added_nodes += 1
        app_key = props.get("app_key")
        if app_key and f"app::{app_key}" in node_keys:
            if _append_unique_rel(
                rels,
                {
                    "relationship_key": f"app::{app_key}__has_bundle__{props.get('bundle_key')}",
                    "from_node_key": f"app::{app_key}",
                    "to_node_key": key,
                    "relationship_type": "APP_HAS_BUNDLE",
                    "properties": {"app_key": app_key, "phase": "P1_STANDARD_REPLACEMENT_NORMALIZED"},
                },
            ):
                added_rels += 1

    for key, props in sorted(scenario_meta.items()):
        if key not in node_keys:
            if _append_unique_node(nodes, {"node_key": key, "labels": ["Scenario"], "properties": props}):
                node_keys.add(key)
                added_nodes += 1

    for scenario_key, bundle_key, app_key in sorted(scenario_bundle_pairs):
        if not scenario_key:
            continue
        if scenario_key in node_keys and bundle_key in node_keys:
            if _append_unique_rel(
                rels,
                {
                    "relationship_key": f"{scenario_key}__has_bundle__{bundle_key}",
                    "from_node_key": scenario_key,
                    "to_node_key": bundle_key,
                    "relationship_type": "SCENARIO_HAS_BUNDLE",
                    "properties": {"app_key": app_key, "phase": "P1_STANDARD_REPLACEMENT_NORMALIZED"},
                },
            ):
                added_rels += 1

    for scenario_key, model_key, app_key in sorted(scenario_model_pairs):
        if scenario_key in node_keys and model_key in node_keys:
            if _append_unique_rel(
                rels,
                {
                    "relationship_key": f"{scenario_key}__uses_model__{model_key}",
                    "from_node_key": scenario_key,
                    "to_node_key": model_key,
                    "relationship_type": "SCENARIO_USES_MODEL",
                    "properties": {"app_key": app_key, "phase": "P1_STANDARD_REPLACEMENT_NORMALIZED"},
                },
            ):
                added_rels += 1

    post_dangling = _calc_dangling(nodes, rels)
    validation = repaired.setdefault("validation_summary", {})
    validation["repaired_context_nodes"] = True
    validation["added_bundle_or_scenario_node_count"] = added_nodes
    validation["added_context_relationship_count"] = added_rels
    validation["dangling_relationship_count"] = len(post_dangling)
    validation["ready_for_neo4j_import"] = len(post_dangling) == 0
    return repaired


def _graph_counts(nodes: list[dict[str, Any]], rels: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, int]]:
    label_counts: dict[str, int] = {}
    rel_counts: dict[str, int] = {}
    for node in nodes:
        for label in _labels(node) or [_node_type(node)]:
            label_counts[label] = label_counts.get(label, 0) + 1
    for rel in rels:
        rt = rel.get("relationship_type") or rel.get("type") or "RELATED_TO"
        rel_counts[str(rt)] = rel_counts.get(str(rt), 0) + 1
    return label_counts, rel_counts


def _verify_cypher() -> dict[str, str]:
    return {
        "node_counts": "MATCH (n) RETURN labels(n) AS labels, count(*) AS count ORDER BY count DESC;",
        "relationship_counts": "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS count ORDER BY count DESC;",
        "p1_models": "MATCH (m:OdooStandardModel) RETURN m.model AS model, m.name_ja AS name_ja ORDER BY model;",
        "domain_value_links": "MATCH (d:DomainValueAnchor)-[:DOMAIN_VALUE_ANCHORS_TO_MODEL]->(m:OdooStandardModel) RETURN d.name_ja AS domain_value, collect(m.model) AS models ORDER BY domain_value;",
        "bundle_model_links": "MATCH (b:Bundle)-[:BUNDLE_USES_MODEL]->(m:OdooStandardModel) RETURN b.bundle_name_ja AS bundle, collect(m.model) AS models ORDER BY bundle;"
    }


def _count_summary(payload: dict[str, Any], nodes: list[dict[str, Any]], rels: list[dict[str, Any]], dangling: list[dict[str, Any]]) -> CountSummary:
    type_counts: dict[str, int] = {}
    for node in nodes:
        t = _node_type(node)
        type_counts[t] = type_counts.get(t, 0) + 1
    standard_models = type_counts.get("OdooStandardModel", 0)
    # Use explicit normalized arrays if present; fallback to node labels.
    domain_links = len(payload.get("domain_value_anchor_links") or []) or type_counts.get("DomainValueAnchor", 0)
    later_links = len(payload.get("later_phase_links") or []) or type_counts.get("LaterPhaseConcept", 0)
    external = len(payload.get("external_or_supporting_anchors") or []) or type_counts.get("ExternalArea", 0)
    return CountSummary(
        nodes=len(nodes),
        relationships=len(rels),
        dangling_relationships=len(dangling),
        standard_models=standard_models,
        domain_value_links=domain_links,
        later_phase_links=later_links,
        external_anchors=external,
        app_count=type_counts.get("App", 0),
        bundle_count=type_counts.get("Bundle", 0),
        scenario_count=type_counts.get("Scenario", 0),
        odoo_models=standard_models,
        odoo_views=0,
        odoo_menus=0,
        demo_data_records=0,
    )


def _find_first(root: Path, names: set[str]) -> Path | None:
    for p in root.rglob("*"):
        if p.is_file() and p.name in names:
            return p
    return None


def _find_normalized_json(root: Path) -> Path | None:
    candidates = sorted(root.rglob("P1_STANDARD_REPLACEMENT_NORMALIZED.json"))
    if candidates:
        return candidates[0]
    candidates = [p for p in root.rglob("*.json") if "NORMALIZED" in p.name.upper()]
    return sorted(candidates)[0] if candidates else None


def _safe_extract_zip(data: bytes, out_dir: Path) -> Path:
    zip_path = out_dir / "uploaded_pack.zip"
    zip_path.write_bytes(data)
    extract_dir = out_dir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.infolist():
                target = extract_dir / member.filename
                resolved = target.resolve()
                if not str(resolved).startswith(str(extract_dir.resolve())):
                    raise HTTPException(status_code=400, detail=f"Unsafe ZIP path: {member.filename}")
            zf.extractall(extract_dir)
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail=f"Invalid ZIP: {exc}") from exc
    return extract_dir


def _phase_label(phase_key: str) -> str:
    return {
        "P0": "Scenario Preservation",
        "P1": "Standard Replacement",
        "P2": "Standard Configuration",
        "P3": "Minor Custom",
        "P4": "Custom Model",
        "P5": "Custom Logic",
        "P6": "Diagrams",
    }.get(phase_key, phase_key)


def _build_p1_statuses(root: Path | None, summary: CountSummary, ready: bool, warnings: list[str]) -> list[PhaseStatus]:
    statuses: list[PhaseStatus] = []
    # Always show P0..P5 global lanes so 未済 is visible.
    for phase in DEFAULT_PHASES:
        if phase == "P1":
            statuses.append(
                PhaseStatus(
                    phase_key="P1",
                    label="P1 Standard Replacement Merge",
                    status="normalized" if ready else "validation_failed",
                    app_key="_merged",
                    json_imported=True,
                    normalized=True,
                    count_summary=summary,
                    warnings=warnings,
                )
            )
        else:
            statuses.append(PhaseStatus(phase_key=phase, label=_phase_label(phase), status="not_started", app_key="_global"))

    app_files: dict[str, Path] = {}
    if root and root.exists():
        for p in root.rglob("P1_STANDARD_REPLACEMENT__*.json"):
            app_key = p.stem.replace("P1_STANDARD_REPLACEMENT__", "")
            app_files[app_key] = p
    for app_key in DEFAULT_APPS:
        status = "json_imported" if app_key in app_files else "not_imported"
        statuses.append(
            PhaseStatus(
                phase_key="P1",
                label=f"P1 / {app_key}",
                status=status,
                app_key=app_key,
                json_imported=app_key in app_files,
                normalized=app_key in app_files,
                warnings=[] if app_key in app_files else ["App-level P1 JSON was not found in uploaded ZIP."],
            )
        )
    return statuses


def _links() -> dict[str, str]:
    return {
        "api": os.getenv("PUBLIC_API_URL", "http://localhost:18181"),
        "console": os.getenv("PUBLIC_CONSOLE_URL", "http://localhost:5179"),
        "neo4j_browser": os.getenv("PUBLIC_NEO4J_BROWSER_URL", "http://localhost:18474/browser/"),
        "neo4j_bolt": os.getenv("PUBLIC_NEO4J_BOLT_URL", "bolt://127.0.0.1:18687"),
        "odoo": os.getenv("PUBLIC_ODOO_URL", "http://localhost:18069"),
        "pgadmin": os.getenv("PUBLIC_PGADMIN_URL", "http://localhost:15180"),
    }


def _write_summary(out_dir: Path, summary: ImportSummary, dangling: list[dict[str, Any]], payload: dict[str, Any]) -> None:
    (out_dir / "import_summary.json").write_text(json.dumps(summary.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "phase_statuses.json").write_text(json.dumps([x.model_dump() for x in summary.phase_statuses], ensure_ascii=False, indent=2), encoding="utf-8")
    if dangling:
        (out_dir / "dangling_relationships.json").write_text(json.dumps(dangling[:500], ensure_ascii=False, indent=2), encoding="utf-8")
    # Store a compact display payload for debugging.
    display = {
        "generated_at": _now_iso(),
        "import_summary": summary.model_dump(),
        "validation_summary": payload.get("validation_summary"),
    }
    (out_dir / "display_summary.json").write_text(json.dumps(display, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "odoo-fg-factory-api",
        "version": app.version,
        "neo4j_apply_enabled": os.getenv("NEO4J_APPLY_ENABLED", "false").lower() == "true",
    }


@app.get("/links")
def public_links() -> dict[str, str]:
    return _links()


@app.post("/p1/import-pack", response_model=ImportSummary)
async def import_p1_pack(file: UploadFile = File(...)) -> ImportSummary:
    data = await file.read()
    import_id = str(uuid4())
    out_dir = ARTIFACT_ROOT / "imports" / import_id
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = file.filename or "uploaded"
    warnings: list[str] = []

    if filename.lower().endswith(".zip"):
        extracted_dir = _safe_extract_zip(data, out_dir)
        normalized_path = _find_normalized_json(extracted_dir)
        if not normalized_path:
            raise HTTPException(status_code=400, detail="P1_STANDARD_REPLACEMENT_NORMALIZED.json was not found in ZIP")
        payload = _load_json_path(normalized_path)
        saved_normalized = out_dir / "P1_STANDARD_REPLACEMENT_NORMALIZED.json"
        shutil.copy2(normalized_path, saved_normalized)
        manifest = _find_first(extracted_dir, {"MANIFEST.json"})
        progress = _find_first(extracted_dir, {"P1_PROGRESS.json"})
        import_type: Literal["json", "zip"] = "zip"
    else:
        payload = _load_json_bytes(data)
        saved_normalized = out_dir / (filename if filename.endswith(".json") else "P1_STANDARD_REPLACEMENT_NORMALIZED.json")
        saved_normalized.write_bytes(data)
        extracted_dir = None
        manifest = None
        progress = None
        import_type = "json"

    nodes, rels = _extract_payload(payload)
    dangling = _calc_dangling(nodes, rels)
    counts = _count_summary(payload, nodes, rels, dangling)
    ready = len(dangling) == 0
    if dangling:
        warnings.append(f"Dangling relationships detected: {len(dangling)}. Bundle/Scenario nodes may be missing in normalized payload.")
    if counts.bundle_count == 0 and any((r.get("relationship_type") == "BUNDLE_USES_MODEL") for r in rels):
        warnings.append("BUNDLE_USES_MODEL relationships exist, but Bundle nodes were not found.")

    phase_statuses = _build_p1_statuses(extracted_dir, counts, ready, warnings)
    status = "ready_for_neo4j" if ready else "validation_failed"
    summary = ImportSummary(
        import_id=import_id,
        filename=filename,
        import_type=import_type,
        phase=payload.get("phase") or "P1_STANDARD_REPLACEMENT_NORMALIZED",
        status=status,
        ready_for_neo4j_import=ready,
        saved_path=str(out_dir / ("uploaded_pack.zip" if import_type == "zip" else saved_normalized.name)),
        extracted_dir=str(extracted_dir) if extracted_dir else None,
        normalized_json_path=str(saved_normalized),
        manifest_path=str(manifest) if manifest else None,
        progress_path=str(progress) if progress else None,
        count_summary=counts,
        phase_statuses=phase_statuses,
        warnings=warnings,
        links=_links(),
    )
    _write_summary(out_dir, summary, dangling, payload)
    return summary


# Backward-compatible endpoint: old UI/scripts can still upload a single normalized JSON here.
@app.post("/p1/import-normalized", response_model=ImportSummary)
async def import_p1_normalized(file: UploadFile = File(...)) -> ImportSummary:
    return await import_p1_pack(file)


@app.get("/p1/imports")
def list_imports() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    root = ARTIFACT_ROOT / "imports"
    if root.exists():
        for p in sorted(root.glob("*/import_summary.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                items.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
    return {"items": items}


@app.get("/p1/imports/{import_id}")
def read_import(import_id: str) -> dict[str, Any]:
    path = ARTIFACT_ROOT / "imports" / import_id / "import_summary.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Import not found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/p1/imports/{import_id}/dangling")
def read_dangling(import_id: str) -> dict[str, Any]:
    path = ARTIFACT_ROOT / "imports" / import_id / "dangling_relationships.json"
    if not path.exists():
        return {"items": []}
    return {"items": json.loads(path.read_text(encoding="utf-8"))}


@app.post("/p1/imports/{import_id}/repair-normalized", response_model=ImportSummary)
def repair_p1_normalized(import_id: str) -> ImportSummary:
    in_dir = ARTIFACT_ROOT / "imports" / import_id
    if not in_dir.exists():
        raise HTTPException(status_code=404, detail="Import not found")
    normalized = in_dir / "P1_STANDARD_REPLACEMENT_NORMALIZED.json"
    if not normalized.exists():
        raise HTTPException(status_code=404, detail="P1_STANDARD_REPLACEMENT_NORMALIZED.json not found for import")

    original_payload = _load_json_path(normalized)
    backup = in_dir / "P1_STANDARD_REPLACEMENT_NORMALIZED.before_context_repair.json"
    if not backup.exists():
        shutil.copy2(normalized, backup)

    repaired_payload = _repair_context_nodes(original_payload)
    repaired_path = in_dir / "P1_STANDARD_REPLACEMENT_NORMALIZED.repaired.json"
    repaired_path.write_text(json.dumps(repaired_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    normalized.write_text(json.dumps(repaired_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    nodes, rels = _extract_payload(repaired_payload)
    dangling = _calc_dangling(nodes, rels)
    counts = _count_summary(repaired_payload, nodes, rels, dangling)
    ready = len(dangling) == 0
    warnings: list[str] = []
    if dangling:
        warnings.append(f"Dangling relationships remain after repair: {len(dangling)}.")

    existing_summary_path = in_dir / "import_summary.json"
    extracted_dir: Path | None = None
    filename = "P1_STANDARD_REPLACEMENT_NORMALIZED.json"
    import_type: Literal["json", "zip"] = "json"
    manifest_path = None
    progress_path = None
    if existing_summary_path.exists():
        old = json.loads(existing_summary_path.read_text(encoding="utf-8"))
        filename = old.get("filename") or filename
        import_type = old.get("import_type") or import_type
        if old.get("extracted_dir"):
            extracted_dir = Path(old["extracted_dir"])
        manifest_path = old.get("manifest_path")
        progress_path = old.get("progress_path")

    phase_statuses = _build_p1_statuses(extracted_dir, counts, ready, warnings)
    status = "ready_for_neo4j" if ready else "validation_failed"
    summary = ImportSummary(
        import_id=import_id,
        filename=filename,
        import_type=import_type,
        phase=repaired_payload.get("phase") or "P1_STANDARD_REPLACEMENT_NORMALIZED",
        status=status,
        ready_for_neo4j_import=ready,
        saved_path=str(in_dir / ("uploaded_pack.zip" if import_type == "zip" else normalized.name)),
        extracted_dir=str(extracted_dir) if extracted_dir else None,
        normalized_json_path=str(normalized),
        manifest_path=manifest_path,
        progress_path=progress_path,
        count_summary=counts,
        phase_statuses=phase_statuses,
        warnings=warnings,
        links=_links(),
    )
    _write_summary(in_dir, summary, dangling, repaired_payload)
    return summary


@app.get("/p1/imports/{import_id}/yfiles", response_model=YFilesPayload)
def p1_yfiles(import_id: str, view: str = "progress") -> YFilesPayload:
    path = ARTIFACT_ROOT / "imports" / import_id / "import_summary.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Import not found")
    summary = ImportSummary.model_validate(json.loads(path.read_text(encoding="utf-8")))
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    nodes.append({"id": "project::current", "label": "F&G Demo", "type": "project", "status": summary.status})
    for st in summary.phase_statuses:
        nid = f"phase::{st.phase_key}::{st.app_key or '_global'}"
        label = st.label if st.app_key in (None, "_global", "_merged") else f"{st.app_key}\n{st.phase_key}"
        nodes.append({"id": nid, "label": label, "type": "phase", "status": st.status, "counts": st.count_summary.model_dump()})
        edges.append({"id": f"edge::project::{nid}", "source": "project::current", "target": nid, "type": "HAS_PHASE_STATUS"})
        previous = nid
        steps = [
            ("import", st.json_imported),
            ("normalize", st.normalized),
            ("neo4j", st.neo4j_applied),
            ("odoo", st.odoo_applied),
            ("demo", st.demo_data_loaded),
        ]
        for step, done in steps:
            sid = f"step::{st.phase_key}::{st.app_key or '_global'}::{step}"
            nodes.append({"id": sid, "label": step, "type": "pipeline_step", "status": "done" if done else "pending"})
            edges.append({"id": f"edge::{previous}::{sid}", "source": previous, "target": sid, "type": "PIPELINE_NEXT"})
            previous = sid
    return YFilesPayload(view=view, nodes=nodes, edges=edges)


@app.post("/p1/apply-neo4j", response_model=Neo4jApplyResult)
def apply_neo4j(req: ApplyRequest) -> Neo4jApplyResult:
    in_dir = ARTIFACT_ROOT / "imports" / req.import_id
    normalized = in_dir / "P1_STANDARD_REPLACEMENT_NORMALIZED.json"
    if not normalized.exists():
        candidates = [p for p in in_dir.glob("*.json") if p.name not in {"import_summary.json", "dangling_relationships.json", "phase_statuses.json", "display_summary.json", "neo4j_apply_result.json"}]
        if not candidates:
            raise HTTPException(status_code=404, detail="Normalized JSON not found for import")
        normalized = candidates[0]
    payload = json.loads(normalized.read_text(encoding="utf-8"))
    nodes, rels = _extract_payload(payload)
    dangling = _calc_dangling(nodes, rels)
    if dangling:
        raise HTTPException(status_code=400, detail=f"Cannot apply to Neo4j while dangling relationships exist: {len(dangling)}")

    label_counts, rel_counts = _graph_counts(nodes, rels)
    uri = os.getenv("NEO4J_URI", "bolt://fg-neo4j:7687")
    browser_url = _links().get("neo4j_browser")
    base_result = {
        "import_id": req.import_id,
        "dry_run": req.dry_run,
        "status": "dry_run_ok" if req.dry_run else "pending",
        "node_count": len(nodes),
        "relationship_count": len(rels),
        "dangling_relationship_count": 0,
        "applied_node_count": 0,
        "applied_relationship_count": 0,
        "skipped_relationship_count": 0,
        "label_counts": label_counts,
        "relationship_type_counts": rel_counts,
        "neo4j_uri": uri,
        "applied_at": None,
        "browser_url": browser_url,
        "verify_cypher": _verify_cypher(),
    }

    if req.dry_run:
        result = Neo4jApplyResult(**base_result)
        (in_dir / "neo4j_dry_run_result.json").write_text(json.dumps(result.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    if os.getenv("NEO4J_APPLY_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=403, detail="NEO4J_APPLY_ENABLED is not true. Set it in infra/docker-compose.yml or .env, then rebuild/restart fg-api.")
    if GraphDatabase is None:
        raise HTTPException(status_code=500, detail="neo4j driver is unavailable")

    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "password")

    applied_nodes = 0
    applied_rels = 0
    skipped_rels = 0
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            # Constraints are idempotent and keep repeated P1 apply safe.
            session.run("CREATE CONSTRAINT fg_node_key_unique IF NOT EXISTS FOR (n:FGNode) REQUIRE n.node_key IS UNIQUE")
            # Also create label-specific constraints for the labels we use often. If a label is absent this is still safe.
            for label in ["App", "Scenario", "Bundle", "OdooStandardModel", "DomainValueAnchor", "LaterPhaseConcept", "ExternalArea"]:
                safe_label = label.replace("`", "")
                session.run(f"CREATE CONSTRAINT fg_{safe_label.lower()}_node_key_unique IF NOT EXISTS FOR (n:`{safe_label}`) REQUIRE n.node_key IS UNIQUE")

            for node in nodes:
                key = _node_key(node)
                if not key:
                    continue
                labels = _neo4j_safe_labels(_labels(node) or ["FGNode"])
                props = _neo4j_safe_props(_graph_node_props(node))
                props["node_key"] = key
                props["fg_phase"] = props.get("fg_phase") or "P1_STANDARD_REPLACEMENT"
                props["fg_source_import_id"] = req.import_id
                session.run(_neo4j_merge_node_cypher(labels), node_key=key, props=props)
                applied_nodes += 1

            for rel in rels:
                rk = _rel_key(rel)
                fk = _from_key(rel)
                tk = _to_key(rel)
                if not fk or not tk:
                    skipped_rels += 1
                    continue
                rt = str(rel.get("relationship_type") or rel.get("type") or "RELATED_TO").replace("`", "").strip() or "RELATED_TO"
                props = _neo4j_safe_props(_graph_rel_props(rel))
                props["relationship_key"] = rk
                props["fg_phase"] = props.get("fg_phase") or "P1_STANDARD_REPLACEMENT"
                props["fg_source_import_id"] = req.import_id
                record = session.run(
                    f"MATCH (a {{node_key: $from_key}}), (b {{node_key: $to_key}}) "
                    f"MERGE (a)-[r:`{rt}` {{relationship_key: $relationship_key}}]->(b) SET r += $props "
                    "RETURN count(r) AS cnt",
                    from_key=fk,
                    to_key=tk,
                    relationship_key=rk,
                    props=props,
                ).single()
                if record and record.get("cnt", 0) > 0:
                    applied_rels += 1
                else:
                    skipped_rels += 1
            if phase_name == "P3_NEO4J_FIRST":
                inferred_p3_support_master_edges = _apply_p3_inferred_support_master_edges(session, import_id, dry_run=False)
                applied_rels += inferred_p3_support_master_edges
    finally:
        driver.close()

    result = Neo4jApplyResult(**{
        **base_result,
        "dry_run": False,
        "status": "neo4j_applied",
        "applied_node_count": applied_nodes,
        "applied_relationship_count": applied_rels,
        "skipped_relationship_count": skipped_rels,
        "applied_at": _now_iso(),
    })
    (in_dir / "neo4j_apply_result.json").write_text(json.dumps(result.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")

    # Mark imported summary as applied.
    summary_path = in_dir / "import_summary.json"
    if summary_path.exists():
        raw = json.loads(summary_path.read_text(encoding="utf-8"))
        raw["status"] = "neo4j_applied"
        raw["ready_for_neo4j_import"] = True
        raw["neo4j_apply_result"] = result.model_dump()
        for st in raw.get("phase_statuses", []):
            if st.get("phase_key") == "P1":
                st["neo4j_applied"] = True
                st["cypher_built"] = True
                st["status"] = "neo4j_applied"
        summary_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


@app.get("/p1/imports/{import_id}/neo4j-apply-result")
def read_neo4j_apply_result(import_id: str) -> dict[str, Any]:
    path = ARTIFACT_ROOT / "imports" / import_id / "neo4j_apply_result.json"
    if not path.exists():
        dry = ARTIFACT_ROOT / "imports" / import_id / "neo4j_dry_run_result.json"
        if dry.exists():
            return json.loads(dry.read_text(encoding="utf-8"))
        raise HTTPException(status_code=404, detail="Neo4j apply result not found")
    return json.loads(path.read_text(encoding="utf-8"))



class OdooAddonResult(BaseModel):
    import_id: str
    status: str
    addon_name: str
    addon_dir: str
    custom_addons_dir: str
    zip_path: str
    download_url: str
    generated_at: str
    record_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    odoo_url: str | None = None
    install_hint: str = "Open Odoo, update Apps List, then install fg_demo_p1_overlay."


def _has_label(node: dict[str, Any], label: str) -> bool:
    return label in _labels(node)


def _props(node: dict[str, Any]) -> dict[str, Any]:
    return dict(node.get("properties") or {})


def _text(v: Any, default: str = "") -> str:
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _xml_escape(v: Any) -> str:
    import xml.sax.saxutils as saxutils
    return saxutils.escape(_text(v), {'"': '&quot;'})


def _xml_id(prefix: str, key: str) -> str:
    import hashlib
    base = re.sub(r"[^0-9a-zA-Z_]+", "_", key.lower()).strip("_")
    if not base or re.match(r"^[0-9]", base):
        base = "x_" + base
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{base[:42]}_{digest}"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _addon_py_manifest() -> str:
    return """# -*- coding: utf-8 -*-
{
    'name': 'F&G Demo P1 Overlay',
    'version': '19.0.1.0.0',
    'category': 'Tools',
    'summary': 'F&G Demo overlay for P1 standard replacement graph.',
    'description': 'Generated overlay addon from odoo-fg-factory P1 graph.',
    'depends': ['base'],
    'data': [
        'security/ir.model.access.csv',
        'data/fg_demo_p1_data.xml',
        'views/fg_demo_p1_views.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
"""


def _addon_init_files() -> dict[str, str]:
    model_init = """# -*- coding: utf-8 -*-
from . import fg_demo_app
from . import fg_demo_bundle
from . import fg_demo_standard_model
from . import fg_demo_domain_value_anchor
from . import fg_demo_later_phase_link
from . import fg_demo_external_area
from . import fg_demo_graph_relation
"""
    root_init = """# -*- coding: utf-8 -*-
from . import models
"""
    models = {
        "models/fg_demo_app.py": """# -*- coding: utf-8 -*-
from odoo import fields, models

class FgDemoApp(models.Model):
    _name = 'fg.demo.app'
    _description = 'F&G Demo App'
    _order = 'app_key'

    name = fields.Char(required=True)
    key = fields.Char(index=True)
    app_key = fields.Char(index=True)
    status = fields.Char(default='imported')
    raw_json = fields.Text()
""",
        "models/fg_demo_bundle.py": """# -*- coding: utf-8 -*-
from odoo import fields, models

class FgDemoBundle(models.Model):
    _name = 'fg.demo.bundle'
    _description = 'F&G Demo Bundle'
    _order = 'app_key, bundle_key'

    name = fields.Char(required=True)
    key = fields.Char(index=True)
    bundle_key = fields.Char(index=True)
    app_key = fields.Char(index=True)
    phase = fields.Char(default='P1')
    raw_json = fields.Text()
""",
        "models/fg_demo_standard_model.py": """# -*- coding: utf-8 -*-
from odoo import fields, models

class FgDemoStandardModel(models.Model):
    _name = 'fg.demo.standard.model'
    _description = 'F&G Demo Standard Model Anchor'
    _order = 'odoo_model'

    name = fields.Char(required=True)
    key = fields.Char(index=True)
    odoo_model = fields.Char(index=True)
    odoo_module = fields.Char()
    app_key = fields.Char(index=True)
    replacement_type = fields.Char()
    role_ja = fields.Text()
    raw_json = fields.Text()
""",
        "models/fg_demo_domain_value_anchor.py": """# -*- coding: utf-8 -*-
from odoo import fields, models

class FgDemoDomainValueAnchor(models.Model):
    _name = 'fg.demo.domain.value.anchor'
    _description = 'F&G Demo Domain Value Anchor'
    _order = 'name'

    name = fields.Char(required=True)
    key = fields.Char(index=True)
    domain_value_type = fields.Char()
    expected_later_phase = fields.Char()
    anchor_models_text = fields.Text()
    must_not_drop = fields.Boolean(default=True)
    raw_json = fields.Text()
""",
        "models/fg_demo_later_phase_link.py": """# -*- coding: utf-8 -*-
from odoo import fields, models

class FgDemoLaterPhaseLink(models.Model):
    _name = 'fg.demo.later.phase.link'
    _description = 'F&G Demo Later Phase Link'
    _order = 'expected_later_phase, name'

    name = fields.Char(required=True)
    key = fields.Char(index=True)
    expected_later_phase = fields.Char(index=True)
    anchor_models_text = fields.Text()
    reason_ja = fields.Text()
    must_not_drop = fields.Boolean(default=True)
    raw_json = fields.Text()
""",
        "models/fg_demo_external_area.py": """# -*- coding: utf-8 -*-
from odoo import fields, models

class FgDemoExternalArea(models.Model):
    _name = 'fg.demo.external.area'
    _description = 'F&G Demo External or Supporting Area'
    _order = 'name'

    name = fields.Char(required=True)
    key = fields.Char(index=True)
    treatment = fields.Char()
    connected_models_text = fields.Text()
    reason_ja = fields.Text()
    raw_json = fields.Text()
""",
        "models/fg_demo_graph_relation.py": """# -*- coding: utf-8 -*-
from odoo import fields, models

class FgDemoGraphRelation(models.Model):
    _name = 'fg.demo.graph.relation'
    _description = 'F&G Demo Graph Relation'
    _order = 'relationship_type, from_node_key'

    name = fields.Char(required=True)
    key = fields.Char(index=True)
    relationship_type = fields.Char(index=True)
    from_node_key = fields.Char(index=True)
    to_node_key = fields.Char(index=True)
    phase = fields.Char(default='P1')
    raw_json = fields.Text()
""",
    }
    return {"__init__.py": root_init, "models/__init__.py": model_init, **models}


def _addon_access_csv() -> str:
    rows = [
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink",
        "access_fg_demo_app,fg.demo.app,model_fg_demo_app,,1,1,1,1",
        "access_fg_demo_bundle,fg.demo.bundle,model_fg_demo_bundle,,1,1,1,1",
        "access_fg_demo_standard_model,fg.demo.standard.model,model_fg_demo_standard_model,,1,1,1,1",
        "access_fg_demo_domain_value_anchor,fg.demo.domain.value.anchor,model_fg_demo_domain_value_anchor,,1,1,1,1",
        "access_fg_demo_later_phase_link,fg.demo.later.phase.link,model_fg_demo_later_phase_link,,1,1,1,1",
        "access_fg_demo_external_area,fg.demo.external.area,model_fg_demo_external_area,,1,1,1,1",
        "access_fg_demo_graph_relation,fg.demo.graph.relation,model_fg_demo_graph_relation,,1,1,1,1",
    ]
    return "\n".join(rows) + "\n"


def _addon_views_xml() -> str:
    def action(model: str, xmlid: str, name: str) -> str:
        return f"""
  <record id="{xmlid}_action" model="ir.actions.act_window">
    <field name="name">{_xml_escape(name)}</field>
    <field name="res_model">{model}</field>
    <field name="view_mode">list,form</field>
  </record>
"""
    views = """<?xml version="1.0" encoding="UTF-8"?>
<odoo>
  <record id="fg_demo_app_list" model="ir.ui.view"><field name="name">fg.demo.app.list</field><field name="model">fg.demo.app</field><field name="arch" type="xml"><list><field name="app_key"/><field name="name"/><field name="status"/></list></field></record>
  <record id="fg_demo_app_form" model="ir.ui.view"><field name="name">fg.demo.app.form</field><field name="model">fg.demo.app</field><field name="arch" type="xml"><form><sheet><group><field name="name"/><field name="key"/><field name="app_key"/><field name="status"/></group><group><field name="raw_json" nolabel="1"/></group></sheet></form></field></record>
  <record id="fg_demo_bundle_list" model="ir.ui.view"><field name="name">fg.demo.bundle.list</field><field name="model">fg.demo.bundle</field><field name="arch" type="xml"><list><field name="app_key"/><field name="bundle_key"/><field name="name"/><field name="phase"/></list></field></record>
  <record id="fg_demo_bundle_form" model="ir.ui.view"><field name="name">fg.demo.bundle.form</field><field name="model">fg.demo.bundle</field><field name="arch" type="xml"><form><sheet><group><field name="name"/><field name="key"/><field name="bundle_key"/><field name="app_key"/><field name="phase"/></group><group><field name="raw_json" nolabel="1"/></group></sheet></form></field></record>
  <record id="fg_demo_standard_model_list" model="ir.ui.view"><field name="name">fg.demo.standard.model.list</field><field name="model">fg.demo.standard.model</field><field name="arch" type="xml"><list><field name="odoo_model"/><field name="name"/><field name="app_key"/><field name="replacement_type"/></list></field></record>
  <record id="fg_demo_standard_model_form" model="ir.ui.view"><field name="name">fg.demo.standard.model.form</field><field name="model">fg.demo.standard.model</field><field name="arch" type="xml"><form><sheet><group><field name="name"/><field name="key"/><field name="odoo_model"/><field name="odoo_module"/><field name="app_key"/><field name="replacement_type"/></group><group><field name="role_ja"/><field name="raw_json" nolabel="1"/></group></sheet></form></field></record>
  <record id="fg_demo_domain_value_anchor_list" model="ir.ui.view"><field name="name">fg.demo.domain.value.anchor.list</field><field name="model">fg.demo.domain.value.anchor</field><field name="arch" type="xml"><list><field name="name"/><field name="domain_value_type"/><field name="expected_later_phase"/><field name="must_not_drop"/></list></field></record>
  <record id="fg_demo_domain_value_anchor_form" model="ir.ui.view"><field name="name">fg.demo.domain.value.anchor.form</field><field name="model">fg.demo.domain.value.anchor</field><field name="arch" type="xml"><form><sheet><group><field name="name"/><field name="key"/><field name="domain_value_type"/><field name="expected_later_phase"/><field name="must_not_drop"/></group><group><field name="anchor_models_text"/><field name="raw_json" nolabel="1"/></group></sheet></form></field></record>
  <record id="fg_demo_later_phase_link_list" model="ir.ui.view"><field name="name">fg.demo.later.phase.link.list</field><field name="model">fg.demo.later.phase.link</field><field name="arch" type="xml"><list><field name="expected_later_phase"/><field name="name"/><field name="must_not_drop"/></list></field></record>
  <record id="fg_demo_later_phase_link_form" model="ir.ui.view"><field name="name">fg.demo.later.phase.link.form</field><field name="model">fg.demo.later.phase.link</field><field name="arch" type="xml"><form><sheet><group><field name="name"/><field name="key"/><field name="expected_later_phase"/><field name="must_not_drop"/></group><group><field name="anchor_models_text"/><field name="reason_ja"/><field name="raw_json" nolabel="1"/></group></sheet></form></field></record>
  <record id="fg_demo_external_area_list" model="ir.ui.view"><field name="name">fg.demo.external.area.list</field><field name="model">fg.demo.external.area</field><field name="arch" type="xml"><list><field name="name"/><field name="treatment"/><field name="connected_models_text"/></list></field></record>
  <record id="fg_demo_external_area_form" model="ir.ui.view"><field name="name">fg.demo.external.area.form</field><field name="model">fg.demo.external.area</field><field name="arch" type="xml"><form><sheet><group><field name="name"/><field name="key"/><field name="treatment"/></group><group><field name="connected_models_text"/><field name="reason_ja"/><field name="raw_json" nolabel="1"/></group></sheet></form></field></record>
  <record id="fg_demo_graph_relation_list" model="ir.ui.view"><field name="name">fg.demo.graph.relation.list</field><field name="model">fg.demo.graph.relation</field><field name="arch" type="xml"><list><field name="relationship_type"/><field name="from_node_key"/><field name="to_node_key"/><field name="phase"/></list></field></record>
  <record id="fg_demo_graph_relation_form" model="ir.ui.view"><field name="name">fg.demo.graph.relation.form</field><field name="model">fg.demo.graph.relation</field><field name="arch" type="xml"><form><sheet><group><field name="name"/><field name="key"/><field name="relationship_type"/><field name="from_node_key"/><field name="to_node_key"/><field name="phase"/></group><group><field name="raw_json" nolabel="1"/></group></sheet></form></field></record>
"""
    views += action("fg.demo.app", "fg_demo_app", "F&G Apps")
    views += action("fg.demo.bundle", "fg_demo_bundle", "F&G Bundles")
    views += action("fg.demo.standard.model", "fg_demo_standard_model", "P1 Standard Models")
    views += action("fg.demo.domain.value.anchor", "fg_demo_domain_value_anchor", "Domain Value Anchors")
    views += action("fg.demo.later.phase.link", "fg_demo_later_phase_link", "Later Phase Links")
    views += action("fg.demo.external.area", "fg_demo_external_area", "External Areas")
    views += action("fg.demo.graph.relation", "fg_demo_graph_relation", "Graph Relations")
    views += """
  <menuitem id="fg_demo_root_menu" name="F&amp;G Demo" sequence="5"/>
  <menuitem id="fg_demo_p1_menu" name="P1 Standard Replacement" parent="fg_demo_root_menu" sequence="10"/>
  <menuitem id="fg_demo_app_menu" name="Apps" parent="fg_demo_p1_menu" action="fg_demo_app_action" sequence="10"/>
  <menuitem id="fg_demo_bundle_menu" name="Bundles" parent="fg_demo_p1_menu" action="fg_demo_bundle_action" sequence="20"/>
  <menuitem id="fg_demo_standard_model_menu" name="Standard Models" parent="fg_demo_p1_menu" action="fg_demo_standard_model_action" sequence="30"/>
  <menuitem id="fg_demo_domain_value_anchor_menu" name="Domain Value Anchors" parent="fg_demo_p1_menu" action="fg_demo_domain_value_anchor_action" sequence="40"/>
  <menuitem id="fg_demo_later_phase_link_menu" name="Later Phase Links" parent="fg_demo_p1_menu" action="fg_demo_later_phase_link_action" sequence="50"/>
  <menuitem id="fg_demo_external_area_menu" name="External Areas" parent="fg_demo_p1_menu" action="fg_demo_external_area_action" sequence="60"/>
  <menuitem id="fg_demo_graph_relation_menu" name="Graph Relations" parent="fg_demo_p1_menu" action="fg_demo_graph_relation_action" sequence="70"/>
</odoo>
"""
    return views


def _record(model: str, rec_id: str, fields: dict[str, Any]) -> str:
    lines = [f'  <record id="{rec_id}" model="{model}">']
    for name, value in fields.items():
        if isinstance(value, bool):
            lines.append(f'    <field name="{name}">{"1" if value else "0"}</field>')
        else:
            lines.append(f'    <field name="{name}">{_xml_escape(value)}</field>')
    lines.append('  </record>')
    return "\n".join(lines)


def _addon_data_xml(payload: dict[str, Any], nodes: list[dict[str, Any]], rels: list[dict[str, Any]]) -> tuple[str, dict[str, int]]:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<odoo noupdate="1">']
    counts = {"apps": 0, "bundles": 0, "standard_models": 0, "domain_values": 0, "later_links": 0, "external_areas": 0, "relations": 0}
    seen: set[str] = set()

    for node in nodes:
        key = _node_key(node) or ""
        props = _props(node)
        raw = json.dumps(node, ensure_ascii=False)
        if _has_label(node, "App"):
            rec_id = _xml_id("app", key)
            if rec_id in seen: continue
            seen.add(rec_id); counts["apps"] += 1
            app_key = props.get("app_key") or key.split("::")[-1]
            parts.append(_record("fg.demo.app", rec_id, {"name": props.get("name") or props.get("app_label") or app_key, "key": key, "app_key": app_key, "status": "imported", "raw_json": raw}))
        elif _has_label(node, "Bundle"):
            rec_id = _xml_id("bundle", key)
            if rec_id in seen: continue
            seen.add(rec_id); counts["bundles"] += 1
            parts.append(_record("fg.demo.bundle", rec_id, {"name": props.get("bundle_name_ja") or props.get("name_ja") or _slug_label(key), "key": key, "bundle_key": props.get("bundle_key") or key.split("::")[-1], "app_key": props.get("app_key") or "", "phase": props.get("phase") or "P1", "raw_json": raw}))
        elif _has_label(node, "OdooStandardModel"):
            rec_id = _xml_id("std_model", key)
            if rec_id in seen: continue
            seen.add(rec_id); counts["standard_models"] += 1
            model = props.get("model") or props.get("odoo_model") or key.split("::")[-1]
            parts.append(_record("fg.demo.standard.model", rec_id, {"name": props.get("name_ja") or props.get("name") or model, "key": key, "odoo_model": model, "odoo_module": props.get("odoo_module") or props.get("module") or "", "app_key": props.get("app_key") or "", "replacement_type": props.get("replacement_type") or "", "role_ja": props.get("role_ja") or props.get("odoo_model_role_ja") or "", "raw_json": raw}))
        elif _has_label(node, "DomainValueAnchor"):
            rec_id = _xml_id("domain", key)
            if rec_id in seen: continue
            seen.add(rec_id); counts["domain_values"] += 1
            parts.append(_record("fg.demo.domain.value.anchor", rec_id, {"name": props.get("domain_value_name_ja") or props.get("name_ja") or props.get("name") or _slug_label(key), "key": key, "domain_value_type": props.get("domain_value_type") or "", "expected_later_phase": props.get("expected_later_phase") or "", "anchor_models_text": _text(props.get("anchor_odoo_models") or props.get("anchor_models") or ""), "must_not_drop": props.get("must_not_drop", True), "raw_json": raw}))
        elif _has_label(node, "LaterPhaseConcept"):
            rec_id = _xml_id("later", key)
            if rec_id in seen: continue
            seen.add(rec_id); counts["later_links"] += 1
            parts.append(_record("fg.demo.later.phase.link", rec_id, {"name": props.get("concept_name_ja") or props.get("name_ja") or props.get("name") or _slug_label(key), "key": key, "expected_later_phase": props.get("expected_later_phase") or "", "anchor_models_text": _text(props.get("anchor_odoo_models") or props.get("anchor_models") or ""), "reason_ja": props.get("why_not_absorbed_in_p1_ja") or props.get("reason_ja") or "", "must_not_drop": props.get("must_not_drop", True), "raw_json": raw}))
        elif _has_label(node, "ExternalArea"):
            rec_id = _xml_id("external", key)
            if rec_id in seen: continue
            seen.add(rec_id); counts["external_areas"] += 1
            parts.append(_record("fg.demo.external.area", rec_id, {"name": props.get("area_name_ja") or props.get("name_ja") or props.get("name") or _slug_label(key), "key": key, "treatment": props.get("treatment") or "", "connected_models_text": _text(props.get("connected_to_models") or props.get("connected_models") or ""), "reason_ja": props.get("reason_ja") or "", "raw_json": raw}))

    for rel in rels:
        rk = _rel_key(rel)
        rec_id = _xml_id("rel", rk)
        if rec_id in seen: continue
        seen.add(rec_id); counts["relations"] += 1
        parts.append(_record("fg.demo.graph.relation", rec_id, {"name": rk, "key": rk, "relationship_type": rel.get("relationship_type") or rel.get("type") or "RELATED_TO", "from_node_key": _from_key(rel) or "", "to_node_key": _to_key(rel) or "", "phase": (rel.get("properties") or {}).get("phase") or "P1", "raw_json": json.dumps(rel, ensure_ascii=False)}))

    parts.append('</odoo>')
    return "\n".join(parts) + "\n", counts


def _zip_dir(src: Path, dst_zip: Path) -> None:
    if dst_zip.exists():
        dst_zip.unlink()
    with zipfile.ZipFile(dst_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in src.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(src.parent))


def _generate_odoo_addon(import_id: str) -> OdooAddonResult:
    in_dir = ARTIFACT_ROOT / "imports" / import_id
    normalized = in_dir / "P1_STANDARD_REPLACEMENT_NORMALIZED.json"
    if not normalized.exists():
        raise HTTPException(status_code=404, detail="P1 normalized JSON not found")
    payload = _load_json_path(normalized)
    nodes, rels = _extract_payload(payload)
    dangling = _calc_dangling(nodes, rels)
    if dangling:
        raise HTTPException(status_code=400, detail=f"Cannot generate Odoo addon while dangling relationships exist: {len(dangling)}")

    addon_name = "fg_demo_p1_overlay"
    generated_dir = GENERATED_ADDONS_ROOT / addon_name
    custom_dir = CUSTOM_ADDONS_ROOT / addon_name
    for d in [generated_dir, custom_dir]:
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    files = _addon_init_files()
    files["__manifest__.py"] = _addon_py_manifest()
    files["security/ir.model.access.csv"] = _addon_access_csv()
    files["views/fg_demo_p1_views.xml"] = _addon_views_xml()
    data_xml, counts = _addon_data_xml(payload, nodes, rels)
    files["data/fg_demo_p1_data.xml"] = data_xml
    files["README.md"] = f"""# F&G Demo P1 Overlay

Generated from import `{import_id}` at {_now_iso()}.

This addon is an F&G demo overlay. It does not modify Odoo standard business models.

Install flow:
1. Restart Odoo if needed.
2. Open Apps.
3. Update Apps List.
4. Search `F&G Demo P1 Overlay` or `fg_demo_p1_overlay`.
5. Install.
"""
    for rel_path, content in files.items():
        _write(generated_dir / rel_path, content)
    shutil.copytree(generated_dir, custom_dir, dirs_exist_ok=True)

    zip_path = GENERATED_ADDONS_ROOT / f"{addon_name}.zip"
    _zip_dir(generated_dir, zip_path)

    result = OdooAddonResult(
        import_id=import_id,
        status="odoo_addon_generated",
        addon_name=addon_name,
        addon_dir=str(generated_dir),
        custom_addons_dir=str(custom_dir),
        zip_path=str(zip_path),
        download_url=f"/p1/imports/{import_id}/odoo-addon/download",
        generated_at=_now_iso(),
        record_counts=counts,
        warnings=[],
        odoo_url=_links().get("odoo"),
    )
    (in_dir / "odoo_addon_result.json").write_text(json.dumps(result.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")

    # Update import summary for UI.
    summary_path = in_dir / "import_summary.json"
    if summary_path.exists():
        raw = json.loads(summary_path.read_text(encoding="utf-8"))
        raw["status"] = "odoo_addon_generated"
        raw["odoo_addon_result"] = result.model_dump()
        cs = raw.setdefault("count_summary", {})
        cs["odoo_models"] = 7
        cs["odoo_views"] = 14
        cs["odoo_menus"] = 8
        for st in raw.get("phase_statuses", []):
            if st.get("phase_key") == "P1":
                st["odoo_generated"] = True
                if st.get("status") == "neo4j_applied":
                    st["status"] = "odoo_addon_generated"
        summary_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


@app.post("/p1/imports/{import_id}/generate-odoo-addon", response_model=OdooAddonResult)
def generate_p1_odoo_addon(import_id: str) -> OdooAddonResult:
    return _generate_odoo_addon(import_id)


@app.get("/p1/imports/{import_id}/odoo-addon")
def read_p1_odoo_addon(import_id: str) -> dict[str, Any]:
    path = ARTIFACT_ROOT / "imports" / import_id / "odoo_addon_result.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Odoo addon result not found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/p1/imports/{import_id}/odoo-addon/download")
def download_p1_odoo_addon(import_id: str) -> FileResponse:
    path = ARTIFACT_ROOT / "imports" / import_id / "odoo_addon_result.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Odoo addon result not found")
    result = json.loads(path.read_text(encoding="utf-8"))
    zip_path = Path(result.get("zip_path") or "")
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Generated addon ZIP not found")
    return FileResponse(str(zip_path), filename=zip_path.name, media_type="application/zip")


# ---------------------------------------------------------------------------
# P1/P2 GAP-aware combined import endpoints (MVP7 / P2-5179)
# ---------------------------------------------------------------------------
# Policy:
# - core payload is the only Neo4j/Odoo auto-generation target.
# - F&G GAP payload is report-only and must not be auto-connected.
# - context repair only materializes explicit Bundle/Scenario endpoints already
#   present in core relationships. It does not perform semantic remapping.

P1P2_IMPORT_ROOT_NAME = "p1p2_imports"
P1P2_COMBINED_FILENAME = "P1P2_GAP_AWARE_COMBINED_IMPORT.json"


def _p1p2_root() -> Path:
    root = ARTIFACT_ROOT / P1P2_IMPORT_ROOT_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _p1p2_import_dir(import_id: str) -> Path:
    return _p1p2_root() / import_id


def _find_p1p2_combined_json(root: Path) -> Path | None:
    exact = sorted(root.rglob(P1P2_COMBINED_FILENAME))
    if exact:
        return exact[0]
    candidates = [p for p in root.rglob("*.json") if "GAP_AWARE_COMBINED" in p.name.upper()]
    return sorted(candidates)[0] if candidates else None


def _combined_core_payload(combined: dict[str, Any]) -> dict[str, Any]:
    core = combined.get("neo4j_import_payload_core") or {}
    return {
        "phase": "P1P2_GAP_AWARE_CORE",
        "artifact_type": "P1P2_GAP_AWARE_CORE",
        "neo4j_import_payload": {
            "description": core.get("description") or "P1/P2 core payload for Neo4j/Odoo auto-generation.",
            "nodes": core.get("nodes") or [],
            "relationships": core.get("relationships") or [],
        },
    }


def _p1p2_gap_payload(combined: dict[str, Any]) -> dict[str, Any]:
    return combined.get("fg_gap_report_payload") or {
        "description": "No F&G GAP payload found.",
        "gap_entries": [],
        "skipped_relationships": [],
        "gap_nodes": [],
        "gap_relationships": [],
    }


def _p1p2_summary_counts(
    combined: dict[str, Any],
    core_nodes: list[dict[str, Any]],
    core_rels: list[dict[str, Any]],
    dangling: list[dict[str, Any]],
) -> dict[str, Any]:
    gap_payload = _p1p2_gap_payload(combined)
    src_summary = combined.get("summary") or {}
    return {
        "core_nodes": len(core_nodes),
        "core_relationships": len(core_rels),
        "core_dangling_relationships": len(dangling),
        "gap_entries": len(gap_payload.get("gap_entries") or []),
        "skipped_relationships": len(gap_payload.get("skipped_relationships") or []),
        "gap_nodes": len(gap_payload.get("gap_nodes") or []),
        "gap_relationships": len(gap_payload.get("gap_relationships") or []),
        "ambiguous_gap_entries": src_summary.get("ambiguous_gap_entries")
        or sum(1 for x in gap_payload.get("gap_entries") or [] if x.get("gap_type") == "ambiguous_reference"),
        "stub_required_gap_entries": src_summary.get("stub_required_gap_entries")
        or sum(1 for x in gap_payload.get("gap_entries") or [] if x.get("gap_type") == "stub_required_reference"),
        "p1_nodes": src_summary.get("p1_nodes"),
        "p2_new_nodes": src_summary.get("p2_new_nodes"),
        "p2_relationships_apply_safe": src_summary.get("p2_relationships_apply_safe"),
        "p2_relationships_skipped_as_gap": src_summary.get("p2_relationships_skipped_as_gap"),
    }


def _p1p2_phase_statuses(
    core_counts: CountSummary,
    ready: bool,
    warnings: list[str],
    gap_counts: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "phase_key": "P1/P2",
            "label": "P1/P2 Combined Core",
            "status": "ready_for_core_apply" if ready else "validation_failed",
            "app_key": "_combined",
            "json_imported": True,
            "normalized": True,
            "cypher_built": ready,
            "neo4j_applied": False,
            "odoo_generated": False,
            "odoo_applied": False,
            "demo_data_loaded": False,
            "count_summary": core_counts.model_dump(),
            "warnings": warnings,
        },
        {
            "phase_key": "F&G GAP",
            "label": "F&G GAP / Report-only",
            "status": "detected_skipped" if gap_counts.get("gap_entries", 0) else "none",
            "app_key": "_report",
            "json_imported": True,
            "normalized": True,
            "cypher_built": False,
            "neo4j_applied": False,
            "odoo_generated": False,
            "odoo_applied": False,
            "demo_data_loaded": False,
            "count_summary": CountSummary(
                nodes=gap_counts.get("gap_nodes", 0),
                relationships=gap_counts.get("gap_relationships", 0),
            ).model_dump(),
            "warnings": [
                "GAP entries are intentionally excluded from Neo4j core apply and Odoo overlay generation.",
                "Use these entries as F&G report / future development candidates.",
            ],
        },
    ]


def _write_p1p2_summary(
    out_dir: Path,
    summary: dict[str, Any],
    combined: dict[str, Any],
    core_payload: dict[str, Any],
    dangling: list[dict[str, Any]],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "import_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "P1P2_CORE_PAYLOAD.json").write_text(
        json.dumps(core_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "P1P2_FG_GAP_PAYLOAD.json").write_text(
        json.dumps(_p1p2_gap_payload(combined), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "dangling_relationships.json").write_text(
        json.dumps(dangling[:500], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_p1p2_import_summary(
    import_id: str,
    filename: str,
    import_type: str,
    out_dir: Path,
    extracted_dir: Path | None,
    combined: dict[str, Any],
    status_override: str | None = None,
) -> dict[str, Any]:
    core_payload = _combined_core_payload(combined)
    core_nodes, core_rels = _extract_payload(core_payload)
    dangling = _calc_dangling(core_nodes, core_rels)
    counts = _count_summary(core_payload, core_nodes, core_rels, dangling)
    gap_counts = _p1p2_summary_counts(combined, core_nodes, core_rels, dangling)
    warnings: list[str] = []
    if dangling:
        warnings.append(
            f"Core dangling relationships detected: {len(dangling)}. Run context repair if they are explicit Bundle/Scenario endpoints."
        )
    if gap_counts.get("gap_entries"):
        warnings.append(
            f"F&G GAP entries detected and kept report-only: {gap_counts['gap_entries']} items / {gap_counts.get('skipped_relationships', 0)} skipped relationships."
        )
    ready = len(dangling) == 0
    status = status_override or ("ready_for_core_apply" if ready else "context_repair_required")
    summary = {
        "import_id": import_id,
        "filename": filename,
        "import_type": import_type,
        "phase": "P1P2_GAP_AWARE_COMBINED",
        "status": status,
        "ready_for_neo4j_import": ready,
        "ready_for_core_apply": ready,
        "ready_for_odoo_overlay_generation": ready,
        "saved_path": str(out_dir / ("uploaded_pack.zip" if import_type == "zip" else P1P2_COMBINED_FILENAME)),
        "extracted_dir": str(extracted_dir) if extracted_dir else None,
        "combined_json_path": str(out_dir / P1P2_COMBINED_FILENAME),
        "normalized_json_path": str(out_dir / "P1P2_CORE_PAYLOAD.json"),
        "count_summary": counts.model_dump(),
        "p1p2_summary": gap_counts,
        "phase_statuses": _p1p2_phase_statuses(counts, ready, warnings, gap_counts),
        "warnings": warnings,
        "links": {**_links(), "fg_gaps": f"/p1p2/imports/{import_id}/fg-gaps"},
    }
    _write_p1p2_summary(out_dir, summary, combined, core_payload, dangling)
    return summary


def _materialize_p1p2_context_nodes(core_payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    repaired = json.loads(json.dumps(core_payload, ensure_ascii=False))
    nodes, rels = _extract_payload(repaired)
    node_keys = {_node_key(n) for n in nodes if _node_key(n)}
    added: list[dict[str, Any]] = []
    missing_keys: set[str] = set()
    for rel in rels:
        for key in (_from_key(rel), _to_key(rel)):
            if key and key not in node_keys and (key.startswith("bundle::") or key.startswith("scenario::")):
                missing_keys.add(key)
    for key in sorted(missing_keys):
        if key.startswith("bundle::"):
            label = "Bundle"
            prop_key = "bundle_key"
        else:
            label = "Scenario"
            prop_key = "scenario_key"
        short = key.split("::", 1)[-1]
        node = {
            "node_key": key,
            "labels": [label, "P1P2MechanicalContextNode"],
            "properties": {
                prop_key: short,
                "name": _slug_label(key),
                "phase": "P1P2_GAP_AWARE_CONTEXT_REPAIR",
                "repair_type": "mechanical_context_materialization",
                "is_context_repair_node": True,
                "semantic_remap_performed": False,
                "gap_item_repaired": False,
                "repair_reason": "Relationship endpoint was explicit in core payload, but the context node was missing from nodes.",
            },
        }
        nodes.append(node)
        node_keys.add(key)
        added.append(node)
    repaired["neo4j_import_payload"]["nodes"] = nodes
    repaired.setdefault("validation_summary", {})["p1p2_context_repair"] = {
        "added_context_nodes": len(added),
        "added_node_keys": [x["node_key"] for x in added],
        "semantic_remap_performed": False,
        "gap_items_repaired": False,
    }
    return repaired, added


@app.post("/p1p2/import-gap-aware-pack")
async def import_p1p2_gap_aware_pack(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read()
    import_id = str(uuid4())
    out_dir = _p1p2_import_dir(import_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = file.filename or "uploaded"
    if filename.lower().endswith(".zip"):
        extracted_dir = _safe_extract_zip(data, out_dir)
        combined_path = _find_p1p2_combined_json(extracted_dir)
        if not combined_path:
            raise HTTPException(status_code=400, detail=f"{P1P2_COMBINED_FILENAME} was not found in ZIP")
        combined = _load_json_path(combined_path)
        shutil.copy2(combined_path, out_dir / P1P2_COMBINED_FILENAME)
        import_type = "zip"
    else:
        combined = _load_json_bytes(data)
        (out_dir / P1P2_COMBINED_FILENAME).write_bytes(data)
        extracted_dir = None
        import_type = "json"
    return _build_p1p2_import_summary(import_id, filename, import_type, out_dir, extracted_dir, combined)


@app.get("/p1p2/imports")
def list_p1p2_imports() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    root = _p1p2_root()
    for p in sorted(root.glob("*/import_summary.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            items.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return {"items": items}


@app.get("/p1p2/imports/{import_id}")
def read_p1p2_import(import_id: str) -> dict[str, Any]:
    path = _p1p2_import_dir(import_id) / "import_summary.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="P1/P2 import not found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/p1p2/imports/{import_id}/fg-gaps")
def read_p1p2_fg_gaps(import_id: str) -> dict[str, Any]:
    in_dir = _p1p2_import_dir(import_id)
    gap_path = in_dir / "P1P2_FG_GAP_PAYLOAD.json"
    if not gap_path.exists():
        raise HTTPException(status_code=404, detail="F&G GAP payload not found")
    return json.loads(gap_path.read_text(encoding="utf-8"))


@app.get("/p1p2/imports/{import_id}/fg-gaps.md")
def read_p1p2_fg_gaps_md(import_id: str) -> FileResponse:
    in_dir = _p1p2_import_dir(import_id)
    md_path = in_dir / "P1P2_FG_GAP_REPORT.md"
    if not md_path.exists():
        gap_payload = json.loads((in_dir / "P1P2_FG_GAP_PAYLOAD.json").read_text(encoding="utf-8"))
        lines = ["# P1/P2 F&G GAP Report", "", "Detected but skipped from Odoo auto-generation.", ""]
        for entry in gap_payload.get("gap_entries") or []:
            lines.extend(
                [
                    f"## {entry.get('source_node_key') or entry.get('gap_key')}",
                    f"- gap_type: {entry.get('gap_type')}",
                    f"- status: {entry.get('gap_status')}",
                    f"- skip_reason_ja: {entry.get('skip_reason_ja')}",
                    f"- probable_meaning_ja: {entry.get('probable_meaning_ja')}",
                    f"- customer_report_message_ja: {entry.get('customer_report_message_ja')}",
                    "",
                ]
            )
        md_path.write_text("\n".join(lines), encoding="utf-8")
    return FileResponse(str(md_path), filename=md_path.name, media_type="text/markdown")


@app.post("/p1p2/imports/{import_id}/repair-context")
def repair_p1p2_context(import_id: str) -> dict[str, Any]:
    in_dir = _p1p2_import_dir(import_id)
    combined_path = in_dir / P1P2_COMBINED_FILENAME
    if not combined_path.exists():
        raise HTTPException(status_code=404, detail="Combined JSON not found")
    combined = _load_json_path(combined_path)
    core_payload = _combined_core_payload(combined)
    before_nodes, before_rels = _extract_payload(core_payload)
    before_dangling = _calc_dangling(before_nodes, before_rels)
    repaired_core, added_nodes = _materialize_p1p2_context_nodes(core_payload)
    after_nodes, after_rels = _extract_payload(repaired_core)
    after_dangling = _calc_dangling(after_nodes, after_rels)
    # Store repaired core inside combined while leaving GAP payload untouched.
    combined["neo4j_import_payload_core"] = repaired_core["neo4j_import_payload"]
    combined.setdefault("validation", {})["context_repair"] = {
        "before_dangling_count": len(before_dangling),
        "after_dangling_count": len(after_dangling),
        "added_context_nodes": len(added_nodes),
        "added_node_keys": [x["node_key"] for x in added_nodes],
        "semantic_remap_performed": False,
        "gap_items_repaired": False,
    }
    backup = in_dir / "P1P2_GAP_AWARE_COMBINED_IMPORT.before_context_repair.json"
    if not backup.exists():
        shutil.copy2(combined_path, backup)
    combined_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = _build_p1p2_import_summary(
        import_id,
        "P1P2_GAP_AWARE_COMBINED_DATA_PACK.zip",
        "zip",
        in_dir,
        None,
        combined,
        status_override="ready_for_core_apply" if not after_dangling else "validation_failed",
    )
    result = {
        "import_id": import_id,
        "status": summary["status"],
        "before_dangling_count": len(before_dangling),
        "after_dangling_count": len(after_dangling),
        "added_context_nodes": len(added_nodes),
        "added_node_keys": [x["node_key"] for x in added_nodes],
        "summary": summary,
    }
    (in_dir / "context_repair_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result



def _apply_p3_inferred_support_master_edges(session: Any, import_id: str, dry_run: bool) -> int:
    """Mechanically connect P3 many2one fields to support masters.

    This is the permanent A-step fix for P3 V4+ payloads where a
    P3OverlayFieldCandidate has relation_model and the corresponding
    P3SupportMasterDefinition has model.  It does not infer semantics.  It
    only joins when import_id, app_key, and technical model name match.
    """
    count_query = """
    MATCH (f:P3OverlayFieldCandidate)
    WHERE f.fg_source_import_id = $import_id
      AND f.suggested_ttype = 'many2one'
      AND f.relation_model IS NOT NULL
      AND f.relation_model <> ''
    MATCH (s:P3SupportMasterDefinition)
    WHERE s.fg_source_import_id = f.fg_source_import_id
      AND s.app_key = f.app_key
      AND s.model = f.relation_model
    RETURN count(*) AS cnt
    """
    write_query = """
    MATCH (f:P3OverlayFieldCandidate)
    WHERE f.fg_source_import_id = $import_id
      AND f.suggested_ttype = 'many2one'
      AND f.relation_model IS NOT NULL
      AND f.relation_model <> ''
    MATCH (s:P3SupportMasterDefinition)
    WHERE s.fg_source_import_id = f.fg_source_import_id
      AND s.app_key = f.app_key
      AND s.model = f.relation_model
    MERGE (f)-[r:FIELD_REFERENCES_SUPPORT_MASTER]->(s)
    ON CREATE SET
      r.relationship_key = 'inferred::' + f.node_key + '::FIELD_REFERENCES_SUPPORT_MASTER::' + s.node_key,
      r.fg_phase = 'P3_NEO4J_FIRST',
      r.fg_source_import_id = $import_id,
      r.fg_gap_aware_core = true,
      r.inferred_by_importer = true
    ON MATCH SET
      r.fg_source_import_id = coalesce(r.fg_source_import_id, $import_id),
      r.fg_phase = coalesce(r.fg_phase, 'P3_NEO4J_FIRST'),
      r.fg_gap_aware_core = coalesce(r.fg_gap_aware_core, true),
      r.inferred_by_importer = coalesce(r.inferred_by_importer, true)
    RETURN count(r) AS cnt
    """
    record = session.run(count_query if dry_run else write_query, import_id=import_id).single()
    return int(record.get("cnt", 0) if record else 0)


def _apply_graph_payload_to_neo4j(
    import_id: str,
    payload: dict[str, Any],
    dry_run: bool,
    artifact_dir: Path,
    phase_name: str,
) -> Neo4jApplyResult:
    nodes, rels = _extract_payload(payload)
    dangling = _calc_dangling(nodes, rels)
    if dangling:
        raise HTTPException(status_code=400, detail=f"Cannot apply while core dangling relationships exist: {len(dangling)}")
    label_counts, rel_counts = _graph_counts(nodes, rels)
    uri = os.getenv("NEO4J_URI", "bolt://fg-neo4j:7687")
    browser_url = _links().get("neo4j_browser")
    base_result = {
        "import_id": import_id,
        "dry_run": dry_run,
        "status": "dry_run_ok" if dry_run else "pending",
        "node_count": len(nodes),
        "relationship_count": len(rels),
        "dangling_relationship_count": 0,
        "applied_node_count": 0,
        "applied_relationship_count": 0,
        "skipped_relationship_count": 0,
        "label_counts": label_counts,
        "relationship_type_counts": rel_counts,
        "neo4j_uri": uri,
        "applied_at": None,
        "browser_url": browser_url,
        "verify_cypher": _verify_cypher(),
    }
    if dry_run:
        result = Neo4jApplyResult(**base_result)
        (artifact_dir / "neo4j_core_dry_run_result.json").write_text(
            json.dumps(result.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result
    if os.getenv("NEO4J_APPLY_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=403, detail="NEO4J_APPLY_ENABLED is not true. Set it before applying core payload.")
    if GraphDatabase is None:
        raise HTTPException(status_code=500, detail="neo4j driver is unavailable")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "password")
    applied_nodes = 0
    applied_rels = 0
    skipped_rels = 0
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            session.run("CREATE CONSTRAINT fg_node_key_unique IF NOT EXISTS FOR (n:FGNode) REQUIRE n.node_key IS UNIQUE")
            for node in nodes:
                key = _node_key(node)
                if not key:
                    continue
                labels = _neo4j_safe_labels(_labels(node) or ["FGNode"])
                props = _neo4j_safe_props(_graph_node_props(node))
                props["node_key"] = key
                props["fg_phase"] = props.get("fg_phase") or phase_name
                props["fg_source_import_id"] = import_id
                props["fg_gap_aware_core"] = True
                session.run(
                    _neo4j_merge_node_cypher(labels),
                    node_key=key,
                    props=props,
                )
                applied_nodes += 1
            for rel in rels:
                rk = _rel_key(rel)
                fk = _from_key(rel)
                tk = _to_key(rel)
                if not fk or not tk:
                    skipped_rels += 1
                    continue
                rt = str(rel.get("relationship_type") or rel.get("type") or "RELATED_TO").replace("`", "").strip() or "RELATED_TO"
                props = _neo4j_safe_props(_graph_rel_props(rel))
                props["relationship_key"] = rk
                props["fg_phase"] = props.get("fg_phase") or phase_name
                props["fg_source_import_id"] = import_id
                props["fg_gap_aware_core"] = True
                record = session.run(
                    f"MATCH (a {{node_key: $from_key}}), (b {{node_key: $to_key}}) "
                    f"MERGE (a)-[r:`{rt}` {{relationship_key: $relationship_key}}]->(b) SET r += $props "
                    "RETURN count(r) AS cnt",
                    from_key=fk,
                    to_key=tk,
                    relationship_key=rk,
                    props=props,
                ).single()
                if record and record.get("cnt", 0) > 0:
                    applied_rels += 1
                else:
                    skipped_rels += 1
            if phase_name == "P3_NEO4J_FIRST":
                inferred_p3_support_master_edges = _apply_p3_inferred_support_master_edges(session, import_id, dry_run=False)
                applied_rels += inferred_p3_support_master_edges
    finally:
        driver.close()
    result = Neo4jApplyResult(
        **{
            **base_result,
            "dry_run": False,
            "status": "neo4j_applied",
            "applied_node_count": applied_nodes,
            "applied_relationship_count": applied_rels,
            "skipped_relationship_count": skipped_rels,
            "applied_at": _now_iso(),
        }
    )
    (artifact_dir / "neo4j_core_apply_result.json").write_text(
        json.dumps(result.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


@app.post("/p1p2/imports/{import_id}/neo4j-dry-run", response_model=Neo4jApplyResult)
def p1p2_neo4j_dry_run(import_id: str) -> Neo4jApplyResult:
    in_dir = _p1p2_import_dir(import_id)
    core_path = in_dir / "P1P2_CORE_PAYLOAD.json"
    if not core_path.exists():
        raise HTTPException(status_code=404, detail="P1/P2 core payload not found")
    return _apply_graph_payload_to_neo4j(import_id, _load_json_path(core_path), True, in_dir, "P1P2_GAP_AWARE_CORE")


@app.post("/p1p2/imports/{import_id}/apply-neo4j", response_model=Neo4jApplyResult)
def p1p2_apply_neo4j(import_id: str) -> Neo4jApplyResult:
    in_dir = _p1p2_import_dir(import_id)
    core_path = in_dir / "P1P2_CORE_PAYLOAD.json"
    if not core_path.exists():
        raise HTTPException(status_code=404, detail="P1/P2 core payload not found")
    result = _apply_graph_payload_to_neo4j(import_id, _load_json_path(core_path), False, in_dir, "P1P2_GAP_AWARE_CORE")
    summary_path = in_dir / "import_summary.json"
    if summary_path.exists():
        raw = json.loads(summary_path.read_text(encoding="utf-8"))
        raw["status"] = "neo4j_core_applied"
        raw["neo4j_core_apply_result"] = result.model_dump()
        for st in raw.get("phase_statuses", []):
            if st.get("phase_key") == "P1/P2":
                st["neo4j_applied"] = True
                st["status"] = "neo4j_core_applied"
        summary_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


@app.get("/p1p2/imports/{import_id}/neo4j-apply-result")
def read_p1p2_neo4j_result(import_id: str) -> dict[str, Any]:
    in_dir = _p1p2_import_dir(import_id)
    for name in ["neo4j_core_apply_result.json", "neo4j_core_dry_run_result.json"]:
        path = in_dir / name
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="P1/P2 Neo4j result not found")


# -----------------------------------------------------------------------------
# P3 Neo4j-first import / validate / apply
# -----------------------------------------------------------------------------
# Plan lane implemented here:
# A. P3 Neo4j-first Import / Validate / Apply
#
# This block intentionally does NOT generate Odoo addon code and does NOT apply
# anything to Odoo.  It only imports the P3 graph payload, validates graph
# safety, performs mechanical burn-in preclassification for later B/C steps, and
# applies the graph to Neo4j when explicitly requested.

P3_IMPORT_ROOT_NAME = "p3_neo4j_first_imports"
P3_NEO4J_FIRST_FILENAME = "P3_NEO4J_FIRST_IMPORT.json"
P3_MINOR_CUSTOM_NORMALIZED_FILENAME = "P3_MINOR_CUSTOM_NORMALIZED.json"
P3_BURNIN_PRECLASSIFICATION_FILENAME = "p3_burnin_preclassification.json"
P3_UNRESOLVED_REFERENCE_FILENAME = "p3_unresolved_reference_candidates.json"
P3_GAP_SEED_REPORT_FILENAME = "p3_fg_gap_seed_report.md"
P3_BURNIN_INSPECTION_FILENAME = "p3_burnin_candidate_inspection.json"
P3_BURNIN_INSPECTION_REPORT_FILENAME = "p3_burnin_candidate_inspection_report.md"
P3_ADDON_INPUT_FILENAME = "p3_addon_input.json"
P3_ADDON_INPUT_REPORT_FILENAME = "p3_addon_input_report.md"
P3_ADDON_INPUT_ZIP_FILENAME = "P3_ADDON_INPUT_GENERATED.zip"
P3_ADDON_INPUT_VALIDATED_FILENAME = "p3_addon_input_validated.json"
P3_ADDON_INPUT_VALIDATION_FILENAME = "p3_addon_input_validation_result.json"
P3_ADDON_INPUT_VALIDATION_REPORT_FILENAME = "p3_addon_input_validation_report.md"
P3_CODEGEN_MATERIAL_FILENAME = "02_p3_codegen_material.json"
P3_CODEGEN_MATERIAL_REPORT_FILENAME = "p3_codegen_material_report.md"
P3_CODEGEN_MATERIAL_ZIP_FILENAME = "P3_ODOO_CODEGEN_MATERIAL_PACK.zip"
ODOO_CODE_IMPORT_ROOT_NAME = "odoo_code_imports"
ODOO_CODE_IMPORT_SUMMARY_FILENAME = "odoo_code_import_summary.json"
ODOO_CODE_VALIDATION_FILENAME = "odoo_code_validation_result.json"
ODOO_CODE_APPLY_RESULT_FILENAME = "odoo_code_apply_result.json"
P3_CODEGEN_EXPECTED_OUTPUT_SCHEMA_FILENAME = "03_expected_output_schema.json"
P3_CODEGEN_PROMPT_FILENAME = "04_codegen_prompt.md"
P3_CODEGEN_VALIDATION_POLICY_FILENAME = "05_validation_policy.md"
P3_CODEGEN_SKIPPED_REPORT_FILENAME = "06_skipped_items_report.md"
P3_CODEGEN_WARNING_NOTES_FILENAME = "08_warning_resolution_notes.md"

P3_ALLOWED_BURNIN_SIMPLE_TYPES = {
    "char",
    "text",
    "html",
    "boolean",
    "date",
    "datetime",
    "integer",
    "float",
    "monetary",
    "selection",
}
P3_ALLOWED_FIELD_TYPES = P3_ALLOWED_BURNIN_SIMPLE_TYPES | {"many2one", "many2many", "one2many"}
P3_RELATION_MODEL_KEYS = (
    "relation_model",
    "comodel_name",
    "target_relation_model",
    "related_model",
    "reference_model",
)


def _p3_root() -> Path:
    root = ARTIFACT_ROOT / P3_IMPORT_ROOT_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _p3_import_dir(import_id: str) -> Path:
    return _p3_root() / import_id


def _find_p3_neo4j_json(root: Path) -> Path | None:
    exact = sorted(root.rglob(P3_NEO4J_FIRST_FILENAME))
    if exact:
        return exact[0]
    candidates = [p for p in root.rglob("*.json") if "P3" in p.name.upper() and "NEO4J" in p.name.upper()]
    return sorted(candidates)[0] if candidates else None


def _find_p3_minor_custom_normalized_json(root: Path) -> Path | None:
    exact = sorted(root.rglob(P3_MINOR_CUSTOM_NORMALIZED_FILENAME))
    if exact:
        return exact[0]
    candidates = [p for p in root.rglob("*.json") if "P3" in p.name.upper() and "NORMALIZED" in p.name.upper()]
    return sorted(candidates)[0] if candidates else None


def _p3_field_name_is_valid(field_name: str | None) -> bool:
    if not field_name or not isinstance(field_name, str):
        return False
    # P3 demo burn-in must not modify standard fields directly.  It creates
    # x_fg_* or other x_* extension fields only.
    return re.fullmatch(r"x_[a-z][a-z0-9_]*", field_name.strip()) is not None


def _p3_extract_standard_models(nodes: list[dict[str, Any]]) -> set[str]:
    models: set[str] = set()
    for node in nodes:
        labels = set(_labels(node))
        props = node.get("properties") or {}
        if "OdooStandardModel" in labels:
            model = props.get("model") or props.get("odoo_model") or props.get("technical_name")
            if not model:
                key = _node_key(node) or ""
                if key.startswith("odoo_model::"):
                    model = key.split("::", 1)[1]
            if model:
                models.add(str(model))
    return models


def _p3_relation_model(props: dict[str, Any]) -> str | None:
    for key in P3_RELATION_MODEL_KEYS:
        value = props.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _p3_preclassify_burnin_candidates(payload: dict[str, Any]) -> dict[str, Any]:
    """Mechanically classify P3 overlay candidates without LLM or semantic remap.

    The classification answers only: is this candidate mechanically safe enough
    to become later Addon Input, or must it be reported as definition-required?
    It never guesses a reference model and never converts field types.
    """
    nodes, rels = _extract_payload(payload)
    standard_models = _p3_extract_standard_models(nodes)
    node_keys = {_node_key(n) for n in nodes if _node_key(n)}
    dangling = _calc_dangling(nodes, rels)
    candidates: list[dict[str, Any]] = []
    unresolved_references: list[dict[str, Any]] = []
    skipped_items: list[dict[str, Any]] = []
    burnin_candidates: list[dict[str, Any]] = []

    for node in nodes:
        if "P3OverlayFieldCandidate" not in _labels(node):
            continue
        props = dict(node.get("properties") or {})
        source_node_key = _node_key(node) or props.get("node_key")
        target_model = props.get("target_model") or props.get("model") or props.get("odoo_model")
        field_name = props.get("suggested_field_name") or props.get("field_name")
        field_type = str(props.get("suggested_ttype") or props.get("ttype") or props.get("field_type") or "").strip().lower()
        relation_model = _p3_relation_model(props)
        reasons: list[str] = []
        report_category = "p3_burnin_candidate"
        status = "p3_burnin_candidate"
        skip_for_p3 = False

        if not target_model:
            reasons.append("target_model_missing")
        elif standard_models and str(target_model) not in standard_models:
            reasons.append("target_model_not_in_p3_standard_model_nodes")

        if not _p3_field_name_is_valid(field_name):
            reasons.append("invalid_or_missing_x_field_name")

        if not field_type:
            reasons.append("field_type_missing")
        elif field_type not in P3_ALLOWED_FIELD_TYPES:
            reasons.append("unknown_field_type")

        if field_type == "many2one" and not relation_model:
            reasons.append("many2one_reference_model_missing")
        elif field_type in {"one2many", "many2many"}:
            # P3 can record these, but they are too risky for automatic demo
            # burn-in unless a later approved Addon Input defines both sides.
            reasons.append(f"{field_type}_requires_explicit_relation_design")
        elif field_type not in P3_ALLOWED_BURNIN_SIMPLE_TYPES and field_type != "many2one":
            reasons.append("not_a_simple_p3_burnin_type")

        if reasons:
            skip_for_p3 = True
            status = "unknown_or_unresolved"
            if "many2one_reference_model_missing" in reasons:
                report_category = "definition_required"
                reference_status = "unresolved_reference"
                skip_reason = "many2one_reference_model_missing"
            elif "target_model_missing" in reasons or "target_model_not_in_p3_standard_model_nodes" in reasons:
                report_category = "definition_required"
                reference_status = "unresolved_target_model"
                skip_reason = reasons[0]
            elif "invalid_or_missing_x_field_name" in reasons:
                report_category = "definition_required"
                reference_status = "invalid_field_name"
                skip_reason = "invalid_or_missing_x_field_name"
            elif "field_type_missing" in reasons or "unknown_field_type" in reasons:
                report_category = "definition_required"
                reference_status = "unknown_field_type"
                skip_reason = reasons[0]
            else:
                report_category = "requires_later_design"
                reference_status = "requires_explicit_design"
                skip_reason = reasons[0]
        else:
            reference_status = "resolved_or_not_required"
            skip_reason = None

        customer_message = (
            "検知しましたが、参照先・処理方法・業務意味が未確定のため、今回のP3デモ焼き込み対象から外しました。"
            "将来のF&G GAP / 開発対象として確認が必要です。"
            if skip_for_p3
            else "P3 Addon Input候補です。最終的なOdoo焼き込み前に人間判断済みデータとして確認してください。"
        )
        entry = {
            "source_node_key": source_node_key,
            "candidate_name": props.get("candidate_name_ja") or props.get("candidate_name") or field_name or source_node_key,
            "app_key": props.get("app_key"),
            "target_model": target_model,
            "suggested_field_name": field_name,
            "suggested_ttype": field_type,
            "relation_model": relation_model,
            "burnin_candidate_status": status,
            "reference_status": reference_status,
            "skip_for_p3_burnin": skip_for_p3,
            "skip_reason": skip_reason,
            "all_reasons": reasons,
            "report_category": report_category,
            "future_phase": "P3 Burn-in Candidate Inspection / P4 / P5" if skip_for_p3 else "P3 Addon Input",
            "customer_report_message": customer_message,
            "mechanical_classification_only": True,
            "semantic_remap_performed": False,
            "llm_used": False,
        }
        candidates.append(entry)
        if skip_for_p3:
            skipped_items.append(entry)
            if reference_status in {"unresolved_reference", "unresolved_target_model"}:
                unresolved_references.append(entry)
        else:
            burnin_candidates.append(entry)

    reason_counts: dict[str, int] = {}
    for entry in skipped_items:
        for reason in entry.get("all_reasons") or []:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    label_counts, rel_counts = _graph_counts(nodes, rels)
    return {
        "schema_name": "p3_burnin_preclassification",
        "schema_version": "0.1.0",
        "generated_at": _now_iso(),
        "plan_position": {
            "current_step": "A. P3 Neo4j-first Import / Validate / Apply",
            "next_step": "B. P3 Burn-in Candidate Inspection",
            "notes": "This is mechanical preclassification only. It prepares B/C but does not decide final Odoo burn-in.",
        },
        "policy": {
            "llm_used": False,
            "semantic_remap_performed": False,
            "auto_reference_completion": False,
            "auto_odoo_codegen": False,
            "auto_odoo_apply": False,
        },
        "summary": {
            "node_count": len(nodes),
            "relationship_count": len(rels),
            "dangling_relationship_count": len(dangling),
            "standard_model_count": len(standard_models),
            "overlay_field_candidate_count": len(candidates),
            "p3_burnin_candidate_count": len(burnin_candidates),
            "skipped_item_count": len(skipped_items),
            "unresolved_reference_count": len(unresolved_references),
            "reason_counts": reason_counts,
            "label_counts": label_counts,
            "relationship_type_counts": rel_counts,
        },
        "standard_models": sorted(standard_models),
        "burnin_candidates": burnin_candidates,
        "skipped_items": skipped_items,
        "unresolved_references": unresolved_references,
        "all_overlay_field_candidates": candidates,
        "node_keys_present": len(node_keys),
    }


def _p3_gap_seed_markdown(preclassification: dict[str, Any]) -> str:
    summary = preclassification.get("summary") or {}
    lines = [
        "# P3 F&G GAP Seed Report",
        "",
        "Current plan step: A. P3 Neo4j-first Import / Validate / Apply",
        "Next plan step: B. P3 Burn-in Candidate Inspection",
        "",
        "This report is generated by deterministic validation rules only. No LLM, semantic remapping, or automatic reference completion was used.",
        "",
        "## Summary",
        f"- overlay_field_candidate_count: {summary.get('overlay_field_candidate_count', 0)}",
        f"- p3_burnin_candidate_count: {summary.get('p3_burnin_candidate_count', 0)}",
        f"- skipped_item_count: {summary.get('skipped_item_count', 0)}",
        f"- unresolved_reference_count: {summary.get('unresolved_reference_count', 0)}",
        "",
        "## Reason counts",
    ]
    for reason, count in sorted((summary.get("reason_counts") or {}).items()):
        lines.append(f"- {reason}: {count}")
    lines.extend(["", "## Definition-required / skipped items"])
    skipped = preclassification.get("skipped_items") or []
    if not skipped:
        lines.append("- None")
    for entry in skipped[:500]:
        lines.extend(
            [
                "",
                f"### {entry.get('candidate_name') or entry.get('source_node_key')}",
                f"- source_node_key: {entry.get('source_node_key')}",
                f"- app_key: {entry.get('app_key')}",
                f"- target_model: {entry.get('target_model')}",
                f"- suggested_field_name: {entry.get('suggested_field_name')}",
                f"- suggested_ttype: {entry.get('suggested_ttype')}",
                f"- reference_status: {entry.get('reference_status')}",
                f"- skip_reason: {entry.get('skip_reason')}",
                f"- all_reasons: {', '.join(entry.get('all_reasons') or [])}",
                f"- future_phase: {entry.get('future_phase')}",
                f"- customer_report_message: {entry.get('customer_report_message')}",
            ]
        )
    if len(skipped) > 500:
        lines.append(f"\n... truncated. Remaining skipped items: {len(skipped) - 500}")
    lines.append("")
    return "\n".join(lines)


def _p3_phase_statuses(counts: CountSummary, ready: bool, warnings: list[str], preclassification: dict[str, Any]) -> list[dict[str, Any]]:
    summary = preclassification.get("summary") or {}
    a_status = "ready_for_neo4j_apply" if ready else "validation_failed"
    return [
        {
            "phase_key": "A",
            "label": "A. P3 Neo4j-first Import / Validate / Apply",
            "status": a_status,
            "json_imported": True,
            "normalized": True,
            "cypher_built": ready,
            "neo4j_applied": False,
            "count_summary": counts.model_dump(),
            "warnings": warnings,
            "current_step": True,
            "next_step": False,
        },
        {
            "phase_key": "B",
            "label": "B. P3 Burn-in Candidate Inspection",
            "status": "preclassification_ready",
            "candidate_count": summary.get("p3_burnin_candidate_count", 0),
            "skipped_item_count": summary.get("skipped_item_count", 0),
            "unresolved_reference_count": summary.get("unresolved_reference_count", 0),
            "warnings": ["Next implementation step. Final burn-in selection is not automated in this A-step."],
            "current_step": False,
            "next_step": True,
        },
        {"phase_key": "C", "label": "C. P3 Addon Input生成", "status": "not_started"},
        {"phase_key": "D", "label": "D. P3 Addon Input Validate", "status": "not_started"},
        {"phase_key": "E", "label": "E. Odoo Codegen Material Pack Export", "status": "not_started"},
        {"phase_key": "F", "label": "F. Generated Odoo Code Pack Import / Validate", "status": "not_started"},
        {"phase_key": "G", "label": "G. Apply Odoo Addon Direct", "status": "not_started"},
        {"phase_key": "H", "label": "H. Phase Matrix / Status表示", "status": "available_for_A"},
        {"phase_key": "I", "label": "I. skipped / F&G GAP report出力", "status": "available_for_A_seed"},
    ]


def _write_p3_import_artifacts(
    out_dir: Path,
    summary: dict[str, Any],
    payload: dict[str, Any],
    dangling: list[dict[str, Any]],
    preclassification: dict[str, Any],
) -> None:
    (out_dir / "import_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "phase_statuses.json").write_text(json.dumps(summary.get("phase_statuses") or [], ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / P3_NEO4J_FIRST_FILENAME).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "p3_neo4j_first_validation_result.json").write_text(
        json.dumps(
            {
                "generated_at": _now_iso(),
                "ready_for_neo4j_import": len(dangling) == 0,
                "dangling_relationship_count": len(dangling),
                "dangling_relationships_sample": dangling[:500],
                "preclassification_summary": preclassification.get("summary"),
                "policy": preclassification.get("policy"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if dangling:
        (out_dir / "dangling_relationships.json").write_text(json.dumps(dangling[:500], ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / P3_BURNIN_PRECLASSIFICATION_FILENAME).write_text(json.dumps(preclassification, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / P3_UNRESOLVED_REFERENCE_FILENAME).write_text(
        json.dumps(
            {
                "generated_at": _now_iso(),
                "unresolved_references": preclassification.get("unresolved_references") or [],
                "summary": {
                    "unresolved_reference_count": len(preclassification.get("unresolved_references") or []),
                    "llm_used": False,
                    "semantic_remap_performed": False,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (out_dir / P3_GAP_SEED_REPORT_FILENAME).write_text(_p3_gap_seed_markdown(preclassification), encoding="utf-8")
    (out_dir / "display_summary.json").write_text(
        json.dumps(
            {
                "generated_at": _now_iso(),
                "plan_current_step": "A. P3 Neo4j-first Import / Validate / Apply",
                "plan_next_step": "B. P3 Burn-in Candidate Inspection",
                "import_summary": summary,
                "preclassification_summary": preclassification.get("summary"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _build_p3_import_summary(
    import_id: str,
    filename: str,
    import_type: str,
    out_dir: Path,
    extracted_dir: Path | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    nodes, rels = _extract_payload(payload)
    dangling = _calc_dangling(nodes, rels)
    counts = _count_summary(payload, nodes, rels, dangling)
    ready = len(dangling) == 0
    warnings: list[str] = []
    if dangling:
        warnings.append(f"Dangling relationships detected: {len(dangling)}")
    preclassification = _p3_preclassify_burnin_candidates(payload)
    pre_summary = preclassification.get("summary") or {}
    if pre_summary.get("unresolved_reference_count", 0):
        warnings.append(f"Unresolved references detected for later report: {pre_summary.get('unresolved_reference_count')}")
    if pre_summary.get("skipped_item_count", 0):
        warnings.append(f"P3 burn-in skipped/definition-required seed items: {pre_summary.get('skipped_item_count')}")
    status = "ready_for_neo4j_apply" if ready else "validation_failed"
    summary = {
        "import_id": import_id,
        "filename": filename,
        "import_type": import_type,
        "phase": payload.get("phase") or "P3_NEO4J_FIRST",
        "artifact_type": "P3_NEO4J_FIRST_IMPORT",
        "status": status,
        "ready_for_neo4j_import": ready,
        "odoo_code_included": False,
        "odoo_apply_included": False,
        "saved_path": str(out_dir / ("uploaded_pack.zip" if import_type == "zip" else P3_NEO4J_FIRST_FILENAME)),
        "extracted_dir": str(extracted_dir) if extracted_dir else None,
        "normalized_json_path": str(out_dir / P3_NEO4J_FIRST_FILENAME),
        "count_summary": counts.model_dump(),
        "p3_preclassification_summary": pre_summary,
        "phase_statuses": _p3_phase_statuses(counts, ready, warnings, preclassification),
        "warnings": warnings,
        "links": {
            **_links(),
            "self": f"/p3/imports/{import_id}",
            "phase_statuses": f"/p3/imports/{import_id}/phase-statuses",
            "graph_summary": f"/p3/imports/{import_id}/graph-summary",
            "burnin_preclassification": f"/p3/imports/{import_id}/burnin-preclassification",
            "unresolved_references": f"/p3/imports/{import_id}/unresolved-references",
            "fg_gap_seed_report": f"/p3/imports/{import_id}/fg-gap-seed-report.md",
            "neo4j_dry_run": f"/p3/imports/{import_id}/neo4j-dry-run",
            "apply_neo4j": f"/p3/imports/{import_id}/apply-neo4j",
        },
        "plan": {
            "current_step": "A. P3 Neo4j-first Import / Validate / Apply",
            "next_step": "B. P3 Burn-in Candidate Inspection",
            "not_started_steps": ["C", "D", "E", "F", "G"],
        },
    }
    _write_p3_import_artifacts(out_dir, summary, payload, dangling, preclassification)
    return summary


@app.post("/p3/import-neo4j-first-pack")
async def import_p3_neo4j_first_pack(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read()
    import_id = str(uuid4())
    out_dir = _p3_import_dir(import_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = file.filename or "uploaded"
    if filename.lower().endswith(".zip"):
        extracted_dir = _safe_extract_zip(data, out_dir)
        neo4j_json_path = _find_p3_neo4j_json(extracted_dir)
        if not neo4j_json_path:
            raise HTTPException(status_code=400, detail=f"{P3_NEO4J_FIRST_FILENAME} was not found in ZIP")
        payload = _load_json_path(neo4j_json_path)
        shutil.copy2(neo4j_json_path, out_dir / P3_NEO4J_FIRST_FILENAME)
        minor_path = _find_p3_minor_custom_normalized_json(extracted_dir)
        if minor_path:
            shutil.copy2(minor_path, out_dir / P3_MINOR_CUSTOM_NORMALIZED_FILENAME)
        import_type = "zip"
    else:
        payload = _load_json_bytes(data)
        (out_dir / P3_NEO4J_FIRST_FILENAME).write_bytes(data)
        extracted_dir = None
        import_type = "json"
    return _build_p3_import_summary(import_id, filename, import_type, out_dir, extracted_dir, payload)


# Short alias for console code and curl ergonomics.
@app.post("/p3/import-pack")
async def import_p3_pack(file: UploadFile = File(...)) -> dict[str, Any]:
    return await import_p3_neo4j_first_pack(file)


@app.get("/p3/imports")
def list_p3_imports() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    root = _p3_root()
    for p in sorted(root.glob("*/import_summary.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            items.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return {"items": items}


@app.get("/p3/imports/{import_id}")
def read_p3_import(import_id: str) -> dict[str, Any]:
    path = _p3_import_dir(import_id) / "import_summary.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 import not found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/p3/imports/{import_id}/phase-statuses")
def read_p3_phase_statuses(import_id: str) -> dict[str, Any]:
    path = _p3_import_dir(import_id) / "phase_statuses.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 phase statuses not found")
    return {"items": json.loads(path.read_text(encoding="utf-8"))}


@app.get("/p3/imports/{import_id}/graph-summary")
def read_p3_graph_summary(import_id: str) -> dict[str, Any]:
    in_dir = _p3_import_dir(import_id)
    payload_path = in_dir / P3_NEO4J_FIRST_FILENAME
    pre_path = in_dir / P3_BURNIN_PRECLASSIFICATION_FILENAME
    if not payload_path.exists():
        raise HTTPException(status_code=404, detail="P3 Neo4j-first payload not found")
    payload = _load_json_path(payload_path)
    nodes, rels = _extract_payload(payload)
    dangling = _calc_dangling(nodes, rels)
    label_counts, rel_counts = _graph_counts(nodes, rels)
    return {
        "import_id": import_id,
        "node_count": len(nodes),
        "relationship_count": len(rels),
        "dangling_relationship_count": len(dangling),
        "label_counts": label_counts,
        "relationship_type_counts": rel_counts,
        "preclassification_summary": json.loads(pre_path.read_text(encoding="utf-8")).get("summary") if pre_path.exists() else None,
        "plan_current_step": "A. P3 Neo4j-first Import / Validate / Apply",
        "plan_next_step": "B. P3 Burn-in Candidate Inspection",
    }


@app.get("/p3/imports/{import_id}/burnin-preclassification")
def read_p3_burnin_preclassification(import_id: str) -> dict[str, Any]:
    path = _p3_import_dir(import_id) / P3_BURNIN_PRECLASSIFICATION_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 burn-in preclassification not found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/p3/imports/{import_id}/unresolved-references")
def read_p3_unresolved_references(import_id: str) -> dict[str, Any]:
    path = _p3_import_dir(import_id) / P3_UNRESOLVED_REFERENCE_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 unresolved reference candidates not found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/p3/imports/{import_id}/fg-gap-seed-report.md")
def read_p3_fg_gap_seed_report(import_id: str) -> FileResponse:
    path = _p3_import_dir(import_id) / P3_GAP_SEED_REPORT_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 F&G GAP seed report not found")
    return FileResponse(str(path), filename=path.name, media_type="text/markdown")


@app.post("/p3/imports/{import_id}/neo4j-dry-run", response_model=Neo4jApplyResult)
def p3_neo4j_dry_run(import_id: str) -> Neo4jApplyResult:
    in_dir = _p3_import_dir(import_id)
    payload_path = in_dir / P3_NEO4J_FIRST_FILENAME
    if not payload_path.exists():
        raise HTTPException(status_code=404, detail="P3 Neo4j-first payload not found")
    result = _apply_graph_payload_to_neo4j(import_id, _load_json_path(payload_path), True, in_dir, "P3_NEO4J_FIRST")
    src = in_dir / "neo4j_core_dry_run_result.json"
    if src.exists():
        shutil.copy2(src, in_dir / "neo4j_dry_run_result.json")
    return result


@app.post("/p3/imports/{import_id}/apply-neo4j", response_model=Neo4jApplyResult)
def p3_apply_neo4j(import_id: str) -> Neo4jApplyResult:
    in_dir = _p3_import_dir(import_id)
    payload_path = in_dir / P3_NEO4J_FIRST_FILENAME
    if not payload_path.exists():
        raise HTTPException(status_code=404, detail="P3 Neo4j-first payload not found")
    result = _apply_graph_payload_to_neo4j(import_id, _load_json_path(payload_path), False, in_dir, "P3_NEO4J_FIRST")
    src = in_dir / "neo4j_core_apply_result.json"
    if src.exists():
        shutil.copy2(src, in_dir / "neo4j_apply_result.json")
    summary_path = in_dir / "import_summary.json"
    if summary_path.exists():
        raw = json.loads(summary_path.read_text(encoding="utf-8"))
        raw["status"] = "p3_neo4j_applied"
        raw["neo4j_apply_result"] = result.model_dump()
        for st in raw.get("phase_statuses", []):
            if st.get("phase_key") == "A":
                st["neo4j_applied"] = True
                st["status"] = "p3_neo4j_applied"
            elif st.get("phase_key") == "B":
                st["status"] = "next_ready"
        summary_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        (in_dir / "phase_statuses.json").write_text(json.dumps(raw.get("phase_statuses") or [], ensure_ascii=False, indent=2), encoding="utf-8")
    return result


@app.get("/p3/imports/{import_id}/neo4j-apply-result")
def read_p3_neo4j_result(import_id: str) -> dict[str, Any]:
    in_dir = _p3_import_dir(import_id)
    for name in ["neo4j_apply_result.json", "neo4j_dry_run_result.json", "neo4j_core_apply_result.json", "neo4j_core_dry_run_result.json"]:
        path = in_dir / name
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="P3 Neo4j result not found")


def _p3_list_nodes_by_label(payload: dict[str, Any], label: str) -> list[dict[str, Any]]:
    nodes, _ = _extract_payload(payload)
    return [node for node in nodes if label in _labels(node)]


def _p3_candidate_view(node: dict[str, Any]) -> dict[str, Any]:
    props = _graph_node_props(node)
    ttype = str(props.get("suggested_ttype") or "").strip()
    relation_model = str(props.get("relation_model") or "").strip()
    field_name = str(props.get("suggested_field_name") or "").strip()
    if ttype == "many2one" and relation_model:
        classification = "p3_burnin_candidate_many2one_with_support_master"
        recommended_action = "include_in_p3_addon_input_after_human_approval"
    elif ttype == "many2one":
        classification = "skip_unresolved_many2one"
        recommended_action = "skip_and_report_definition_required"
    elif ttype in P3_ALLOWED_BURNIN_SIMPLE_TYPES and field_name:
        classification = "p3_burnin_candidate_simple_field"
        recommended_action = "include_in_p3_addon_input_after_human_approval"
    elif not field_name:
        classification = "skip_missing_field_name"
        recommended_action = "skip_and_report_definition_required"
    elif not ttype:
        classification = "skip_unknown_field_type"
        recommended_action = "skip_and_report_definition_required"
    else:
        classification = "needs_manual_review"
        recommended_action = "review_before_p3_addon_input"
    return {
        "node_key": props.get("node_key") or _node_key(node),
        "app_key": props.get("app_key"),
        "target_model": props.get("target_model"),
        "suggested_field_name": props.get("suggested_field_name"),
        "candidate_name_ja": props.get("candidate_name_ja"),
        "suggested_ttype": props.get("suggested_ttype"),
        "relation_model": props.get("relation_model"),
        "burnin_readiness": props.get("burnin_readiness"),
        "classification": classification,
        "recommended_action": recommended_action,
        "p0_bundle_keys": props.get("p0_bundle_keys"),
        "p1_anchor_refs": props.get("p1_anchor_refs"),
        "p2_configuration_refs": props.get("p2_configuration_refs"),
        "source_refs": props.get("source_refs"),
    }


def _build_p3_burnin_inspection(import_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    fields = [_p3_candidate_view(node) for node in _p3_list_nodes_by_label(payload, "P3OverlayFieldCandidate")]
    support_masters = []
    for node in _p3_list_nodes_by_label(payload, "P3SupportMasterDefinition"):
        props = _enrich_p3_support_master_props(node, _graph_node_props(node))
        support_masters.append({
            "node_key": props.get("node_key") or _node_key(node),
            "app_key": props.get("app_key"),
            "model": props.get("model"),
            "support_model": props.get("support_model"),
            "name": props.get("name"),
            "support_master_name": props.get("support_master_name"),
            "candidate_name_ja": props.get("candidate_name_ja"),
            "purpose": props.get("purpose"),
            "burnin_readiness": props.get("burnin_readiness"),
        })
    support_by_app_model = {(x.get("app_key"), x.get("model")): x for x in support_masters}
    support_link_issues = []
    for f in fields:
        if f.get("suggested_ttype") == "many2one" and f.get("relation_model"):
            key = (f.get("app_key"), f.get("relation_model"))
            if key not in support_by_app_model:
                support_link_issues.append({
                    "node_key": f.get("node_key"),
                    "app_key": f.get("app_key"),
                    "target_model": f.get("target_model"),
                    "suggested_field_name": f.get("suggested_field_name"),
                    "candidate_name_ja": f.get("candidate_name_ja"),
                    "relation_model": f.get("relation_model"),
                    "issue": "support_master_definition_missing_for_relation_model",
                })
    skipped = []
    for node in _p3_list_nodes_by_label(payload, "P3SkippedForOdooBurnin"):
        props = _graph_node_props(node)
        skipped.append({
            "node_key": props.get("node_key") or _node_key(node),
            "app_key": props.get("app_key"),
            "target_model": props.get("target_model"),
            "candidate_name_ja": props.get("candidate_name_ja"),
            "suggested_field_name": props.get("suggested_field_name"),
            "skip_reason": props.get("skip_reason"),
            "future_phase": props.get("future_phase"),
            "customer_report_message": props.get("customer_report_message"),
        })
    class_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    app_counts: dict[str, int] = {}
    model_counts: dict[str, int] = {}
    for f in fields:
        class_counts[str(f.get("classification") or "unknown")] = class_counts.get(str(f.get("classification") or "unknown"), 0) + 1
        type_counts[str(f.get("suggested_ttype") or "unknown")] = type_counts.get(str(f.get("suggested_ttype") or "unknown"), 0) + 1
        app_counts[str(f.get("app_key") or "unknown")] = app_counts.get(str(f.get("app_key") or "unknown"), 0) + 1
        model_counts[str(f.get("target_model") or "unknown")] = model_counts.get(str(f.get("target_model") or "unknown"), 0) + 1
    include_candidates = [f for f in fields if str(f.get("classification") or "").startswith("p3_burnin_candidate_")]
    review_candidates = [f for f in fields if f.get("classification") == "needs_manual_review"]
    skip_candidates = [f for f in fields if str(f.get("classification") or "").startswith("skip_")]
    status = "ready_for_p3_addon_input_generation" if not support_link_issues and not review_candidates else "inspection_requires_review"
    return {
        "schema_name": "p3_burnin_candidate_inspection",
        "version": "v1",
        "import_id": import_id,
        "plan_current_step": "B. P3 Burn-in Candidate Inspection",
        "plan_next_step": "C. P3 Addon Input生成",
        "status": status,
        "policy": {
            "llm_used": False,
            "semantic_remap_used": False,
            "mechanical_rules_only": True,
            "auto_odoo_apply": False,
            "human_approval_required_before_addon_input": True,
        },
        "summary": {
            "field_candidate_count": len(fields),
            "support_master_count": len(support_masters),
            "skipped_item_count": len(skipped),
            "include_candidate_count": len(include_candidates),
            "review_candidate_count": len(review_candidates),
            "skip_candidate_count": len(skip_candidates),
            "support_link_issue_count": len(support_link_issues),
            "classification_counts": class_counts,
            "type_counts": type_counts,
            "app_counts": app_counts,
            "target_model_counts": model_counts,
        },
        "include_candidates": include_candidates,
        "review_candidates": review_candidates,
        "skip_candidates": skip_candidates,
        "support_master_definitions": support_masters,
        "support_link_issues": support_link_issues,
        "skipped_items": skipped,
    }


def _p3_burnin_inspection_markdown(inspection: dict[str, Any]) -> str:
    summary = inspection.get("summary") or {}
    lines = [
        "# P3 Burn-in Candidate Inspection",
        "",
        f"- import_id: {inspection.get('import_id')}",
        f"- status: {inspection.get('status')}",
        f"- current_step: {inspection.get('plan_current_step')}",
        f"- next_step: {inspection.get('plan_next_step')}",
        "",
        "## Summary",
        f"- field_candidate_count: {summary.get('field_candidate_count', 0)}",
        f"- support_master_count: {summary.get('support_master_count', 0)}",
        f"- include_candidate_count: {summary.get('include_candidate_count', 0)}",
        f"- review_candidate_count: {summary.get('review_candidate_count', 0)}",
        f"- skip_candidate_count: {summary.get('skip_candidate_count', 0)}",
        f"- support_link_issue_count: {summary.get('support_link_issue_count', 0)}",
        "",
        "## Classification Counts",
    ]
    for key, count in sorted((summary.get("classification_counts") or {}).items()):
        lines.append(f"- {key}: {count}")
    lines.extend(["", "## Include Candidates", ""])
    for f in inspection.get("include_candidates") or []:
        relation = f" -> {f.get('relation_model')}" if f.get("relation_model") else ""
        lines.append(f"- [{f.get('app_key')}] {f.get('target_model')}.{f.get('suggested_field_name')} ({f.get('suggested_ttype')}{relation}) - {f.get('candidate_name_ja')}")
    if inspection.get("support_link_issues"):
        lines.extend(["", "## Support Link Issues", ""])
        for issue in inspection.get("support_link_issues") or []:
            lines.append(f"- [{issue.get('app_key')}] {issue.get('target_model')}.{issue.get('suggested_field_name')} -> {issue.get('relation_model')}: {issue.get('issue')}")
    lines.extend(["", "## Policy", "", "LLMは使用していません。意味的な再マッピングや自動採否判断は行わず、P3 Addon Input生成前に人間確認を前提にします。"])
    return "\n".join(lines) + "\n"


@app.post("/p3/imports/{import_id}/burnin-inspection")
def generate_p3_burnin_inspection(import_id: str) -> dict[str, Any]:
    in_dir = _p3_import_dir(import_id)
    payload_path = in_dir / P3_NEO4J_FIRST_FILENAME
    if not payload_path.exists():
        raise HTTPException(status_code=404, detail="P3 Neo4j-first payload not found")
    inspection = _build_p3_burnin_inspection(import_id, _load_json_path(payload_path))
    (in_dir / P3_BURNIN_INSPECTION_FILENAME).write_text(json.dumps(inspection, ensure_ascii=False, indent=2), encoding="utf-8")
    (in_dir / P3_BURNIN_INSPECTION_REPORT_FILENAME).write_text(_p3_burnin_inspection_markdown(inspection), encoding="utf-8")
    summary_path = in_dir / "import_summary.json"
    if summary_path.exists():
        raw = json.loads(summary_path.read_text(encoding="utf-8"))
        raw["status"] = "p3_burnin_inspection_generated"
        raw["p3_burnin_inspection_summary"] = inspection.get("summary") or {}
        raw.setdefault("links", {})["burnin_inspection"] = f"/p3/imports/{import_id}/burnin-inspection"
        raw.setdefault("links", {})["burnin_inspection_report"] = f"/p3/imports/{import_id}/burnin-inspection-report.md"
        for st in raw.get("phase_statuses", []):
            if st.get("phase_key") == "A":
                st["status"] = "p3_neo4j_applied"
            elif st.get("phase_key") == "B":
                st["status"] = inspection.get("status") or "inspection_generated"
                st["inspection_generated"] = True
                st["candidate_count"] = inspection.get("summary", {}).get("include_candidate_count", 0)
            elif st.get("phase_key") == "C":
                st["status"] = "next_ready" if inspection.get("status") == "ready_for_p3_addon_input_generation" else "not_started"
        summary_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        (in_dir / "phase_statuses.json").write_text(json.dumps(raw.get("phase_statuses") or [], ensure_ascii=False, indent=2), encoding="utf-8")
    return inspection


@app.get("/p3/imports/{import_id}/burnin-inspection")
def read_p3_burnin_inspection(import_id: str) -> dict[str, Any]:
    path = _p3_import_dir(import_id) / P3_BURNIN_INSPECTION_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 burn-in inspection not found. Run POST /p3/imports/{import_id}/burnin-inspection first.")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/p3/imports/{import_id}/burnin-inspection-report.md")
def read_p3_burnin_inspection_report(import_id: str) -> FileResponse:
    path = _p3_import_dir(import_id) / P3_BURNIN_INSPECTION_REPORT_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 burn-in inspection report not found")
    return FileResponse(str(path), filename=path.name, media_type="text/markdown")






def _p3_slug(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    raw = raw.replace(".", "_").replace("-", "_").replace("::", "_")
    raw = re.sub(r"[^a-z0-9_]+", "_", raw)
    raw = re.sub(r"_+", "_", raw).strip("_")
    return raw or "item"


def _p3_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "ok", "include", "included"}


def _p3_default_view_section(target_model: str | None) -> str:
    if not target_model:
        return "F&G P3 Demo"
    return f"F&G P3 Demo - {target_model}"


def _build_p3_addon_input(import_id: str, inspection: dict[str, Any]) -> dict[str, Any]:
    """Build the C-step Addon Input from the B-step inspection.

    This is intentionally mechanical. It does not make semantic remapping or
    final customer/business decisions.  It converts already-inspected include
    candidates into an Odoo burn-in input shape that can be validated in D.
    """
    if inspection.get("status") != "ready_for_p3_addon_input_generation":
        raise HTTPException(
            status_code=400,
            detail=f"B inspection is not ready for C. status={inspection.get('status')}",
        )
    summary = inspection.get("summary") or {}
    if int(summary.get("support_link_issue_count") or 0) > 0:
        raise HTTPException(status_code=400, detail="Cannot generate addon input while support link issues remain")
    if int(summary.get("review_candidate_count") or 0) > 0:
        raise HTTPException(status_code=400, detail="Cannot generate addon input while review candidates remain")

    include_candidates = list(inspection.get("include_candidates") or [])
    support_masters = list(inspection.get("support_master_definitions") or [])
    skipped_items = list(inspection.get("skipped_items") or [])

    used_support_models = {
        str(f.get("relation_model") or "").strip()
        for f in include_candidates
        if f.get("suggested_ttype") == "many2one" and str(f.get("relation_model") or "").strip()
    }

    master_definitions: list[dict[str, Any]] = []
    for master in sorted(support_masters, key=lambda x: (str(x.get("app_key") or ""), str(x.get("model") or ""))):
        model = str(master.get("model") or master.get("support_model") or "").strip()
        if not model or model not in used_support_models:
            continue
        name = str(master.get("name") or master.get("support_master_name") or master.get("candidate_name_ja") or model).strip()
        app_key = master.get("app_key")
        master_definitions.append({
            "node_key": master.get("node_key"),
            "app_key": app_key,
            "model": model,
            "technical_name": model,
            "name": name,
            "display_name": name,
            "description": master.get("purpose") or "P3デモ用の簡易マスタ",
            "master_type": "p3_demo_support_master",
            "code_field": "code",
            "name_field": "name",
            "active_field": "active",
            "recommended_menu_group": f"F&G P3 Demo / {app_key or 'common'}",
            "source_node_key": master.get("node_key"),
            "burnin_readiness": master.get("burnin_readiness") or "support_master_required",
            "llm_used": False,
            "semantic_remap_used": False,
        })

    master_by_model = {m["model"]: m for m in master_definitions}
    field_definitions: list[dict[str, Any]] = []
    view_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    simple_logic_definitions: list[dict[str, Any]] = []
    fg_gap_report_items: list[dict[str, Any]] = []

    for f in sorted(include_candidates, key=lambda x: (str(x.get("app_key") or ""), str(x.get("target_model") or ""), str(x.get("suggested_field_name") or ""))):
        target_model = str(f.get("target_model") or "").strip()
        field_name = str(f.get("suggested_field_name") or "").strip()
        ttype = str(f.get("suggested_ttype") or "").strip()
        relation_model = str(f.get("relation_model") or "").strip()
        label = str(f.get("candidate_name_ja") or field_name).strip()
        if not target_model or not field_name or not ttype:
            fg_gap_report_items.append({
                "source_node_key": f.get("node_key"),
                "candidate_name": label,
                "target_model": target_model,
                "suggested_field_name": field_name,
                "skip_reason": "addon_input_required_property_missing",
                "future_phase": "D. P3 Addon Input Validate",
                "customer_report_message": "P3 Addon Input生成時に必須プロパティが不足したため、検証対象に回しました。",
            })
            continue
        if ttype == "many2one" and relation_model and relation_model not in master_by_model:
            fg_gap_report_items.append({
                "source_node_key": f.get("node_key"),
                "candidate_name": label,
                "target_model": target_model,
                "suggested_field_name": field_name,
                "skip_reason": "support_master_definition_missing_for_relation_model",
                "future_phase": "D. P3 Addon Input Validate",
                "customer_report_message": "参照先簡易マスタ定義が見つからないため、P3 Addon Inputから除外しました。",
            })
            continue
        field_entry = {
            "node_key": f.get("node_key"),
            "app_key": f.get("app_key"),
            "target_model": target_model,
            "field_name": field_name,
            "string": label,
            "label_ja": label,
            "ttype": ttype,
            "relation_model": relation_model if ttype == "many2one" else None,
            "required": False,
            "readonly": False,
            "copy": True,
            "store": True,
            "tracking": False,
            "index": ttype == "many2one",
            "help": f"F&G P3 Demo候補: {label}",
            "burnin_readiness": f.get("burnin_readiness"),
            "classification": f.get("classification"),
            "source_refs": f.get("source_refs"),
            "p0_bundle_keys": f.get("p0_bundle_keys"),
            "p1_anchor_refs": f.get("p1_anchor_refs"),
            "p2_configuration_refs": f.get("p2_configuration_refs"),
            "source_node_key": f.get("node_key"),
            "llm_used": False,
            "semantic_remap_used": False,
        }
        if ttype != "many2one":
            field_entry.pop("relation_model", None)
        field_definitions.append(field_entry)
        group_key = (str(f.get("app_key") or "common"), target_model)
        view_groups.setdefault(group_key, []).append({
            "field_name": field_name,
            "string": label,
            "ttype": ttype,
            "relation_model": relation_model if ttype == "many2one" else None,
        })
        if ttype == "boolean" or field_name.endswith("_flag") or "warning" in field_name or "required" in field_name:
            simple_logic_definitions.append({
                "logic_key": f"p3_demo_visibility_{_p3_slug(target_model)}_{_p3_slug(field_name)}",
                "app_key": f.get("app_key"),
                "target_model": target_model,
                "field_name": field_name,
                "logic_type": "demo_indicator_only",
                "description": f"{label} をP3デモ上の注意・要否・確認表示として扱う。自動業務判断は実装しない。",
                "auto_compute": False,
                "requires_human_rule_definition": False,
                "llm_used": False,
            })

    view_placements: list[dict[str, Any]] = []
    for (app_key, target_model), fields in sorted(view_groups.items(), key=lambda x: (x[0][0], x[0][1])):
        view_placements.append({
            "placement_key": f"p3_demo_section_{_p3_slug(app_key)}_{_p3_slug(target_model)}",
            "app_key": app_key,
            "target_model": target_model,
            "view_type": "form",
            "section_label": _p3_default_view_section(target_model),
            "position_policy": "append_to_form_sheet_as_demo_group",
            "fields": fields,
            "llm_used": False,
        })

    for skipped in skipped_items:
        fg_gap_report_items.append({
            "source_node_key": skipped.get("node_key"),
            "candidate_name": skipped.get("candidate_name_ja"),
            "target_model": skipped.get("target_model"),
            "suggested_field_name": skipped.get("suggested_field_name"),
            "skip_reason": skipped.get("skip_reason") or "p3_skipped_for_odoo_burnin",
            "future_phase": skipped.get("future_phase") or "P4/P5",
            "customer_report_message": skipped.get("customer_report_message") or "検知しましたが、今回のP3デモ焼き込み対象から外しました。将来のF&G GAP / 開発対象として確認が必要です。",
        })

    app_counts: dict[str, int] = {}
    target_model_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for field in field_definitions:
        app_counts[str(field.get("app_key") or "unknown")] = app_counts.get(str(field.get("app_key") or "unknown"), 0) + 1
        target_model_counts[str(field.get("target_model") or "unknown")] = target_model_counts.get(str(field.get("target_model") or "unknown"), 0) + 1
        type_counts[str(field.get("ttype") or "unknown")] = type_counts.get(str(field.get("ttype") or "unknown"), 0) + 1

    return {
        "schema_name": "p3_addon_input",
        "version": "v1",
        "import_id": import_id,
        "source_inspection_schema": inspection.get("schema_name"),
        "plan_current_step": "C. P3 Addon Input生成",
        "plan_next_step": "D. P3 Addon Input Validate",
        "status": "p3_addon_input_generated",
        "policy": {
            "llm_used": False,
            "semantic_remap_used": False,
            "mechanical_rules_only": True,
            "auto_odoo_code_generation": False,
            "auto_odoo_apply": False,
            "human_approval_required_before_d": True,
        },
        "summary": {
            "master_definition_count": len(master_definitions),
            "field_definition_count": len(field_definitions),
            "view_placement_count": len(view_placements),
            "simple_logic_definition_count": len(simple_logic_definitions),
            "skipped_item_count": len(skipped_items),
            "fg_gap_report_item_count": len(fg_gap_report_items),
            "app_counts": app_counts,
            "target_model_counts": target_model_counts,
            "type_counts": type_counts,
        },
        "master_definitions": master_definitions,
        "field_definitions": field_definitions,
        "view_placements": view_placements,
        "simple_logic_definitions": simple_logic_definitions,
        "skipped_items": skipped_items,
        "fg_gap_report_items": fg_gap_report_items,
    }


def _p3_addon_input_markdown(addon_input: dict[str, Any]) -> str:
    summary = addon_input.get("summary") or {}
    lines = [
        "# P3 Addon Input",
        "",
        f"- import_id: {addon_input.get('import_id')}",
        f"- status: {addon_input.get('status')}",
        f"- current_step: {addon_input.get('plan_current_step')}",
        f"- next_step: {addon_input.get('plan_next_step')}",
        "",
        "## Summary",
        f"- master_definition_count: {summary.get('master_definition_count', 0)}",
        f"- field_definition_count: {summary.get('field_definition_count', 0)}",
        f"- view_placement_count: {summary.get('view_placement_count', 0)}",
        f"- simple_logic_definition_count: {summary.get('simple_logic_definition_count', 0)}",
        f"- fg_gap_report_item_count: {summary.get('fg_gap_report_item_count', 0)}",
        "",
        "## Master Definitions",
    ]
    for m in addon_input.get("master_definitions") or []:
        lines.append(f"- [{m.get('app_key')}] {m.get('model')} - {m.get('name')}")
    lines.extend(["", "## Field Definitions", ""])
    for f in addon_input.get("field_definitions") or []:
        relation = f" -> {f.get('relation_model')}" if f.get("relation_model") else ""
        lines.append(f"- [{f.get('app_key')}] {f.get('target_model')}.{f.get('field_name')} ({f.get('ttype')}{relation}) - {f.get('label_ja')}")
    lines.extend(["", "## Policy", "", "LLMは使用していません。意味的な再マッピング、自動コード生成、Odoo反映は行っていません。"])
    return "\n".join(lines) + "\n"


def _write_p3_addon_input_zip(import_id: str, out_dir: Path, addon_input: dict[str, Any]) -> Path:
    zip_path = out_dir / P3_ADDON_INPUT_ZIP_FILENAME
    report_path = out_dir / P3_ADDON_INPUT_REPORT_FILENAME
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(out_dir / P3_ADDON_INPUT_FILENAME, arcname=P3_ADDON_INPUT_FILENAME)
        if report_path.exists():
            zf.write(report_path, arcname=P3_ADDON_INPUT_REPORT_FILENAME)
        zf.writestr("README.md", "# P3 Addon Input Generated Pack\n\nThis pack is generated by C. P3 Addon Input生成. In the normal in-system flow, D validates the saved p3_addon_input.json directly without re-importing this ZIP. Use this ZIP for review, audit, or external handoff only.\n")
        zf.writestr("NEXT_THREAD_START_MESSAGE.md", f"添付した `{P3_ADDON_INPUT_ZIP_FILENAME}` はCで生成された確認・外部受け渡し用PACKです。通常フローでは同じシステムへ再Importせず、保存済みのp3_addon_input.jsonをD. P3 Addon Input Validateで検証してください。対象Import IDは `{import_id}` です。\n")
    return zip_path


@app.post("/p3/imports/{import_id}/addon-input")
def generate_p3_addon_input(import_id: str) -> dict[str, Any]:
    in_dir = _p3_import_dir(import_id)
    inspection_path = in_dir / P3_BURNIN_INSPECTION_FILENAME
    if not inspection_path.exists():
        raise HTTPException(status_code=404, detail="P3 burn-in inspection not found. Run B before C.")
    inspection = json.loads(inspection_path.read_text(encoding="utf-8"))
    addon_input = _build_p3_addon_input(import_id, inspection)
    (in_dir / P3_ADDON_INPUT_FILENAME).write_text(json.dumps(addon_input, ensure_ascii=False, indent=2), encoding="utf-8")
    (in_dir / P3_ADDON_INPUT_REPORT_FILENAME).write_text(_p3_addon_input_markdown(addon_input), encoding="utf-8")
    zip_path = _write_p3_addon_input_zip(import_id, in_dir, addon_input)
    summary_path = in_dir / "import_summary.json"
    if summary_path.exists():
        raw = json.loads(summary_path.read_text(encoding="utf-8"))
        raw["status"] = "p3_addon_input_generated"
        raw["p3_addon_input_summary"] = addon_input.get("summary") or {}
        raw.setdefault("links", {})["p3_addon_input"] = f"/p3/imports/{import_id}/addon-input"
        raw.setdefault("links", {})["p3_addon_input_report"] = f"/p3/imports/{import_id}/addon-input-report.md"
        raw.setdefault("links", {})["p3_addon_input_zip"] = f"/p3/imports/{import_id}/addon-input.zip"
        raw.setdefault("plan", {})["current_step"] = "C. P3 Addon Input生成"
        raw.setdefault("plan", {})["next_step"] = "D. P3 Addon Input Validate"
        for st in raw.get("phase_statuses", []):
            if st.get("phase_key") == "C":
                st["status"] = "p3_addon_input_generated"
                st["addon_input_generated"] = True
                st["summary"] = addon_input.get("summary") or {}
            elif st.get("phase_key") == "D":
                st["status"] = "next_ready"
            elif st.get("phase_key") == "B":
                st["status"] = "ready_for_p3_addon_input_generation"
        summary_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        (in_dir / "phase_statuses.json").write_text(json.dumps(raw.get("phase_statuses") or [], ensure_ascii=False, indent=2), encoding="utf-8")
    result = dict(addon_input)
    result["download_url"] = f"/p3/imports/{import_id}/addon-input.zip"
    result["zip_path"] = str(zip_path)
    return result



def _p3_addon_validation_issue(severity: str, code: str, message: str, *, path: str | None = None, item_key: str | None = None) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "path": path,
        "item_key": item_key,
    }


def _normalize_p3_gap_item(item: dict[str, Any], idx: int) -> dict[str, Any]:
    normalized = dict(item or {})
    normalized.setdefault("source_node_key", normalized.get("node_key") or normalized.get("source_key") or f"p3_gap_item::{idx}")
    normalized.setdefault("candidate_name", normalized.get("candidate_name_ja") or normalized.get("label_ja") or normalized.get("suggested_field_name") or "未確定候補")
    normalized.setdefault("target_model", normalized.get("target_model") or "")
    normalized.setdefault("suggested_field_name", normalized.get("suggested_field_name") or normalized.get("field_name") or "")
    normalized["skip_reason"] = normalized.get("skip_reason") or "p3_skipped_for_odoo_burnin"
    normalized["future_phase"] = normalized.get("future_phase") or "P4/P5"
    normalized["customer_report_message"] = normalized.get("customer_report_message") or "検知しましたが、今回のP3デモ焼き込み対象から外しました。将来のF&G GAP / 開発対象として確認が必要です。"
    normalized.setdefault("llm_used", False)
    normalized.setdefault("semantic_remap_used", False)
    return normalized


def _validate_p3_addon_input(import_id: str, addon_input: dict[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    normalized = json.loads(json.dumps(addon_input, ensure_ascii=False))

    if normalized.get("schema_name") != "p3_addon_input":
        issues.append(_p3_addon_validation_issue("error", "invalid_schema_name", "schema_name must be p3_addon_input", path="schema_name"))
    if normalized.get("import_id") and normalized.get("import_id") != import_id:
        warnings.append(_p3_addon_validation_issue("warning", "source_import_id_mismatch", "addon_input.import_id differs from URL import_id", path="import_id"))

    master_definitions = list(normalized.get("master_definitions") or [])
    field_definitions = list(normalized.get("field_definitions") or [])
    view_placements = list(normalized.get("view_placements") or [])
    simple_logic_definitions = list(normalized.get("simple_logic_definitions") or [])
    skipped_items = list(normalized.get("skipped_items") or [])
    fg_gap_report_items = list(normalized.get("fg_gap_report_items") or [])

    master_by_model: dict[str, dict[str, Any]] = {}
    duplicate_master_models: set[str] = set()
    for idx, master in enumerate(master_definitions):
        model = str(master.get("model") or master.get("technical_name") or master.get("support_model") or "").strip()
        name = str(master.get("name") or master.get("display_name") or master.get("support_master_name") or "").strip()
        if not model:
            issues.append(_p3_addon_validation_issue("error", "master_model_missing", "master_definitions[].model is required", path=f"master_definitions[{idx}].model", item_key=master.get("node_key")))
            continue
        if not model.startswith("x_fg_p3_"):
            warnings.append(_p3_addon_validation_issue("warning", "master_model_not_p3_prefixed", "support master model should usually start with x_fg_p3_", path=f"master_definitions[{idx}].model", item_key=model))
        if not name:
            issues.append(_p3_addon_validation_issue("error", "master_name_missing", "master_definitions[].name/display_name is required", path=f"master_definitions[{idx}].name", item_key=model))
        for key in ["code_field", "name_field", "active_field"]:
            if not master.get(key):
                issues.append(_p3_addon_validation_issue("error", f"master_{key}_missing", f"master_definitions[].{key} is required", path=f"master_definitions[{idx}].{key}", item_key=model))
        if model in master_by_model:
            duplicate_master_models.add(model)
        master_by_model[model] = master
    for model in sorted(duplicate_master_models):
        warnings.append(_p3_addon_validation_issue("warning", "duplicate_master_model", "Duplicate support master model name; keep for D audit, resolve before codegen if needed", path="master_definitions", item_key=model))

    field_keys: set[tuple[str, str]] = set()
    duplicate_fields: set[tuple[str, str]] = set()
    allowed_field_types = set(P3_ALLOWED_FIELD_TYPES)
    relation_models_used: set[str] = set()
    for idx, field in enumerate(field_definitions):
        target_model = str(field.get("target_model") or "").strip()
        field_name = str(field.get("field_name") or "").strip()
        ttype = str(field.get("ttype") or "").strip()
        item_key = field.get("node_key") or f"{target_model}.{field_name}"
        if not target_model:
            issues.append(_p3_addon_validation_issue("error", "field_target_model_missing", "field_definitions[].target_model is required", path=f"field_definitions[{idx}].target_model", item_key=item_key))
        if not field_name:
            issues.append(_p3_addon_validation_issue("error", "field_name_missing", "field_definitions[].field_name is required", path=f"field_definitions[{idx}].field_name", item_key=item_key))
        elif not re.match(r"^x_[a-z0-9_]+$", field_name):
            issues.append(_p3_addon_validation_issue("error", "invalid_custom_field_name", "field_name must be an Odoo custom field name like x_fg_*", path=f"field_definitions[{idx}].field_name", item_key=field_name))
        if not ttype:
            issues.append(_p3_addon_validation_issue("error", "field_type_missing", "field_definitions[].ttype is required", path=f"field_definitions[{idx}].ttype", item_key=item_key))
        elif ttype not in allowed_field_types:
            issues.append(_p3_addon_validation_issue("error", "unsupported_field_type", f"Unsupported ttype: {ttype}", path=f"field_definitions[{idx}].ttype", item_key=item_key))
        key = (target_model, field_name)
        if target_model and field_name:
            if key in field_keys:
                duplicate_fields.add(key)
            field_keys.add(key)
        if ttype == "many2one":
            relation_model = str(field.get("relation_model") or "").strip()
            if not relation_model:
                issues.append(_p3_addon_validation_issue("error", "many2one_relation_model_missing", "many2one field requires relation_model", path=f"field_definitions[{idx}].relation_model", item_key=item_key))
            elif relation_model not in master_by_model:
                issues.append(_p3_addon_validation_issue("error", "relation_master_missing", "relation_model does not exist in master_definitions", path=f"field_definitions[{idx}].relation_model", item_key=relation_model))
            else:
                relation_models_used.add(relation_model)
    for target_model, field_name in sorted(duplicate_fields):
        warnings.append(_p3_addon_validation_issue("warning", "duplicate_field_definition", "Duplicate field definition for target_model + field_name; keep for D audit, resolve before codegen if needed", path="field_definitions", item_key=f"{target_model}.{field_name}"))

    unused_masters = sorted(set(master_by_model) - relation_models_used)
    for model in unused_masters:
        warnings.append(_p3_addon_validation_issue("warning", "unused_master_definition", "Support master is defined but not referenced by any many2one field", path="master_definitions", item_key=model))

    for idx, placement in enumerate(view_placements):
        target_model = str(placement.get("target_model") or "").strip()
        fields = list(placement.get("fields") or [])
        if not target_model:
            issues.append(_p3_addon_validation_issue("error", "view_target_model_missing", "view_placements[].target_model is required", path=f"view_placements[{idx}].target_model", item_key=placement.get("placement_key")))
        if not fields:
            warnings.append(_p3_addon_validation_issue("warning", "view_without_fields", "view placement has no fields", path=f"view_placements[{idx}].fields", item_key=placement.get("placement_key")))
        for fidx, field_ref in enumerate(fields):
            field_name = str(field_ref.get("field_name") or "").strip()
            if target_model and field_name and (target_model, field_name) not in field_keys:
                issues.append(_p3_addon_validation_issue("error", "view_field_not_defined", "view placement references a field not present in field_definitions", path=f"view_placements[{idx}].fields[{fidx}]", item_key=f"{target_model}.{field_name}"))

    for idx, logic in enumerate(simple_logic_definitions):
        target_model = str(logic.get("target_model") or "").strip()
        field_name = str(logic.get("field_name") or "").strip()
        if target_model and field_name and (target_model, field_name) not in field_keys:
            issues.append(_p3_addon_validation_issue("error", "logic_field_not_defined", "simple logic references a field not present in field_definitions", path=f"simple_logic_definitions[{idx}]", item_key=f"{target_model}.{field_name}"))

    normalized["skipped_items"] = [_normalize_p3_gap_item(item, idx) for idx, item in enumerate(skipped_items)]
    normalized["fg_gap_report_items"] = [_normalize_p3_gap_item(item, idx) for idx, item in enumerate(fg_gap_report_items)]
    normalized.setdefault("policy", {})["validated_without_reimport"] = True
    normalized.setdefault("policy", {})["external_import_required"] = False
    normalized["plan_current_step"] = "D. P3 Addon Input Validate"
    normalized["plan_next_step"] = "E. Odoo Codegen Material Pack Export"

    error_count = sum(1 for issue in issues if issue.get("severity") == "error")
    warning_count = len(warnings) + sum(1 for issue in issues if issue.get("severity") == "warning")
    status = "p3_addon_input_validated" if error_count == 0 else "p3_addon_input_validation_failed"
    validation = {
        "schema_name": "p3_addon_input_validation_result",
        "version": "v1",
        "import_id": import_id,
        "status": status,
        "valid": error_count == 0,
        "validated_without_reimport": True,
        "external_import_required": False,
        "plan_current_step": "D. P3 Addon Input Validate",
        "plan_next_step": "E. Odoo Codegen Material Pack Export" if error_count == 0 else "C. P3 Addon Input生成 / data correction",
        "summary": {
            "master_definition_count": len(master_definitions),
            "field_definition_count": len(field_definitions),
            "view_placement_count": len(view_placements),
            "simple_logic_definition_count": len(simple_logic_definitions),
            "skipped_item_count": len(normalized.get("skipped_items") or []),
            "fg_gap_report_item_count": len(normalized.get("fg_gap_report_items") or []),
            "many2one_field_count": sum(1 for field in field_definitions if field.get("ttype") == "many2one"),
            "simple_field_count": sum(1 for field in field_definitions if field.get("ttype") != "many2one"),
            "relation_model_count": len(relation_models_used),
            "unused_master_count": len(unused_masters),
            "error_count": error_count,
            "warning_count": warning_count,
        },
        "errors": [issue for issue in issues if issue.get("severity") == "error"],
        "warnings": warnings + [issue for issue in issues if issue.get("severity") == "warning"],
        "normalized_addon_input_filename": P3_ADDON_INPUT_VALIDATED_FILENAME,
    }
    normalized["status"] = status
    normalized["valid"] = error_count == 0
    normalized["validation_status"] = status
    normalized["validation_summary"] = validation["summary"]
    return {"validation": validation, "normalized_addon_input": normalized}


def _p3_addon_input_validation_markdown(validation: dict[str, Any]) -> str:
    summary = validation.get("summary") or {}
    lines = [
        "# P3 Addon Input Validation",
        "",
        f"- import_id: {validation.get('import_id')}",
        f"- status: {validation.get('status')}",
        f"- valid: {validation.get('valid')}",
        f"- current_step: {validation.get('plan_current_step')}",
        f"- next_step: {validation.get('plan_next_step')}",
        f"- validated_without_reimport: {validation.get('validated_without_reimport')}",
        "",
        "## Summary",
        f"- master_definition_count: {summary.get('master_definition_count', 0)}",
        f"- field_definition_count: {summary.get('field_definition_count', 0)}",
        f"- many2one_field_count: {summary.get('many2one_field_count', 0)}",
        f"- simple_field_count: {summary.get('simple_field_count', 0)}",
        f"- view_placement_count: {summary.get('view_placement_count', 0)}",
        f"- simple_logic_definition_count: {summary.get('simple_logic_definition_count', 0)}",
        f"- error_count: {summary.get('error_count', 0)}",
        f"- warning_count: {summary.get('warning_count', 0)}",
        "",
        "## Errors",
    ]
    errors = validation.get("errors") or []
    if errors:
        for issue in errors:
            lines.append(f"- {issue.get('code')}: {issue.get('message')} ({issue.get('item_key') or issue.get('path') or '-'})")
    else:
        lines.append("- none")
    lines.extend(["", "## Warnings"])
    warnings = validation.get("warnings") or []
    if warnings:
        for issue in warnings[:100]:
            lines.append(f"- {issue.get('code')}: {issue.get('message')} ({issue.get('item_key') or issue.get('path') or '-'})")
    else:
        lines.append("- none")
    lines.extend(["", "## Policy", "", "DはCで生成・保存済みのp3_addon_input.jsonを検証します。同じZIPを再Importしません。外部編集済みZIPの持ち込みはOptional工程です。"])
    return "\n".join(lines) + "\n"


@app.post("/p3/imports/{import_id}/addon-input/validate")
def validate_p3_addon_input(import_id: str) -> dict[str, Any]:
    in_dir = _p3_import_dir(import_id)
    addon_path = in_dir / P3_ADDON_INPUT_FILENAME
    if not addon_path.exists():
        raise HTTPException(status_code=404, detail="P3 Addon Input not found. Run C before D Validate.")
    addon_input = json.loads(addon_path.read_text(encoding="utf-8"))
    result = _validate_p3_addon_input(import_id, addon_input)
    validation = result["validation"]
    normalized = result["normalized_addon_input"]
    (in_dir / P3_ADDON_INPUT_VALIDATED_FILENAME).write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    (in_dir / P3_ADDON_INPUT_VALIDATION_FILENAME).write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    (in_dir / P3_ADDON_INPUT_VALIDATION_REPORT_FILENAME).write_text(_p3_addon_input_validation_markdown(validation), encoding="utf-8")
    summary_path = in_dir / "import_summary.json"
    if summary_path.exists():
        raw = json.loads(summary_path.read_text(encoding="utf-8"))
        raw["status"] = validation.get("status")
        raw["p3_addon_input_validation_summary"] = validation.get("summary") or {}
        raw.setdefault("links", {})["p3_addon_input_validation"] = f"/p3/imports/{import_id}/addon-input/validation"
        raw.setdefault("links", {})["p3_addon_input_validation_report"] = f"/p3/imports/{import_id}/addon-input-validation-report.md"
        raw.setdefault("links", {})["p3_addon_input_validated"] = f"/p3/imports/{import_id}/addon-input/validated"
        raw.setdefault("plan", {})
        raw["plan"]["current_step"] = "D. P3 Addon Input Validate"
        raw["plan"]["next_step"] = validation.get("plan_next_step")
        for st in raw.get("phase_statuses", []):
            if st.get("phase_key") == "C":
                st["label"] = "C. P3 Addon Input生成"
                st["status"] = "p3_addon_input_generated"
                st["addon_input_generated"] = True
            elif st.get("phase_key") == "D":
                st["label"] = "D. P3 Addon Input Validate"
                st["status"] = validation.get("status")
                st["validated_without_reimport"] = True
                st["validation_summary"] = validation.get("summary") or {}
                st["validation_generated"] = True
            elif st.get("phase_key") == "E":
                st["label"] = "E. Odoo Codegen Material Pack Export"
                st["status"] = "next_ready" if validation.get("valid") else "not_started"
        raw["phase_statuses"] = raw.get("phase_statuses") or []
        summary_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        (in_dir / "phase_statuses.json").write_text(json.dumps(raw.get("phase_statuses") or [], ensure_ascii=False, indent=2), encoding="utf-8")
    return validation


@app.get("/p3/imports/{import_id}/addon-input/validation")
def read_p3_addon_input_validation(import_id: str) -> dict[str, Any]:
    path = _p3_import_dir(import_id) / P3_ADDON_INPUT_VALIDATION_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 Addon Input validation result not found. Run POST /p3/imports/{import_id}/addon-input/validate first.")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/p3/imports/{import_id}/addon-input/validated")
def read_p3_addon_input_validated(import_id: str) -> dict[str, Any]:
    path = _p3_import_dir(import_id) / P3_ADDON_INPUT_VALIDATED_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="Validated P3 Addon Input not found. Run D Validate first.")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/p3/imports/{import_id}/addon-input-validation-report.md")
def read_p3_addon_input_validation_report(import_id: str) -> FileResponse:
    path = _p3_import_dir(import_id) / P3_ADDON_INPUT_VALIDATION_REPORT_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 Addon Input validation report not found")
    return FileResponse(str(path), filename=path.name, media_type="text/markdown")


@app.get("/p3/imports/{import_id}/addon-input")
def read_p3_addon_input(import_id: str) -> dict[str, Any]:
    path = _p3_import_dir(import_id) / P3_ADDON_INPUT_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 Addon Input not found. Run POST /p3/imports/{import_id}/addon-input first.")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/p3/imports/{import_id}/addon-input-report.md")
def read_p3_addon_input_report(import_id: str) -> FileResponse:
    path = _p3_import_dir(import_id) / P3_ADDON_INPUT_REPORT_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 Addon Input report not found")
    return FileResponse(str(path), filename=path.name, media_type="text/markdown")


@app.get("/p3/imports/{import_id}/addon-input.zip")
def download_p3_addon_input_zip(import_id: str) -> FileResponse:
    path = _p3_import_dir(import_id) / P3_ADDON_INPUT_ZIP_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 Addon Input ZIP not found. Generate C first.")
    return FileResponse(str(path), filename=path.name, media_type="application/zip")



def _p3_group_by(items: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items or []:
        grouped.setdefault(str(item.get(key) or "unknown"), []).append(item)
    return grouped



def _p3_slug(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "unknown"


def _p3_appwise_model_name(app_key: Any, model: Any) -> str:
    app = _p3_slug(app_key)
    raw = str(model or "").strip()
    base = raw
    if base.startswith("x_fg_p3_"):
        base = base[len("x_fg_p3_"):]
    base = _p3_slug(base)
    if base.startswith(f"{app}_"):
        return f"x_fg_p3_{base}"
    return f"x_fg_p3_{app}_{base}"


def _p3_appwise_field_name(app_key: Any, field_name: Any) -> str:
    app = _p3_slug(app_key)
    raw = str(field_name or "").strip()
    base = raw
    if base.startswith("x_fg_"):
        base = base[len("x_fg_"):]
    base = _p3_slug(base)
    if base.startswith(f"{app}_"):
        return f"x_fg_{base}"
    return f"x_fg_{app}_{base}"


def _p3_enrich_appwise_codegen_keys(masters: list[dict[str, Any]], fields: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    master_groups: dict[str, list[dict[str, Any]]] = {}
    for master in masters:
        model = str(master.get("model") or master.get("technical_name") or master.get("support_model") or "").strip()
        if model:
            master_groups.setdefault(model, []).append(master)
    duplicate_master_models = sorted([model for model, items in master_groups.items() if len(items) > 1])

    effective_model_by_node: dict[str, str] = {}
    effective_model_by_app_model: dict[tuple[str, str], str] = {}
    enriched_masters: list[dict[str, Any]] = []
    for master in masters:
        item = dict(master)
        model = str(item.get("model") or item.get("technical_name") or item.get("support_model") or "").strip()
        app_key = str(item.get("app_key") or "unknown").strip()
        if model in duplicate_master_models:
            effective_model = _p3_appwise_model_name(app_key, model)
            item["codegen_duplicate_resolution"] = "appwise_model_split"
        else:
            effective_model = model
            item["codegen_duplicate_resolution"] = "none"
        item["codegen_effective_model"] = effective_model
        item["codegen_effective_support_model"] = effective_model
        item["codegen_original_model"] = model
        node_key = str(item.get("node_key") or "").strip()
        if node_key:
            effective_model_by_node[node_key] = effective_model
        if app_key and model:
            effective_model_by_app_model[(app_key, model)] = effective_model
        enriched_masters.append(item)

    field_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for field in fields:
        target = str(field.get("target_model") or "").strip()
        name = str(field.get("field_name") or "").strip()
        if target and name:
            field_groups.setdefault((target, name), []).append(field)
    duplicate_field_keys = sorted([f"{target}.{name}" for (target, name), items in field_groups.items() if len(items) > 1])
    duplicate_field_set = set(duplicate_field_keys)

    enriched_fields: list[dict[str, Any]] = []
    selection_fallback_fields: list[str] = []
    for field in fields:
        item = dict(field)
        app_key = str(item.get("app_key") or "unknown").strip()
        target = str(item.get("target_model") or "").strip()
        name = str(item.get("field_name") or "").strip()
        key = f"{target}.{name}"
        if key in duplicate_field_set:
            effective_name = _p3_appwise_field_name(app_key, name)
            item["codegen_duplicate_resolution"] = "appwise_field_split"
        else:
            effective_name = name
            item["codegen_duplicate_resolution"] = "none"
        item["codegen_effective_field_name"] = effective_name
        item["codegen_original_field_name"] = name
        relation_model = str(item.get("relation_model") or "").strip()
        if relation_model:
            item["codegen_effective_relation_model"] = effective_model_by_app_model.get((app_key, relation_model), relation_model)
        if item.get("ttype") == "selection" and not item.get("selection_values"):
            item["codegen_effective_ttype"] = "char"
            item["codegen_selection_fallback"] = "char_no_business_safe_selection_values"
            selection_fallback_fields.append(key)
        else:
            item["codegen_effective_ttype"] = item.get("ttype")
        enriched_fields.append(item)

    return enriched_masters, enriched_fields, {
        "duplicate_master_models": duplicate_master_models,
        "duplicate_field_keys": duplicate_field_keys,
        "selection_fallback_fields": selection_fallback_fields,
    }

def _build_p3_codegen_material(import_id: str, addon_input: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    masters = list(addon_input.get("master_definitions") or [])
    fields = list(addon_input.get("field_definitions") or [])
    views = list(addon_input.get("view_placements") or [])
    logic = list(addon_input.get("simple_logic_definitions") or [])
    skipped = list(addon_input.get("skipped_items") or [])
    gaps = list(addon_input.get("fg_gap_report_items") or [])
    warnings = list(validation.get("warnings") or [])
    errors = list(validation.get("errors") or [])

    enriched_masters, enriched_fields, appwise_resolution = _p3_enrich_appwise_codegen_keys(masters, fields)
    duplicate_master_models = list(appwise_resolution.get("duplicate_master_models") or [])
    duplicate_field_keys = list(appwise_resolution.get("duplicate_field_keys") or [])
    selection_fallback_fields = list(appwise_resolution.get("selection_fallback_fields") or [])

    type_counts: dict[str, int] = {}
    effective_type_counts: dict[str, int] = {}
    model_counts: dict[str, int] = {}
    app_counts: dict[str, int] = {}
    for f in enriched_fields:
        type_counts[str(f.get("ttype") or "unknown")] = type_counts.get(str(f.get("ttype") or "unknown"), 0) + 1
        effective_type_counts[str(f.get("codegen_effective_ttype") or f.get("ttype") or "unknown")] = effective_type_counts.get(str(f.get("codegen_effective_ttype") or f.get("ttype") or "unknown"), 0) + 1
        model_counts[str(f.get("target_model") or "unknown")] = model_counts.get(str(f.get("target_model") or "unknown"), 0) + 1
        app_counts[str(f.get("app_key") or "unknown")] = app_counts.get(str(f.get("app_key") or "unknown"), 0) + 1

    return {
        "schema_name": "p3_odoo_codegen_material",
        "version": "v1",
        "import_id": import_id,
        "source_addon_input_schema": addon_input.get("schema_name"),
        "source_validation_schema": validation.get("schema_name"),
        "status": "p3_codegen_material_exported",
        "plan_current_step": "E. Odoo Codegen Material Pack Export",
        "plan_next_step": "F. Generated Odoo Code Pack Import / Validate",
        "policy": {
            "llm_used_in_system": False,
            "system_generates_odoo_code": False,
            "system_applies_odoo_code": False,
            "chatgpt_generates_code_from_exported_pack": True,
            "semantic_remap_allowed": False,
            "mechanical_dedup_allowed_for_same_technical_key": False,
            "appwise_duplicate_merge_forbidden": True,
            "selection_placeholder_values_forbidden": True,
            "human_approval_required_before_generated_code_import": True,
        },
        "validation_gate": {
            "valid": bool(validation.get("valid")),
            "error_count": int((validation.get("summary") or {}).get("error_count") or 0),
            "warning_count": int((validation.get("summary") or {}).get("warning_count") or 0),
            "warnings_forwarded_to_codegen_pack": warnings,
            "errors_forwarded_to_codegen_pack": errors,
            "codegen_blocked": bool(errors),
        },
        "summary": {
            "master_definition_count": len(masters),
            "field_definition_count": len(fields),
            "view_placement_count": len(views),
            "simple_logic_definition_count": len(logic),
            "skipped_item_count": len(skipped),
            "fg_gap_report_item_count": len(gaps),
            "duplicate_master_model_count": len(duplicate_master_models),
            "duplicate_field_definition_count": len(duplicate_field_keys),
            "selection_fallback_field_count": len(selection_fallback_fields),
            "type_counts": type_counts,
            "effective_type_counts": effective_type_counts,
            "target_model_counts": model_counts,
            "app_counts": app_counts,
        },
        "codegen_appwise_policy": {
            "duplicate_master_models": duplicate_master_models,
            "duplicate_field_keys": duplicate_field_keys,
            "selection_fallback_fields": selection_fallback_fields,
            "rule": "Do not merge duplicate master or field definitions across app_key. Use codegen_effective_model, codegen_effective_field_name, codegen_effective_relation_model, and codegen_effective_ttype exactly. If a selection field has no business-safe selection_values, generate a Char field fallback and record it in P3_CODEGEN_REPORT.md. Never emit placeholder options such as P3 Option 1..3.",
        },
        "addon_target": {
            "addon_name": "fg_p3_demo_extension",
            "technical_module_name": "fg_p3_demo_extension",
            "odoo_version_assumption": "Odoo 19 unless repository/runtime config states otherwise",
            "installable": True,
            "application": False,
            "auto_install": False,
        },
        "master_definitions": enriched_masters,
        "field_definitions": enriched_fields,
        "view_placements": views,
        "simple_logic_definitions": logic,
        "skipped_items": skipped,
        "fg_gap_report_items": gaps,
    }


def _p3_expected_output_schema() -> dict[str, Any]:
    return {
        "schema_name": "p3_generated_odoo_code_pack_expected_output",
        "version": "v1",
        "required_zip_name": "P3_GENERATED_ODOO_CODE_PACK.zip",
        "required_files": [
            "fg_p3_demo_extension/__manifest__.py",
            "fg_p3_demo_extension/__init__.py",
            "fg_p3_demo_extension/models/__init__.py",
            "fg_p3_demo_extension/models/fg_p3_demo_extension.py",
            "fg_p3_demo_extension/security/ir.model.access.csv",
            "fg_p3_demo_extension/views/fg_p3_demo_extension_views.xml",
            "fg_p3_demo_extension/data/fg_p3_master_values.xml",
            "fg_p3_demo_extension/README.md",
            "P3_CODEGEN_REPORT.md",
            "README.md",
        ],
        "must_not_include": [
            "network calls",
            "external python dependencies",
            "destructive overrides of standard Odoo business flows",
            "semantic remapping not present in the material pack",
        ],
    }


def _p3_codegen_prompt(material: dict[str, Any]) -> str:
    return f"""# P3 Odoo Codegen Prompt

Use the attached P3 Odoo Codegen Material Pack to generate an Odoo addon ZIP.

## Goal
Generate `P3_GENERATED_ODOO_CODE_PACK.zip` containing an Odoo demo addon named `fg_p3_demo_extension`.

The addon should make P3 demo content visible in Odoo: support masters, `x_fg_*` fields on standard models, demo form sections, search filters where safe, simple indicator-only logic, access control, and demo master records.

## Strict policy
- Do not use external network access.
- Do not add external dependencies.
- Do not guess missing business semantics.
- Do not auto-resolve GAP/skipped items.
- Do not change standard Odoo workflow behavior destructively.
- Do not merge duplicate master models across different `app_key` values.
- Do not merge duplicate field definitions across different `app_key` values.
- Use `codegen_effective_model`, `codegen_effective_field_name`, `codegen_effective_relation_model`, and `codegen_effective_ttype` exactly when present.
- If a selection field has no business-safe `selection_values`, do not create placeholder options such as `P3 Option 1..3`; use the provided `codegen_effective_ttype` fallback and record it in `P3_CODEGEN_REPORT.md`.
- Never generate placeholder demo master values such as `Demo 1`, `Demo 2`, `Demo 3`, or generic `P3 Option` labels in `data/` or `demo/` XML/CSV/JSON files.
- Every support-master seed record must have a business-meaningful `name` value derived from the material label/app context. If exact values are not known, use safe generic business values such as `標準`, `要確認`, `保留`, `通常`, `例外` combined with the master label; do not use `Demo` labels.
- For P3 usability/enhancement packs, include an idempotent update path for existing installed databases where older XML IDs were loaded with `noupdate=true`. Prefer a `post_init_hook` or module upgrade hook that rewrites only `x_fg_p3_*` demo master names that still contain `Demo 1` / `Demo 2` / `Demo 3`.
- Use the current available default model/environment. Do not specify or require a particular model name.

## Input files
- `01_p3_addon_input_validated.json`
- `02_p3_codegen_material.json`
- `03_expected_output_schema.json`
- `05_validation_policy.md`
- `06_skipped_items_report.md`
- `08_warning_resolution_notes.md`

## Expected output
Create `P3_GENERATED_ODOO_CODE_PACK.zip` with the structure described in `03_expected_output_schema.json`.

## Current material summary
- import_id: {material.get('import_id')}
- masters: {(material.get('summary') or {}).get('master_definition_count')}
- fields: {(material.get('summary') or {}).get('field_definition_count')}
- views: {(material.get('summary') or {}).get('view_placement_count')}
- simple logic: {(material.get('summary') or {}).get('simple_logic_definition_count')}
- skipped/GAP: {(material.get('summary') or {}).get('fg_gap_report_item_count')}
"""


def _p3_validation_policy_md(material: dict[str, Any]) -> str:
    return """# P3 Codegen Validation Policy

The generated Odoo code pack will later be imported back into the system and validated before Apply.

## Required checks
- ZIP structure must match expected schema.
- `__manifest__.py` must be parseable.
- Python files must compile.
- XML files must parse.
- No external dependencies may be introduced.
- `security/ir.model.access.csv` must exist for generated support master models.
- many2one fields must reference generated support master models from the material pack.
- skipped/GAP items must remain report-only and must not be silently implemented.

## App-wise duplicate handling
Duplicate master models or duplicate target_model.field_name entries are warnings from D. For P3, app-specific semantics must be preserved.

- Do not merge duplicates across different `app_key` values.
- Use `codegen_effective_model` for generated support master model names.
- Use `codegen_effective_field_name` for generated standard-model extension field names.
- Use `codegen_effective_relation_model` for many2one comodel references.
- Use `codegen_effective_ttype` for field type generation.
- If `codegen_selection_fallback` is present, generate a Char field fallback and record the original selection request in `P3_CODEGEN_REPORT.md`.
- Do not emit placeholder selection values such as `P3 Option 1`, `P3 Option 2`, or `P3 Option 3`.
- Do not emit placeholder master/demo names such as `Demo 1`, `Demo 2`, or `Demo 3` anywhere under `data/` or `demo/`.
- P3 usability-enhanced packs must be upgrade-safe against existing `noupdate=true` demo records. If older records already contain `Demo 1` / `Demo 2`, generated code must update only the P3 demo support-master tables/records to meaningful names during install/upgrade.
"""


def _p3_skipped_report_md(material: dict[str, Any]) -> str:
    lines = ["# P3 skipped / F&G GAP report", ""]
    gaps = material.get("fg_gap_report_items") or []
    if not gaps:
        lines.append("No skipped/GAP items.")
    for g in gaps:
        lines.append(f"- {g.get('candidate_name') or g.get('candidate_name_ja') or g.get('source_node_key')}: {g.get('skip_reason')} / future_phase={g.get('future_phase')}")
        msg = g.get("customer_report_message")
        if msg:
            lines.append(f"  - {msg}")
    return "\n".join(lines) + "\n"


def _p3_warning_notes_md(material: dict[str, Any]) -> str:
    validation_gate = material.get("validation_gate") or {}
    warnings = validation_gate.get("warnings_forwarded_to_codegen_pack") or []
    dedup = material.get("codegen_appwise_policy") or {}
    lines = ["# P3 Codegen Warning Notes", "", "These warnings came from D. P3 Addon Input Validate and must be visible to the code generation thread.", ""]
    if warnings:
        for w in warnings:
            lines.append(f"- {w.get('code')}: {w.get('message')} ({w.get('item_key') or w.get('path') or '-'})")
    else:
        lines.append("- none")
    lines.extend([
        "",
        "## App-wise codegen policy",
        dedup.get("rule") or "N/A",
        "",
        f"- duplicate_master_models: {dedup.get('duplicate_master_models') or []}",
        f"- duplicate_field_keys: {dedup.get('duplicate_field_keys') or []}",
        f"- selection_fallback_fields: {dedup.get('selection_fallback_fields') or []}",
        "",
        "## Demo value hardening",
        "- Demo 1 / Demo 2 / Demo 3 are forbidden in generated master values.",
        "- Use meaningful Japanese business values for every P3 support master seed.",
        "- Include an upgrade-safe hook/update routine for old noupdate=true Demo records when generating usability enhancement code.",
    ])
    return "\n".join(lines) + "\n"


def _p3_codegen_material_report_md(material: dict[str, Any]) -> str:
    summary = material.get("summary") or {}
    gate = material.get("validation_gate") or {}
    lines = [
        "# P3 Odoo Codegen Material Pack Report",
        "",
        f"- import_id: {material.get('import_id')}",
        f"- status: {material.get('status')}",
        f"- current_step: {material.get('plan_current_step')}",
        f"- next_step: {material.get('plan_next_step')}",
        f"- validation_valid: {gate.get('valid')}",
        f"- codegen_blocked: {gate.get('codegen_blocked')}",
        "",
        "## Summary",
        f"- master_definition_count: {summary.get('master_definition_count', 0)}",
        f"- field_definition_count: {summary.get('field_definition_count', 0)}",
        f"- view_placement_count: {summary.get('view_placement_count', 0)}",
        f"- simple_logic_definition_count: {summary.get('simple_logic_definition_count', 0)}",
        f"- fg_gap_report_item_count: {summary.get('fg_gap_report_item_count', 0)}",
        f"- duplicate_master_model_count: {summary.get('duplicate_master_model_count', 0)}",
        f"- duplicate_field_definition_count: {summary.get('duplicate_field_definition_count', 0)}",
        "",
        "## Policy",
        "This step exports material only. It does not generate Odoo code and does not apply anything to Odoo.",
    ]
    return "\n".join(lines) + "\n"


def _write_p3_codegen_material_zip(import_id: str, out_dir: Path, addon_input: dict[str, Any], validation: dict[str, Any], material: dict[str, Any]) -> Path:
    zip_path = out_dir / P3_CODEGEN_MATERIAL_ZIP_FILENAME
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("START_HERE.md", "# START HERE - P3 Odoo Codegen Material Pack\n\nRead `NEXT_THREAD_START_MESSAGE.md` and generate an Odoo code pack from the included validated materials.\n")
        zf.writestr("NEXT_THREAD_START_MESSAGE.md", f"添付した `{P3_CODEGEN_MATERIAL_ZIP_FILENAME}` を使って、P3用OdooコードPACKを生成してください。まず `START_HERE.md`、`04_codegen_prompt.md`、`05_validation_policy.md` を読み、`03_expected_output_schema.json` に従って `P3_GENERATED_ODOO_CODE_PACK.zip` を作成してください。対象Import IDは `{import_id}` です。\n")
        zf.writestr("01_p3_addon_input_validated.json", json.dumps(addon_input, ensure_ascii=False, indent=2))
        zf.writestr(P3_CODEGEN_MATERIAL_FILENAME, json.dumps(material, ensure_ascii=False, indent=2))
        zf.writestr(P3_CODEGEN_EXPECTED_OUTPUT_SCHEMA_FILENAME, json.dumps(_p3_expected_output_schema(), ensure_ascii=False, indent=2))
        zf.writestr(P3_CODEGEN_PROMPT_FILENAME, _p3_codegen_prompt(material))
        zf.writestr(P3_CODEGEN_VALIDATION_POLICY_FILENAME, _p3_validation_policy_md(material))
        zf.writestr(P3_CODEGEN_SKIPPED_REPORT_FILENAME, _p3_skipped_report_md(material))
        zf.writestr("07_d_validation_result.json", json.dumps(validation, ensure_ascii=False, indent=2))
        zf.writestr(P3_CODEGEN_WARNING_NOTES_FILENAME, _p3_warning_notes_md(material))
        zf.writestr("README.md", "# P3 Odoo Codegen Material Pack\n\nThis pack is exported by E. It is for ChatGPT-side Odoo code generation. The system has not generated or applied Odoo code in this step.\n")
    return zip_path


@app.post("/p3/imports/{import_id}/codegen-material-pack")
def export_p3_codegen_material_pack(import_id: str) -> dict[str, Any]:
    in_dir = _p3_import_dir(import_id)
    validated_path = in_dir / P3_ADDON_INPUT_VALIDATED_FILENAME
    validation_path = in_dir / P3_ADDON_INPUT_VALIDATION_FILENAME
    if not validated_path.exists() or not validation_path.exists():
        raise HTTPException(status_code=404, detail="Validated P3 Addon Input not found. Run D Validate before E.")
    addon_input = json.loads(validated_path.read_text(encoding="utf-8"))
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if not validation.get("valid"):
        raise HTTPException(status_code=400, detail="Cannot export codegen material pack while D validation is invalid.")
    material = _build_p3_codegen_material(import_id, addon_input, validation)
    (in_dir / P3_CODEGEN_MATERIAL_FILENAME).write_text(json.dumps(material, ensure_ascii=False, indent=2), encoding="utf-8")
    (in_dir / P3_CODEGEN_MATERIAL_REPORT_FILENAME).write_text(_p3_codegen_material_report_md(material), encoding="utf-8")
    zip_path = _write_p3_codegen_material_zip(import_id, in_dir, addon_input, validation, material)
    summary_path = in_dir / "import_summary.json"
    if summary_path.exists():
        raw = json.loads(summary_path.read_text(encoding="utf-8"))
        raw["status"] = "p3_codegen_material_pack_exported"
        raw["p3_codegen_material_summary"] = material.get("summary") or {}
        raw.setdefault("links", {})["p3_codegen_material"] = f"/p3/imports/{import_id}/codegen-material-pack"
        raw.setdefault("links", {})["p3_codegen_material_report"] = f"/p3/imports/{import_id}/codegen-material-pack-report.md"
        raw.setdefault("links", {})["p3_codegen_material_zip"] = f"/p3/imports/{import_id}/codegen-material-pack.zip"
        raw.setdefault("plan", {})
        raw["plan"]["current_step"] = "E. Odoo Codegen Material Pack Export"
        raw["plan"]["next_step"] = "F. Generated Odoo Code Pack Import / Validate"
        for st in raw.get("phase_statuses", []):
            if st.get("phase_key") == "D":
                st["label"] = "D. P3 Addon Input Validate"
                st["status"] = "p3_addon_input_validated"
            elif st.get("phase_key") == "E":
                st["label"] = "E. Odoo Codegen Material Pack Export"
                st["status"] = "p3_codegen_material_pack_exported"
                st["codegen_material_exported"] = True
                st["download_url"] = f"/p3/imports/{import_id}/codegen-material-pack.zip"
                st["summary"] = material.get("summary") or {}
            elif st.get("phase_key") == "F":
                st["label"] = "F. Generated Odoo Code Pack Import / Validate"
                st["status"] = "next_ready"
        summary_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        (in_dir / "phase_statuses.json").write_text(json.dumps(raw.get("phase_statuses") or [], ensure_ascii=False, indent=2), encoding="utf-8")
    result = {
        "schema_name": "p3_codegen_material_pack_export_result",
        "version": "v1",
        "import_id": import_id,
        "status": "p3_codegen_material_pack_exported",
        "plan_current_step": "E. Odoo Codegen Material Pack Export",
        "plan_next_step": "F. Generated Odoo Code Pack Import / Validate",
        "summary": material.get("summary") or {},
        "validation_gate": material.get("validation_gate") or {},
        "download_url": f"/p3/imports/{import_id}/codegen-material-pack.zip",
        "report_url": f"/p3/imports/{import_id}/codegen-material-pack-report.md",
    }
    return result


@app.get("/p3/imports/{import_id}/codegen-material-pack")
def read_p3_codegen_material(import_id: str) -> dict[str, Any]:
    path = _p3_import_dir(import_id) / P3_CODEGEN_MATERIAL_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 codegen material not found. Run E export first.")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/p3/imports/{import_id}/codegen-material-pack-report.md")
def read_p3_codegen_material_report(import_id: str) -> FileResponse:
    path = _p3_import_dir(import_id) / P3_CODEGEN_MATERIAL_REPORT_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 codegen material report not found")
    return FileResponse(str(path), filename=path.name, media_type="text/markdown")


@app.get("/p3/imports/{import_id}/codegen-material-pack.zip")
def download_p3_codegen_material_pack(import_id: str) -> FileResponse:
    path = _p3_import_dir(import_id) / P3_CODEGEN_MATERIAL_ZIP_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 codegen material pack ZIP not found. Run E export first.")
    return FileResponse(str(path), filename=path.name, media_type="application/zip")



def _odoo_code_import_root() -> Path:
    root = ARTIFACT_ROOT / ODOO_CODE_IMPORT_ROOT_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _odoo_code_import_dir(code_import_id: str) -> Path:
    return _odoo_code_import_root() / code_import_id


def _safe_extract_zip(zip_path: Path, out_dir: Path) -> list[str]:
    names: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            names.append(name)
            normalized = Path(name)
            if name.startswith("/") or ".." in normalized.parts:
                raise HTTPException(status_code=400, detail=f"Unsafe ZIP entry: {name}")
        zf.extractall(out_dir)
    return names


def _find_odoo_addon_dirs(root: Path) -> list[Path]:
    addons: list[Path] = []
    if (root / "__manifest__.py").exists():
        addons.append(root)
    for child in root.iterdir() if root.exists() else []:
        if child.is_dir() and (child / "__manifest__.py").exists():
            addons.append(child)
    return sorted(set(addons), key=lambda p: p.name)


def _parse_manifest(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = ast.literal_eval(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "manifest_not_dict"
        return data, None
    except Exception as exc:
        return None, f"manifest_parse_error: {exc}"


def _validate_python_file(path: Path) -> str | None:
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
        return None
    except Exception as exc:
        return f"python_compile_error: {exc}"


def _validate_xml_file(path: Path) -> str | None:
    try:
        ET.parse(path)
        return None
    except Exception as exc:
        return f"xml_parse_error: {exc}"


def _infer_odoo_code_pack_kind(filename: str, source_context: str | None = None) -> str:
    base = (filename or "").lower()
    ctx = (source_context or "").lower()
    text = f"{base} {ctx}"
    if "usability" in text or "enhance" in text or "enhancement" in text:
        return "p3_demo_usability_enhancement"
    if "patch" in text or "fix" in text or "repair" in text:
        return "odoo_code_patch"
    if "generated" in text or "code_pack" in text:
        return "generated_odoo_code_pack"
    return "generic_odoo_addon_code"



_FORBIDDEN_ODOO_ENTERPRISE_MODEL_REFS = (
    "quality.alert",
    "quality.check",
    "quality_control.",
)
_DEMO_PLACEHOLDER_PATTERNS = (
    "Demo 1",
    "Demo 2",
    "Demo 3",
    "P3 Option 1",
    "P3 Option 2",
    "P3 Option 3",
)


def _model_external_id(model_name: str) -> str:
    return "model_" + (model_name or "").replace(".", "_")


def _text_file_candidates(addon_dir: Path) -> list[Path]:
    suffixes = {".py", ".xml", ".csv", ".json", ".md", ".txt"}
    return [p for p in addon_dir.rglob("*") if p.is_file() and p.suffix.lower() in suffixes]


def _scan_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def _collect_declared_model_names(addon_dir: Path) -> dict[str, list[str]]:
    """Return Odoo models declared with _name in Python files.

    This intentionally ignores pure _inherit extensions because ir.model.access.csv
    should not contain access rows for inherited standard models. It is mainly used
    to catch generated access rows that point to models whose Python class was not
    generated, such as x_fg_p3_standard_exclusion_note.
    """
    import re

    declared: dict[str, list[str]] = {}
    pattern = re.compile(r"^\s*_name\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
    for py in addon_dir.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        text = _scan_text_file(py)
        for model_name in pattern.findall(text):
            declared.setdefault(model_name, []).append(str(py.relative_to(addon_dir)))
    return declared


def _collect_access_model_external_ids(addon_dir: Path) -> list[dict[str, str]]:
    import csv
    from io import StringIO

    rows: list[dict[str, str]] = []
    csv_path = addon_dir / "security" / "ir.model.access.csv"
    if not csv_path.exists():
        return rows
    text = _scan_text_file(csv_path)
    reader = csv.DictReader(StringIO(text))
    for idx, row in enumerate(reader, start=2):
        model_ref = (row.get("model_id:id") or row.get("model_id") or "").strip()
        if model_ref:
            rows.append({"line": str(idx), "model_external_id": model_ref})
    return rows


def _extra_odoo_addon_semantic_checks(addon_dir: Path, extract_dir: Path, manifest: dict[str, Any], pack_kind: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Run generated-addon semantic checks that Odoo parse alone cannot catch.

    These checks are deliberately mechanical. They do not infer business meaning;
    they only prevent known breakages observed in P3 usability packs:
    - Enterprise Quality refs remaining after quality_control was removed
    - access CSV rows for missing Python models
    - duplicate _name model declarations
    - placeholder Demo 1 / Demo 2 values in usability enhancement data
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    rel_addon = str(addon_dir.relative_to(extract_dir))

    depends = manifest.get("depends") if isinstance(manifest, dict) else []
    if not isinstance(depends, list):
        depends = []
    forbidden_depends = [x for x in depends if x in {"quality", "quality_control"}]
    if forbidden_depends:
        errors.append({
            "severity": "error",
            "code": "forbidden_quality_dependency",
            "message": "Quality/quality_control is intentionally excluded from P3 demo usability packs because it may be paid/uninstallable.",
            "path": f"{rel_addon}/__manifest__.py",
            "items": forbidden_depends,
        })

    forbidden_ref_hits: list[dict[str, str]] = []
    for file in _text_file_candidates(addon_dir):
        if "__pycache__" in file.parts:
            continue
        text = _scan_text_file(file)
        for needle in _FORBIDDEN_ODOO_ENTERPRISE_MODEL_REFS:
            if needle in text:
                forbidden_ref_hits.append({"path": str(file.relative_to(extract_dir)), "ref": needle})
    if forbidden_ref_hits:
        errors.append({
            "severity": "error",
            "code": "forbidden_quality_reference",
            "message": "quality.alert / quality.check / quality_control references must be removed completely from P3 demo usability code packs.",
            "path": rel_addon,
            "items": forbidden_ref_hits[:50],
        })

    declared = _collect_declared_model_names(addon_dir)
    details["declared_model_count"] = len(declared)
    duplicate_models = {m: paths for m, paths in declared.items() if len(paths) > 1}
    if duplicate_models:
        errors.append({
            "severity": "error",
            "code": "duplicate_python_model_definition",
            "message": "The same _name appears more than once. Keep exactly one Python model class per generated model.",
            "path": rel_addon,
            "items": duplicate_models,
        })

    declared_external_ids = {_model_external_id(m) for m in declared}
    missing_access_models: list[dict[str, str]] = []
    for row in _collect_access_model_external_ids(addon_dir):
        ext_id = row["model_external_id"]
        # Only enforce generated x_* access refs; standard/addon model refs may be external.
        if ext_id.startswith("model_x_") and ext_id not in declared_external_ids:
            missing_access_models.append(row)
    if missing_access_models:
        errors.append({
            "severity": "error",
            "code": "access_csv_model_missing_in_python",
            "message": "ir.model.access.csv references generated models that are not declared with _name in Python.",
            "path": f"{rel_addon}/security/ir.model.access.csv",
            "items": missing_access_models,
        })

    demo_hits: list[dict[str, str]] = []
    for folder in [addon_dir / "data", addon_dir / "demo"]:
        if not folder.exists():
            continue
        for file in folder.rglob("*"):
            if not file.is_file() or file.suffix.lower() not in {".xml", ".csv", ".json"}:
                continue
            text = _scan_text_file(file)
            for needle in _DEMO_PLACEHOLDER_PATTERNS:
                if needle in text:
                    demo_hits.append({"path": str(file.relative_to(extract_dir)), "placeholder": needle})
    if demo_hits:
        issue = {
            "severity": "error" if pack_kind == "p3_demo_usability_enhancement" else "warning",
            "code": "placeholder_demo_master_values_found",
            "message": "Placeholder values such as Demo 1 / Demo 2 / P3 Option are not acceptable for usability-enhanced demo data.",
            "path": rel_addon,
            "items": demo_hits[:80],
        }
        if pack_kind == "p3_demo_usability_enhancement":
            errors.append(issue)
        else:
            warnings.append(issue)

    # For usability packs, make sure the exclusion-note model exists when related access/view refs exist.
    exclusion_refs = []
    for file in _text_file_candidates(addon_dir):
        text = _scan_text_file(file)
        if "x_fg_p3_standard_exclusion_note" in text:
            exclusion_refs.append(str(file.relative_to(extract_dir)))
    if exclusion_refs and "x_fg_p3_standard_exclusion_note" not in declared:
        errors.append({
            "severity": "error",
            "code": "standard_exclusion_note_model_missing",
            "message": "The exclusion-note view/security/data references exist but Python model x_fg_p3_standard_exclusion_note is missing.",
            "path": rel_addon,
            "items": sorted(set(exclusion_refs)),
        })

    details["semantic_error_count"] = len(errors)
    details["semantic_warning_count"] = len(warnings)
    details["duplicate_model_count"] = len(duplicate_models)
    details["forbidden_quality_ref_count"] = len(forbidden_ref_hits)
    details["placeholder_demo_value_count"] = len(demo_hits)
    details["missing_access_model_count"] = len(missing_access_models)
    return errors, warnings, details


def _verify_applied_odoo_addon(addon_dir: Path) -> dict[str, Any]:
    """Verify the actual files deployed under extra-addons after G Apply."""
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    details: dict[str, Any] = {"addon_path": str(addon_dir)}
    manifest_path = addon_dir / "__manifest__.py"
    manifest, manifest_error = _parse_manifest(manifest_path) if manifest_path.exists() else ({}, "missing_manifest")
    if manifest_error:
        errors.append({"severity": "error", "code": "applied_manifest_error", "message": manifest_error, "path": str(manifest_path)})
    semantic_errors, semantic_warnings, semantic_details = _extra_odoo_addon_semantic_checks(addon_dir, addon_dir.parent, manifest or {}, _infer_odoo_code_pack_kind(addon_dir.name, "applied_addon"))
    errors.extend(semantic_errors)
    warnings.extend(semantic_warnings)
    details.update(semantic_details)
    pycache_files = [str(p.relative_to(addon_dir)) for p in addon_dir.rglob("*.pyc")]
    if pycache_files:
        warnings.append({"severity": "warning", "code": "applied_pycache_found", "message": "Compiled Python cache files should not be deployed with addon source", "items": pycache_files[:50]})
    details["error_count"] = len(errors)
    details["warning_count"] = len(warnings)
    return {"valid": not errors, "errors": errors, "warnings": warnings, "details": details}


def _validate_odoo_code_pack(code_import_id: str, extract_dir: Path, source_p3_import_id: str | None = None, pack_kind: str = "generic_odoo_addon_code") -> dict[str, Any]:
    addons = _find_odoo_addon_dirs(extract_dir)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    addon_results: list[dict[str, Any]] = []
    if not addons:
        errors.append({"severity": "error", "code": "addon_manifest_missing", "message": "No Odoo addon directory with __manifest__.py was found", "path": str(extract_dir)})
    dangerous_suffixes = {".exe", ".dll", ".so", ".dylib"}
    ignored_artifact_suffixes = {".pyc", ".pyo"}
    ignored_artifact_names = {".ds_store"}
    for file in extract_dir.rglob("*"):
        if not file.is_file():
            continue
        rel = str(file.relative_to(extract_dir))
        if file.suffix.lower() in dangerous_suffixes:
            errors.append({"severity": "error", "code": "dangerous_binary_file", "message": "Binary executable/shared library is not allowed in imported Odoo code pack", "path": rel})
        elif file.suffix.lower() in ignored_artifact_suffixes or file.name.lower() in ignored_artifact_names or "__pycache__" in file.parts:
            warnings.append({"severity": "warning", "code": "ignored_generated_artifact", "message": "Generated/cache artifact is ignored for validation and apply copy", "path": rel})
    for addon_dir in addons:
        manifest_path = addon_dir / "__manifest__.py"
        manifest, manifest_error = _parse_manifest(manifest_path)
        if manifest_error:
            errors.append({"severity": "error", "code": "manifest_parse_error", "message": manifest_error, "path": str(manifest_path.relative_to(extract_dir))})
            manifest = {}
        py_files = sorted(addon_dir.rglob("*.py"))
        xml_files = sorted(addon_dir.rglob("*.xml"))
        csv_files = sorted(addon_dir.rglob("*.csv"))
        for py in py_files:
            err = _validate_python_file(py)
            if err:
                errors.append({"severity": "error", "code": "python_compile_error", "message": err, "path": str(py.relative_to(extract_dir))})
        for xml in xml_files:
            err = _validate_xml_file(xml)
            if err:
                errors.append({"severity": "error", "code": "xml_parse_error", "message": err, "path": str(xml.relative_to(extract_dir))})
        if not (addon_dir / "security" / "ir.model.access.csv").exists():
            warnings.append({"severity": "warning", "code": "access_csv_missing", "message": "security/ir.model.access.csv was not found. This is allowed for pure code patches but not for new models.", "path": str(addon_dir.relative_to(extract_dir))})
        if manifest and manifest.get("installable") is False:
            warnings.append({"severity": "warning", "code": "manifest_not_installable", "message": "Addon manifest has installable=False", "path": str(manifest_path.relative_to(extract_dir))})
        depends = manifest.get("depends") if isinstance(manifest, dict) else []
        if depends is None:
            depends = []
        if not isinstance(depends, list):
            warnings.append({"severity": "warning", "code": "manifest_depends_not_list", "message": "manifest depends should be a list", "path": str(manifest_path.relative_to(extract_dir))})
            depends = []
        semantic_errors, semantic_warnings, semantic_details = _extra_odoo_addon_semantic_checks(addon_dir, extract_dir, manifest if isinstance(manifest, dict) else {}, pack_kind)
        errors.extend(semantic_errors)
        warnings.extend(semantic_warnings)
        addon_results.append({
            "addon_name": addon_dir.name,
            "addon_path": str(addon_dir.relative_to(extract_dir)),
            "manifest": manifest,
            "depends": depends,
            "python_file_count": len(py_files),
            "xml_file_count": len(xml_files),
            "csv_file_count": len(csv_files),
            "has_access_csv": (addon_dir / "security" / "ir.model.access.csv").exists(),
            "semantic_checks": semantic_details,
        })
    valid = not errors
    return {
        "schema_name": "odoo_code_import_validation_result",
        "version": "v1",
        "code_import_id": code_import_id,
        "source_p3_import_id": source_p3_import_id,
        "pack_kind": pack_kind,
        "status": "odoo_code_pack_validated" if valid else "odoo_code_pack_validation_failed",
        "valid": valid,
        "ready_for_odoo_apply": valid,
        "plan_current_step": "F. Odoo Code Pack Import / Validate" if source_p3_import_id else "Odoo Code Pack Import / Validate",
        "plan_next_step": "G. Apply Odoo Addon Direct" if valid else "Code correction / regenerate code pack",
        "summary": {
            "addon_count": len(addon_results),
            "python_file_count": sum(x.get("python_file_count", 0) for x in addon_results),
            "xml_file_count": sum(x.get("xml_file_count", 0) for x in addon_results),
            "csv_file_count": sum(x.get("csv_file_count", 0) for x in addon_results),
            "error_count": len(errors),
            "warning_count": len(warnings),
        },
        "addons": addon_results,
        "errors": errors,
        "warnings": warnings,
    }


async def _import_odoo_code_pack_common(file: UploadFile, source_p3_import_id: str | None = None, source_context: str = "generic_odoo_code", requested_pack_kind: str | None = None) -> dict[str, Any]:
    code_import_id = str(uuid4())
    out_dir = _odoo_code_import_dir(code_import_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = file.filename or "odoo_code_pack.zip"
    pack_kind = requested_pack_kind or _infer_odoo_code_pack_kind(filename, source_context)
    uploaded_path = out_dir / "uploaded_code_pack.zip"
    data = await file.read()
    uploaded_path.write_bytes(data)
    extracted_dir = out_dir / "extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    if not zipfile.is_zipfile(uploaded_path):
        raise HTTPException(status_code=400, detail="Odoo code pack must be a ZIP file")
    entries = _safe_extract_zip(uploaded_path, extracted_dir)
    validation = _validate_odoo_code_pack(code_import_id, extracted_dir, source_p3_import_id, pack_kind=pack_kind)
    validation_path = out_dir / ODOO_CODE_VALIDATION_FILENAME
    validation_path.write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "schema_name": "odoo_code_import_summary",
        "version": "v1",
        "code_import_id": code_import_id,
        "source_context": source_context,
        "source_p3_import_id": source_p3_import_id,
        "pack_kind": pack_kind,
        "filename": filename,
        "status": validation.get("status"),
        "valid": validation.get("valid"),
        "ready_for_odoo_apply": validation.get("ready_for_odoo_apply"),
        "uploaded_path": str(uploaded_path),
        "extracted_dir": str(extracted_dir),
        "zip_entry_count": len(entries),
        "summary": validation.get("summary") or {},
        "links": {
            "self": f"/odoo-code/imports/{code_import_id}",
            "validation": f"/odoo-code/imports/{code_import_id}/validation",
            "uploaded_pack": f"/odoo-code/imports/{code_import_id}/uploaded-pack.zip",
        },
        "plan_current_step": validation.get("plan_current_step"),
        "plan_next_step": validation.get("plan_next_step"),
    }
    (out_dir / ODOO_CODE_IMPORT_SUMMARY_FILENAME).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if source_p3_import_id:
        p3_dir = _p3_import_dir(source_p3_import_id)
        if p3_dir.exists():
            (p3_dir / "p3_generated_odoo_code_import_result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            summary_path = p3_dir / "import_summary.json"
            if summary_path.exists():
                raw = json.loads(summary_path.read_text(encoding="utf-8"))
                raw["status"] = "p3_odoo_code_pack_imported" if validation.get("valid") else "p3_odoo_code_pack_validation_failed"
                raw["p3_generated_odoo_code_import"] = summary
                raw["p3_odoo_code_pack_import"] = summary
                raw.setdefault("links", {})["generated_odoo_code_import"] = f"/p3/imports/{source_p3_import_id}/generated-odoo-code-pack"
                raw.setdefault("links", {})["generated_odoo_code_validation"] = f"/p3/imports/{source_p3_import_id}/generated-odoo-code-pack/validation"
                raw.setdefault("plan", {})["current_step"] = "F. Generated Odoo Code Pack Import / Validate"
                raw.setdefault("plan", {})["next_step"] = "G. Apply Odoo Addon Direct" if validation.get("valid") else "Code correction / regenerate"
                for st in raw.get("phase_statuses", []):
                    if st.get("phase_key") == "F":
                        st["status"] = "p3_odoo_code_pack_validated" if validation.get("valid") else "p3_odoo_code_pack_validation_failed"
                        st["generated_code_imported"] = True
                        st["ready_for_odoo_apply"] = bool(validation.get("valid"))
                        st["summary"] = validation.get("summary") or {}
                    elif st.get("phase_key") == "G" and validation.get("valid"):
                        st["status"] = "next_ready"
                summary_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
                (p3_dir / "phase_statuses.json").write_text(json.dumps(raw.get("phase_statuses") or [], ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def _odoo_apply_target_root() -> Path:
    """Return the addon deployment target used by G. Apply Odoo Addon Direct.

    ODOO_EXTRA_ADDONS_ROOT is preferred when provided.  Otherwise the existing
    CUSTOM_ADDONS_ROOT is used so the feature also works in local/dev stacks
    where /app/custom_addons is mounted as the Odoo extra-addons directory.
    """
    root = Path(os.getenv("ODOO_EXTRA_ADDONS_ROOT", str(CUSTOM_ADDONS_ROOT)))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _copytree_replace(src: Path, dst: Path, backup_root: Path, backup_suffix: str) -> dict[str, Any]:
    backup_path: Path | None = None
    if dst.exists():
        backup_root.mkdir(parents=True, exist_ok=True)
        backup_path = backup_root / f"{dst.name}__backup__{backup_suffix}"
        if backup_path.exists():
            shutil.rmtree(backup_path)
        shutil.copytree(dst, backup_path)
        shutil.rmtree(dst)

    def _ignore_generated_artifacts(dir_path: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        for name in names:
            if name == "__pycache__" or name.endswith((".pyc", ".pyo", ".Identifier")) or name == ".DS_Store":
                ignored.add(name)
        return ignored

    shutil.copytree(src, dst, ignore=_ignore_generated_artifacts)
    return {
        "addon_name": dst.name,
        "source_path": str(src),
        "target_path": str(dst),
        "backup_path": str(backup_path) if backup_path else None,
        "replaced_existing": backup_path is not None,
    }


def _apply_odoo_code_import(code_import_id: str, source_p3_import_id: str | None = None) -> dict[str, Any]:
    """Apply a previously validated Odoo code import to the extra-addons target.

    This is intentionally a filesystem deployment step only. It does not call
    Odoo module install/upgrade. Odoo app list refresh and module install/upgrade
    remain an explicit user/admin action after this G step.
    """
    code_dir = _odoo_code_import_dir(code_import_id)
    summary_path = code_dir / ODOO_CODE_IMPORT_SUMMARY_FILENAME
    validation_path = code_dir / ODOO_CODE_VALIDATION_FILENAME
    if not summary_path.exists() or not validation_path.exists():
        raise HTTPException(status_code=404, detail="Validated Odoo code import was not found")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if not validation.get("valid") or not validation.get("ready_for_odoo_apply"):
        raise HTTPException(status_code=400, detail="Odoo code pack is not valid or not ready for apply")
    extract_dir = Path(summary.get("extracted_dir") or code_dir / "extracted")
    if not extract_dir.exists():
        raise HTTPException(status_code=404, detail="Extracted Odoo code pack directory was not found")
    addons = _find_odoo_addon_dirs(extract_dir)
    if not addons:
        raise HTTPException(status_code=400, detail="No Odoo addon directories found to apply")
    target_root = _odoo_apply_target_root()
    backup_root = code_dir / "backups"
    applied_at = _now_iso()
    backup_suffix = applied_at.replace(":", "").replace("+", "_").replace(".", "_")
    applied_addons: list[dict[str, Any]] = []
    post_apply_checks: list[dict[str, Any]] = []
    for addon_dir in addons:
        applied = _copytree_replace(addon_dir, target_root / addon_dir.name, backup_root, backup_suffix)
        verification = _verify_applied_odoo_addon(Path(applied["target_path"]))
        applied["post_apply_verification"] = verification
        applied_addons.append(applied)
        post_apply_checks.append({"addon_name": applied["addon_name"], **verification})
    post_apply_error_count = sum(len(x.get("errors") or []) for x in post_apply_checks)
    result = {
        "schema_name": "odoo_code_apply_result",
        "version": "v1",
        "code_import_id": code_import_id,
        "source_context": summary.get("source_context"),
        "source_p3_import_id": source_p3_import_id or summary.get("source_p3_import_id"),
        "status": "odoo_addon_direct_applied" if post_apply_error_count == 0 else "odoo_addon_direct_applied_with_verification_errors",
        "ready_for_odoo_module_install_or_upgrade": post_apply_error_count == 0,
        "odoo_module_install_or_upgrade_done": False,
        "target_root": str(target_root),
        "applied_at": applied_at,
        "applied_addon_count": len(applied_addons),
        "applied_addons": applied_addons,
        "post_apply_error_count": post_apply_error_count,
        "post_apply_checks": post_apply_checks,
        "validation_summary": validation.get("summary") or {},
        "next_manual_steps": [
            "Open Odoo Apps or update the app list.",
            "Install or Upgrade the applied addon(s) in Odoo.",
            "Confirm the P3 Demo fields, support masters, simple indicators, and views on target screens.",
        ],
        "plan_current_step": "G. Apply Odoo Addon Direct" if (source_p3_import_id or summary.get("source_p3_import_id")) else "Odoo Addon Direct Apply",
        "plan_next_step": "Odoo Apps Update / Install or Upgrade / Screen Confirmation",
    }
    (code_dir / ODOO_CODE_APPLY_RESULT_FILENAME).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["status"] = "odoo_addon_direct_applied"
    summary["odoo_apply_result"] = result
    summary.setdefault("links", {})["apply_result"] = f"/odoo-code/imports/{code_import_id}/apply-result"
    summary["plan_current_step"] = result["plan_current_step"]
    summary["plan_next_step"] = result["plan_next_step"]
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    p3_id = source_p3_import_id or summary.get("source_p3_import_id")
    if p3_id:
        p3_dir = _p3_import_dir(str(p3_id))
        if p3_dir.exists():
            (p3_dir / "p3_odoo_addon_direct_apply_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            p3_summary_path = p3_dir / "import_summary.json"
            if p3_summary_path.exists():
                raw = json.loads(p3_summary_path.read_text(encoding="utf-8"))
                raw["status"] = "p3_odoo_addon_direct_applied"
                raw["p3_odoo_apply_result"] = result
                raw.setdefault("links", {})["p3_odoo_apply_result"] = f"/p3/imports/{p3_id}/apply-odoo-addon-direct-result"
                raw.setdefault("plan", {})["current_step"] = "G. Apply Odoo Addon Direct"
                raw.setdefault("plan", {})["next_step"] = "Odoo画面確認 / Apps Update / Install or Upgrade"
                for st in raw.get("phase_statuses", []):
                    if st.get("phase_key") == "F":
                        st["status"] = "p3_generated_odoo_code_validated"
                    elif st.get("phase_key") == "G":
                        st["status"] = "p3_odoo_addon_direct_applied"
                        st["odoo_applied"] = True
                        st["applied_addon_count"] = len(applied_addons)
                        st["target_root"] = str(target_root)
                p3_summary_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
                (p3_dir / "phase_statuses.json").write_text(json.dumps(raw.get("phase_statuses") or [], ensure_ascii=False, indent=2), encoding="utf-8")
    return result


@app.post("/odoo-code/import")
async def import_generic_odoo_code_pack(file: UploadFile = File(...), pack_kind: str | None = Form(default=None)) -> dict[str, Any]:
    return await _import_odoo_code_pack_common(file, source_p3_import_id=None, source_context="generic_odoo_code", requested_pack_kind=pack_kind)


@app.get("/odoo-code/imports/{code_import_id}")
def read_odoo_code_import_summary(code_import_id: str) -> dict[str, Any]:
    path = _odoo_code_import_dir(code_import_id) / ODOO_CODE_IMPORT_SUMMARY_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="Odoo code import summary not found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/odoo-code/imports/{code_import_id}/validation")
def read_odoo_code_import_validation(code_import_id: str) -> dict[str, Any]:
    path = _odoo_code_import_dir(code_import_id) / ODOO_CODE_VALIDATION_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="Odoo code import validation not found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/odoo-code/imports/{code_import_id}/uploaded-pack.zip")
def download_odoo_code_uploaded_pack(code_import_id: str) -> FileResponse:
    path = _odoo_code_import_dir(code_import_id) / "uploaded_code_pack.zip"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Uploaded Odoo code pack not found")
    return FileResponse(str(path), filename=path.name, media_type="application/zip")


@app.post("/p3/imports/{import_id}/generated-odoo-code-pack")
async def import_p3_generated_odoo_code_pack(import_id: str, file: UploadFile = File(...), pack_kind: str | None = Form(default=None)) -> dict[str, Any]:
    if not _p3_import_dir(import_id).exists():
        raise HTTPException(status_code=404, detail="P3 import not found")
    return await _import_odoo_code_pack_common(file, source_p3_import_id=import_id, source_context="p3_odoo_code_pack", requested_pack_kind=pack_kind)


@app.get("/p3/imports/{import_id}/generated-odoo-code-pack")
def read_p3_generated_odoo_code_import(import_id: str) -> dict[str, Any]:
    path = _p3_import_dir(import_id) / "p3_generated_odoo_code_import_result.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 generated Odoo code import result not found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/p3/imports/{import_id}/generated-odoo-code-pack/validation")
def read_p3_generated_odoo_code_validation(import_id: str) -> dict[str, Any]:
    result_path = _p3_import_dir(import_id) / "p3_generated_odoo_code_import_result.json"
    if not result_path.exists():
        raise HTTPException(status_code=404, detail="P3 generated Odoo code import result not found")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    code_import_id = result.get("code_import_id")
    if not code_import_id:
        raise HTTPException(status_code=404, detail="Linked code import id not found")
    return read_odoo_code_import_validation(code_import_id)


@app.post("/odoo-code/imports/{code_import_id}/apply")
def apply_generic_odoo_code_import(code_import_id: str) -> dict[str, Any]:
    return _apply_odoo_code_import(code_import_id)


@app.get("/odoo-code/imports/{code_import_id}/apply-result")
def read_odoo_code_apply_result(code_import_id: str) -> dict[str, Any]:
    path = _odoo_code_import_dir(code_import_id) / ODOO_CODE_APPLY_RESULT_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="Odoo code apply result not found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/p3/imports/{import_id}/apply-odoo-addon-direct")
def apply_p3_odoo_addon_direct(import_id: str) -> dict[str, Any]:
    p3_dir = _p3_import_dir(import_id)
    result_path = p3_dir / "p3_generated_odoo_code_import_result.json"
    if not result_path.exists():
        raise HTTPException(status_code=404, detail="P3 generated Odoo code import result not found. Run F first.")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    code_import_id = result.get("code_import_id")
    if not code_import_id:
        raise HTTPException(status_code=404, detail="Linked code import id not found")
    return _apply_odoo_code_import(str(code_import_id), source_p3_import_id=import_id)


@app.get("/p3/imports/{import_id}/apply-odoo-addon-direct-result")
def read_p3_odoo_addon_direct_apply_result(import_id: str) -> dict[str, Any]:
    path = _p3_import_dir(import_id) / "p3_odoo_addon_direct_apply_result.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 Odoo addon direct apply result not found")
    return json.loads(path.read_text(encoding="utf-8"))

@app.post("/p1p2/imports/{import_id}/generate-odoo-overlay-data")
def generate_p1p2_odoo_overlay_data(import_id: str) -> dict[str, Any]:
    in_dir = _p1p2_import_dir(import_id)
    core_path = in_dir / "P1P2_CORE_PAYLOAD.json"
    gap_path = in_dir / "P1P2_FG_GAP_PAYLOAD.json"
    if not core_path.exists():
        raise HTTPException(status_code=404, detail="P1/P2 core payload not found")
    core_payload = _load_json_path(core_path)
    core_nodes, core_rels = _extract_payload(core_payload)
    dangling = _calc_dangling(core_nodes, core_rels)
    if dangling:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot generate overlay data while core dangling relationships exist: {len(dangling)}",
        )
    gap_payload = _load_json_path(gap_path) if gap_path.exists() else {"gap_entries": [], "skipped_relationships": []}
    generated_dir = GENERATED_ADDONS_ROOT / f"p1p2_overlay_data_{import_id}"
    if generated_dir.exists():
        shutil.rmtree(generated_dir)
    generated_dir.mkdir(parents=True, exist_ok=True)
    label_counts, rel_counts = _graph_counts(core_nodes, core_rels)
    overlay_summary = {
        "import_id": import_id,
        "generated_at": _now_iso(),
        "purpose": "Odoo overlay data source for P1/P2 core payload. F&G GAP is report-only and excluded from auto-generation.",
        "core_nodes": len(core_nodes),
        "core_relationships": len(core_rels),
        "gap_entries_excluded": len(gap_payload.get("gap_entries") or []),
        "skipped_relationships_excluded": len(gap_payload.get("skipped_relationships") or []),
        "label_counts": label_counts,
        "relationship_type_counts": rel_counts,
    }
    (generated_dir / "odoo_overlay_core_payload.json").write_text(
        json.dumps(core_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (generated_dir / "fg_gap_report.json").write_text(
        json.dumps(gap_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (generated_dir / "overlay_summary.json").write_text(
        json.dumps(overlay_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    gap_lines = ["# F&G GAP Report", "", "These items were detected but excluded from current Odoo auto-generation.", ""]
    for entry in gap_payload.get("gap_entries") or []:
        gap_lines.extend(
            [
                f"## {entry.get('source_node_key') or entry.get('gap_key')}",
                f"- type: {entry.get('gap_type')}",
                f"- reason: {entry.get('skip_reason_ja')}",
                f"- probable meaning: {entry.get('probable_meaning_ja')}",
                f"- report message: {entry.get('customer_report_message_ja')}",
                "",
            ]
        )
    (generated_dir / "fg_gap_report.md").write_text("\n".join(gap_lines), encoding="utf-8")
    (generated_dir / "README.md").write_text(
        "# P1/P2 Odoo Overlay Data\n\n"
        "This is a data pack for Odoo overlay generation. It is not an installable Odoo addon by itself.\n\n"
        "- `odoo_overlay_core_payload.json`: apply/generation target.\n"
        "- `fg_gap_report.json` and `.md`: report-only GAP items excluded from auto-generation.\n"
        "\nDo not auto-connect GAP entries back into core without approved mapping.\n",
        encoding="utf-8",
    )
    zip_path = GENERATED_ADDONS_ROOT / f"p1p2_overlay_data_{import_id}.zip"
    _zip_dir(generated_dir, zip_path)
    result = {
        "import_id": import_id,
        "status": "odoo_overlay_data_generated",
        "generated_at": _now_iso(),
        "overlay_data_dir": str(generated_dir),
        "zip_path": str(zip_path),
        "download_url": f"/p1p2/imports/{import_id}/odoo-overlay-data/download",
        "record_counts": overlay_summary,
        "warnings": ["F&G GAP entries are excluded from Odoo auto-generation and kept in fg_gap_report.*"],
    }
    (in_dir / "odoo_overlay_data_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary_path = in_dir / "import_summary.json"
    if summary_path.exists():
        raw = json.loads(summary_path.read_text(encoding="utf-8"))
        raw["status"] = "odoo_overlay_data_generated"
        raw["odoo_overlay_data_result"] = result
        for st in raw.get("phase_statuses", []):
            if st.get("phase_key") == "P1/P2":
                st["odoo_generated"] = True
                st["status"] = "odoo_overlay_data_generated"
        summary_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


@app.get("/p1p2/imports/{import_id}/odoo-overlay-data")
def read_p1p2_odoo_overlay_data(import_id: str) -> dict[str, Any]:
    path = _p1p2_import_dir(import_id) / "odoo_overlay_data_result.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Odoo overlay data result not found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/p1p2/imports/{import_id}/odoo-overlay-data/download")
def download_p1p2_odoo_overlay_data(import_id: str) -> FileResponse:
    path = _p1p2_import_dir(import_id) / "odoo_overlay_data_result.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Odoo overlay data result not found")
    result = json.loads(path.read_text(encoding="utf-8"))
    zip_path = Path(result.get("zip_path") or "")
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Generated overlay data ZIP not found")
    return FileResponse(str(zip_path), filename=zip_path.name, media_type="application/zip")



# ---------------------------------------------------------------------------
# P6-DIAGRAM-1: P3 Diagram Pack Import / Validate / Dynamic ER Downloads
# ---------------------------------------------------------------------------
# Purpose:
# - Import P3_DIAGRAM_DATA_PACK style ZIPs generated outside this system.
# - Validate that the pack is explicitly scoped up to P3.
# - Build dynamic download actions from the imported pack contents instead of
#   hard-coding app keys such as sales/purchase/inventory.
# - Keep P6 read/inspect/download only. No Neo4j Apply and no Odoo Apply.

P6_DIAGRAM_ROOT_NAME = "p6_diagram_packs"
P6_SUMMARY_FILENAME = "p6_diagram_import_summary.json"
P6_VALIDATION_FILENAME = "p6_diagram_validation.json"
P6_ACTIONS_FILENAME = "p6_diagram_actions.json"
P6_INDEX_FILENAME = "P3_DIAGRAM_DATA_INDEX.json"


P6_P3_CONFIRMATION_PROMPT_FILENAME = "prompts/P3_CONFIRMATION_MATERIALS_PROMPT.md"
P6_P3_CONFIRMATION_START_FILENAME = "NEXT_THREAD_START_MESSAGE.md"

P6_P3_CONFIRMATION_MATERIALS_PROMPT = r"""添付する download_p3_er_all_apps.zip を使って、P3確認資料を作成してください。

これまでの別バリエーション作成内容や既存のTable Field Diagram Pack / Table Relation Diagram Packの内容には引きずられず、以下の4種類の確認資料だけを作成してください。

必要な確認資料:
1. テーブル一覧
2. フィールド一覧（テーブルごと）
3. テーブル同士のつながり図（テーブル名のみ・日本語のみ・アプリごと）
4. 全体図（元のような全体概要図）

入力:
- download_p3_er_all_apps.zip
  - graphs/P3_STRUCTURAL_GRAPH_ALL_APPS.json
  - graphs/P3_YFILES_PAYLOAD_ALL_APPS.json
  - mermaid/P3_MERMAID_ALL_APPS_BASE_MODELS.mmd
  - DOWNLOAD_MANIFEST.json

出力ZIP:
P3_CONFIRMATION_MATERIALS_v1.zip
  README.md
  index.html
  manifest.json
  tables/
    table_list.html
    table_list.csv
    field_list_by_table.html
    field_list_by_table.csv
  diagrams/
    dot/
      overall_overview.dot
      table_connections_<app_key>.dot
    svg/
      overall_overview.svg
      table_connections_<app_key>.svg
    png/
      overall_overview.png
      table_connections_<app_key>.png
  data/
    table_list.json
    field_list_by_table.json

重要:
- 成果物ZIP内に、このプロンプト本文は入れないでください。
- 既存のRelation Diagram PackやTable Field Diagram Packは変更しないでください。
- 今回は、確認資料だけを新規作成してください。
- P4/P5の顧客回答、詳細ロジック、最終Odoo反映結果は含めないでください。
- 推測でフィールドやテーブルを追加しないでください。

1. テーブル一覧の要件:
- テーブル一覧は、必ずテーブル/モデル単位の一覧にしてください。
- テーブル一覧にフィールド行を混ぜないでください。
- 1行 = 1テーブル/1モデル/1中間テーブル としてください。
- フィールド名、フィールド型、フィールドclassをテーブル一覧の主内容にしないでください。
- テーブル一覧に表示する列は以下を基本にしてください。
  - app_key
  - app_name_ja
  - table_name_ja
  - technical_name
  - node_type
  - status
  - custom_field_count
  - sf_relation_field_count
  - relation_count_in_app
- relation_table は中間テーブルとして1行で表示してよいです。
- table_list.html と table_list.csv を作成してください。
- table_list.html の冒頭に「この一覧はテーブル/モデル単位です。フィールド行は含めていません。」と明記してください。

2. フィールド一覧の要件:
- フィールド一覧は、必ずテーブルごとの一覧にしてください。
- フラットなフィールド行だけの一覧にせず、HTMLでは1テーブル/1モデルを1カードまたは1セクションとして表示してください。
- 各テーブル/モデルの下に、そのテーブルに属するフィールドを縦に並べてください。
- field_list_by_table.html を作成してください。
- field_list_by_table.csv も作成してください。CSVはフラット行でもよいですが、必ず app / model 情報を列に含めて、テーブルごとに並ぶようにソートしてください。
- field_list_by_table.json は、テーブルごとに fields 配列を持つ構造にしてください。

フィールド一覧に表示するフィールド:
- P3カスタムフィールド CusF
- P3カスタムリレーションフィールド CusF
- SF標準フィールドは全件出さない
- SFは、同一アプリ内の表示対象モデルに関係するリレーションフィールド、またはmissing_modelに関係する主要フィールドのみ出してください
- NFFは明示的に判定済みでなければ出さないでください

フィールド一覧の各フィールド表示項目:
- 表示名
- 技術名
- 型
- class
- relation_model
- business_role_ja がある場合は表示

3. テーブル同士のつながり図の要件:
- アプリごとに作成してください。
- アプリ名は固定せず、入力JSONの app_key から動的に作成してください。
- 図はテーブル名のみで表示してください。
- 図のノード内にフィールド一覧は表示しないでください。
- 図のノード内に技術名も表示しないでください。
- 日本語名のみを表示してください。
- どうしても日本語名が無い場合のみ、最低限の代替表示を使ってください。
- モデル間のつながり線は表示してください。
- many2one / one2many / many2many の関係は線として表現してください。
- relation_table がある場合は「中間テーブル」として箱だけ表示してよいです。
- エッジラベルにフィールド名は出さないでください。
- つながり図は以下を出力してください。
  - diagrams/dot/table_connections_<app_key>.dot
  - diagrams/svg/table_connections_<app_key>.svg
  - diagrams/png/table_connections_<app_key>.png

4. 全体図の要件:
- 元のような全体概要図を作成してください。
- 全体図は、アプリ → 主要ベースモデル の概要にしてください。
- 全体図も日本語名を中心に表示してください。
- 全フィールドや全リレーションを詰め込まないでください。
- 全体図は以下を出力してください。
  - diagrams/dot/overall_overview.dot
  - diagrams/svg/overall_overview.svg
  - diagrams/png/overall_overview.png

index.html 要件:
- index.html は顧客が開いて確認する入口にしてください。
- 上から以下の順に表示してください。
  1. タイトル
  2. 説明
  3. テーブル一覧へのリンク
  4. フィールド一覧（テーブルごと）へのリンク
  5. アプリ別つながり図へのナビゲーション
  6. 全体図
  7. アプリ別つながり図
- テーブル一覧とフィールド一覧は、index.html内に全件を埋め込まず、個別HTMLへのリンクでよいです。
- 図はindex.html上でSVGを表示してください。
- 画像は max-width: 100%; height: auto; display: block; にしてください。
- 横スクロール前提の巨大資料にしないでください。

manifest.json には以下を入れてください。
- pack_key
- pack_type
- source
- materials
- app_keys
- counts
- note_ja

最後に、作成したZIPをダウンロードできる形で提示してください。"""

P6_P3_CONFIRMATION_NEXT_THREAD_MESSAGE = """このZIPに含まれる `prompts/P3_CONFIRMATION_MATERIALS_PROMPT.md` に従って、P3確認資料を作成してください。入力データは同じZIP内の `graphs/`、`mermaid/`、`DOWNLOAD_MANIFEST.json` を使ってください。成果物は `P3_CONFIRMATION_MATERIALS_v1.zip` としてダウンロードできる形にしてください。"""


def _p6_root() -> Path:
    root = ARTIFACT_ROOT / P6_DIAGRAM_ROOT_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _p6_import_dir(diagram_import_id: str) -> Path:
    return _p6_root() / diagram_import_id


def _p6_safe_extract_upload(data: bytes, out_dir: Path) -> Path:
    source_zip = out_dir / "source.zip"
    source_zip.write_bytes(data)
    extracted_dir = out_dir / "extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(source_zip) as zf:
            for member in zf.infolist():
                name = member.filename
                normalized = Path(name)
                if name.startswith("/") or ".." in normalized.parts:
                    raise HTTPException(status_code=400, detail=f"Unsafe ZIP entry: {name}")
            zf.extractall(extracted_dir)
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail=f"Invalid ZIP: {exc}") from exc
    return extracted_dir


def _p6_find_index(extracted_dir: Path) -> Path | None:
    direct = extracted_dir / P6_INDEX_FILENAME
    if direct.exists():
        return direct
    matches = sorted(extracted_dir.rglob(P6_INDEX_FILENAME))
    return matches[0] if matches else None


def _p6_rel_file_exists(extracted_dir: Path, rel_path: str | None) -> bool:
    if not rel_path:
        return False
    p = extracted_dir / rel_path
    return p.exists() and p.is_file()


def _p6_action_file_set(extracted_dir: Path, graph_name: str) -> list[str]:
    files: list[str] = []
    graph_path = f"graphs/{graph_name}"
    if _p6_rel_file_exists(extracted_dir, graph_path):
        files.append(graph_path)
    if graph_name == "P3_STRUCTURAL_GRAPH_ALL_APPS.json":
        for candidate in [
            "graphs/P3_YFILES_PAYLOAD_ALL_APPS.json",
            "mermaid/P3_MERMAID_ALL_APPS_BASE_MODELS.mmd",
        ]:
            if _p6_rel_file_exists(extracted_dir, candidate):
                files.append(candidate)
    elif graph_name.startswith("P3_STRUCTURAL_GRAPH__"):
        app_key = graph_name.replace("P3_STRUCTURAL_GRAPH__", "").replace(".json", "")
        for candidate in [
            f"graphs/P3_YFILES_PAYLOAD__{app_key}.json",
            f"mermaid/P3_MERMAID__{app_key}.mmd",
        ]:
            if _p6_rel_file_exists(extracted_dir, candidate):
                files.append(candidate)
    return files


def _p6_label_from_graph_name(graph_name: str) -> tuple[str, str | None, str]:
    if graph_name == "P3_STRUCTURAL_GRAPH_ALL_APPS.json":
        return "全体ER図DL", None, "全アプリ / 3-hop"
    app_key = graph_name.replace("P3_STRUCTURAL_GRAPH__", "").replace(".json", "")
    if not app_key or app_key == graph_name:
        return graph_name, None, "P3 / 3-hop"
    label = app_key.replace("_", " ").title()
    return f"{label} ER図DL", app_key, f"{label} / 3-hop"


def _p6_build_dynamic_actions(diagram_import_id: str, extracted_dir: Path, index: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = [
        {
            "action_key": "download_all",
            "label_ja": "図示PACK一式DL",
            "action_type": "download_original_zip",
            "group_key": "export",
            "group_label_ja": "Export",
            "badge": "P3まで / Diagram Pack",
            "phase_scope": "up_to_P3",
            "diagram_type": "pack",
            "enabled": True,
            "download_url": f"/p6/diagram-packs/{diagram_import_id}/download/download_all",
        }
    ]
    graph_files = index.get("graph_files") or {}
    for graph_name, stats in sorted(graph_files.items(), key=lambda x: (0 if x[0] == "P3_STRUCTURAL_GRAPH_ALL_APPS.json" else 1, x[0])):
        if not str(graph_name).startswith("P3_STRUCTURAL_GRAPH"):
            continue
        files = _p6_action_file_set(extracted_dir, str(graph_name))
        if not files:
            continue
        label_ja, app_key, scope_label = _p6_label_from_graph_name(str(graph_name))
        action_key = "download_p3_er_all_apps" if app_key is None else f"download_p3_er_{app_key}"
        action: dict[str, Any] = {
            "action_key": action_key,
            "label_ja": label_ja,
            "action_type": "download_files_zip",
            "group_key": "p3_er",
            "group_label_ja": "P3 ER図",
            "badge": "P3まで / 3-hop / ER",
            "phase_scope": "up_to_P3",
            "source_phases": ["P1", "P2", "P3"],
            "diagram_type": "structural_er",
            "app_key": app_key,
            "scope_label_ja": scope_label,
            "download_files": files,
            "enabled": True,
            "stats": stats if isinstance(stats, dict) else {},
            "download_url": f"/p6/diagram-packs/{diagram_import_id}/download/{action_key}",
        }
        if action_key == "download_p3_er_all_apps":
            action["label_ja"] = "全体ER図DL + P3確認資料作成Prompt"
            action["badge"] = "P3まで / 3-hop / ER / ChatGPT確認資料Prompt付き"
            action["includes_confirmation_prompt"] = True
            action["prompt_file"] = P6_P3_CONFIRMATION_PROMPT_FILENAME
            action["start_message_file"] = P6_P3_CONFIRMATION_START_FILENAME
        actions.append(action)
    return actions


def _p6_validate_extracted_pack(extracted_dir: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    index_path = _p6_find_index(extracted_dir)
    index: dict[str, Any] = {}
    if not index_path:
        errors.append(f"{P6_INDEX_FILENAME} was not found.")
    else:
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{P6_INDEX_FILENAME} is invalid JSON: {exc}")
    graph_files = index.get("graph_files") or {}
    if not graph_files:
        errors.append("graph_files is missing or empty in P3 diagram index.")
    if "P3_STRUCTURAL_GRAPH_ALL_APPS.json" not in graph_files:
        errors.append("P3_STRUCTURAL_GRAPH_ALL_APPS.json is missing from graph_files.")
    for graph_name in graph_files.keys():
        if str(graph_name).startswith("P3_STRUCTURAL_GRAPH") and not _p6_rel_file_exists(extracted_dir, f"graphs/{graph_name}"):
            errors.append(f"graphs/{graph_name} was declared but not found.")
    if not _p6_rel_file_exists(extracted_dir, "reports/P3_DIAGRAM_DATA_VALIDATION_REPORT.md"):
        warnings.append("reports/P3_DIAGRAM_DATA_VALIDATION_REPORT.md was not found.")
    # This pack is expected to be generated from P3 diagram data. If a future pack
    # omits explicit phase metadata, keep it accepted but warn instead of guessing.
    source_summary_path = extracted_dir / "source_summaries" / "SOURCE_SUMMARY.json"
    if not source_summary_path.exists():
        warnings.append("source_summaries/SOURCE_SUMMARY.json was not found; phase scope is inferred from P3 file names only.")
    return {
        "valid": not errors,
        "status": "valid" if not errors else "validation_failed",
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "index_path": str(index_path) if index_path else None,
        "summary": {
            "graph_file_count": len(graph_files),
            "structural_graph_count": sum(1 for k in graph_files if str(k).startswith("P3_STRUCTURAL_GRAPH")),
            "yfiles_payload_count": len(list(extracted_dir.glob("graphs/P3_YFILES_PAYLOAD*.json"))),
            "mermaid_file_count": len(list(extracted_dir.glob("mermaid/*.mmd"))),
            "data_scope": "up_to_P3",
            "source_phases": ["P1", "P2", "P3"],
        },
        "index": index,
    }


def _p6_write_summary(diagram_import_id: str, filename: str, out_dir: Path, extracted_dir: Path, validation: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    index = validation.get("index") or {}
    graph_files = index.get("graph_files") or {}
    all_stats = graph_files.get("P3_STRUCTURAL_GRAPH_ALL_APPS.json") or {}
    summary = {
        "diagram_import_id": diagram_import_id,
        "filename": filename,
        "status": "imported" if validation.get("valid") else "validation_failed",
        "imported_at": _now_iso(),
        "diagram_pack_type": "p3_er_diagram_pack",
        "data_scope": "up_to_P3",
        "source_phases": ["P1", "P2", "P3"],
        "description_ja": "この図示PACKはP3までの成果物とOdoo DB抽出情報をもとにしたER図です。P4/P5の顧客回答、詳細ロジック、最終Odoo反映結果は含みません。",
        "source_zip_path": str(out_dir / "source.zip"),
        "extracted_dir": str(extracted_dir),
        "validation": {k: v for k, v in validation.items() if k != "index"},
        "actions": actions,
        "summary": {
            "action_count": len(actions),
            "graph_file_count": len(graph_files),
            "node_count": all_stats.get("node_count", 0),
            "edge_count": all_stats.get("edge_count", 0),
            "base_model_count": all_stats.get("base_model_count", 0),
            "custom_field_count": all_stats.get("custom_field_count", 0),
            "relation_table_count": all_stats.get("relation_table_count", 0),
            "missing_model_count": all_stats.get("missing_model_count", 0),
        },
        "links": {
            "self": f"/p6/diagram-packs/{diagram_import_id}",
            "validate": f"/p6/diagram-packs/{diagram_import_id}/validate",
            "actions": f"/p6/diagram-packs/{diagram_import_id}/actions",
        },
    }
    (out_dir / P6_VALIDATION_FILENAME).write_text(json.dumps({k: v for k, v in validation.items() if k != "index"}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / P6_ACTIONS_FILENAME).write_text(json.dumps({"items": actions}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / P6_SUMMARY_FILENAME).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


@app.post("/p6/diagram-packs/import")
async def import_p6_diagram_pack(file: UploadFile = File(...)) -> dict[str, Any]:
    filename = file.filename or "P3_DIAGRAM_DATA_PACK.zip"
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="P6 Diagram Pack import accepts ZIP files only.")
    data = await file.read()
    diagram_import_id = str(uuid4())
    out_dir = _p6_import_dir(diagram_import_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted_dir = _p6_safe_extract_upload(data, out_dir)
    validation = _p6_validate_extracted_pack(extracted_dir)
    actions = _p6_build_dynamic_actions(diagram_import_id, extracted_dir, validation.get("index") or {}) if validation.get("index") else []
    return _p6_write_summary(diagram_import_id, filename, out_dir, extracted_dir, validation, actions)


@app.get("/p6/diagram-packs")
def list_p6_diagram_packs() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for path in sorted(_p6_root().glob(f"*/{P6_SUMMARY_FILENAME}"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            items.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return {"items": items, "count": len(items)}


@app.get("/p6/diagram-packs/{diagram_import_id}")
def read_p6_diagram_pack(diagram_import_id: str) -> dict[str, Any]:
    path = _p6_import_dir(diagram_import_id) / P6_SUMMARY_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="P6 diagram pack not found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/p6/diagram-packs/{diagram_import_id}/validate")
def validate_p6_diagram_pack(diagram_import_id: str) -> dict[str, Any]:
    out_dir = _p6_import_dir(diagram_import_id)
    extracted_dir = out_dir / "extracted"
    if not extracted_dir.exists():
        raise HTTPException(status_code=404, detail="P6 diagram extracted directory not found")
    validation = _p6_validate_extracted_pack(extracted_dir)
    actions = _p6_build_dynamic_actions(diagram_import_id, extracted_dir, validation.get("index") or {}) if validation.get("index") else []
    old = _read_json_if_exists(out_dir / P6_SUMMARY_FILENAME)
    _p6_write_summary(diagram_import_id, old.get("filename") or "uploaded.zip", out_dir, extracted_dir, validation, actions)
    return {k: v for k, v in validation.items() if k != "index"}


@app.get("/p6/diagram-packs/{diagram_import_id}/actions")
def list_p6_diagram_actions(diagram_import_id: str) -> dict[str, Any]:
    path = _p6_import_dir(diagram_import_id) / P6_ACTIONS_FILENAME
    if not path.exists():
        pack = read_p6_diagram_pack(diagram_import_id)
        return {"items": pack.get("actions") or []}
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/p6/diagram-packs/{diagram_import_id}/download/{action_key}")
def download_p6_diagram_action(diagram_import_id: str, action_key: str) -> FileResponse:
    out_dir = _p6_import_dir(diagram_import_id)
    summary = read_p6_diagram_pack(diagram_import_id)
    action = None
    for item in summary.get("actions") or []:
        if item.get("action_key") == action_key:
            action = item
            break
    if not action:
        raise HTTPException(status_code=404, detail=f"P6 diagram action not found: {action_key}")
    if action_key == "download_all":
        source_zip = out_dir / "source.zip"
        if not source_zip.exists():
            raise HTTPException(status_code=404, detail="Original P6 diagram ZIP not found")
        return FileResponse(str(source_zip), filename=summary.get("filename") or "P3_DIAGRAM_DATA_PACK.zip", media_type="application/zip")
    files = action.get("download_files") or []
    if not files:
        raise HTTPException(status_code=404, detail="No files are bound to this P6 diagram action")
    extracted_dir = out_dir / "extracted"
    download_dir = out_dir / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    zip_path = download_dir / f"{action_key}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "diagram_import_id": diagram_import_id,
            "action": action,
            "data_scope": "up_to_P3",
            "source_phases": ["P1", "P2", "P3"],
            "note_ja": "このER図はP3までのデータから作成されています。P4/P5の顧客回答・詳細ロジック・最終Odoo反映結果は含みません。",
            "confirmation_materials_prompt": P6_P3_CONFIRMATION_PROMPT_FILENAME if action.get("includes_confirmation_prompt") else None,
            "next_thread_start_message": P6_P3_CONFIRMATION_START_FILENAME if action.get("includes_confirmation_prompt") else None,
        }
        zf.writestr("DOWNLOAD_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        if action.get("includes_confirmation_prompt"):
            zf.writestr(P6_P3_CONFIRMATION_PROMPT_FILENAME, P6_P3_CONFIRMATION_MATERIALS_PROMPT)
            zf.writestr(P6_P3_CONFIRMATION_START_FILENAME, P6_P3_CONFIRMATION_NEXT_THREAD_MESSAGE)
        for rel in files:
            p = extracted_dir / rel
            if p.exists() and p.is_file():
                zf.write(p, arcname=rel)
    return FileResponse(str(zip_path), filename=zip_path.name, media_type="application/zip")


# ---------------------------------------------------------------------------
# P7 Authority / Organization yFiles Visualization API
# ---------------------------------------------------------------------------
# Purpose:
# - Import SAMPLECO_P6_AUTHORITY_VISUALIZATION_PACK style ZIPs.
# - Validate and expose yFiles payloads for organization / approval / visibility.
# - Keep this feature read/import/inspect/download only.
# - Do not generate/apply Odoo code and do not apply Neo4j changes here.

P7_AUTHORITY_ROOT_NAME = "p7_authority_packs"
P7_SUMMARY_FILENAME = "p7_authority_import_summary.json"
P7_VALIDATION_FILENAME = "p7_authority_validation.json"
P7_ACTIONS_FILENAME = "p7_authority_actions.json"
P7_MANIFEST_FILENAME = "MANIFEST.json"
P7_EXPECTED_PACK_KEY = "SAMPLECO_P6_AUTHORITY_VISUALIZATION_PACK"

P7_VIEW_EXPORT_FALLBACKS = {
    "view.organization_overview": "exports/yfiles_organization_overview.json",
    "view.approval_sales_order_confirm": "exports/yfiles_approval_sales_order_confirm.json",
    "view.approval_purchase_order_confirm": "exports/yfiles_approval_purchase_order_confirm.json",
    "view.visibility_sales": "exports/yfiles_visibility_sales.json",
    "view.visibility_purchase": "exports/yfiles_visibility_purchase.json",
    "view.app_model_responsibility": "exports/yfiles_app_model_responsibility.json",
}


def _p7_root() -> Path:
    root = ARTIFACT_ROOT / P7_AUTHORITY_ROOT_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _p7_import_dir(authority_import_id: str) -> Path:
    return _p7_root() / authority_import_id


def _p7_safe_extract_upload(data: bytes, out_dir: Path) -> Path:
    source_zip = out_dir / "source.zip"
    source_zip.write_bytes(data)
    extracted_dir = out_dir / "extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(source_zip) as zf:
            for member in zf.infolist():
                name = member.filename
                normalized = Path(name)
                if name.startswith("/") or ".." in normalized.parts:
                    raise HTTPException(status_code=400, detail=f"Unsafe ZIP entry: {name}")
            zf.extractall(extracted_dir)
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail=f"Invalid ZIP: {exc}") from exc
    return extracted_dir


def _p7_find_file(extracted_dir: Path, rel_path: str) -> Path | None:
    direct = extracted_dir / rel_path
    if direct.exists() and direct.is_file():
        return direct
    matches = sorted(extracted_dir.rglob(rel_path))
    for match in matches:
        if match.is_file():
            return match
    name_matches = sorted(extracted_dir.rglob(Path(rel_path).name))
    for match in name_matches:
        if match.is_file() and str(match).replace("\\", "/").endswith(rel_path):
            return match
    return None


def _p7_find_dir(extracted_dir: Path, rel_path: str) -> Path | None:
    direct = extracted_dir / rel_path
    if direct.exists() and direct.is_dir():
        return direct
    matches = sorted(extracted_dir.rglob(Path(rel_path).name))
    for match in matches:
        if match.is_dir() and str(match).replace("\\", "/").endswith(rel_path):
            return match
    return None


def _p7_read_json(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _p7_relpath(extracted_dir: Path, path: Path | None) -> str | None:
    if not path:
        return None
    try:
        return str(path.relative_to(extracted_dir)).replace("\\", "/")
    except Exception:
        return str(path)


def _p7_flatten_items(payload: dict[str, Any]) -> list[Any]:
    items = payload.get("items")
    if isinstance(items, list):
        return items
    if isinstance(payload, list):
        return payload
    return []


def _p7_count_items(path: Path | None) -> int:
    data = _p7_read_json(path)
    return len(_p7_flatten_items(data))


def _p7_count_approval_processes(path: Path | None) -> int:
    items = _p7_flatten_items(_p7_read_json(path))
    return len([x for x in items if isinstance(x, dict) and x.get("target_model") and x.get("target_operation")])


def _p7_view_key_to_filename(view_key: str) -> str:
    cleaned = view_key.replace("view.", "").replace(".", "_").replace("-", "_")
    return f"exports/yfiles_{cleaned}.json"


def _p7_export_path_for_view(extracted_dir: Path, view_key: str) -> Path | None:
    candidates = []
    if view_key in P7_VIEW_EXPORT_FALLBACKS:
        candidates.append(P7_VIEW_EXPORT_FALLBACKS[view_key])
    candidates.append(_p7_view_key_to_filename(view_key))
    for rel in candidates:
        path = _p7_find_file(extracted_dir, rel)
        if path:
            return path
    exports_dir = _p7_find_dir(extracted_dir, "exports")
    if exports_dir:
        suffix = view_key.replace("view.", "").replace(".", "_")
        for path in sorted(exports_dir.glob("yfiles_*.json")):
            if suffix in path.stem:
                return path
    return None


def _p7_list_yfiles_exports(extracted_dir: Path) -> list[Path]:
    exports_dir = _p7_find_dir(extracted_dir, "exports")
    if not exports_dir:
        return []
    return sorted([p for p in exports_dir.glob("yfiles_*.json") if p.is_file()])


def _p7_validate_yfiles_payload(path: Path) -> dict[str, Any]:
    payload = _p7_read_json(path)
    nodes = payload.get("nodes") or []
    edges = payload.get("edges") or []
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(nodes, list):
        errors.append(f"{path.name}: nodes is not a list")
        nodes = []
    if not isinstance(edges, list):
        errors.append(f"{path.name}: edges is not a list")
        edges = []
    node_ids = {str(n.get("id")) for n in nodes if isinstance(n, dict) and n.get("id")}
    missing: list[dict[str, str]] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        edge_id = str(edge.get("id") or f"edge:{source}:{target}")
        if source not in node_ids or target not in node_ids:
            missing.append({"edge_id": edge_id, "source": source, "target": target})
    if missing:
        errors.append(f"{path.name}: {len(missing)} edge endpoint(s) missing")
    if len(nodes) > 300:
        warnings.append(f"{path.name}: large view ({len(nodes)} nodes)")
    return {
        "file": path.name,
        "view_key": payload.get("view_key") or path.stem,
        "view_type": payload.get("view_type"),
        "title_ja": payload.get("title_ja"),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "missing_endpoint_count": len(missing),
        "missing_endpoints": missing[:50],
        "errors": errors,
        "warnings": warnings,
    }


def _p7_scan_legacy_identifier_occurrences(extracted_dir: Path) -> int:
    """Count legacy identifier residue only in generated machine-readable graph/view data.

    INPUT_FILE_INVENTORY / policy notes may mention the source file history.
    P7 validation should be strict for the generated data used by the app, not
    for explanatory markdown that documents where the sample came from.
    """
    count = 0
    for rel_root in ["data", "exports"]:
        root = _p7_find_dir(extracted_dir, rel_root)
        if not root:
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".json", ".md", ".txt", ".csv"}:
                continue
            try:
                count += path.read_text(encoding="utf-8", errors="ignore").count("LEGACY_ORG")
            except Exception:
                continue
    return count


def _p7_validate_extracted_pack(extracted_dir: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    manifest_path = _p7_find_file(extracted_dir, P7_MANIFEST_FILENAME)
    graph_nodes_path = _p7_find_file(extracted_dir, "data/graph_nodes.json")
    graph_edges_path = _p7_find_file(extracted_dir, "data/graph_edges.json")
    yfiles_views_path = _p7_find_file(extracted_dir, "data/yfiles_views.json")
    approval_processes_path = _p7_find_file(extracted_dir, "data/approval_processes.json")
    approval_codegen_path = _p7_find_file(extracted_dir, "data/approval_codegen_patch_units.json")
    p3_refs_path = _p7_find_file(extracted_dir, "data/p3_structural_refs.json")
    p4p5_refs_path = _p7_find_file(extracted_dir, "data/p4p5_theme_refs.json")
    validation_report_path = _p7_find_file(extracted_dir, "VALIDATION_REPORT.md")

    required = {
        "MANIFEST.json": manifest_path,
        "data/graph_nodes.json": graph_nodes_path,
        "data/graph_edges.json": graph_edges_path,
        "data/yfiles_views.json": yfiles_views_path,
    }
    for label, path in required.items():
        if not path:
            errors.append(f"Missing required file: {label}")

    manifest = _p7_read_json(manifest_path)
    pack_key = str(manifest.get("pack_key") or "")
    if manifest_path and P7_EXPECTED_PACK_KEY not in pack_key:
        errors.append(f"Unexpected pack_key: {pack_key}")

    exports = _p7_list_yfiles_exports(extracted_dir)
    if not exports:
        errors.append("No exports/yfiles_*.json files found")

    view_results = [_p7_validate_yfiles_payload(p) for p in exports]
    for result in view_results:
        errors.extend(result.get("errors") or [])
        warnings.extend(result.get("warnings") or [])

    legacy_identifier_occurrences = _p7_scan_legacy_identifier_occurrences(extracted_dir)
    if legacy_identifier_occurrences:
        errors.append(f"Legacy identifier occurrences found: {legacy_identifier_occurrences}")

    yfiles_views = _p7_read_json(yfiles_views_path)
    view_items = _p7_flatten_items(yfiles_views)
    candidate_count = 0
    confirmation_required_count = 0
    for path in [graph_nodes_path, graph_edges_path, yfiles_views_path, approval_processes_path, approval_codegen_path]:
        data = _p7_read_json(path)
        text = json.dumps(data, ensure_ascii=False)
        candidate_count += text.count('"candidate"')
        confirmation_required_count += text.count('confirmation_required')

    counts = {
        "graph_nodes": _p7_count_items(graph_nodes_path),
        "graph_edges": _p7_count_items(graph_edges_path),
        "yfiles_views": len(view_items) or len(exports),
        "yfiles_exports": len(exports),
        "approval_processes": _p7_count_approval_processes(approval_processes_path),
        "approval_codegen_patch_units": _p7_count_items(approval_codegen_path),
        "p3_structural_refs": _p7_count_items(p3_refs_path),
        "p4p5_theme_refs": _p7_count_items(p4p5_refs_path),
        "candidate_occurrences": candidate_count,
        "confirmation_required_occurrences": confirmation_required_count,
        "legacy_identifier_occurrences": legacy_identifier_occurrences,
    }

    ready_for_codegen_count = 0
    for item in _p7_flatten_items(_p7_read_json(approval_codegen_path)):
        if isinstance(item, dict) and item.get("status") == "ready_for_codegen":
            ready_for_codegen_count += 1
    counts["codegen_ready"] = ready_for_codegen_count

    return {
        "valid": not errors,
        "status": "valid" if not errors else "validation_failed",
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "counts": counts,
        "manifest": manifest,
        "files": {
            "manifest": _p7_relpath(extracted_dir, manifest_path),
            "graph_nodes": _p7_relpath(extracted_dir, graph_nodes_path),
            "graph_edges": _p7_relpath(extracted_dir, graph_edges_path),
            "yfiles_views": _p7_relpath(extracted_dir, yfiles_views_path),
            "approval_processes": _p7_relpath(extracted_dir, approval_processes_path),
            "approval_codegen_patch_units": _p7_relpath(extracted_dir, approval_codegen_path),
            "validation_report": _p7_relpath(extracted_dir, validation_report_path),
        },
        "views": view_results,
    }


def _p7_build_actions(authority_import_id: str, extracted_dir: Path, validation: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = [
        {
            "action_key": "download_all",
            "label_ja": "P7権限・組織PACK一式DL",
            "action_type": "download_original_zip",
            "group_key": "pack",
            "group_label_ja": "Pack",
            "download_url": f"/p7/authority-packs/{authority_import_id}/download/download_all",
        }
    ]
    for rel, label in [
        ("VALIDATION_REPORT.md", "Validation Report"),
        ("data/graph_nodes.json", "Graph Nodes JSON"),
        ("data/graph_edges.json", "Graph Edges JSON"),
        ("data/approval_codegen_patch_units.json", "Approval Codegen Patch Units"),
        ("templates/SAMPLECO_P6_AUTHORITY_ORG_YFILES_TEMPLATE_integrated.xlsx", "Integrated Excel Template"),
    ]:
        path = _p7_find_file(extracted_dir, rel)
        if path:
            action_key = rel.replace("/", "__").replace(".", "_")
            actions.append({
                "action_key": action_key,
                "label_ja": label,
                "action_type": "download_file",
                "group_key": "data",
                "group_label_ja": "Data / Template",
                "file": _p7_relpath(extracted_dir, path),
                "download_url": f"/p7/authority-packs/{authority_import_id}/download/{action_key}",
            })
    for view in validation.get("views") or []:
        view_key = view.get("view_key") or view.get("file")
        file_name = view.get("file")
        path = _p7_export_path_for_view(extracted_dir, str(view_key)) or _p7_find_file(extracted_dir, f"exports/{file_name}")
        if not path:
            continue
        action_key = f"download_view__{str(view_key).replace('.', '_')}"
        actions.append({
            "action_key": action_key,
            "label_ja": view.get("title_ja") or str(view_key),
            "action_type": "download_yfiles_json",
            "group_key": "yfiles",
            "group_label_ja": "yFiles Views",
            "view_key": view_key,
            "file": _p7_relpath(extracted_dir, path),
            "node_count": view.get("node_count"),
            "edge_count": view.get("edge_count"),
            "download_url": f"/p7/authority-packs/{authority_import_id}/download/{action_key}",
        })
    return actions


def _p7_write_summary(authority_import_id: str, filename: str, out_dir: Path, extracted_dir: Path, validation: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    counts = validation.get("counts") or {}
    summary = {
        "authority_import_id": authority_import_id,
        "filename": filename,
        "status": "imported" if validation.get("valid") else "validation_failed",
        "imported_at": _now_iso(),
        "pack_type": "p7_authority_visualization_pack",
        "description_ja": "P7権限・組織・承認・可視範囲のyFiles可視化PACKです。Odoo Codegen/Applyはこの画面では実行しません。",
        "source_zip_path": str(out_dir / "source.zip"),
        "extracted_dir": str(extracted_dir),
        "validation": {k: v for k, v in validation.items() if k not in {"manifest"}},
        "summary": {
            "action_count": len(actions),
            "view_count": counts.get("yfiles_views", 0),
            "yfiles_export_count": counts.get("yfiles_exports", 0),
            "node_count": counts.get("graph_nodes", 0),
            "edge_count": counts.get("graph_edges", 0),
            "approval_process_count": counts.get("approval_processes", 0),
            "codegen_ready_count": counts.get("codegen_ready", 0),
            "p3_structural_ref_count": counts.get("p3_structural_refs", 0),
            "p4p5_theme_ref_count": counts.get("p4p5_theme_refs", 0),
            "legacy_identifier_occurrences": counts.get("legacy_identifier_occurrences", 0),
        },
        "views": validation.get("views") or [],
        "actions": actions,
        "links": {
            "self": f"/p7/authority-packs/{authority_import_id}",
            "validate": f"/p7/authority-packs/{authority_import_id}/validate",
            "views": f"/p7/authority-packs/{authority_import_id}/views",
        },
    }
    (out_dir / P7_VALIDATION_FILENAME).write_text(json.dumps({k: v for k, v in validation.items() if k != "manifest"}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / P7_ACTIONS_FILENAME).write_text(json.dumps({"items": actions}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / P7_SUMMARY_FILENAME).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


@app.post("/p7/authority-packs/import")
async def import_p7_authority_pack(file: UploadFile = File(...)) -> dict[str, Any]:
    filename = file.filename or "SAMPLECO_P6_AUTHORITY_VISUALIZATION_PACK_v1.zip"
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="P7 Authority Pack import accepts ZIP files only.")
    data = await file.read()
    authority_import_id = str(uuid4())
    out_dir = _p7_import_dir(authority_import_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted_dir = _p7_safe_extract_upload(data, out_dir)
    validation = _p7_validate_extracted_pack(extracted_dir)
    actions = _p7_build_actions(authority_import_id, extracted_dir, validation)
    return _p7_write_summary(authority_import_id, filename, out_dir, extracted_dir, validation, actions)


@app.get("/p7/authority-packs")
def list_p7_authority_packs() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for path in sorted(_p7_root().glob(f"*/{P7_SUMMARY_FILENAME}"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            items.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return {"items": items, "count": len(items)}


@app.get("/p7/authority-packs/latest")
def latest_p7_authority_pack() -> dict[str, Any]:
    items = list_p7_authority_packs().get("items") or []
    if not items:
        raise HTTPException(status_code=404, detail="No P7 authority pack imported")
    return items[0]


@app.get("/p7/authority-packs/{authority_import_id}")
def read_p7_authority_pack(authority_import_id: str) -> dict[str, Any]:
    path = _p7_import_dir(authority_import_id) / P7_SUMMARY_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="P7 authority pack not found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/p7/authority-packs/{authority_import_id}/validate")
def validate_p7_authority_pack(authority_import_id: str) -> dict[str, Any]:
    out_dir = _p7_import_dir(authority_import_id)
    extracted_dir = out_dir / "extracted"
    if not extracted_dir.exists():
        raise HTTPException(status_code=404, detail="P7 authority extracted directory not found")
    validation = _p7_validate_extracted_pack(extracted_dir)
    actions = _p7_build_actions(authority_import_id, extracted_dir, validation)
    old = _p7_read_json(out_dir / P7_SUMMARY_FILENAME)
    _p7_write_summary(authority_import_id, old.get("filename") or "uploaded.zip", out_dir, extracted_dir, validation, actions)
    return {k: v for k, v in validation.items() if k != "manifest"}


@app.get("/p7/authority-packs/{authority_import_id}/views")
def list_p7_authority_views(authority_import_id: str) -> dict[str, Any]:
    summary = read_p7_authority_pack(authority_import_id)
    return {"authority_import_id": authority_import_id, "items": summary.get("views") or [], "count": len(summary.get("views") or [])}


@app.get("/p7/authority-packs/{authority_import_id}/views/{view_key:path}")
def read_p7_authority_view(authority_import_id: str, view_key: str) -> dict[str, Any]:
    out_dir = _p7_import_dir(authority_import_id)
    extracted_dir = out_dir / "extracted"
    if not extracted_dir.exists():
        raise HTTPException(status_code=404, detail="P7 authority extracted directory not found")
    path = _p7_export_path_for_view(extracted_dir, view_key)
    if not path:
        raise HTTPException(status_code=404, detail=f"P7 yFiles view not found: {view_key}")
    payload = _p7_read_json(path)
    return {
        "authority_import_id": authority_import_id,
        "view_key": payload.get("view_key") or view_key,
        "file": _p7_relpath(extracted_dir, path),
        "payload": payload,
    }


@app.get("/p7/authority-packs/{authority_import_id}/views/{view_key:path}/download")
def download_p7_authority_view(authority_import_id: str, view_key: str) -> FileResponse:
    out_dir = _p7_import_dir(authority_import_id)
    extracted_dir = out_dir / "extracted"
    path = _p7_export_path_for_view(extracted_dir, view_key)
    if not path:
        raise HTTPException(status_code=404, detail=f"P7 yFiles view not found: {view_key}")
    return FileResponse(str(path), filename=path.name, media_type="application/json")


@app.get("/p7/authority-packs/{authority_import_id}/download/{action_key}")
def download_p7_authority_action(authority_import_id: str, action_key: str) -> FileResponse:
    out_dir = _p7_import_dir(authority_import_id)
    summary = read_p7_authority_pack(authority_import_id)
    if action_key == "download_all":
        source_zip = out_dir / "source.zip"
        if not source_zip.exists():
            raise HTTPException(status_code=404, detail="Original P7 authority ZIP not found")
        return FileResponse(str(source_zip), filename=summary.get("filename") or "SAMPLECO_P6_AUTHORITY_VISUALIZATION_PACK_v1.zip", media_type="application/zip")
    action = None
    for item in summary.get("actions") or []:
        if item.get("action_key") == action_key:
            action = item
            break
    if not action:
        raise HTTPException(status_code=404, detail=f"P7 authority action not found: {action_key}")
    rel = action.get("file")
    if not rel:
        raise HTTPException(status_code=404, detail="No file is bound to this P7 authority action")
    path = out_dir / "extracted" / rel
    if not path.exists() or not path.is_file():
        fallback = _p7_find_file(out_dir / "extracted", rel)
        if not fallback:
            raise HTTPException(status_code=404, detail=f"P7 authority file not found: {rel}")
        path = fallback
    media = "application/json" if path.suffix.lower() == ".json" else "application/octet-stream"
    if path.suffix.lower() == ".xlsx":
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if path.suffix.lower() == ".md":
        media = "text/markdown"
    return FileResponse(str(path), filename=path.name, media_type=media)




# ---------------------------------------------------------------------------
# P4-Q-1: P3 Internal Structural Binding Pack Import API
# ---------------------------------------------------------------------------
# Purpose:
# - Import P3_INTERNAL_STRUCTURAL_BINDING_PACK_v1.zip into the system as data.
# - Do not read arbitrary files at P4 export time; later P4 question-pack export
#   must reference this persisted import by binding_import_id.
# - Keep this step import/inspect/validate only. No P4 theme generation here.

P4_P3_BINDING_ROOT_NAME = "p4_p3_internal_bindings"
P4_P3_BINDING_SUMMARY_FILENAME = "p3_internal_binding_import_summary.json"
P4_P3_BINDING_VALIDATION_FILENAME = "p3_internal_binding_validation.json"
P4_P3_BINDING_MANIFEST_FILENAME = "manifest.json"

P4_P3_BINDING_REQUIRED_FILES = [
    "manifest.json",
    "data/P3_INTERNAL_NODE_CATALOG.json",
    "data/P3_INTERNAL_EDGE_CATALOG.json",
    "data/P3_INTERNAL_FIELD_CATALOG.json",
    "data/P3_INTERNAL_MODEL_CATALOG.json",
    "data/P3_INTERNAL_RELATION_TABLE_CATALOG.json",
    "data/P3_INTERNAL_MISSING_MODEL_CATALOG.json",
    "data/P3_INTERNAL_GRAPH_BINDING_INDEX.json",
    "data/P3_INTERNAL_AFFECTED_STRUCTURAL_ELEMENTS_TEMPLATE.json",
]

P4_P3_BINDING_OPTIONAL_FILES = [
    "data/P3_INTERNAL_SOURCE_REF_CATALOG.json",
    "data/P3_INTERNAL_THEME_BINDING_CANDIDATES.json",
    "schemas/p3_internal_node.schema.json",
    "schemas/p3_internal_edge.schema.json",
    "schemas/p3_internal_theme_binding.schema.json",
    "schemas/affected_structural_elements.schema.json",
    "prompts/P4_THEME_BINDING_GENERATION_PROMPT.md",
    "prompts/P4_CUSTOMER_PACK_WITH_DIAGRAM_PROMPT.md",
    "validation_report.md",
    "README.md",
]


def _p4_p3_binding_root() -> Path:
    root = ARTIFACT_ROOT / P4_P3_BINDING_ROOT_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _p4_p3_binding_dir(binding_import_id: str) -> Path:
    return _p4_p3_binding_root() / binding_import_id


def _safe_zip_extract(data: bytes, out_dir: Path) -> Path:
    source_zip = out_dir / "source.zip"
    source_zip.write_bytes(data)
    extracted_dir = out_dir / "extracted"
    if extracted_dir.exists():
        shutil.rmtree(extracted_dir)
    extracted_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(source_zip) as zf:
            for member in zf.infolist():
                name = member.filename
                normalized = Path(name)
                if name.startswith("/") or ".." in normalized.parts:
                    raise HTTPException(status_code=400, detail=f"Unsafe ZIP entry: {name}")
            zf.extractall(extracted_dir)
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail=f"Invalid ZIP: {exc}") from exc
    return extracted_dir


def _find_rel_file(extracted_dir: Path, rel_path: str) -> Path | None:
    direct = extracted_dir / rel_path
    if direct.exists() and direct.is_file():
        return direct
    matches = sorted(extracted_dir.rglob(Path(rel_path).name))
    # Prefer exact suffix path when ZIP has one root directory.
    for m in matches:
        try:
            if str(m.relative_to(extracted_dir)).replace("\\", "/").endswith(rel_path):
                return m
        except Exception:
            pass
    return matches[0] if matches else None


def _read_json_file_or_none(path: Path | None) -> Any:
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _catalog_len(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        if isinstance(value.get("items"), list):
            return len(value["items"])
        return len(value)
    return 0


def _p4_p3_validate_binding_pack(extracted_dir: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    required_paths: dict[str, str | None] = {}
    optional_paths: dict[str, str | None] = {}

    for rel in P4_P3_BINDING_REQUIRED_FILES:
        p = _find_rel_file(extracted_dir, rel)
        required_paths[rel] = str(p.relative_to(extracted_dir)).replace("\\", "/") if p else None
        if not p:
            errors.append(f"Missing required file: {rel}")
    for rel in P4_P3_BINDING_OPTIONAL_FILES:
        p = _find_rel_file(extracted_dir, rel)
        optional_paths[rel] = str(p.relative_to(extracted_dir)).replace("\\", "/") if p else None
        if not p:
            warnings.append(f"Optional file not found: {rel}")

    manifest = _read_json_file_or_none(_find_rel_file(extracted_dir, "manifest.json")) or {}
    pack_type = manifest.get("pack_type")
    data_scope = manifest.get("data_scope")
    source_phases = manifest.get("source_phases") or []
    if pack_type and pack_type != "p3_internal_structural_binding":
        warnings.append(f"Unexpected pack_type: {pack_type}")
    if data_scope and data_scope != "up_to_P3":
        warnings.append(f"Unexpected data_scope: {data_scope}")

    nodes = _read_json_file_or_none(_find_rel_file(extracted_dir, "data/P3_INTERNAL_NODE_CATALOG.json")) or []
    edges = _read_json_file_or_none(_find_rel_file(extracted_dir, "data/P3_INTERNAL_EDGE_CATALOG.json")) or []
    fields = _read_json_file_or_none(_find_rel_file(extracted_dir, "data/P3_INTERNAL_FIELD_CATALOG.json")) or []
    models = _read_json_file_or_none(_find_rel_file(extracted_dir, "data/P3_INTERNAL_MODEL_CATALOG.json")) or []
    relation_tables = _read_json_file_or_none(_find_rel_file(extracted_dir, "data/P3_INTERNAL_RELATION_TABLE_CATALOG.json")) or []
    missing_models = _read_json_file_or_none(_find_rel_file(extracted_dir, "data/P3_INTERNAL_MISSING_MODEL_CATALOG.json")) or []
    source_refs = _read_json_file_or_none(_find_rel_file(extracted_dir, "data/P3_INTERNAL_SOURCE_REF_CATALOG.json")) or []
    theme_candidates = _read_json_file_or_none(_find_rel_file(extracted_dir, "data/P3_INTERNAL_THEME_BINDING_CANDIDATES.json")) or []
    graph_index = _read_json_file_or_none(_find_rel_file(extracted_dir, "data/P3_INTERNAL_GRAPH_BINDING_INDEX.json")) or {}

    node_keys = [str(n.get("node_key") or "") for n in nodes if isinstance(n, dict)]
    edge_keys = [str(e.get("edge_key") or "") for e in edges if isinstance(e, dict)]
    duplicate_node_keys = sorted({k for k in node_keys if k and node_keys.count(k) > 1})
    duplicate_edge_keys = sorted({k for k in edge_keys if k and edge_keys.count(k) > 1})
    if duplicate_node_keys:
        errors.append(f"Duplicate node_key count: {len(duplicate_node_keys)}")
    if duplicate_edge_keys:
        errors.append(f"Duplicate edge_key count: {len(duplicate_edge_keys)}")

    node_key_set = set(node_keys)
    dangling_edges: list[dict[str, Any]] = []
    for e in edges if isinstance(edges, list) else []:
        if not isinstance(e, dict):
            continue
        fk = e.get("from_node_key")
        tk = e.get("to_node_key")
        if (fk and fk not in node_key_set) or (tk and tk not in node_key_set):
            dangling_edges.append({"edge_key": e.get("edge_key"), "from_node_key": fk, "to_node_key": tk})
    if dangling_edges:
        errors.append(f"Dangling edge count: {len(dangling_edges)}")

    app_keys: set[str] = set()
    for n in nodes if isinstance(nodes, list) else []:
        if not isinstance(n, dict):
            continue
        if n.get("app_key"):
            app_keys.add(str(n["app_key"]))
        for ak in n.get("app_keys") or []:
            if ak:
                app_keys.add(str(ak))
    by_app = graph_index.get("by_app") if isinstance(graph_index, dict) else {}
    if isinstance(by_app, dict):
        app_keys.update(str(k) for k in by_app.keys())

    bundle_keys: set[str] = set()
    for n in nodes if isinstance(nodes, list) else []:
        if isinstance(n, dict):
            bundle_keys.update(str(x) for x in (n.get("bundle_keys") or []) if x)
    for e in edges if isinstance(edges, list) else []:
        if isinstance(e, dict):
            bundle_keys.update(str(x) for x in (e.get("bundle_keys") or []) if x)

    counts = {
        "nodes": _catalog_len(nodes),
        "edges": _catalog_len(edges),
        "models": _catalog_len(models),
        "fields": _catalog_len(fields),
        "custom_fields": sum(1 for f in fields if isinstance(f, dict) and f.get("field_class") == "CusF"),
        "relation_tables": _catalog_len(relation_tables),
        "missing_models": _catalog_len(missing_models),
        "source_refs": _catalog_len(source_refs),
        "theme_candidates": _catalog_len(theme_candidates),
        "app_keys": len(app_keys),
        "bundle_keys": len(bundle_keys),
        "duplicate_node_keys": len(duplicate_node_keys),
        "duplicate_edge_keys": len(duplicate_edge_keys),
        "dangling_edges": len(dangling_edges),
    }

    return {
        "valid": not errors,
        "status": "valid" if not errors else "validation_failed",
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "required_files": required_paths,
        "optional_files": optional_paths,
        "manifest": manifest,
        "pack_type": pack_type or "unknown",
        "data_scope": data_scope or "unknown",
        "source_phases": source_phases,
        "counts": counts,
        "app_keys": sorted(app_keys),
        "bundle_keys_sample": sorted(bundle_keys)[:30],
        "duplicate_node_keys_sample": duplicate_node_keys[:20],
        "duplicate_edge_keys_sample": duplicate_edge_keys[:20],
        "dangling_edges_sample": dangling_edges[:20],
        "missing_models_sample": missing_models[:20] if isinstance(missing_models, list) else [],
        "usage_note_ja": "このP3内部BindingはP4質問PACK生成時に、システムに取り込まれたデータとして参照します。ファイルを都度直接読む用途ではありません。",
    }


def _p4_p3_write_binding_summary(binding_import_id: str, filename: str, out_dir: Path, extracted_dir: Path, validation: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "binding_import_id": binding_import_id,
        "filename": filename,
        "status": "imported" if validation.get("valid") else "validation_failed",
        "imported_at": _now_iso(),
        "binding_pack_type": "p3_internal_structural_binding",
        "data_scope": validation.get("data_scope") or "up_to_P3",
        "source_phases": validation.get("source_phases") or ["P1", "P2", "P3"],
        "source_zip_path": str(out_dir / "source.zip"),
        "extracted_dir": str(extracted_dir),
        "description_ja": "P4テーマ別Question Pack生成で参照する内部用P3構造Bindingです。顧客向け資料ではありません。",
        "validation": {k: v for k, v in validation.items() if k not in {"manifest"}},
        "counts": validation.get("counts") or {},
        "app_keys": validation.get("app_keys") or [],
        "links": {
            "self": f"/p4/internal-p3-bindings/{binding_import_id}",
            "validate": f"/p4/internal-p3-bindings/{binding_import_id}/validate",
        },
        "next_usage": {
            "assumption": "P4P5_DEVELOPMENT_THEME_CATALOG_ALL_APPS_v2 is already imported in the system.",
            "future_export_flow": "P4 Theme Catalog import -> select this binding_import_id -> generate P4 Customer Question Pack with diagrams/tables from persisted system data.",
        },
    }
    (out_dir / P4_P3_BINDING_VALIDATION_FILENAME).write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / P4_P3_BINDING_SUMMARY_FILENAME).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


@app.post("/p4/internal-p3-bindings/import")
async def import_p4_internal_p3_binding(file: UploadFile = File(...)) -> dict[str, Any]:
    filename = file.filename or "P3_INTERNAL_STRUCTURAL_BINDING_PACK_v1.zip"
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="P3 Internal Binding import accepts ZIP files only.")
    data = await file.read()
    binding_import_id = str(uuid4())
    out_dir = _p4_p3_binding_dir(binding_import_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted_dir = _safe_zip_extract(data, out_dir)
    validation = _p4_p3_validate_binding_pack(extracted_dir)
    return _p4_p3_write_binding_summary(binding_import_id, filename, out_dir, extracted_dir, validation)


@app.get("/p4/internal-p3-bindings")
def list_p4_internal_p3_bindings() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for path in sorted(_p4_p3_binding_root().glob(f"*/{P4_P3_BINDING_SUMMARY_FILENAME}"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            items.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return {"items": items, "count": len(items)}


@app.get("/p4/internal-p3-bindings/{binding_import_id}")
def read_p4_internal_p3_binding(binding_import_id: str) -> dict[str, Any]:
    path = _p4_p3_binding_dir(binding_import_id) / P4_P3_BINDING_SUMMARY_FILENAME
    if not path.exists():
        raise HTTPException(status_code=404, detail="P3 internal binding import not found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/p4/internal-p3-bindings/{binding_import_id}/validate")
def validate_p4_internal_p3_binding(binding_import_id: str) -> dict[str, Any]:
    out_dir = _p4_p3_binding_dir(binding_import_id)
    extracted_dir = out_dir / "extracted"
    if not extracted_dir.exists():
        raise HTTPException(status_code=404, detail="P3 internal binding extracted directory not found")
    validation = _p4_p3_validate_binding_pack(extracted_dir)
    old = json.loads((out_dir / P4_P3_BINDING_SUMMARY_FILENAME).read_text(encoding="utf-8")) if (out_dir / P4_P3_BINDING_SUMMARY_FILENAME).exists() else {}
    _p4_p3_write_binding_summary(binding_import_id, old.get("filename") or "uploaded.zip", out_dir, extracted_dir, validation)
    return validation


@app.get("/p4/internal-p3-bindings/{binding_import_id}/download/source")
def download_p4_internal_p3_binding_source(binding_import_id: str) -> FileResponse:
    out_dir = _p4_p3_binding_dir(binding_import_id)
    source_zip = out_dir / "source.zip"
    if not source_zip.exists():
        raise HTTPException(status_code=404, detail="Original P3 internal binding ZIP not found")
    summary = read_p4_internal_p3_binding(binding_import_id)
    return FileResponse(str(source_zip), filename=summary.get("filename") or "P3_INTERNAL_STRUCTURAL_BINDING_PACK_v1.zip", media_type="application/zip")

def _p6_artifacts() -> dict[str, Any]:
    phase_key = "P6"
    summary_path = None
    summaries = sorted(_p6_root().glob(f"*/{P6_SUMMARY_FILENAME}"), key=lambda x: x.stat().st_mtime, reverse=True)
    if summaries:
        summary_path = summaries[0]
    if not summary_path:
        return _empty_phase_artifacts(phase_key)
    import_dir = summary_path.parent
    summary = _read_json_if_exists(summary_path)
    import_id = summary.get("diagram_import_id") or import_dir.name
    files = [p for p in import_dir.iterdir() if p.is_file()]
    downloads = import_dir / "downloads"
    if downloads.exists():
        files.extend([p for p in downloads.iterdir() if p.is_file()])
    artifacts = _collect_files(files, phase_key, import_id, "p6_diagram_pack")
    s = summary.get("summary") or {}
    return {
        "phase_key": phase_key,
        "label": _phase_label(phase_key),
        "status": summary.get("status") or "imported",
        "import_id": import_id,
        "core_nodes": s.get("node_count", 0),
        "core_relationships": s.get("edge_count", 0),
        "gap_entries": s.get("missing_model_count", 0),
        "skipped_relationships": 0,
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "summary": summary,
    }


def _p7_artifacts() -> dict[str, Any]:
    phase_key = "P7"
    summaries = sorted(_p7_root().glob(f"*/{P7_SUMMARY_FILENAME}"), key=lambda x: x.stat().st_mtime, reverse=True)
    if not summaries:
        return _empty_phase_artifacts(phase_key)
    summary_path = summaries[0]
    import_dir = summary_path.parent
    summary = _read_json_if_exists(summary_path)
    import_id = summary.get("authority_import_id") or import_dir.name
    files = [p for p in import_dir.iterdir() if p.is_file()]
    extracted = import_dir / "extracted"
    for rel in ["VALIDATION_REPORT.md", "data/graph_nodes.json", "data/graph_edges.json", "data/yfiles_views.json", "data/approval_codegen_patch_units.json"]:
        p = extracted / rel
        if p.exists():
            files.append(p)
    exports = _p7_find_dir(extracted, "exports") if extracted.exists() else None
    if exports:
        files.extend([p for p in exports.glob("yfiles_*.json") if p.is_file()])
    artifacts = _collect_files(files, phase_key, import_id, "p7_authority_pack")
    s = summary.get("summary") or {}
    return {
        "phase_key": phase_key,
        "label": _phase_label(phase_key),
        "status": summary.get("status") or "imported",
        "import_id": import_id,
        "core_nodes": s.get("node_count", 0),
        "core_relationships": s.get("edge_count", 0),
        "gap_entries": s.get("codegen_ready_count", 0),
        "skipped_relationships": 0,
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "summary": summary,
    }

# ---------------------------------------------------------------------------
# P2-5180-1: Phase Artifact Index API
# ---------------------------------------------------------------------------
# Purpose:
# - Make command-generated artifacts visible to the console without changing
#   the existing P1/P2 generation flow.
# - Present P1..P5 with the same coarse-grained status shape.
# - Keep this phase read/download only. It does not repair, apply, generate, or
#   mutate phase data.

PHASE_ARTIFACT_ALLOWED_ROOTS = [ARTIFACT_ROOT, GENERATED_ADDONS_ROOT]


def _artifact_mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except Exception:
        return None


def _artifact_kind(path: Path) -> str:
    name = path.name.lower()
    if name.endswith('.zip'):
        return 'zip'
    if name.endswith('.md'):
        return 'markdown'
    if name.endswith('.json'):
        if 'summary' in name:
            return 'summary_json'
        if 'gap' in name:
            return 'fg_gap_json'
        if 'payload' in name:
            return 'payload_json'
        if 'result' in name:
            return 'result_json'
        return 'json'
    if name.endswith('.txt'):
        return 'text'
    return 'file'


def _phase_artifact_id(phase_key: str, import_id: str | None, path: Path) -> str:
    raw = f"{phase_key}|{import_id or ''}|{path.resolve()}"
    digest = hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]
    return f"{digest}__{path.name}"


def _is_under_allowed_artifact_root(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except Exception:
        return False
    for root in PHASE_ARTIFACT_ALLOWED_ROOTS:
        try:
            if str(resolved).startswith(str(root.resolve())):
                return True
        except Exception:
            continue
    return False


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _artifact_entry(phase_key: str, import_id: str | None, path: Path, source: str) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file() or not _is_under_allowed_artifact_root(path):
        return None
    artifact_id = _phase_artifact_id(phase_key, import_id, path)
    try:
        size = path.stat().st_size
    except Exception:
        size = None
    return {
        'artifact_id': artifact_id,
        'phase_key': phase_key,
        'import_id': import_id,
        'name': path.name,
        'kind': _artifact_kind(path),
        'source': source,
        'path': str(path),
        'size_bytes': size,
        'modified_at': _artifact_mtime(path),
        'download_url': f"/phase-artifacts/{phase_key}/downloads/{artifact_id}",
    }


def _collect_files(paths: list[Path], phase_key: str, import_id: str | None, source: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        entry = _artifact_entry(phase_key, import_id, path, source)
        if not entry:
            continue
        if entry['artifact_id'] in seen:
            continue
        seen.add(entry['artifact_id'])
        items.append(entry)
    return items


def _latest_summary_path(root: Path) -> Path | None:
    if not root.exists():
        return None
    summaries = [p for p in root.glob('*/import_summary.json') if p.is_file()]
    if not summaries:
        return None
    return sorted(summaries, key=lambda x: x.stat().st_mtime, reverse=True)[0]


def _p1_artifacts() -> dict[str, Any]:
    phase_key = 'P1'
    root = ARTIFACT_ROOT / 'imports'
    summary_path = _latest_summary_path(root)
    if not summary_path:
        return {
            'phase_key': phase_key,
            'label': _phase_label(phase_key),
            'status': 'not_imported',
            'import_id': None,
            'core_nodes': 0,
            'core_relationships': 0,
            'gap_entries': 0,
            'skipped_relationships': 0,
            'artifact_count': 0,
            'artifacts': [],
        }
    import_dir = summary_path.parent
    summary = _read_json_if_exists(summary_path)
    import_id = summary.get('import_id') or import_dir.name
    files = [p for p in import_dir.iterdir() if p.is_file()]
    addon_result = _read_json_if_exists(import_dir / 'odoo_addon_result.json')
    zip_path = Path(addon_result.get('zip_path') or '') if addon_result else Path('')
    if zip_path and zip_path.exists():
        files.append(zip_path)
    artifacts = _collect_files(files, phase_key, import_id, 'p1_import')
    counts = summary.get('count_summary') or {}
    return {
        'phase_key': phase_key,
        'label': _phase_label(phase_key),
        'status': summary.get('status') or 'imported',
        'import_id': import_id,
        'core_nodes': counts.get('nodes', 0),
        'core_relationships': counts.get('relationships', 0),
        'gap_entries': 0,
        'skipped_relationships': 0,
        'artifact_count': len(artifacts),
        'artifacts': artifacts,
        'summary': summary,
    }


def _p2_artifacts() -> dict[str, Any]:
    phase_key = 'P2'
    summary_path = _latest_summary_path(_p1p2_root())
    if not summary_path:
        return {
            'phase_key': phase_key,
            'label': _phase_label(phase_key),
            'status': 'not_imported',
            'import_id': None,
            'core_nodes': 0,
            'core_relationships': 0,
            'gap_entries': 0,
            'skipped_relationships': 0,
            'artifact_count': 0,
            'artifacts': [],
        }
    import_dir = summary_path.parent
    summary = _read_json_if_exists(summary_path)
    import_id = summary.get('import_id') or import_dir.name
    files = [p for p in import_dir.iterdir() if p.is_file()]
    overlay_result = _read_json_if_exists(import_dir / 'odoo_overlay_data_result.json')
    zip_path = Path(overlay_result.get('zip_path') or '') if overlay_result else Path('')
    overlay_dir = Path(overlay_result.get('overlay_data_dir') or '') if overlay_result else Path('')
    if zip_path and zip_path.exists():
        files.append(zip_path)
    if overlay_dir and overlay_dir.exists():
        files.extend([p for p in overlay_dir.iterdir() if p.is_file()])
    artifacts = _collect_files(files, phase_key, import_id, 'p1p2_combined')
    p1p2 = summary.get('p1p2_summary') or {}
    counts = summary.get('count_summary') or {}
    status = summary.get('status') or 'imported'
    if overlay_result:
        status = overlay_result.get('status') or status
    return {
        'phase_key': phase_key,
        'label': _phase_label(phase_key),
        'status': status,
        'import_id': import_id,
        'core_nodes': p1p2.get('core_nodes', counts.get('nodes', 0)),
        'core_relationships': p1p2.get('core_relationships', counts.get('relationships', 0)),
        'gap_entries': p1p2.get('gap_entries', 0),
        'skipped_relationships': p1p2.get('skipped_relationships', 0),
        'artifact_count': len(artifacts),
        'artifacts': artifacts,
        'summary': summary,
        'overlay_data_result': overlay_result or None,
    }


def _p3_artifacts() -> dict[str, Any]:
    phase_key = 'P3'
    summary_path = _latest_summary_path(_p3_root())
    if not summary_path:
        return {
            'phase_key': phase_key,
            'label': _phase_label(phase_key),
            'status': 'not_imported',
            'import_id': None,
            'core_nodes': 0,
            'core_relationships': 0,
            'gap_entries': 0,
            'skipped_relationships': 0,
            'artifact_count': 0,
            'artifacts': [],
        }
    import_dir = summary_path.parent
    summary = _read_json_if_exists(summary_path)
    import_id = summary.get('import_id') or import_dir.name
    files = [p for p in import_dir.iterdir() if p.is_file()]
    artifacts = _collect_files(files, phase_key, import_id, 'p3_neo4j_first')
    counts = summary.get('count_summary') or {}
    pre = summary.get('p3_preclassification_summary') or {}
    return {
        'phase_key': phase_key,
        'label': _phase_label(phase_key),
        'status': summary.get('status') or 'imported',
        'import_id': import_id,
        'core_nodes': counts.get('nodes', 0),
        'core_relationships': counts.get('relationships', 0),
        'gap_entries': pre.get('skipped_item_count', 0),
        'skipped_relationships': pre.get('unresolved_reference_count', 0),
        'artifact_count': len(artifacts),
        'artifacts': artifacts,
        'summary': summary,
        'p3_preclassification_summary': pre,
    }


def _empty_phase_artifacts(phase_key: str) -> dict[str, Any]:
    return {
        'phase_key': phase_key,
        'label': _phase_label(phase_key),
        'status': 'not_imported',
        'import_id': None,
        'core_nodes': 0,
        'core_relationships': 0,
        'gap_entries': 0,
        'skipped_relationships': 0,
        'artifact_count': 0,
        'artifacts': [],
    }


def _phase_artifact_index() -> dict[str, Any]:
    phases: list[dict[str, Any]] = []
    by_phase: dict[str, dict[str, Any]] = {}
    p1 = _p1_artifacts()
    p2 = _p2_artifacts()
    p3 = _p3_artifacts()
    by_phase['P1'] = p1
    by_phase['P2'] = p2
    by_phase['P3'] = p3
    by_phase['P6'] = _p6_artifacts()
    by_phase['P7'] = _p7_artifacts()
    for phase_key in DEFAULT_PHASES:
        phases.append(by_phase.get(phase_key) or _empty_phase_artifacts(phase_key))
    totals = {
        'phases': len(phases),
        'imported_phases': sum(1 for p in phases if p.get('status') != 'not_imported'),
        'artifact_count': sum(int(p.get('artifact_count') or 0) for p in phases),
        'gap_entries': sum(int(p.get('gap_entries') or 0) for p in phases),
        'skipped_relationships': sum(int(p.get('skipped_relationships') or 0) for p in phases),
    }
    return {
        'generated_at': _now_iso(),
        'purpose': 'Read-only phase artifact index for P1..P7 console display and downloads.',
        'phases': phases,
        'totals': totals,
        'links': _links(),
        'notes': [
            'P2 currently indexes P1/P2 GAP-aware combined artifacts.',
            'P3 indexes only P3 Neo4j-first import artifacts and does not affect P1/P2/P4/P5.',
            'F&G GAP artifacts are report-only and excluded from Odoo auto-generation.',
            'P6 indexes imported Diagram Packs and exposes dynamic download actions for P3 ER diagrams.',
            'P7 indexes imported Authority Visualization Packs and exposes yFiles view downloads for organization, approval, visibility, and app/model responsibility.',
            'This endpoint is read/download only and does not mutate phase data.',
        ],
    }


@app.get('/phase-artifacts')
def list_phase_artifacts() -> dict[str, Any]:
    return _phase_artifact_index()


@app.get('/phase-artifacts/{phase_key}')
def read_phase_artifacts(phase_key: str) -> dict[str, Any]:
    key = phase_key.upper()
    index = _phase_artifact_index()
    for phase in index.get('phases', []):
        if str(phase.get('phase_key')).upper() == key:
            return phase
    raise HTTPException(status_code=404, detail=f'Unknown phase: {phase_key}')


@app.get('/phase-artifacts/{phase_key}/downloads')
def list_phase_downloads(phase_key: str) -> dict[str, Any]:
    phase = read_phase_artifacts(phase_key)
    return {
        'phase_key': phase.get('phase_key'),
        'status': phase.get('status'),
        'import_id': phase.get('import_id'),
        'artifact_count': phase.get('artifact_count'),
        'items': phase.get('artifacts') or [],
    }


@app.get('/phase-artifacts/{phase_key}/downloads/{artifact_id}')
def download_phase_artifact(phase_key: str, artifact_id: str) -> FileResponse:
    phase = read_phase_artifacts(phase_key)
    for artifact in phase.get('artifacts') or []:
        if artifact.get('artifact_id') == artifact_id:
            path = Path(artifact.get('path') or '')
            if not path.exists() or not _is_under_allowed_artifact_root(path):
                raise HTTPException(status_code=404, detail='Artifact file not found')
            return FileResponse(str(path), filename=path.name, media_type='application/octet-stream')
    raise HTTPException(status_code=404, detail=f'Artifact not found: {artifact_id}')

# ---------------------------------------------------------------------------
# P2-5180-1A: Overlay Data Source Inspector API
# ---------------------------------------------------------------------------
# Purpose:
# - Read generated P1/P2 overlay data source artifacts.
# - Summarize addon-mapping candidates for ChatGPT-side semantic processing.
# - Do not generate prompt packs, import ChatGPT results, repair data, apply Neo4j,
#   or produce Odoo addons in this step.

INSPECTOR_CANDIDATE_LABELS = {
    "P2StandardConfiguration": "standard_configurations",
    "DomainValueConfigLink": "domain_value_config_links",
    "LaterPhaseConcept": "later_phase_concepts",
    "P2ExternalSupportingAnchor": "supporting_anchors",
    "OdooStandardModel": "odoo_standard_models",
    "Bundle": "bundle_anchors",
    "Scenario": "scenario_anchors",
    "App": "apps",
}


def _compact_props(props: dict[str, Any] | None) -> dict[str, Any]:
    props = dict(props or {})
    preferred = [
        "app_key",
        "app_keys",
        "used_in_apps",
        "configuration_key",
        "domain_value_key",
        "concept_key",
        "area_key",
        "model",
        "module",
        "name",
        "name_ja",
        "title",
        "label",
        "description",
        "description_ja",
        "configuration_type",
        "domain_value_type",
        "handling_in_p2",
        "expected_later_phase",
        "phase",
        "odoo_module",
        "odoo_models",
        "used_in_bundles",
        "used_in_scenarios",
        "treatment",
        "source",
        "alignment_status",
        "alignment_match_type",
        "alignment_confidence",
        "alignment_reason",
    ]
    compact: dict[str, Any] = {}
    for key in preferred:
        if key in props:
            compact[key] = props[key]
    # Keep unknown scalar-ish fields up to a small cap for ChatGPT mapping context.
    for key, value in props.items():
        if key in compact:
            continue
        if len(compact) >= 24:
            break
        if isinstance(value, (str, int, float, bool)) or value is None:
            compact[key] = value
        elif isinstance(value, list) and len(value) <= 12 and all(isinstance(x, (str, int, float, bool)) for x in value):
            compact[key] = value
    return compact


def _node_display_name(node: dict[str, Any]) -> str:
    props = node.get("properties") or {}
    for key in ("name_ja", "name", "title", "label", "configuration_key", "domain_value_key", "concept_key", "model", "app_key"):
        value = props.get(key)
        if value:
            return str(value)
    return str(_node_key(node) or "")


def _node_app_keys(node: dict[str, Any]) -> list[str]:
    props = node.get("properties") or {}
    values: list[str] = []
    for key in ("app_key", "app"):
        value = props.get(key)
        if isinstance(value, str) and value:
            values.append(value)
    for key in ("app_keys", "used_in_apps"):
        value = props.get(key)
        if isinstance(value, list):
            values.extend([str(x) for x in value if x])
        elif isinstance(value, str) and value:
            values.append(value)
    # Derive from node_key, e.g. domain_value_config_link::sales::...
    key = str(_node_key(node) or "")
    parts = key.split("::")
    if len(parts) >= 3 and parts[1] in DEFAULT_APPS:
        values.append(parts[1])
    if key.startswith("app::") and len(parts) >= 2:
        values.append(parts[1])
    return sorted({v for v in values if v})


def _primary_candidate_kind(node: dict[str, Any]) -> str:
    labels = _labels(node)
    for label, kind in INSPECTOR_CANDIDATE_LABELS.items():
        if label in labels:
            return kind
    return "other_nodes"


def _compact_node_for_inspector(node: dict[str, Any], rel_index: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    key = _node_key(node)
    labels = _labels(node)
    rels = (rel_index or {}).get(str(key), {}) if key else {}
    return {
        "node_key": key,
        "labels": labels,
        "candidate_kind": _primary_candidate_kind(node),
        "display_name": _node_display_name(node),
        "app_keys": _node_app_keys(node),
        "properties": _compact_props(node.get("properties") or {}),
        "relationship_counts": rels,
    }


def _compact_relationship_for_inspector(rel: dict[str, Any]) -> dict[str, Any]:
    return {
        "relationship_key": rel.get("relationship_key") or rel.get("id"),
        "relationship_type": rel.get("relationship_type") or rel.get("type"),
        "from_node_key": _from_key(rel),
        "to_node_key": _to_key(rel),
        "properties": _compact_props(rel.get("properties") or {}),
    }


def _relationship_index_for_nodes(rels: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    index: dict[str, dict[str, int]] = {}
    for rel in rels:
        rel_type = str(rel.get("relationship_type") or rel.get("type") or "UNKNOWN")
        for key in (_from_key(rel), _to_key(rel)):
            if not key:
                continue
            bucket = index.setdefault(str(key), {})
            bucket[rel_type] = bucket.get(rel_type, 0) + 1
    return index


def _find_overlay_data_paths(import_id: str) -> dict[str, Path | None]:
    import_dir = _p1p2_import_dir(import_id)
    overlay_result_path = import_dir / "odoo_overlay_data_result.json"
    overlay_result = _read_json_if_exists(overlay_result_path)
    overlay_dir = Path(overlay_result.get("overlay_data_dir") or "") if overlay_result else Path("")
    paths: dict[str, Path | None] = {
        "import_dir": import_dir if import_dir.exists() else None,
        "overlay_result": overlay_result_path if overlay_result_path.exists() else None,
        "overlay_dir": overlay_dir if overlay_dir.exists() else None,
        "overlay_summary": None,
        "core_payload": None,
        "fg_gap_report_json": None,
        "fg_gap_report_md": None,
    }
    # Prefer generated overlay data source files when they exist.
    if overlay_dir and overlay_dir.exists():
        for key, filename in {
            "overlay_summary": "overlay_summary.json",
            "core_payload": "odoo_overlay_core_payload.json",
            "fg_gap_report_json": "fg_gap_report.json",
            "fg_gap_report_md": "fg_gap_report.md",
        }.items():
            path = overlay_dir / filename
            if path.exists():
                paths[key] = path
    # Fallback to import artifacts if overlay data generation has not run or
    # generated-addons volume is not mounted in the current environment.
    fallbacks = {
        "core_payload": import_dir / "P1P2_CORE_PAYLOAD.json",
        "fg_gap_report_json": import_dir / "P1P2_FG_GAP_PAYLOAD.json",
        "fg_gap_report_md": import_dir / "P1P2_FG_GAP_REPORT.md",
        "overlay_summary": import_dir / "import_summary.json",
    }
    for key, path in fallbacks.items():
        if paths.get(key) is None and path.exists():
            paths[key] = path
    return paths


def _load_overlay_core_payload_for_inspector(import_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Path | None]]:
    paths = _find_overlay_data_paths(import_id)
    core_path = paths.get("core_payload")
    if not core_path or not core_path.exists():
        raise HTTPException(status_code=404, detail="Overlay core payload not found. Generate overlay data or import P1/P2 combined pack first.")
    payload = _read_json_if_exists(core_path)
    try:
        nodes, rels = _extract_payload(payload)
    except HTTPException:
        neo = payload.get("neo4j_import_payload") or {}
        nodes = neo.get("nodes") or []
        rels = neo.get("relationships") or []
    return payload, nodes, rels, paths


def _load_gap_payload_for_inspector(paths: dict[str, Path | None]) -> dict[str, Any]:
    path = paths.get("fg_gap_report_json")
    if path and path.exists():
        data = _read_json_if_exists(path)
        if data:
            return data
    return {"gap_entries": [], "skipped_relationships": [], "gap_nodes": [], "gap_relationships": []}


def _count_by_label(nodes: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in nodes:
        for label in _labels(node):
            counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: (-x[1], x[0])))


def _count_by_relationship_type(rels: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rel in rels:
        typ = str(rel.get("relationship_type") or rel.get("type") or "UNKNOWN")
        counts[typ] = counts.get(typ, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: (-x[1], x[0])))


def _inspector_app_breakdown(candidate_material: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, int]]:
    apps = {app: {} for app in DEFAULT_APPS}
    apps["_unknown"] = {}
    for kind, items in candidate_material.items():
        for item in items:
            item_apps = item.get("app_keys") or []
            if not item_apps:
                item_apps = ["_unknown"]
            for app_key in item_apps:
                bucket = apps.setdefault(str(app_key), {})
                bucket[kind] = bucket.get(kind, 0) + 1
    return {k: v for k, v in apps.items() if v}


def _source_file_entry(path: Path | None, purpose: str) -> dict[str, Any]:
    if not path:
        return {"purpose": purpose, "found": False, "path": None, "size_bytes": None, "modified_at": None}
    return {
        "purpose": purpose,
        "found": path.exists(),
        "path": str(path) if path else None,
        "size_bytes": path.stat().st_size if path.exists() else None,
        "modified_at": _artifact_mtime(path) if path.exists() else None,
    }


def _build_overlay_inspector(import_id: str) -> dict[str, Any]:
    payload, nodes, rels, paths = _load_overlay_core_payload_for_inspector(import_id)
    gap_payload = _load_gap_payload_for_inspector(paths)
    rel_index = _relationship_index_for_nodes(rels)
    label_counts = _count_by_label(nodes)
    rel_counts = _count_by_relationship_type(rels)

    candidate_material: dict[str, list[dict[str, Any]]] = {
        "standard_configurations": [],
        "domain_value_config_links": [],
        "later_phase_concepts": [],
        "supporting_anchors": [],
        "odoo_standard_models": [],
        "bundle_anchors": [],
        "scenario_anchors": [],
        "apps": [],
    }
    other_nodes: list[dict[str, Any]] = []
    for node in nodes:
        compact = _compact_node_for_inspector(node, rel_index)
        kind = compact.get("candidate_kind") or "other_nodes"
        if kind in candidate_material:
            candidate_material[kind].append(compact)
        else:
            other_nodes.append(compact)

    gap_entries = gap_payload.get("gap_entries") or []
    skipped_relationships = gap_payload.get("skipped_relationships") or []
    fg_gap_items = []
    for entry in gap_entries:
        fg_gap_items.append({
            "gap_key": entry.get("gap_key") or entry.get("source_node_key"),
            "source_node_key": entry.get("source_node_key"),
            "gap_type": entry.get("gap_type") or entry.get("type"),
            "status": entry.get("gap_status") or entry.get("status"),
            "odoo_development_scope": entry.get("odoo_development_scope"),
            "fg_report_scope": entry.get("fg_report_scope"),
            "skip_reason_ja": entry.get("skip_reason_ja") or entry.get("reason"),
            "probable_meaning_ja": entry.get("probable_meaning_ja") or entry.get("probable meaning"),
            "customer_report_message_ja": entry.get("customer_report_message_ja") or entry.get("report message"),
            "candidate_p1_targets": entry.get("candidate_p1_targets") or [],
            "affected_relationships": entry.get("affected_relationships") or [],
            "auto_resolution_allowed": bool(entry.get("auto_resolution_allowed", False)),
            "requires_human_decision": bool(entry.get("requires_human_decision", True)),
        })

    candidate_counts = {kind: len(items) for kind, items in candidate_material.items()}
    candidate_counts["fg_gap_items"] = len(fg_gap_items)
    candidate_counts["skipped_relationships"] = len(skipped_relationships)
    if other_nodes:
        candidate_counts["other_nodes"] = len(other_nodes)

    source_files = [
        _source_file_entry(paths.get("overlay_summary"), "overlay_summary"),
        _source_file_entry(paths.get("core_payload"), "odoo_overlay_core_payload"),
        _source_file_entry(paths.get("fg_gap_report_json"), "fg_gap_report_json"),
        _source_file_entry(paths.get("fg_gap_report_md"), "fg_gap_report_md"),
        _source_file_entry(paths.get("overlay_result"), "overlay_generation_result"),
    ]

    overlay_summary = _read_json_if_exists(paths.get("overlay_summary") or Path("")) if paths.get("overlay_summary") else {}
    summary_counts = overlay_summary.get("record_counts") or overlay_summary

    return {
        "import_id": import_id,
        "status": "overlay_inspected",
        "generated_at": _now_iso(),
        "scope": "P2-5180-1A Overlay Inspector API",
        "read_only": True,
        "source_files": source_files,
        "summary": {
            "core_nodes": len(nodes),
            "core_relationships": len(rels),
            "gap_entries_excluded": len(gap_entries),
            "skipped_relationships_excluded": len(skipped_relationships),
            "overlay_summary_core_nodes": summary_counts.get("core_nodes"),
            "overlay_summary_core_relationships": summary_counts.get("core_relationships"),
        },
        "label_counts": label_counts,
        "relationship_type_counts": rel_counts,
        "candidate_counts": candidate_counts,
        "app_breakdown": _inspector_app_breakdown({**candidate_material, "fg_gap_items": fg_gap_items}),
        "candidate_material": {
            **candidate_material,
            "fg_gap_items": fg_gap_items,
        },
        "relationship_material": {
            "type_counts": rel_counts,
            "samples": [_compact_relationship_for_inspector(rel) for rel in rels[:80]],
        },
        "other_nodes_sample": other_nodes[:80],
        "ready_for_prompt_pack_export": True,
        "recommended_prompt_mode": "all_at_once",
        "notes": [
            "This endpoint only inspects generated overlay data source artifacts.",
            "It does not decide Odoo model mapping, generate prompt packs, import ChatGPT results, or generate addons.",
            "Meaningful mapping decisions should be handled on the ChatGPT side and imported as an approved/candidate addon input in a later step.",
            "F&G GAP items remain report-only and excluded from Odoo auto-generation.",
        ],
        "next_step": "P2-5180-1B: export ChatGPT addon mapping prompt pack from this inspector material.",
    }


@app.get("/p1p2/imports/{import_id}/overlay-inspector")
def inspect_p1p2_overlay_data_source(import_id: str) -> dict[str, Any]:
    return _build_overlay_inspector(import_id)


@app.get("/p1p2/imports/{import_id}/overlay-inspector/summary")
def inspect_p1p2_overlay_data_source_summary(import_id: str) -> dict[str, Any]:
    inspector = _build_overlay_inspector(import_id)
    return {
        "import_id": inspector["import_id"],
        "status": inspector["status"],
        "generated_at": inspector["generated_at"],
        "read_only": True,
        "source_files": inspector["source_files"],
        "summary": inspector["summary"],
        "candidate_counts": inspector["candidate_counts"],
        "app_breakdown": inspector["app_breakdown"],
        "ready_for_prompt_pack_export": inspector["ready_for_prompt_pack_export"],
        "recommended_prompt_mode": inspector["recommended_prompt_mode"],
        "next_step": inspector["next_step"],
    }

# ---------------------------------------------------------------------------
# P2-5180-1B: ChatGPT Addon Mapping Prompt Pack Export
# ---------------------------------------------------------------------------
# Purpose:
# - Use the read-only overlay inspector material from P2-5180-1A.
# - Export a compact prompt pack ZIP for ChatGPT-side semantic mapping.
# - Keep all-at-once as the default route while also emitting app-split material.
# - Do not perform semantic mapping, import ChatGPT results, or generate addons here.

PROMPT_PACK_KIND = "p1p2_odoo_addon_mapping_prompt_pack"


def _prompt_pack_dir(import_id: str) -> Path:
    return GENERATED_ADDONS_ROOT / f"{PROMPT_PACK_KIND}_{import_id}"


def _prompt_pack_zip_path(import_id: str) -> Path:
    return GENERATED_ADDONS_ROOT / f"{PROMPT_PACK_KIND}_{import_id}.zip"


def _safe_json_dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _prompt_pack_expected_output_schema() -> dict[str, Any]:
    return {
        "schema_name": "p1p2_odoo_addon_input_candidate",
        "schema_version": "0.1.0",
        "purpose": "Candidate input for the later lightweight Odoo addon generator. Semantic mapping is performed by ChatGPT and reviewed by the user before addon generation.",
        "required_top_level_keys": [
            "addon_name",
            "display_name",
            "source_import_id",
            "status",
            "models",
            "gap_items",
            "summary",
        ],
        "allowed_record_statuses": [
            "approved_candidate",
            "needs_review",
            "excluded",
            "report_only",
        ],
        "recommended_models": [
            {
                "model": "fg.p1p2.standard.configuration",
                "display_name": "P1/P2 Standard Configuration",
                "record_source": "P2StandardConfiguration nodes",
            },
            {
                "model": "fg.p1p2.domain.value.config.link",
                "display_name": "Domain Value Config Link",
                "record_source": "DomainValueConfigLink nodes",
            },
            {
                "model": "fg.p1p2.later.phase.concept",
                "display_name": "Later Phase Concept",
                "record_source": "LaterPhaseConcept nodes",
            },
            {
                "model": "fg.p1p2.supporting.anchor",
                "display_name": "Supporting Anchor",
                "record_source": "P2ExternalSupportingAnchor nodes",
            },
            {
                "model": "fg.p1p2.gap.item",
                "display_name": "F&G GAP Item",
                "record_source": "F&G GAP report-only items",
            },
        ],
        "example": {
            "addon_name": "fg_p1p2_overlay",
            "display_name": "F&G P1/P2 Overlay",
            "source_import_id": "<IMPORT_ID>",
            "status": "candidate",
            "models": [
                {
                    "model": "fg.p1p2.standard.configuration",
                    "display_name": "P1/P2 Standard Configuration",
                    "status": "approved_candidate",
                    "records": [
                        {
                            "source_node_key": "p2_config::sales::example",
                            "name": "Example configuration",
                            "app_key": "sales",
                            "related_model": "sale.order",
                            "related_bundle": "bundle::example",
                            "related_scenario": "scenario::example",
                            "description": "Human-readable summary.",
                            "status": "approved_candidate",
                            "mapping_reason": "Display-only overlay record from P2 standard configuration node."
                        }
                    ]
                }
            ],
            "gap_items": [
                {
                    "source_key": "bundle::example_unresolved",
                    "gap_type": "ambiguous_reference",
                    "reason": "処理方法または接続先が不明確なため、Odoo自動反映対象から除外。",
                    "probable_meaning": "Likely business area, not confirmed.",
                    "customer_report_message": "検知しましたが、処理不明のため今回対象外。",
                    "excluded_from_auto_generation": True,
                    "status": "report_only"
                }
            ],
            "summary": {
                "source_core_nodes": 0,
                "source_core_relationships": 0,
                "gap_items": 0,
                "notes": []
            }
        }
    }


def _prompt_all_apps_markdown(import_id: str) -> str:
    return f"""# P1/P2 Odoo Addon Mapping Prompt - All Apps

Use this prompt pack to create **one** candidate JSON for a lightweight Odoo overlay addon.

## Inputs

Read these files in this prompt pack:

1. `01_overlay_source_summary.json`
2. `02_addon_mapping_material_compact.json`
3. `03_fg_gap_report.json`
4. `04_expected_output_schema.json`
5. `apps/*.json` only as supplemental app-split material if needed

## Source import

- source_import_id: `{import_id}`

## Required output

Create these files:

1. `p1p2_odoo_addon_input_candidate.json`
2. `p1p2_odoo_addon_mapping_report.md`

## Important rules

- Do **not** solve or remap F&G GAP items.
- Keep F&G GAP items as report-only records.
- Do **not** directly write to Odoo standard business models like `sale.order`, `stock.picking`, or `account.move`.
- This addon is an overlay viewer / F&G inspection addon, not a business execution addon.
- Use source node keys whenever possible.
- Each record must have a `status` value: `approved_candidate`, `needs_review`, `excluded`, or `report_only`.
- If uncertain, use `needs_review` or `report_only`; do not force a decision.

## Recommended Odoo overlay models

- `fg.p1p2.standard.configuration`
- `fg.p1p2.domain.value.config.link`
- `fg.p1p2.later.phase.concept`
- `fg.p1p2.supporting.anchor`
- `fg.p1p2.gap.item`

## Output guidance

The final JSON should be a single all-apps candidate. Do not split the final answer by app unless explicitly requested.

The report should summarize:

- record counts per proposed Odoo model
- excluded/report-only GAP count
- any records that still need review
- why this is safe as an Odoo overlay addon
"""


def _prompt_merge_markdown(import_id: str) -> str:
    return f"""# Optional Merge Prompt for App-by-App Runs

Use this only if the mapping was processed app-by-app.

Input files should be app-level candidate JSON files. Merge them into one:

- `p1p2_odoo_addon_input_candidate.json`
- `p1p2_odoo_addon_mapping_report.md`

Rules:

- Preserve all source node keys.
- Deduplicate records by `(model, source_node_key)`.
- Keep GAP items as `report_only`.
- Do not promote GAP items into auto-generation records.
- source_import_id must remain `{import_id}`.
"""


def _start_here_prompt_pack(import_id: str) -> str:
    return f"""# START HERE - P1/P2 Odoo Addon Mapping Prompt Pack

This pack was generated from the P1/P2 overlay data source.

## Recommended mode

Use **all-at-once** mode for this P1/P2 dataset.

The data size is small enough for a single ChatGPT run after inspector compaction.

## Main prompt

Open and follow:

- `05_mapping_prompt_all_apps.md`

## Expected output

Return a ZIP containing:

- `p1p2_odoo_addon_input_candidate.json`
- `p1p2_odoo_addon_mapping_report.md`
- optional `README.md`

This output will later be imported back into the development system.

## Source import id

`{import_id}`
"""


def _app_material_from_inspector(inspector: dict[str, Any], app_key: str) -> dict[str, Any]:
    material = inspector.get("candidate_material") or {}
    app_material: dict[str, Any] = {
        "app_key": app_key,
        "source_import_id": inspector.get("import_id"),
        "recommended_prompt_mode": "app_split_optional",
        "candidate_material": {},
        "notes": [
            "This app-split material is supplemental. The default route is all-at-once mapping.",
            "Do not use app-split output as final unless a later merge step is performed.",
        ],
    }
    for kind, items in material.items():
        if not isinstance(items, list):
            continue
        selected = []
        for item in items:
            apps = item.get("app_keys") or []
            if app_key == "_unknown":
                if not apps:
                    selected.append(item)
            elif app_key in apps:
                selected.append(item)
        app_material["candidate_material"][kind] = selected
    app_material["candidate_counts"] = {
        kind: len(items)
        for kind, items in app_material["candidate_material"].items()
        if isinstance(items, list)
    }
    return app_material


def _build_addon_mapping_prompt_pack(import_id: str) -> dict[str, Any]:
    inspector = _build_overlay_inspector(import_id)
    out_dir = _prompt_pack_dir(import_id)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    apps_dir = out_dir / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "import_id": import_id,
        "generated_at": _now_iso(),
        "prompt_pack_kind": PROMPT_PACK_KIND,
        "recommended_prompt_mode": "all_at_once",
        "source_summary": inspector.get("summary"),
        "candidate_counts": inspector.get("candidate_counts"),
        "app_breakdown": inspector.get("app_breakdown"),
        "source_files": inspector.get("source_files"),
        "notes": [
            "This prompt pack is for ChatGPT-side semantic mapping into an Odoo overlay addon input candidate.",
            "It does not change source data and does not generate an Odoo addon.",
            "F&G GAP items must remain report-only unless the user explicitly approves otherwise in a separate process.",
        ],
    }

    compact_material = {
        "import_id": import_id,
        "generated_at": _now_iso(),
        "recommended_prompt_mode": "all_at_once",
        "summary": inspector.get("summary"),
        "candidate_counts": inspector.get("candidate_counts"),
        "candidate_material": inspector.get("candidate_material"),
        "relationship_material": inspector.get("relationship_material"),
        "app_breakdown": inspector.get("app_breakdown"),
        "instructions": {
            "meaning_decisions_on_chatgpt_side": True,
            "do_not_auto_solve_gaps": True,
            "final_output_single_json": "p1p2_odoo_addon_input_candidate.json",
        }
    }

    fg_gap_payload = {
        "import_id": import_id,
        "fg_gap_items": (inspector.get("candidate_material") or {}).get("fg_gap_items") or [],
        "policy": "report_only_excluded_from_odoo_auto_generation",
    }

    _safe_json_dump(out_dir / "01_overlay_source_summary.json", summary)
    _safe_json_dump(out_dir / "02_addon_mapping_material_compact.json", compact_material)
    _safe_json_dump(out_dir / "03_fg_gap_report.json", fg_gap_payload)
    _safe_json_dump(out_dir / "04_expected_output_schema.json", _prompt_pack_expected_output_schema())
    (out_dir / "05_mapping_prompt_all_apps.md").write_text(_prompt_all_apps_markdown(import_id), encoding="utf-8")
    (out_dir / "06_optional_merge_prompt.md").write_text(_prompt_merge_markdown(import_id), encoding="utf-8")
    (out_dir / "START_HERE.md").write_text(_start_here_prompt_pack(import_id), encoding="utf-8")
    (out_dir / "README.md").write_text(
        "# P1/P2 Odoo Addon Mapping Prompt Pack\n\n"
        "Use START_HERE.md first. The recommended route is all-at-once. "
        "App-split files are provided only as supplemental material.\n",
        encoding="utf-8",
    )

    app_keys = sorted(set(DEFAULT_APPS) | set((inspector.get("app_breakdown") or {}).keys()))
    for app_key in app_keys:
        safe_app = re.sub(r"[^a-zA-Z0-9_\-]+", "_", str(app_key))
        _safe_json_dump(apps_dir / f"{safe_app}.json", _app_material_from_inspector(inspector, app_key))

    zip_path = _prompt_pack_zip_path(import_id)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(out_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(out_dir))

    result = {
        "import_id": import_id,
        "status": "addon_mapping_prompt_pack_generated",
        "generated_at": _now_iso(),
        "prompt_pack_dir": str(out_dir),
        "zip_path": str(zip_path),
        "download_url": f"/p1p2/imports/{import_id}/addon-mapping-prompt-pack/download",
        "recommended_prompt_mode": "all_at_once",
        "file_count": len([p for p in out_dir.rglob("*") if p.is_file()]),
        "candidate_counts": inspector.get("candidate_counts"),
        "next_step": "Download the prompt pack and process it in ChatGPT to produce p1p2_odoo_addon_input_candidate.json.",
    }
    result_path = _p1p2_import_dir(import_id) / "addon_mapping_prompt_pack_result.json"
    _safe_json_dump(result_path, result)
    return result


@app.post("/p1p2/imports/{import_id}/export-addon-mapping-prompt-pack")
def export_p1p2_addon_mapping_prompt_pack(import_id: str) -> dict[str, Any]:
    return _build_addon_mapping_prompt_pack(import_id)


@app.get("/p1p2/imports/{import_id}/addon-mapping-prompt-pack")
def read_p1p2_addon_mapping_prompt_pack_result(import_id: str) -> dict[str, Any]:
    result_path = _p1p2_import_dir(import_id) / "addon_mapping_prompt_pack_result.json"
    if not result_path.exists():
        raise HTTPException(status_code=404, detail="Addon mapping prompt pack has not been generated yet.")
    return _read_json_if_exists(result_path)


@app.get("/p1p2/imports/{import_id}/addon-mapping-prompt-pack/download")
def download_p1p2_addon_mapping_prompt_pack(import_id: str) -> FileResponse:
    zip_path = _prompt_pack_zip_path(import_id)
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Addon mapping prompt pack ZIP not found. Generate it first.")
    return FileResponse(str(zip_path), filename=zip_path.name, media_type="application/zip")

# ---------------------------------------------------------------------------
# P2-5180-1C/4: Addon Input Candidate Import / Validate + Direct Odoo Apply
# ---------------------------------------------------------------------------
# This section intentionally performs no semantic remapping. It imports an
# already-created ChatGPT addon input candidate, validates its shape, and, when
# explicitly requested, writes a lightweight Odoo overlay addon directly into
# custom_addons. No Odoo addon ZIP is produced by this flow.

ADDON_INPUT_ROOT = ARTIFACT_ROOT / "p1p2_addon_inputs"
ADDON_INPUT_ROOT.mkdir(parents=True, exist_ok=True)

ALLOWED_P1P2_ADDON_MODELS = {
    "fg.p1p2.standard.configuration",
    "fg.p1p2.domain.value.config.link",
    "fg.p1p2.later.phase.concept",
    "fg.p1p2.supporting.anchor",
    "fg.p1p2.gap.item",
}
ALLOWED_P1P2_ADDON_STATUSES = {"approved_candidate", "needs_review", "report_only", "excluded", "draft"}


def _addon_input_dir(addon_input_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9\-]{32,64}", addon_input_id):
        raise HTTPException(status_code=400, detail="Invalid addon_input_id")
    return ADDON_INPUT_ROOT / addon_input_id


def _find_file_case_insensitive(root: Path, filename: str) -> Path | None:
    lower = filename.lower()
    for path in root.rglob("*"):
        if path.is_file() and path.name.lower() == lower:
            return path
    return None


def _iter_addon_candidate_records(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for model_block in candidate.get("models") or []:
        model_name = model_block.get("model") or model_block.get("model_name") or ""
        display_name = model_block.get("display_name") or model_block.get("name") or model_name
        for rec in model_block.get("records") or []:
            if isinstance(rec, dict):
                rows.append({"model": model_name, "model_display_name": display_name, "record": rec})
    for gap in candidate.get("gap_items") or []:
        if isinstance(gap, dict):
            rows.append({"model": "fg.p1p2.gap.item", "model_display_name": "F&G GAP Item", "record": gap})
    return rows


def _validate_addon_input_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    addon_name = candidate.get("addon_name") or ""
    if not addon_name:
        errors.append("addon_name is required")
    elif not re.fullmatch(r"[a-z][a-z0-9_]*", str(addon_name)):
        errors.append("addon_name must be snake_case and start with a lowercase letter")
    source_import_id = candidate.get("source_import_id") or (candidate.get("summary") or {}).get("source_import_id")
    if not source_import_id:
        warnings.append("source_import_id is missing; import will continue but traceability is weaker")
    rows = _iter_addon_candidate_records(candidate)
    if not rows:
        errors.append("No records found in models[].records or gap_items")
    model_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    missing_source_key = 0
    invalid_models: set[str] = set()
    invalid_statuses: set[str] = set()
    gap_not_report_only = 0
    for row in rows:
        model = row["model"]
        rec = row["record"]
        model_counts[model] = model_counts.get(model, 0) + 1
        if model not in ALLOWED_P1P2_ADDON_MODELS:
            invalid_models.add(model)
        status = str(rec.get("status") or row.get("status") or "draft")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status not in ALLOWED_P1P2_ADDON_STATUSES:
            invalid_statuses.add(status)
        if not (rec.get("source_node_key") or rec.get("source_key") or rec.get("node_key")):
            missing_source_key += 1
        if model == "fg.p1p2.gap.item":
            if status != "report_only" or rec.get("excluded_from_auto_generation") is not True:
                gap_not_report_only += 1
    if invalid_models:
        errors.append("Unsupported model(s): " + ", ".join(sorted(invalid_models)))
    if invalid_statuses:
        errors.append("Unsupported status value(s): " + ", ".join(sorted(invalid_statuses)))
    if missing_source_key:
        warnings.append(f"{missing_source_key} record(s) do not have source_node_key/source_key/node_key")
    if gap_not_report_only:
        errors.append(f"{gap_not_report_only} GAP record(s) are not report_only or excluded_from_auto_generation=true")
    return {
        "validation_status": "ok" if not errors else "failed",
        "errors": errors,
        "warnings": warnings,
        "total_records": len(rows),
        "record_counts": model_counts,
        "status_counts": status_counts,
        "addon_name": addon_name,
        "display_name": candidate.get("display_name") or addon_name,
        "source_import_id": source_import_id,
    }


def _addon_input_summary_path(addon_input_id: str) -> Path:
    return _addon_input_dir(addon_input_id) / "import_summary.json"


def _load_addon_input_summary(addon_input_id: str) -> dict[str, Any]:
    path = _addon_input_summary_path(addon_input_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Addon input not found")
    return json.loads(path.read_text(encoding="utf-8"))


def _list_addon_input_summaries() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not ADDON_INPUT_ROOT.exists():
        return items
    for path in sorted(ADDON_INPUT_ROOT.glob("*/import_summary.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            items.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return items


@app.post("/p1p2/addon-inputs/import")
async def import_p1p2_addon_input_candidate(file: UploadFile = File(...)) -> dict[str, Any]:
    addon_input_id = str(uuid4())
    out_dir = _addon_input_dir(addon_input_id)
    extracted = out_dir / "extracted"
    out_dir.mkdir(parents=True, exist_ok=True)
    uploaded = out_dir / "uploaded_pack.zip"
    with uploaded.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        with zipfile.ZipFile(uploaded) as zf:
            zf.extractall(extracted)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid ZIP")

    candidate_path = _find_file_case_insensitive(extracted, "p1p2_odoo_addon_input_candidate.json")
    if not candidate_path:
        raise HTTPException(status_code=400, detail="p1p2_odoo_addon_input_candidate.json not found in ZIP")
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    validation = _validate_addon_input_candidate(candidate)

    mapping_report = _find_file_case_insensitive(extracted, "p1p2_odoo_addon_mapping_report.md")
    split_plan = _find_file_case_insensitive(extracted, "p1p2_odoo_addon_codegen_split_plan.md")
    readme = _find_file_case_insensitive(extracted, "README.md")

    shutil.copy2(candidate_path, out_dir / "p1p2_odoo_addon_input_candidate.json")
    if mapping_report:
        shutil.copy2(mapping_report, out_dir / "p1p2_odoo_addon_mapping_report.md")
    if split_plan:
        shutil.copy2(split_plan, out_dir / "p1p2_odoo_addon_codegen_split_plan.md")
    if readme:
        shutil.copy2(readme, out_dir / "README.md")

    summary = {
        "addon_input_id": addon_input_id,
        "filename": file.filename,
        "status": "addon_input_imported" if validation["validation_status"] == "ok" else "addon_input_validation_failed",
        "validation_status": validation["validation_status"],
        "imported_at": _now_iso(),
        "addon_name": validation.get("addon_name"),
        "display_name": validation.get("display_name"),
        "source_import_id": validation.get("source_import_id"),
        "total_records": validation.get("total_records", 0),
        "record_counts": validation.get("record_counts", {}),
        "status_counts": validation.get("status_counts", {}),
        "errors": validation.get("errors", []),
        "warnings": validation.get("warnings", []),
        "candidate_path": str(out_dir / "p1p2_odoo_addon_input_candidate.json"),
        "mapping_report_path": str(out_dir / "p1p2_odoo_addon_mapping_report.md") if mapping_report else None,
        "codegen_split_plan_path": str(out_dir / "p1p2_odoo_addon_codegen_split_plan.md") if split_plan else None,
        "uploaded_pack_path": str(uploaded),
        "apply_status": "not_applied",
        "addon_path": None,
    }
    _safe_json_dump(out_dir / "import_summary.json", summary)
    return summary


@app.get("/p1p2/addon-inputs")
def list_p1p2_addon_inputs() -> dict[str, Any]:
    items = _list_addon_input_summaries()
    return {"items": items, "count": len(items)}


@app.get("/p1p2/addon-inputs/{addon_input_id}")
def read_p1p2_addon_input(addon_input_id: str) -> dict[str, Any]:
    summary = _load_addon_input_summary(addon_input_id)
    candidate_path = _addon_input_dir(addon_input_id) / "p1p2_odoo_addon_input_candidate.json"
    candidate = _read_json_if_exists(candidate_path) if candidate_path.exists() else None
    return {"summary": summary, "candidate": candidate}


def _record_value(record: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in record and record.get(key) not in (None, ""):
            return record.get(key)
    return default


def _addon_record_name(record: dict[str, Any]) -> str:
    return str(_record_value(record, "name", "display_name", "title", "label", "source_node_key", "source_key", default="Unnamed"))


def _p1p2_overlay_manifest(addon_name: str, display_name: str) -> str:
    return """{
    'name': '%s',
    'version': '19.0.1.0.0',
    'summary': 'F&G P1/P2 overlay inspection records',
    'category': 'Productivity',
    'license': 'LGPL-3',
    'depends': ['base'],
    'data': [
        'security/ir.model.access.csv',
        'views/fg_p1p2_overlay_views.xml',
        'data/fg_p1p2_overlay_data.xml',
    ],
    'application': True,
    'installable': True,
}
""" % _xml_escape(display_name).replace("'", "\\'")


def _p1p2_overlay_models_py() -> str:
    return '''from odoo import fields, models


class FgP1P2StandardConfiguration(models.Model):
    _name = "fg.p1p2.standard.configuration"
    _description = "F&G P1/P2 Standard Configuration"
    _order = "app_key, name"

    name = fields.Char(required=True)
    source_node_key = fields.Char(index=True)
    source_import_id = fields.Char(index=True)
    app_key = fields.Char(index=True)
    related_model = fields.Char(index=True)
    related_bundle = fields.Char(index=True)
    related_scenario = fields.Char(index=True)
    status = fields.Selection([("approved_candidate", "Approved Candidate"), ("needs_review", "Needs Review"), ("excluded", "Excluded"), ("draft", "Draft")], default="approved_candidate", index=True)
    description = fields.Text()
    mapping_reason = fields.Text()
    raw_json = fields.Text()


class FgP1P2DomainValueConfigLink(models.Model):
    _name = "fg.p1p2.domain.value.config.link"
    _description = "F&G P1/P2 Domain Value Config Link"
    _order = "name"

    name = fields.Char(required=True)
    source_node_key = fields.Char(index=True)
    source_import_id = fields.Char(index=True)
    domain_value = fields.Char(index=True)
    configuration_key = fields.Char(index=True)
    related_model = fields.Char(index=True)
    related_bundle = fields.Char(index=True)
    status = fields.Selection([("approved_candidate", "Approved Candidate"), ("needs_review", "Needs Review"), ("excluded", "Excluded"), ("draft", "Draft")], default="approved_candidate", index=True)
    description = fields.Text()
    mapping_reason = fields.Text()
    raw_json = fields.Text()


class FgP1P2LaterPhaseConcept(models.Model):
    _name = "fg.p1p2.later.phase.concept"
    _description = "F&G P1/P2 Later Phase Concept"
    _order = "status, name"

    name = fields.Char(required=True)
    source_node_key = fields.Char(index=True)
    source_import_id = fields.Char(index=True)
    phase_hint = fields.Char(index=True)
    related_model = fields.Char(index=True)
    related_configuration = fields.Char(index=True)
    status = fields.Selection([("approved_candidate", "Approved Candidate"), ("needs_review", "Needs Review"), ("excluded", "Excluded"), ("draft", "Draft")], default="needs_review", index=True)
    reason = fields.Text()
    auto_generation_target = fields.Boolean(default=False)
    raw_json = fields.Text()


class FgP1P2SupportingAnchor(models.Model):
    _name = "fg.p1p2.supporting.anchor"
    _description = "F&G P1/P2 Supporting Anchor"
    _order = "name"

    name = fields.Char(required=True)
    source_node_key = fields.Char(index=True)
    source_import_id = fields.Char(index=True)
    anchor_type = fields.Char(index=True)
    related_model = fields.Char(index=True)
    related_configuration = fields.Char(index=True)
    status = fields.Selection([("approved_candidate", "Approved Candidate"), ("needs_review", "Needs Review"), ("excluded", "Excluded"), ("draft", "Draft")], default="approved_candidate", index=True)
    description = fields.Text()
    raw_json = fields.Text()


class FgP1P2GapItem(models.Model):
    _name = "fg.p1p2.gap.item"
    _description = "F&G P1/P2 GAP Item"
    _order = "gap_type, name"

    name = fields.Char(required=True)
    source_key = fields.Char(index=True)
    source_import_id = fields.Char(index=True)
    gap_type = fields.Char(index=True)
    status = fields.Selection([("report_only", "Report Only"), ("needs_review", "Needs Review"), ("excluded", "Excluded"), ("draft", "Draft")], default="report_only", index=True)
    reason = fields.Text()
    probable_meaning = fields.Text()
    customer_report_message = fields.Text()
    excluded_from_auto_generation = fields.Boolean(default=True)
    raw_json = fields.Text()
'''


def _p1p2_overlay_access_csv() -> str:
    return "\n".join([
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink",
        "access_fg_p1p2_standard_configuration,fg.p1p2.standard.configuration,model_fg_p1p2_standard_configuration,,1,1,1,1",
        "access_fg_p1p2_domain_value_config_link,fg.p1p2.domain.value.config.link,model_fg_p1p2_domain_value_config_link,,1,1,1,1",
        "access_fg_p1p2_later_phase_concept,fg.p1p2.later.phase.concept,model_fg_p1p2_later_phase_concept,,1,1,1,1",
        "access_fg_p1p2_supporting_anchor,fg.p1p2.supporting.anchor,model_fg_p1p2_supporting_anchor,,1,1,1,1",
        "access_fg_p1p2_gap_item,fg.p1p2.gap.item,model_fg_p1p2_gap_item,,1,1,1,1",
    ]) + "\n"


def _p1p2_overlay_views_xml() -> str:
    models = [
        ("standard_configuration", "fg.p1p2.standard.configuration", "Standard Configurations", ["app_key", "name", "related_model", "status"]),
        ("domain_value_config_link", "fg.p1p2.domain.value.config.link", "Domain Value Config Links", ["domain_value", "name", "related_model", "status"]),
        ("later_phase_concept", "fg.p1p2.later.phase.concept", "Later Phase Concepts", ["status", "phase_hint", "name", "related_model"]),
        ("supporting_anchor", "fg.p1p2.supporting.anchor", "Supporting Anchors", ["anchor_type", "name", "related_model", "status"]),
        ("gap_item", "fg.p1p2.gap.item", "F&G GAP Items", ["gap_type", "name", "status", "excluded_from_auto_generation"]),
    ]
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<odoo>']
    for key, model, title, list_fields in models:
        parts.append(f'  <record id="fg_p1p2_{key}_list" model="ir.ui.view"><field name="name">{model}.list</field><field name="model">{model}</field><field name="arch" type="xml"><list>' + ''.join(f'<field name="{f}"/>' for f in list_fields) + '</list></field></record>')
        parts.append(f'  <record id="fg_p1p2_{key}_form" model="ir.ui.view"><field name="name">{model}.form</field><field name="model">{model}</field><field name="arch" type="xml"><form><sheet><group><field name="name"/><field name="source_import_id"/><field name="status"/></group><group><field name="raw_json" nolabel="1"/></group></sheet></form></field></record>')
        parts.append(f'  <record id="fg_p1p2_{key}_action" model="ir.actions.act_window"><field name="name">{_xml_escape(title)}</field><field name="res_model">{model}</field><field name="view_mode">list,form</field></record>')
    parts.append('  <menuitem id="fg_p1p2_root_menu" name="F&amp;G Overlay" sequence="7"/>')
    parts.append('  <menuitem id="fg_p1p2_overlay_menu" name="P1/P2 Overlay" parent="fg_p1p2_root_menu" sequence="10"/>')
    for seq, (key, _model, title, _fields) in enumerate(models, start=10):
        parts.append(f'  <menuitem id="fg_p1p2_{key}_menu" name="{_xml_escape(title)}" parent="fg_p1p2_overlay_menu" action="fg_p1p2_{key}_action" sequence="{seq}"/>')
    parts.append('</odoo>')
    return "\n".join(parts) + "\n"


def _map_candidate_record_to_xml_fields(model: str, rec: dict[str, Any], source_import_id: str) -> dict[str, Any]:
    raw_json = json.dumps(rec, ensure_ascii=False)
    status = str(rec.get("status") or ("report_only" if model == "fg.p1p2.gap.item" else "approved_candidate"))
    if model == "fg.p1p2.standard.configuration":
        return {
            "name": _addon_record_name(rec),
            "source_node_key": _record_value(rec, "source_node_key", "node_key", default=""),
            "source_import_id": source_import_id,
            "app_key": _record_value(rec, "app_key", default=""),
            "related_model": _record_value(rec, "related_model", "odoo_model", "model", default=""),
            "related_bundle": _record_value(rec, "related_bundle", "bundle_key", default=""),
            "related_scenario": _record_value(rec, "related_scenario", "scenario_key", default=""),
            "status": status,
            "description": _record_value(rec, "description", "summary", default=""),
            "mapping_reason": _record_value(rec, "mapping_reason", "reason", default=""),
            "raw_json": raw_json,
        }
    if model == "fg.p1p2.domain.value.config.link":
        return {
            "name": _addon_record_name(rec),
            "source_node_key": _record_value(rec, "source_node_key", "node_key", default=""),
            "source_import_id": source_import_id,
            "domain_value": _record_value(rec, "domain_value", "domain_value_name", "domain_value_name_ja", default=""),
            "configuration_key": _record_value(rec, "configuration_key", "related_configuration", default=""),
            "related_model": _record_value(rec, "related_model", "odoo_model", "model", default=""),
            "related_bundle": _record_value(rec, "related_bundle", "bundle_key", default=""),
            "status": status,
            "description": _record_value(rec, "description", "summary", default=""),
            "mapping_reason": _record_value(rec, "mapping_reason", "reason", default=""),
            "raw_json": raw_json,
        }
    if model == "fg.p1p2.later.phase.concept":
        return {
            "name": _addon_record_name(rec),
            "source_node_key": _record_value(rec, "source_node_key", "node_key", default=""),
            "source_import_id": source_import_id,
            "phase_hint": _record_value(rec, "phase_hint", "expected_later_phase", default=""),
            "related_model": _record_value(rec, "related_model", "odoo_model", "model", default=""),
            "related_configuration": _record_value(rec, "related_configuration", "configuration_key", default=""),
            "status": status,
            "reason": _record_value(rec, "reason", "mapping_reason", "description", default=""),
            "auto_generation_target": bool(rec.get("auto_generation_target", status == "approved_candidate")),
            "raw_json": raw_json,
        }
    if model == "fg.p1p2.supporting.anchor":
        return {
            "name": _addon_record_name(rec),
            "source_node_key": _record_value(rec, "source_node_key", "node_key", default=""),
            "source_import_id": source_import_id,
            "anchor_type": _record_value(rec, "anchor_type", "type", default=""),
            "related_model": _record_value(rec, "related_model", "odoo_model", "model", default=""),
            "related_configuration": _record_value(rec, "related_configuration", "configuration_key", default=""),
            "status": status,
            "description": _record_value(rec, "description", "summary", "reason", default=""),
            "raw_json": raw_json,
        }
    return {
        "name": _addon_record_name(rec),
        "source_key": _record_value(rec, "source_key", "source_node_key", "gap_key", "node_key", default=""),
        "source_import_id": source_import_id,
        "gap_type": _record_value(rec, "gap_type", "type", default=""),
        "status": "report_only",
        "reason": _record_value(rec, "reason", "skip_reason_ja", default=""),
        "probable_meaning": _record_value(rec, "probable_meaning", "probable_meaning_ja", default=""),
        "customer_report_message": _record_value(rec, "customer_report_message", "customer_report_message_ja", default=""),
        "excluded_from_auto_generation": True,
        "raw_json": raw_json,
    }


def _p1p2_overlay_data_xml(candidate: dict[str, Any]) -> tuple[str, dict[str, int]]:
    source_import_id = str(candidate.get("source_import_id") or (candidate.get("summary") or {}).get("source_import_id") or "")
    counts: dict[str, int] = {}
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<odoo noupdate="1">']
    idx = 0
    for row in _iter_addon_candidate_records(candidate):
        model = row["model"]
        if model not in ALLOWED_P1P2_ADDON_MODELS:
            continue
        rec = row["record"]
        fields = _map_candidate_record_to_xml_fields(model, rec, source_import_id)
        key = str(_record_value(rec, "source_node_key", "source_key", "node_key", default=f"row_{idx}"))
        rec_id = _xml_id("p1p2", f"{model}_{key}_{idx}")
        parts.append(_record(model, rec_id, fields))
        counts[model] = counts.get(model, 0) + 1
        idx += 1
    parts.append('</odoo>')
    return "\n".join(parts) + "\n", counts


@app.post("/p1p2/addon-inputs/{addon_input_id}/apply-odoo-addon")
def apply_p1p2_addon_input_to_odoo(addon_input_id: str) -> dict[str, Any]:
    summary = _load_addon_input_summary(addon_input_id)
    if summary.get("validation_status") != "ok":
        raise HTTPException(status_code=400, detail={"message": "Addon input validation is not ok", "summary": summary})
    candidate_path = _addon_input_dir(addon_input_id) / "p1p2_odoo_addon_input_candidate.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    addon_name = str(candidate.get("addon_name") or summary.get("addon_name") or "fg_p1p2_overlay")
    if addon_name != "fg_p1p2_overlay":
        raise HTTPException(status_code=400, detail="Only addon_name=fg_p1p2_overlay is allowed for this generator")
    display_name = str(candidate.get("display_name") or summary.get("display_name") or "F&G P1/P2 Overlay")
    addon_dir = CUSTOM_ADDONS_ROOT / addon_name
    if addon_dir.exists():
        shutil.rmtree(addon_dir)
    addon_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "__init__.py": "from . import models\n",
        "models/__init__.py": "from . import fg_p1p2_overlay\n",
        "models/fg_p1p2_overlay.py": _p1p2_overlay_models_py(),
        "security/ir.model.access.csv": _p1p2_overlay_access_csv(),
        "views/fg_p1p2_overlay_views.xml": _p1p2_overlay_views_xml(),
        "__manifest__.py": _p1p2_overlay_manifest(addon_name, display_name),
        "README.md": f"# {display_name}\n\nGenerated directly from addon input `{addon_input_id}` at {_now_iso()}.\n\nThis addon is a lightweight overlay viewer. It does not modify Odoo standard business models.\n",
    }
    data_xml, data_counts = _p1p2_overlay_data_xml(candidate)
    files["data/fg_p1p2_overlay_data.xml"] = data_xml
    for rel_path, content in files.items():
        _write(addon_dir / rel_path, content)

    result = {
        "addon_input_id": addon_input_id,
        "status": "odoo_addon_applied_to_custom_addons",
        "addon_name": addon_name,
        "display_name": display_name,
        "addon_path": str(addon_dir),
        "applied_at": _now_iso(),
        "record_counts": data_counts,
        "total_records": sum(data_counts.values()),
        "warnings": [
            "This is a lightweight overlay addon. It does not modify Odoo standard business models.",
            "Restart Odoo and update Apps List before installing/upgrading the addon.",
        ],
    }
    _safe_json_dump(_addon_input_dir(addon_input_id) / "odoo_addon_apply_result.json", result)
    summary.update({
        "apply_status": result["status"],
        "addon_path": result["addon_path"],
        "applied_at": result["applied_at"],
        "apply_result_path": str(_addon_input_dir(addon_input_id) / "odoo_addon_apply_result.json"),
    })
    _safe_json_dump(_addon_input_summary_path(addon_input_id), summary)
    return result


@app.get("/p1p2/addon-inputs/{addon_input_id}/odoo-addon-apply-result")
def read_p1p2_addon_apply_result(addon_input_id: str) -> dict[str, Any]:
    path = _addon_input_dir(addon_input_id) / "odoo_addon_apply_result.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Odoo addon apply result not found")
    return json.loads(path.read_text(encoding="utf-8"))

# ---------------------------------------------------------------------------
# P4/P5 Customer Decision Catalog v2.1 Import / Validate / Customer PACK Export
# ---------------------------------------------------------------------------
P4P5_ROOT = STORAGE_ROOT / "p4p5"
P4P5_ROOT.mkdir(parents=True, exist_ok=True)

P4P5_MAIN_CATALOG_NAMES = {
    "P4P5_CUSTOMER_DECISION_PACK_CATALOG_ALL_APPS_v2_1_FIXED.json",
    "P4P5_CUSTOMER_DECISION_PACK_CATALOG_ALL_APPS_v2_1.json",
    "P4P5_CUSTOMER_DECISION_PACK_CATALOG_ALL_APPS_v2.json",
    "P4P5_CUSTOMER_DECISION_PACK_CATALOG_ALL_APPS.json",
    "P4P5_DEVELOPMENT_THEME_CATALOG_ALL_APPS_WITH_P3_CONTEXT.json",
    "P4P5_DEVELOPMENT_THEME_CATALOG_ALL_APPS.json",
}


def _p4p5_import_dir(import_id: str) -> Path:
    return P4P5_ROOT / import_id


def _p4p5_summary_path(import_id: str) -> Path:
    return _p4p5_import_dir(import_id) / "IMPORT_SUMMARY.json"


def _p4p5_catalog_path(import_id: str) -> Path:
    return _p4p5_import_dir(import_id) / "CATALOG.json"


def _p4p5_validation_path(import_id: str) -> Path:
    return _p4p5_import_dir(import_id) / "VALIDATION.json"


def _p4p5_pack_dir(import_id: str) -> Path:
    return _p4p5_import_dir(import_id) / "customer_packs"


def _p4p5_answered_pack_dir(import_id: str) -> Path:
    return _p4p5_import_dir(import_id) / "answered_packs"


def _p4p5_answer_status_path(import_id: str) -> Path:
    return _p4p5_import_dir(import_id) / "ANSWERED_STATUS.json"


def _p4p5_uploaded_pack_path(import_id: str) -> Path:
    return _p4p5_import_dir(import_id) / "uploaded_pack.zip"


def _p4p5_safe_extract_bytes(data: bytes, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as zf:
        for member in zf.infolist():
            target = (out_dir / member.filename).resolve()
            if not str(target).startswith(str(out_dir.resolve())):
                raise HTTPException(status_code=400, detail=f"Unsafe zip entry: {member.filename}")
        zf.extractall(out_dir)


def _p4p5_find_catalog(root: Path) -> Path | None:
    for name in P4P5_MAIN_CATALOG_NAMES:
        found = list(root.rglob(name))
        if found:
            return found[0]
    candidates: list[Path] = []
    for p in root.rglob("*.json"):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("all_development_theme_candidates"), list):
            candidates.append(p)
    return candidates[0] if candidates else None


def _p4p5_theme_list(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    themes = catalog.get("all_development_theme_candidates") or catalog.get("development_theme_candidates") or []
    return themes if isinstance(themes, list) else []


def _p4p5_theme_key(theme: dict[str, Any]) -> str:
    return str(theme.get("development_theme_key") or theme.get("theme_key") or "")


def _p4p5_theme_title(theme: dict[str, Any]) -> str:
    display = theme.get("display") if isinstance(theme.get("display"), dict) else {}
    return str(display.get("title_ja") or display.get("title") or _p4p5_theme_key(theme))


def _p4p5_theme_summary(theme: dict[str, Any]) -> str:
    display = theme.get("display") if isinstance(theme.get("display"), dict) else {}
    return str(display.get("summary_ja") or display.get("business_user_explanation_ja") or "")


def _p4p5_theme_app(theme: dict[str, Any]) -> str:
    if theme.get("source_app_key"):
        return str(theme.get("source_app_key"))
    if theme.get("app_key"):
        return str(theme.get("app_key"))
    classification = theme.get("classification") if isinstance(theme.get("classification"), dict) else {}
    apps = classification.get("target_apps") if isinstance(classification, dict) else []
    if isinstance(apps, list) and apps:
        return str(apps[0]).lower().replace(" ", "_")
    key = _p4p5_theme_key(theme)
    parts = key.split(".")
    return parts[1] if len(parts) > 2 else "unknown"


def _p4p5_prompt_seed(theme: dict[str, Any]) -> dict[str, Any]:
    seed = theme.get("customer_question_prompt_seed")
    return seed if isinstance(seed, dict) else {}


def _p4p5_question_blocks(theme: dict[str, Any]) -> list[dict[str, Any]]:
    seed = _p4p5_prompt_seed(theme)
    blocks = seed.get("question_blocks")
    if isinstance(blocks, list):
        return [b for b in blocks if isinstance(b, dict)]
    cps = theme.get("customer_pack_seed") if isinstance(theme.get("customer_pack_seed"), dict) else {}
    groups = cps.get("question_groups") if isinstance(cps.get("question_groups"), list) else []
    result: list[dict[str, Any]] = []
    for group in groups:
        if isinstance(group, dict):
            result.append(group)
    return result


def _p4p5_hypothesis_item_count(theme: dict[str, Any]) -> int:
    count = 0
    for block in _p4p5_question_blocks(theme):
        items = block.get("hypothesis_items") if isinstance(block.get("hypothesis_items"), list) else []
        if items:
            count += len([x for x in items if isinstance(x, dict)])
        elif isinstance(block.get("questions"), list):
            count += len(block.get("questions") or [])
    return count


def _p4p5_question_count(theme: dict[str, Any]) -> int:
    blocks = _p4p5_question_blocks(theme)
    return len(blocks) if blocks else 0


def _p4p5_scenario_count(theme: dict[str, Any]) -> int:
    scenarios = _p4p5_prompt_seed(theme).get("scenario_seed")
    return len(scenarios) if isinstance(scenarios, list) else 0


def _p4p5_context_counts(theme: dict[str, Any]) -> dict[str, int]:
    refs = theme.get("p3_context_refs") if isinstance(theme.get("p3_context_refs"), dict) else {}
    def n(*names: str) -> int:
        for name in names:
            v = refs.get(name)
            if isinstance(v, list):
                return len(v)
        return 0
    return {
        "support_masters": n("related_support_masters", "related_p3_support_master_keys"),
        "overlay_fields": n("related_overlay_fields", "related_p3_overlay_field_keys"),
        "gap_items": n("related_p3_gap_items", "related_p3_gap_keys"),
        "skipped_items": n("related_skipped_items", "related_p3_skipped_keys"),
        "standard_models": n("related_standard_models", "related_p3_standard_models"),
    }


# ---------------------------------------------------------------------------
# P4-Q-2: Customer Question Pack Export enrichment from imported P3 binding
# ---------------------------------------------------------------------------
# The P4 Theme Catalog is treated as system-imported data.  At export time we do
# not ask ChatGPT to infer diagrams from files.  We read the persisted
# P3_INTERNAL_STRUCTURAL_BINDING import and mechanically create a small P3
# reference subset for each P4 theme.  Missing or unmatched items are reported;
# they are not inferred or auto-filled.


def _p4_latest_internal_binding_id() -> str | None:
    root = _p4_p3_binding_root()
    candidates = sorted(
        root.glob(f"*/{P4_P3_BINDING_SUMMARY_FILENAME}"),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if obj.get("status") in ("imported", "valid", "warning"):
            return str(obj.get("binding_import_id") or path.parent.name)
    return candidates[0].parent.name if candidates else None


def _p4_binding_extracted_dir(binding_import_id: str | None) -> Path | None:
    if not binding_import_id:
        binding_import_id = _p4_latest_internal_binding_id()
    if not binding_import_id:
        return None
    path = _p4_p3_binding_dir(binding_import_id) / "extracted"
    return path if path.exists() else None


def _p4_load_p3_binding_data(binding_import_id: str | None) -> dict[str, Any] | None:
    extracted = _p4_binding_extracted_dir(binding_import_id)
    if not extracted:
        return None

    def read(rel: str, default: Any) -> Any:
        p = extracted / rel
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default

    binding_id = extracted.parent.name
    return {
        "binding_import_id": binding_id,
        "extracted_dir": str(extracted),
        "manifest": read("manifest.json", {}),
        "nodes": read("data/P3_INTERNAL_NODE_CATALOG.json", []),
        "edges": read("data/P3_INTERNAL_EDGE_CATALOG.json", []),
        "fields": read("data/P3_INTERNAL_FIELD_CATALOG.json", []),
        "models": read("data/P3_INTERNAL_MODEL_CATALOG.json", []),
        "relation_tables": read("data/P3_INTERNAL_RELATION_TABLE_CATALOG.json", []),
        "missing_models": read("data/P3_INTERNAL_MISSING_MODEL_CATALOG.json", []),
        "graph_index": read("data/P3_INTERNAL_GRAPH_BINDING_INDEX.json", {}),
    }


def _p4_norm_token(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"^fg_p4p5[._-]", "", text)
    text = re.sub(r"^[a-z0-9]+__[pP]3[_-]?", "", text)
    text = re.sub(r"^[a-z0-9]+[_-]+p3[_-]+", "", text)
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    text = re.sub(r"_id$", "", text)
    return text


def _p4_token_parts(value: Any) -> list[str]:
    token = _p4_norm_token(value)
    return [p for p in token.split("_") if p and p not in {"p3", "link", "field", "master", "model", "key"}]


def _p4_label_ja(obj: dict[str, Any], fallback: str = "") -> str:
    for key in ["display_name_ja", "title_ja", "label_ja", "name_ja", "display_name", "technical_name", "model", "field_name", "node_key"]:
        v = obj.get(key)
        if v:
            return str(v)
    return fallback


def _p4_as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _p4_theme_reference_values(theme: dict[str, Any]) -> dict[str, list[str]]:
    refs = theme.get("p3_context_refs") if isinstance(theme.get("p3_context_refs"), dict) else {}
    odoo = theme.get("odoo_mapping_seed") if isinstance(theme.get("odoo_mapping_seed"), dict) else {}
    standard_models: list[str] = []
    for item in _p4_as_list(refs.get("related_p3_standard_models") or refs.get("related_standard_models")):
        standard_models.append(str(item.get("model") if isinstance(item, dict) else item))
    for item in _p4_as_list(odoo.get("standard_models")):
        if isinstance(item, dict) and item.get("model"):
            standard_models.append(str(item.get("model")))

    support_masters = [str(x.get("key") if isinstance(x, dict) else x) for x in _p4_as_list(refs.get("related_p3_support_master_keys") or refs.get("related_support_masters"))]
    overlay_fields = [str(x.get("key") if isinstance(x, dict) else x) for x in _p4_as_list(refs.get("related_p3_overlay_field_keys") or refs.get("related_overlay_fields"))]
    gap_keys = [str(x.get("key") if isinstance(x, dict) else x) for x in _p4_as_list(refs.get("related_p3_gap_keys") or refs.get("related_p3_gap_items"))]
    skipped_keys = [str(x.get("key") if isinstance(x, dict) else x) for x in _p4_as_list(refs.get("related_p3_skipped_keys") or refs.get("related_skipped_items"))]
    return {
        "standard_models": sorted({x for x in standard_models if x}),
        "support_masters": sorted({x for x in support_masters if x}),
        "overlay_fields": sorted({x for x in overlay_fields if x}),
        "gap_keys": sorted({x for x in gap_keys if x}),
        "skipped_keys": sorted({x for x in skipped_keys if x}),
    }


def _p4_matches_reference(candidate_text: str, reference: str) -> bool:
    c = _p4_norm_token(candidate_text)
    r = _p4_norm_token(reference)
    if not c or not r:
        return False
    if c == r or r in c or c in r:
        return True
    parts = _p4_token_parts(reference)
    if not parts:
        return False
    return all(part in c for part in parts)


def _p4_build_theme_structural_subset(theme: dict[str, Any], binding: dict[str, Any] | None) -> dict[str, Any]:
    key = _p4p5_theme_key(theme)
    app_key = _p4p5_theme_app(theme)
    refs = _p4_theme_reference_values(theme)
    empty = {
        "schema_name": "p4_theme_structural_subset",
        "version": "v1",
        "development_theme_key": key,
        "app_key": app_key,
        "p3_binding_import_id": None,
        "status": "no_p3_binding",
        "reference_only": True,
        "models": [],
        "fields": [],
        "relation_tables": [],
        "missing_models": [],
        "edges": [],
        "unmapped_refs": refs,
        "warnings": ["P3_INTERNAL_STRUCTURAL_BINDING が選択されていないため、P3図表は差し込んでいません。"],
    }
    if not binding:
        return empty

    nodes = binding.get("nodes") or []
    fields = binding.get("fields") or []
    models = binding.get("models") or []
    reltables = binding.get("relation_tables") or []
    missing = binding.get("missing_models") or []
    edges = binding.get("edges") or []

    node_by_key = {str(n.get("node_key")): n for n in nodes if n.get("node_key")}
    model_by_key = {str(m.get("model_key")): m for m in models if m.get("model_key")}
    field_by_key = {str(f.get("field_key")): f for f in fields if f.get("field_key")}

    include_node_keys: set[str] = set()
    include_field_keys: set[str] = set()
    include_edge_keys: set[str] = set()
    unmapped: dict[str, list[str]] = {k: [] for k in refs}

    # 1. Exact standard model references.
    for model in refs["standard_models"]:
        mkey = f"model:{model}"
        if mkey in node_by_key or mkey in model_by_key:
            include_node_keys.add(mkey)
        else:
            unmapped["standard_models"].append(model)

    # 2. Support master references; match only by explicit key/technical token.
    for ref in refs["support_masters"]:
        matched = False
        for n in nodes:
            hay = " ".join(str(n.get(k) or "") for k in ["node_key", "technical_name", "model", "display_name_ja", "business_role_ja", "node_type"])
            # Support master keys are technical tokens produced earlier in P3.
            # Match them mechanically against P3 node keys / model names.  Do not
            # require a perfect node_type because some internal packs normalize
            # P3 support masters as normal model nodes.
            if app_key in (n.get("app_keys") or [n.get("app_key")]) or n.get("app_key") in ("", None, app_key):
                if _p4_matches_reference(hay, ref):
                    include_node_keys.add(str(n.get("node_key")))
                    matched = True
        if not matched:
            unmapped["support_masters"].append(ref)

    # 3. Overlay field references; exact-ish technical token matching only.
    for ref in refs["overlay_fields"]:
        matched = False
        for f in fields:
            hay = " ".join(str(f.get(k) or "") for k in ["field_key", "field_name", "technical_name", "display_name_ja", "business_role_ja"])
            if _p4_matches_reference(hay, ref):
                fkey = str(f.get("field_key") or f.get("node_key"))
                include_field_keys.add(fkey)
                if f.get("node_key"):
                    include_node_keys.add(str(f.get("node_key")))
                if f.get("owner_model_node_key"):
                    include_node_keys.add(str(f.get("owner_model_node_key")))
                if f.get("relation_model_node_key"):
                    include_node_keys.add(str(f.get("relation_model_node_key")))
                matched = True
        if not matched:
            unmapped["overlay_fields"].append(ref)

    # Include only the narrow structural context required for the selected theme.
    # Do not expand every relation connected to a standard model; that makes the
    # customer pack unreadable.  We include:
    # - edges directly involving selected overlay/custom fields
    # - edges between already selected model/custom-master/missing nodes
    # - app->selected base/custom-master edges
    for edge in edges:
        ek = str(edge.get("edge_key") or "")
        if not ek:
            continue
        from_key = str(edge.get("from_node_key") or "")
        to_key = str(edge.get("to_node_key") or "")
        via = str(edge.get("via_field_key") or "")
        edge_type = str(edge.get("edge_type") or "")

        field_edge = from_key in include_field_keys or to_key in include_field_keys or via in include_field_keys
        selected_model_edge = from_key in include_node_keys and to_key in include_node_keys
        app_to_selected = from_key == f"app:{app_key}" and to_key in include_node_keys

        if field_edge or selected_model_edge or app_to_selected:
            include_edge_keys.add(ek)
            if field_edge:
                if from_key in field_by_key or from_key.startswith("field:"):
                    include_field_keys.add(from_key)
                if to_key in field_by_key or to_key.startswith("field:"):
                    include_field_keys.add(to_key)
                if via:
                    include_field_keys.add(via)
                # For selected field edges, include owner/target nodes only.
                for fkey in [from_key, to_key, via]:
                    f = field_by_key.get(fkey)
                    if f:
                        if f.get("owner_model_node_key"):
                            include_node_keys.add(str(f.get("owner_model_node_key")))
                        if f.get("relation_model_node_key"):
                            include_node_keys.add(str(f.get("relation_model_node_key")))
                        if f.get("node_key"):
                            include_node_keys.add(str(f.get("node_key")))
            elif edge_type.startswith("app_"):
                if to_key:
                    include_node_keys.add(to_key)

    # Add field owner / target models for selected field keys.
    for fkey in list(include_field_keys):
        f = field_by_key.get(fkey)
        if not f:
            continue
        for k in ["owner_model_node_key", "relation_model_node_key", "node_key"]:
            if f.get(k):
                include_node_keys.add(str(f.get(k)))

    selected_models: list[dict[str, Any]] = []
    for m in models:
        mkey = str(m.get("model_key") or "")
        if mkey in include_node_keys:
            selected_models.append(m)
    for nkey in include_node_keys:
        if nkey.startswith("model:") and nkey not in {m.get("model_key") for m in selected_models}:
            n = node_by_key.get(nkey)
            if n:
                selected_models.append({
                    "model_key": nkey,
                    "model": n.get("model") or n.get("technical_name") or nkey.replace("model:", ""),
                    "display_name_ja": _p4_label_ja(n, nkey),
                    "technical_name": n.get("technical_name") or n.get("model") or "",
                    "app_keys": n.get("app_keys") or [n.get("app_key")],
                    "node_type": n.get("node_type"),
                    "is_base_model": n.get("is_base_model", False),
                    "is_custom_master": n.get("is_custom_master", False),
                    "is_missing_model": n.get("is_missing_model", False),
                })

    selected_fields = [f for f in fields if str(f.get("field_key") or "") in include_field_keys]
    # Keep only key relation/custom fields, not a full SF dump.
    selected_fields = [f for f in selected_fields if f.get("field_class") == "CusF" or f.get("relation_model") or f.get("relation_table")]
    selected_reltables = [r for r in reltables if str(r.get("relation_table_key") or "") in include_node_keys or any(str(k) in include_edge_keys for k in (r.get("edge_keys") or []))]
    selected_missing = [m for m in missing if str(m.get("missing_model_key") or f"model:{m.get('model')}") in include_node_keys]
    selected_edges = [e for e in edges if str(e.get("edge_key") or "") in include_edge_keys]

    affected = {
        "models": [m.get("model_key") for m in selected_models if m.get("model_key")],
        "fields": [f.get("field_key") for f in selected_fields if f.get("field_key")],
        "custom_masters": [m.get("model_key") for m in selected_models if m.get("is_custom_master")],
        "relation_tables": [r.get("relation_table_key") for r in selected_reltables if r.get("relation_table_key")],
        "missing_models": [m.get("missing_model_key") for m in selected_missing if m.get("missing_model_key")],
        "source_p3_node_keys": sorted(include_node_keys),
        "source_p3_edge_keys": sorted(include_edge_keys),
    }

    theme_graph_binding = {
        "development_theme_key": key,
        "app_key": app_key,
        "theme_title_ja": _p4p5_theme_title(theme),
        "related_graph_refs": {
            "base_p3_graph_keys": [f"p3.internal.graph.binding.{app_key}"],
            "theme_structural_graph_key": f"p4.theme_er.{key}",
            "process_graph_key": f"p4.process.{key}",
        },
        "p3_structural_binding": {
            "include_node_keys": sorted(include_node_keys),
            "include_edge_keys": sorted(include_edge_keys),
            "include_edge_types": sorted({str(e.get("edge_type")) for e in selected_edges if e.get("edge_type")}),
            "include_model_keys": sorted({str(m.get("model_key")) for m in selected_models if m.get("model_key")}),
            "include_field_keys": sorted({str(f.get("field_key")) for f in selected_fields if f.get("field_key")}),
            "include_relation_table_keys": sorted({str(r.get("relation_table_key")) for r in selected_reltables if r.get("relation_table_key")}),
            "include_missing_model_keys": sorted({str(m.get("missing_model_key")) for m in selected_missing if m.get("missing_model_key")}),
            "expansion_policy": {
                "max_hop_from_included_models": 1,
                "include_relation_tables": True,
                "include_missing_models": True,
                "include_fields": ["CusF", "NFF", "related_SF"],
                "exclude_full_standard_field_dump": True,
            },
        },
    }

    return {
        "schema_name": "p4_theme_structural_subset",
        "version": "v1",
        "development_theme_key": key,
        "title_ja": _p4p5_theme_title(theme),
        "app_key": app_key,
        "p3_binding_import_id": binding.get("binding_import_id"),
        "status": "built",
        "reference_only": True,
        "models": selected_models,
        "fields": selected_fields,
        "relation_tables": selected_reltables,
        "missing_models": selected_missing,
        "edges": selected_edges,
        "affected_structural_elements": affected,
        "theme_graph_binding": theme_graph_binding,
        "unmapped_refs": {k: v for k, v in unmapped.items() if v},
        "warnings": ["P3情報はreference_onlyです。顧客回答により採用・対象外・保留を判断します。"],
    }


def _p4_html_escape(value: Any) -> str:
    import html
    return html.escape(str(value or ""))


def _p4_write_theme_tables(theme_dir: Path, subset: dict[str, Any]) -> dict[str, str]:
    tables_dir = theme_dir / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)
    models = subset.get("models") or []
    fields = subset.get("fields") or []

    table_csv = tables_dir / "affected_tables.csv"
    with table_csv.open("w", encoding="utf-8", newline="") as f:
        import csv
        w = csv.DictWriter(f, fieldnames=["app_key", "table_name_ja", "technical_name", "node_type", "status", "is_custom_master", "is_missing_model"])
        w.writeheader()
        for m in models:
            w.writerow({
                "app_key": subset.get("app_key"),
                "table_name_ja": m.get("display_name_ja"),
                "technical_name": m.get("technical_name") or m.get("model"),
                "node_type": m.get("node_type"),
                "status": m.get("status"),
                "is_custom_master": m.get("is_custom_master"),
                "is_missing_model": m.get("is_missing_model"),
            })

    field_csv = tables_dir / "affected_fields_by_table.csv"
    with field_csv.open("w", encoding="utf-8", newline="") as f:
        import csv
        w = csv.DictWriter(f, fieldnames=["app_key", "owner_model", "field_name_ja", "technical_name", "field_type", "field_class", "relation_model", "business_role_ja"])
        w.writeheader()
        for field in fields:
            w.writerow({
                "app_key": subset.get("app_key"),
                "owner_model": field.get("owner_model"),
                "field_name_ja": field.get("display_name_ja"),
                "technical_name": field.get("technical_name") or field.get("field_name"),
                "field_type": field.get("field_type"),
                "field_class": field.get("field_class"),
                "relation_model": field.get("relation_model"),
                "business_role_ja": field.get("business_role_ja"),
            })

    table_html = tables_dir / "affected_tables.html"
    rows = []
    for m in models:
        rows.append(f"<tr><td>{_p4_html_escape(m.get('display_name_ja'))}</td><td>{_p4_html_escape(m.get('technical_name') or m.get('model'))}</td><td>{_p4_html_escape(m.get('node_type'))}</td><td>{'Yes' if m.get('is_custom_master') else ''}</td><td>{'Yes' if m.get('is_missing_model') else ''}</td></tr>")
    table_html.write_text("""
<!doctype html><html lang=\"ja\"><meta charset=\"utf-8\"><title>関係テーブル一覧</title>
<style>body{font-family:system-ui,sans-serif;line-height:1.6;padding:24px;background:#fafafa;color:#17202a}table{border-collapse:collapse;width:100%;background:white}th,td{border:1px solid #d6dee8;padding:8px;vertical-align:top;word-break:break-word}th{background:#eef4fb}</style>
<h1>関係テーブル一覧</h1><p>この一覧はP3 reference_only情報から機械的に抽出した、テーマ関連候補です。</p>
<table><thead><tr><th>日本語名</th><th>技術名</th><th>種別</th><th>Custom Master</th><th>Missing</th></tr></thead><tbody>
""" + "\n".join(rows) + "</tbody></table></html>", encoding="utf-8")

    by_owner: dict[str, list[dict[str, Any]]] = {}
    for field in fields:
        by_owner.setdefault(str(field.get("owner_model") or "unknown"), []).append(field)
    field_sections: list[str] = []
    for owner, group in sorted(by_owner.items()):
        fr = []
        for field in group:
            fr.append(f"<tr><td>{_p4_html_escape(field.get('display_name_ja'))}</td><td>{_p4_html_escape(field.get('technical_name') or field.get('field_name'))}</td><td>{_p4_html_escape(field.get('field_type'))}</td><td>{_p4_html_escape(field.get('field_class'))}</td><td>{_p4_html_escape(field.get('relation_model'))}</td></tr>")
        field_sections.append(f"<section><h2>{_p4_html_escape(owner)}</h2><table><thead><tr><th>表示名</th><th>技術名</th><th>型</th><th>class</th><th>relation</th></tr></thead><tbody>{''.join(fr)}</tbody></table></section>")
    field_html = tables_dir / "affected_fields_by_table.html"
    field_html.write_text("""
<!doctype html><html lang=\"ja\"><meta charset=\"utf-8\"><title>関係フィールド一覧</title>
<style>body{font-family:system-ui,sans-serif;line-height:1.6;padding:24px;background:#fafafa;color:#17202a}section{margin:0 0 24px}table{border-collapse:collapse;width:100%;background:white}th,td{border:1px solid #d6dee8;padding:8px;vertical-align:top;word-break:break-word}th{background:#eef4fb}</style>
<h1>関係フィールド一覧</h1><p>SF全件ではなく、P3 Custom Fieldまたはテーマ関連リレーション候補だけを表示します。</p>
""" + "\n".join(field_sections) + "</html>", encoding="utf-8")
    return {
        "affected_tables_html": str(table_html.relative_to(theme_dir)),
        "affected_tables_csv": str(table_csv.relative_to(theme_dir)),
        "affected_fields_html": str(field_html.relative_to(theme_dir)),
        "affected_fields_csv": str(field_csv.relative_to(theme_dir)),
    }


def _p4_dot_id(value: str) -> str:
    return "n" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def _p4_mermaid_id(value: str) -> str:
    return "n" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]


def _p4_mermaid_label(model: dict[str, Any], node_key: str) -> str:
    label = str(_p4_label_ja(model, node_key) or node_key)
    tech = str(model.get("technical_name") or model.get("model") or "")
    label = label.replace("[", "(").replace("]", ")").replace("|", "/").replace('"', "'")
    tech = tech.replace("[", "(").replace("]", ")").replace("|", "/").replace('"', "'")
    if tech and tech not in label:
        label = f"{label}<br/>{tech}"
    if model.get("is_missing_model") or str(model.get("node_type") or "").lower() == "missing_model":
        if "missing_model" not in label:
            label = f"{label}<br/>missing_model"
    return label


def _p4_model_technical_name(model: dict[str, Any]) -> str:
    return str(model.get("technical_name") or model.get("model") or "")


def _p4_select_mermaid_center(app_key: str, node_items: list[tuple[str, dict[str, Any]]]) -> tuple[str, dict[str, Any]] | None:
    """Select a stable, theme-friendly center node without LLM inference."""
    priority_by_app = {
        "sales": ["sale.order", "sale.order.line", "stock.picking", "res.partner"],
        "purchase": ["purchase.order", "purchase.order.line", "stock.picking", "res.partner"],
        "inventory": ["stock.picking", "stock.move", "stock.lot", "stock.quant", "stock.warehouse"],
        "manufacturing": ["mrp.production", "mrp.bom", "stock.picking", "stock.lot"],
        "mrp_planning": ["mrp.production", "mrp.bom", "stock.warehouse", "stock.lot"],
        "quality": ["quality.check", "quality.alert", "quality.point", "stock.lot", "stock.picking"],
        "accounting_billing": ["account.move", "account.payment", "sale.order", "purchase.order", "res.partner"],
        "master_common": ["product.template", "product.product", "res.partner", "stock.warehouse"],
        "cross_app": ["sale.order", "stock.picking", "purchase.order", "mrp.production", "account.move"],
    }
    by_technical = {_p4_model_technical_name(model): (nkey, model) for nkey, model in node_items}
    for technical in priority_by_app.get(app_key, []):
        if technical in by_technical:
            return by_technical[technical]

    def score(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
        nkey, model = item
        technical = _p4_model_technical_name(model)
        node_type = str(model.get("node_type") or "").lower()
        if technical.startswith("x_") or model.get("is_custom_master"):
            rank = 30
        elif model.get("is_missing_model") or node_type == "missing_model":
            rank = 40
        elif technical:
            rank = 10
        else:
            rank = 20
        return (rank, technical or nkey)

    return sorted(node_items, key=score)[0] if node_items else None


def _p4_mermaid_node_category(model: dict[str, Any]) -> str:
    technical = _p4_model_technical_name(model)
    node_type = str(model.get("node_type") or "").lower()
    if model.get("is_missing_model") or node_type == "missing_model":
        return "missing"
    if model.get("is_custom_master") or technical.startswith("x_"):
        return "p3"
    return "standard"


def _p4_mermaid_node_line(nkey: str, model: dict[str, Any]) -> str:
    node_id = _p4_mermaid_id(nkey)
    label = _p4_mermaid_label(model, nkey).replace('"', "'")
    return f'  {node_id}["{label}"]'


def _p4_write_theme_mermaid(theme_dir: Path, subset: dict[str, Any], node_keys: dict[str, dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    """Write a chat-optimized Mermaid partial ER diagram.

    This is intentionally different from the detailed DOT/SVG export.  The goal is
    to render clearly in ChatGPT, so it uses a vertical center-model layout and
    avoids the unreadable one-line chain fallback.
    """
    diagrams_dir = theme_dir / "diagrams"
    diagrams_dir.mkdir(parents=True, exist_ok=True)

    app_key = str(subset.get("app_key") or "")
    all_node_items = list(sorted(node_keys.items()))
    center_item = _p4_select_mermaid_center(app_key, all_node_items)

    # Build adjacency from real imported P3 Binding edges only.
    adjacency: dict[str, set[str]] = {nkey: set() for nkey, _ in all_node_items}
    for edge in edges[:240]:
        f = str(edge.get("from_node_key") or "")
        t = str(edge.get("to_node_key") or "")
        if f in node_keys and t in node_keys and f != t:
            adjacency.setdefault(f, set()).add(t)
            adjacency.setdefault(t, set()).add(f)

    max_nodes = 10
    selected: list[tuple[str, dict[str, Any]]] = []
    if center_item:
        selected.append(center_item)
        center_key = center_item[0]
        # Prefer real one-hop neighbors so the simplified diagram stays aligned
        # with the P3 structural subset.
        neighbor_keys = sorted(adjacency.get(center_key, set()))
        for nkey in neighbor_keys:
            if nkey in node_keys and nkey != center_key:
                selected.append((nkey, node_keys[nkey]))
            if len(selected) >= max_nodes:
                break
    else:
        center_key = ""

    selected_keys = {nkey for nkey, _ in selected}

    # If the theme extraction contains selected models but no direct edge from the
    # center, add a small, deterministic set of important nodes.  This is not a
    # semantic remap; it only chooses display order from already-selected nodes.
    def display_priority(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
        nkey, model = item
        technical = _p4_model_technical_name(model)
        category = _p4_mermaid_node_category(model)
        app_priorities = {
            "sales": ["res.partner", "sale.order.line", "stock.picking", "stock.lot", "x_fg_p3_hold_reason"],
            "purchase": ["res.partner", "purchase.order.line", "stock.picking", "stock.lot"],
            "inventory": ["stock.lot", "stock.move", "res.partner", "x_fg_p3_inventory_hold_reason"],
            "manufacturing": ["stock.lot", "mrp.bom", "stock.picking", "x_fg_p3_planning_hold_reason"],
            "mrp_planning": ["stock.lot", "mrp.bom", "stock.picking", "x_fg_p3_planning_hold_reason"],
            "quality": ["quality.alert", "quality.point", "stock.lot", "x_fg_p3_quality_hold_reason"],
        }
        plist = app_priorities.get(app_key, [])
        try:
            rank = plist.index(technical)
        except ValueError:
            rank = 50
        if category == "p3":
            rank += 20
        elif category == "missing":
            rank += 30
        return (rank, technical or nkey)

    for item in sorted(all_node_items, key=display_priority):
        if len(selected) >= max_nodes:
            break
        if item[0] not in selected_keys:
            selected.append(item)
            selected_keys.add(item[0])

    selected_node_keys = {k for k, _ in selected}
    center_key = selected[0][0] if selected else ""

    standard_nodes = [(k, m) for k, m in selected if k != center_key and _p4_mermaid_node_category(m) == "standard"]
    p3_nodes = [(k, m) for k, m in selected if k != center_key and _p4_mermaid_node_category(m) == "p3"]
    missing_nodes = [(k, m) for k, m in selected if k != center_key and _p4_mermaid_node_category(m) == "missing"]

    lines = [
        "flowchart TB",
        "  %% ChatGPT表示用の簡略テーマ部分ER図。詳細確認はSVG/DOT/JSONを参照してください。",
        "",
        "  subgraph CORE[中心業務モデル]",
    ]
    if selected:
        lines.append(_p4_mermaid_node_line(selected[0][0], selected[0][1]))
    lines.append("  end")

    if standard_nodes:
        lines += ["", "  subgraph STD[関連標準モデル]"]
        for nkey, model in standard_nodes:
            lines.append(_p4_mermaid_node_line(nkey, model))
        lines.append("  end")

    if p3_nodes:
        lines += ["", "  subgraph P3[P3参考マスタ / カスタム候補]"]
        for nkey, model in p3_nodes:
            lines.append(_p4_mermaid_node_line(nkey, model))
        lines.append("  end")

    if missing_nodes:
        lines += ["", "  subgraph MISS[missing_model / 要確認]"]
        for nkey, model in missing_nodes:
            lines.append(_p4_mermaid_node_line(nkey, model))
        lines.append("  end")

    # Draw real edges first, prioritizing center-related edges.  Avoid creating a
    # left-to-right chain; keep the center as the visual anchor.
    drawn = 0
    seen_edges: set[tuple[str, str, str]] = set()

    def add_edge(f: str, t: str, label: str, dotted: bool = False) -> None:
        nonlocal drawn
        if f not in selected_node_keys or t not in selected_node_keys or f == t:
            return
        fm = node_keys.get(f) or {}
        tm = node_keys.get(t) or {}
        missing = bool(fm.get("is_missing_model") or tm.get("is_missing_model") or _p4_mermaid_node_category(fm) == "missing" or _p4_mermaid_node_category(tm) == "missing")
        if missing:
            arrow = "-.未導入候補.->"
        elif dotted:
            arrow = f"-.{label}.->"
        else:
            arrow = f"-- {label} -->" if label else "-->"
        edge_key = (_p4_mermaid_id(f), _p4_mermaid_id(t), arrow)
        if edge_key in seen_edges:
            return
        seen_edges.add(edge_key)
        lines.append(f"  {_p4_mermaid_id(f)} {arrow} {_p4_mermaid_id(t)}")
        drawn += 1

    real_edges: list[tuple[int, str, str]] = []
    for edge in edges[:240]:
        f = str(edge.get("from_node_key") or "")
        t = str(edge.get("to_node_key") or "")
        if f in selected_node_keys and t in selected_node_keys and f != t:
            priority = 0 if center_key in (f, t) else 1
            real_edges.append((priority, f, t))
    for _priority, f, t in sorted(real_edges)[:16]:
        add_edge(f, t, "参考関連")
        if drawn >= 14:
            break

    # Some P3 structural subsets include relevant models but no model-to-model
    # edge after narrow filtering.  In that case, draw a center-star with a dotted
    # label.  It is explicitly marked as a reference display relationship and does
    # not create a new data relation.
    if center_key and drawn < min(4, max(0, len(selected) - 1)):
        for nkey, _model in selected[1:]:
            if drawn >= 10:
                break
            add_edge(center_key, nkey, "参考候補", dotted=True)

    if len(all_node_items) > len(selected):
        omitted = len(all_node_items) - len(selected)
        lines += ["", f"  %% 表示上の可読性のため、残り{omitted}件の関連候補はHTML/CSV/JSON側に回しています。"]

    lines += [
        "",
        "  %% この図はP3 Internal Bindingから機械的に抽出したreference_onlyのテーマ部分ERです。",
        "  %% 顧客回答や実装採否を自動確定しません。",
    ]
    mmd = "\n".join(lines).strip() + "\n"
    mmd_path = diagrams_dir / "table_connections.mmd"
    mmd_path.write_text(mmd, encoding="utf-8")
    return str(mmd_path.relative_to(theme_dir))


def _p4_write_theme_diagram(theme_dir: Path, subset: dict[str, Any]) -> dict[str, str]:
    diagrams_dir = theme_dir / "diagrams"
    diagrams_dir.mkdir(parents=True, exist_ok=True)
    models = subset.get("models") or []
    edges = subset.get("edges") or []
    node_keys = {str(m.get("model_key") or f"model:{m.get('model')}"): m for m in models}
    lines = [
        "digraph G {",
        "  graph [rankdir=TB, fontsize=10, fontname=\"Noto Sans CJK JP\", labelloc=t, labeljust=l];",
        "  node [shape=box, style=\"rounded,filled\", fillcolor=\"#f8fbff\", color=\"#9db7d5\", fontname=\"Noto Sans CJK JP\", fontsize=10];",
        "  edge [color=\"#7d8fa3\", arrowsize=0.7, fontname=\"Noto Sans CJK JP\", fontsize=9];",
    ]
    for nkey, m in sorted(node_keys.items()):
        label = _p4_label_ja(m, nkey)
        lines.append(f'  {_p4_dot_id(nkey)} [label="{label.replace(chr(34), "")}"];')
    for e in edges[:120]:
        f = str(e.get("from_node_key") or "")
        t = str(e.get("to_node_key") or "")
        if f in node_keys and t in node_keys:
            lines.append(f"  {_p4_dot_id(f)} -> {_p4_dot_id(t)};")
    lines.append("}")
    dot = "\n".join(lines)
    dot_path = diagrams_dir / "table_connections.dot"
    dot_path.write_text(dot, encoding="utf-8")

    # Simple inline SVG fallback so the pack is usable even when graphviz is not installed.
    width = 960
    row_h = 72
    height = max(160, 80 + len(node_keys) * row_h)
    svg_parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">', '<style>text{font-family:sans-serif;font-size:14px}.box{fill:#f8fbff;stroke:#9db7d5;stroke-width:1.2}.line{stroke:#7d8fa3;stroke-width:1.2;fill:none;marker-end:url(#a)}</style>', '<defs><marker id="a" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L7,3 L0,6 Z" fill="#7d8fa3"/></marker></defs>']
    pos: dict[str, tuple[int, int]] = {}
    for i, (nkey, m) in enumerate(sorted(node_keys.items())):
        x = 60 + (i % 2) * 460
        y = 50 + (i // 2) * row_h
        pos[nkey] = (x, y)
        label = _p4_html_escape(_p4_label_ja(m, nkey))
        svg_parts.append(f'<rect class="box" x="{x}" y="{y}" rx="10" ry="10" width="360" height="44"/>')
        svg_parts.append(f'<text x="{x+16}" y="{y+27}">{label}</text>')
    drawn = 0
    for e in edges[:120]:
        f = str(e.get("from_node_key") or "")
        t = str(e.get("to_node_key") or "")
        if f in pos and t in pos and f != t:
            x1, y1 = pos[f]; x2, y2 = pos[t]
            svg_parts.append(f'<path class="line" d="M{x1+180},{y1+44} C{x1+180},{y1+62} {x2+180},{y2-18} {x2+180},{y2}"/>')
            drawn += 1
            if drawn > 180:
                break
    svg_parts.append('</svg>')
    svg_path = diagrams_dir / "table_connections.svg"
    svg_path.write_text("\n".join(svg_parts), encoding="utf-8")
    mmd_rel = _p4_write_theme_mermaid(theme_dir, subset, node_keys, edges)
    return {
        "dot": str(dot_path.relative_to(theme_dir)),
        "svg": str(svg_path.relative_to(theme_dir)),
        "mmd": mmd_rel,
    }


def _p4_write_theme_index_html(theme_dir: Path, theme: dict[str, Any], subset: dict[str, Any], rels: dict[str, str]) -> None:
    html = f"""<!doctype html><html lang=\"ja\"><meta charset=\"utf-8\"><title>{_p4_html_escape(_p4p5_theme_title(theme))}</title>
<style>body{{font-family:system-ui,sans-serif;line-height:1.6;padding:24px;background:#fafafa;color:#17202a}}.note{{background:#fff8e5;border:1px solid #f0d48a;padding:12px;border-radius:10px}}.links a{{display:inline-block;margin:4px 8px 4px 0;padding:8px 10px;background:#eef4fb;border-radius:8px;color:#17456b;text-decoration:none}}img{{max-width:100%;height:auto;display:block;margin:16px auto;background:white;border:1px solid #d6dee8}}</style>
<h1>{_p4_html_escape(_p4p5_theme_title(theme))}</h1>
<p>{_p4_html_escape(_p4p5_theme_summary(theme))}</p>
<div class=\"note\">このP3情報はreference_onlyです。顧客回答により採用・対象外・保留を判断します。P4/P5の回答は自動補完していません。</div>
<h2>質問PACK</h2><p><a href=\"ANSWER_PACK.md\">ANSWER_PACK.md</a> / <a href=\"ANSWER_TEMPLATE.json\">ANSWER_TEMPLATE.json</a></p>
<h2>P3関連テーブル・フィールド</h2><div class=\"links\"><a href=\"{rels.get('affected_tables_html')}\">関係テーブル一覧</a><a href=\"{rels.get('affected_fields_html')}\">関係フィールド一覧</a><a href=\"data/theme_graph_binding.json\">theme_graph_binding.json</a><a href=\"data/affected_structural_elements.json\">affected_structural_elements.json</a></div>
<h2>テーブル同士のつながり図</h2><img src=\"diagrams/table_connections.svg\" alt=\"テーブル同士のつながり図\" />
<p class=\"links\"><a href=\"diagrams/table_connections.svg\">SVGを開く</a><a href=\"diagrams/table_connections.dot\">DOTを開く</a></p>
</html>"""
    (theme_dir / "index.html").write_text(html, encoding="utf-8")


def _p4_append_p3_section_to_answer_pack(path: Path, subset: dict[str, Any]) -> None:
    if not path.exists():
        return
    models = subset.get("models") or []
    fields = subset.get("fields") or []
    missing = subset.get("missing_models") or []
    unmapped = subset.get("unmapped_refs") or {}
    lines = [
        "",
        "---",
        "",
        "## P3構造情報（参考 / reference_only）",
        "",
        "このP3情報は、P4回答を分かりやすくするための参考情報です。採用・対象外・保留は顧客回答後に判断します。",
        "",
        f"- 関連テーブル候補: {len(models)}件",
        f"- 関連フィールド候補: {len(fields)}件",
        f"- 未導入/未存在モデル候補: {len(missing)}件",
        "",
        "### 同梱資料",
        "",
        "- `index.html`: このテーマの確認入口",
        "- `tables/affected_tables.html`: 関係テーブル一覧",
        "- `tables/affected_fields_by_table.html`: 関係フィールド一覧",
        "- `diagrams/table_connections.svg`: テーブル同士のつながり図",
        "- `diagrams/table_connections.mmd`: チャット本文に表示しやすいMermaid図コード",
        "- `data/theme_graph_binding.json`: P3内部Bindingから生成した紐づけ",
        "- `data/affected_structural_elements.json`: 影響構造要素",
        "",
    ]
    if unmapped:
        lines += ["### P3参照のうち紐づかなかったもの", ""]
        for k, vals in unmapped.items():
            lines.append(f"- {k}: {', '.join(map(str, vals))}")
        lines.append("")
    path.write_text(path.read_text(encoding="utf-8") + "\n" + "\n".join(lines), encoding="utf-8")


def _p4p5_decision_key_is_prefixed(theme_key: str, decision_key: str) -> bool:
    return bool(decision_key and (decision_key == theme_key or decision_key.startswith(f"{theme_key}.")))


def _p4p5_validate_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    themes = _p4p5_theme_list(catalog)
    issues: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    apps: dict[str, int] = {}
    total_questions = 0
    total_hypothesis_items = 0
    total_scenarios = 0
    p3_context_theme_count = 0
    pack_ready: list[str] = []
    needs_answer: list[str] = []
    p3_counts = {"support_masters": 0, "overlay_fields": 0, "gap_items": 0, "skipped_items": 0, "standard_models": 0}
    required_groups = {f"Q{i}" for i in range(1, 11)}

    if not themes:
        issues.append({"severity": "error", "code": "NO_THEMES", "message": "Development Themeが見つかりません。"})

    for idx, theme in enumerate(themes):
        key = _p4p5_theme_key(theme)
        path = f"themes[{idx}]"
        if not key:
            issues.append({"severity": "error", "code": "MISSING_THEME_KEY", "path": path, "message": "development_theme_key がありません。"})
            continue
        seen[key] = seen.get(key, 0) + 1
        app_key = _p4p5_theme_app(theme)
        apps[app_key] = apps.get(app_key, 0) + 1
        if not _p4p5_theme_title(theme):
            issues.append({"severity": "error", "code": "MISSING_TITLE", "path": key, "message": "display.title_ja がありません。"})

        seed = _p4p5_prompt_seed(theme)
        blocks = _p4p5_question_blocks(theme)
        scenario_count = _p4p5_scenario_count(theme)
        hypothesis_count = _p4p5_hypothesis_item_count(theme)
        total_questions += len(blocks)
        total_hypothesis_items += hypothesis_count
        total_scenarios += scenario_count

        if not seed:
            issues.append({"severity": "error", "code": "MISSING_CUSTOMER_QUESTION_PROMPT_SEED", "path": key, "message": "customer_question_prompt_seed がありません。"})
        else:
            policy = seed.get("answer_policy") if isinstance(seed.get("answer_policy"), dict) else {}
            if policy.get("do_not_autofill_customer_answers") is not True:
                issues.append({"severity": "error", "code": "AUTOFILL_POLICY_NOT_DISABLED", "path": key, "message": "do_not_autofill_customer_answers=true が必要です。"})
            if policy.get("default_answer_status") not in ("not_answered", None):
                issues.append({"severity": "warning", "code": "DEFAULT_ANSWER_STATUS_NOT_NOT_ANSWERED", "path": key, "message": "default_answer_status は not_answered 推奨です。"})
            if scenario_count <= 0:
                issues.append({"severity": "warning", "code": "NO_SCENARIOS", "path": key, "message": "scenario_seed がありません。"})
            groups = {str(b.get("question_id") or b.get("question_group") or b.get("group_key") or "") for b in blocks}
            missing_groups = sorted(required_groups - groups)
            if missing_groups:
                issues.append({"severity": "warning", "code": "MISSING_Q_GROUPS", "path": key, "message": "Q1〜Q10 の一部が不足しています。", "missing": missing_groups})
            for bidx, block in enumerate(blocks):
                if not block.get("question_background_ja"):
                    issues.append({"severity": "warning", "code": "MISSING_QUESTION_BACKGROUND", "path": f"{key}.question_blocks[{bidx}]", "message": "question_background_ja がありません。"})
                if not block.get("answer_hint_ja"):
                    issues.append({"severity": "warning", "code": "MISSING_ANSWER_HINT", "path": f"{key}.question_blocks[{bidx}]", "message": "answer_hint_ja がありません。"})
                items = block.get("hypothesis_items") if isinstance(block.get("hypothesis_items"), list) else []
                if not items:
                    issues.append({"severity": "warning", "code": "NO_HYPOTHESIS_ITEMS", "path": f"{key}.question_blocks[{bidx}]", "message": "hypothesis_items がありません。"})
                for iidx, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    decision_key = str(item.get("decision_key") or "")
                    if not decision_key:
                        issues.append({"severity": "error", "code": "MISSING_DECISION_KEY", "path": f"{key}.question_blocks[{bidx}].hypothesis_items[{iidx}]", "message": "decision_key がありません。"})
                    elif not _p4p5_decision_key_is_prefixed(key, decision_key):
                        issues.append({"severity": "error", "code": "DECISION_KEY_NOT_PREFIXED", "path": f"{key}.question_blocks[{bidx}].hypothesis_items[{iidx}]", "decision_key": decision_key, "message": "decision_key は <development_theme_key>.<local_key> 形式にしてください。"})

        refs = theme.get("p3_context_refs") if isinstance(theme.get("p3_context_refs"), dict) else {}
        if refs:
            p3_context_theme_count += 1
            if refs.get("p3_usage_policy") != "reference_only":
                issues.append({"severity": "error", "code": "P3_POLICY_NOT_REFERENCE_ONLY", "path": key, "message": "p3_context_refs.p3_usage_policy は reference_only にしてください。"})
        for k, v in _p4p5_context_counts(theme).items():
            p3_counts[k] += v
        readiness = theme.get("readiness") if isinstance(theme.get("readiness"), dict) else {}
        if readiness.get("customer_answer_status") in (None, "not_answered", "needs_user_input"):
            needs_answer.append(key)
        if seed and scenario_count > 0 and blocks and hypothesis_count > 0:
            pack_ready.append(key)

    duplicates = sorted([k for k, c in seen.items() if c > 1])
    for key in duplicates:
        issues.append({"severity": "error", "code": "DUPLICATE_THEME_KEY", "path": key, "message": "development_theme_key が重複しています。"})

    error_count = len([x for x in issues if x.get("severity") == "error"])
    warning_count = len([x for x in issues if x.get("severity") == "warning"])
    return {
        "schema_name": "p4p5_customer_decision_catalog_validation",
        "version": "v2.1",
        "validated_at": _now_iso(),
        "status": "valid" if error_count == 0 else "invalid",
        "theme_count": len(themes),
        "app_count": len(apps),
        "app_counts": dict(sorted(apps.items())),
        "theme_key_unique": not duplicates,
        "duplicate_theme_keys": duplicates,
        "customer_pack_ready_theme_count": len(pack_ready),
        "themes_ready_for_pack_export": pack_ready,
        "themes_requiring_customer_answer": needs_answer,
        "total_question_count": total_questions,
        "total_hypothesis_item_count": total_hypothesis_items,
        "total_scenario_count": total_scenarios,
        "p3_context_theme_count": p3_context_theme_count,
        "p3_context_counts": p3_counts,
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": issues,
    }




def _p4p5_load_answer_status(import_id: str) -> dict[str, Any]:
    path = _p4p5_answer_status_path(import_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("themes", {})
                return data
        except Exception:
            pass
    return {
        "schema_name": "p4p5_customer_decision_answer_status",
        "version": "v1",
        "import_id": import_id,
        "updated_at": _now_iso(),
        "answered_theme_count": 0,
        "themes": {},
    }


def _p4p5_save_answer_status(import_id: str, status: dict[str, Any]) -> None:
    themes = status.get("themes") if isinstance(status.get("themes"), dict) else {}
    status["schema_name"] = "p4p5_customer_decision_answer_status"
    status["version"] = "v1"
    status["import_id"] = import_id
    status["updated_at"] = _now_iso()
    status["answered_theme_count"] = len([v for v in themes.values() if isinstance(v, dict) and v.get("answer_status") in ("answered", "answered_with_definition")])
    status["themes"] = themes
    _safe_json_dump(_p4p5_answer_status_path(import_id), status)


def _p4p5_answer_record_for_theme(import_id: str, theme_key: str) -> dict[str, Any] | None:
    status = _p4p5_load_answer_status(import_id)
    themes = status.get("themes") if isinstance(status.get("themes"), dict) else {}
    record = themes.get(theme_key)
    return record if isinstance(record, dict) else None


def _p4p5_internal_design_pack_dir(import_id: str) -> Path:
    return _p4p5_import_dir(import_id) / "internal_design_exports"


def _p4p5_internal_design_pack_status_path(import_id: str) -> Path:
    return _p4p5_import_dir(import_id) / "INTERNAL_DESIGN_EXPORT_STATUS.json"


def _p4p5_load_internal_design_export_status(import_id: str) -> dict[str, Any]:
    path = _p4p5_internal_design_pack_status_path(import_id)
    if path.exists():
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                obj.setdefault("themes", {})
                obj.setdefault("exports", [])
                return obj
        except Exception:
            pass
    return {
        "schema_name": "p4p5_internal_design_export_status",
        "version": "v1",
        "import_id": import_id,
        "updated_at": _now_iso(),
        "exported_theme_count": 0,
        "themes": {},
        "exports": [],
    }


def _p4p5_save_internal_design_export_status(import_id: str, status: dict[str, Any]) -> None:
    themes = status.get("themes") if isinstance(status.get("themes"), dict) else {}
    status["schema_name"] = "p4p5_internal_design_export_status"
    status["version"] = "v1"
    status["import_id"] = import_id
    status["updated_at"] = _now_iso()
    status["exported_theme_count"] = len([v for v in themes.values() if isinstance(v, dict) and v.get("internal_design_export_status") == "exported"])
    status["themes"] = themes
    status.setdefault("exports", [])
    _safe_json_dump(_p4p5_internal_design_pack_status_path(import_id), status)


def _p4p5_internal_prompt_files() -> dict[str, str]:
    return {
        "01_generate_domain_guide_patch.md": """# 01 Generate DomainGuide Patch INTERNAL

You are generating INTERNAL design artifacts from answered P4/P5 Development Themes.

## Input
Read `input/ANSWERED_THEMES.json`.

## Rules
- Do not invent customer decisions.
- Use only answered content, theme source, P3 context, and explicit customization definitions.
- Treat P3 context as `reference_only` unless the customer answer explicitly approves it.
- Preserve `development_theme_key` and `decision_key`.
- Record out_of_scope, human_review_required, and unresolved/open items.

## Output
Create `outputs/DOMAIN_GUIDE_PATCH_INTERNAL.md`.

For each theme, include:
- Theme summary
- Confirmed customer decisions
- Implementation judgement
- Standard model usage
- Custom model / logic candidates
- Safe implementation scope
- Out of scope
- Human review required
- Open questions / GAPs
""",
        "02_generate_ontology_delta.md": """# 02 Generate OntologyDelta INTERNAL

## Input
Read `input/ANSWERED_THEMES.json` and the DomainGuide output if already created.

## Rules
- Create business concepts and relationships only from explicit answers or high-confidence source seeds.
- Mark uncertain items as `candidate` or `needs_review`.
- Do not convert a P3 reference into an approved entity unless the answer approves it.
- Keep all keys stable.

## Output
Create `outputs/ONTOLOGY_DELTA_INTERNAL.json`.

Expected top-level shape:
```json
{
  "schema_name": "p4p5_ontology_delta_internal",
  "version": "v1",
  "project_key": "",
  "themes": [],
  "entities": [],
  "relationships": [],
  "business_rules": [],
  "open_questions": [],
  "validation": {}
}
```
""",
        "03_generate_neo4j_projection_source.md": """# 03 Generate Neo4j Projection Source INTERNAL

## Input
Read `outputs/ONTOLOGY_DELTA_INTERNAL.json` and `input/ANSWERED_THEMES.json`.

## Rules
- Neo4j is a projection/validation layer, not the source of truth.
- Keep source references to `development_theme_key`, `decision_key`, P3 node refs, and answered pack import IDs.
- Do not apply to Neo4j here. Only generate source payload.

## Output
Create `outputs/NEO4J_PROJECTION_SOURCE_INTERNAL.json`.

Expected shape:
```json
{
  "schema_name": "p4p5_neo4j_projection_source_internal",
  "version": "v1",
  "nodes": [],
  "relationships": [],
  "cypher_preview": [],
  "validation": {}
}
```
""",
        "04_generate_odoo_codegen_input.md": """# 04 Generate Odoo Codegen Input INTERNAL

## Input
Read `input/ANSWERED_THEMES.json`, DomainGuide Patch, and OntologyDelta.

## Rules
- Do not overwrite standard Odoo core logic.
- Generate implementation input only for approved/answered scope.
- Keep unclear items in GAP/open questions.
- Prefer support masters, overlays, buttons, warnings, reports, and standard document integration.
- Do not auto-confirm ambiguous remapping.

## Output
Create `outputs/ODOO_CODEGEN_INPUT_INTERNAL.json`.

Expected shape:
```json
{
  "schema_name": "p4p5_odoo_codegen_input_internal",
  "version": "v1",
  "addons": [],
  "models": [],
  "fields": [],
  "views": [],
  "buttons": [],
  "reports": [],
  "safe_scope": [],
  "out_of_scope": [],
  "open_questions": []
}
```
""",
        "05_validate_internal_outputs.md": """# 05 Validate Internal Outputs

Validate the generated internal artifacts before importing them into P5.

Check:
- All theme keys exist in `input/ANSWERED_THEMES.json`.
- All decision keys either map to customer answers or are marked as source seed/candidate.
- P3 references remain `reference_only` unless approved in answers.
- No unresolved item is silently treated as approved.
- Odoo implementation input does not overwrite standard core logic.
- DomainGuide, OntologyDelta, Neo4j projection, and Odoo input are mutually consistent.

Create `outputs/VALIDATION_REPORT_INTERNAL.md`.
""",
        "06_odoo_incremental_codegen_start.md": """# 06 Odoo Incremental Codegen START INTERNAL

Use this prompt when you want ChatGPT to generate Odoo implementation material gradually, app by app, from `outputs/ODOO_CODEGEN_INPUT_INTERNAL.json`.

## Goal

Create an incremental Odoo build plan and the first app-level codegen package. Do not try to generate all apps at once.

## Inputs

Read these files first:

- `input/ANSWERED_THEMES.json`
- `outputs/DOMAIN_GUIDE_PATCH_INTERNAL.md`
- `outputs/ONTOLOGY_DELTA_INTERNAL.json`
- `outputs/ODOO_CODEGEN_INPUT_INTERNAL.json`
- `outputs/VALIDATION_REPORT_INTERNAL.md`

## Critical rules

- Generate Odoo implementation material app by app.
- Start with the first app that has answered and validated themes.
- Do not overwrite standard Odoo core logic.
- Prefer additive implementation: support masters, overlay fields, buttons, warnings, lists, menus, reports, and safe validation helpers.
- Do not implement unclear, unanswered, or unapproved decisions. Keep them as GAP/open questions.
- P3 context remains reference-only unless the customer answer explicitly approved it.
- Keep `development_theme_key`, `decision_key`, and source references in every generated item.
- Target Odoo version must be taken from the project/repository context if available. If absent, keep version as `to_be_confirmed` instead of guessing.
- Do not write instructions that require selecting or changing a specific ChatGPT/Codex model.

## Output

Create:

- `outputs/odoo_incremental/ODOO_INCREMENTAL_BUILD_PLAN.md`
- `outputs/odoo_incremental/ODOO_INCREMENTAL_BUILD_PLAN.json`
- `outputs/odoo_incremental/<app_key>/APP_CODEGEN_INPUT.json`
- `outputs/odoo_incremental/<app_key>/APP_BUILD_PROMPT.md`
- `outputs/odoo_incremental/<app_key>/APP_VALIDATION_CHECKLIST.md`

## Build plan content

The build plan must include:

1. app processing order
2. answered theme keys per app
3. skipped/unanswered themes
4. shared/common models that must not be duplicated
5. safe implementation scope
6. out-of-scope items
7. dependencies between apps
8. next app to generate when the user says `next`

## First app package

For the first app only, create an app-level implementation package with:

- app key and app label
- included theme keys
- models to create or extend
- fields to add
- menus/views/buttons/lists/reports to add
- warnings/validation helpers
- demo data or master seed candidates if approved
- explicit GAP/open questions
- implementation notes
- validation checklist

Do not produce final Odoo addon source for every app in this step.
""",
        "07_odoo_incremental_codegen_next.md": """# 07 Odoo Incremental Codegen NEXT INTERNAL

Use this prompt after `06_odoo_incremental_codegen_start.md`.

When the user sends `next`, generate the next app-level Odoo implementation package based on the build plan.

## Inputs

Read:

- `outputs/odoo_incremental/ODOO_INCREMENTAL_BUILD_PLAN.json`
- previously generated `outputs/odoo_incremental/<app_key>/APP_CODEGEN_INPUT.json` files
- `outputs/ODOO_CODEGEN_INPUT_INTERNAL.json`
- `outputs/VALIDATION_REPORT_INTERNAL.md`

## Rules

- Generate only one app per response.
- Do not regenerate apps already completed unless the user explicitly asks.
- Reuse shared/common models instead of duplicating them.
- If a model, field, or button is ambiguous, put it in GAP/open questions.
- Keep implementation additive and safe.
- Preserve all source references.
- At the end, state which app was generated and which app is next.

## Output for each app

Create or update:

- `outputs/odoo_incremental/<app_key>/APP_CODEGEN_INPUT.json`
- `outputs/odoo_incremental/<app_key>/APP_BUILD_PROMPT.md`
- `outputs/odoo_incremental/<app_key>/APP_VALIDATION_CHECKLIST.md`

The app package should be ready to pass to an Odoo addon generation step, but should not silently implement unapproved decisions.
""",
        "08_odoo_incremental_codegen_merge_validate.md": """# 08 Odoo Incremental Codegen MERGE / VALIDATE INTERNAL

Use this prompt after all app-level Odoo implementation packages have been generated.

## Goal

Merge app-level Odoo codegen inputs into one final Odoo build pack input, while checking duplicates, shared models, conflicting fields, and unresolved GAPs.

## Inputs

Read:

- all `outputs/odoo_incremental/<app_key>/APP_CODEGEN_INPUT.json` files
- all `outputs/odoo_incremental/<app_key>/APP_VALIDATION_CHECKLIST.md` files
- `outputs/ODOO_CODEGEN_INPUT_INTERNAL.json`
- `outputs/VALIDATION_REPORT_INTERNAL.md`

## Checks

- duplicate technical model names
- duplicate field names on the same Odoo model
- duplicate menu/action XML IDs
- shared/common master duplication
- standard Odoo core overwrite attempts
- unapproved P3 references
- unapproved customer decisions
- unresolved GAPs mistakenly included as implementation

## Output

Create:

- `outputs/odoo_incremental/ODOO_FINAL_BUILD_INPUT.json`
- `outputs/odoo_incremental/ODOO_FINAL_BUILD_PROMPT.md`
- `outputs/odoo_incremental/ODOO_FINAL_BUILD_VALIDATION_REPORT.md`

The final build input should be suitable for a later Odoo addon generation step.
Do not apply to Odoo directly in this step.
""",
    }


def _p4p5_read_answer_template_for_record(import_id: str, record: dict[str, Any]) -> dict[str, Any]:
    rel = record.get("answer_template_path")
    if not rel:
        return {}
    path = _p4p5_import_dir(import_id) / str(rel)
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _p4p5_read_customization_definition_for_record(import_id: str, record: dict[str, Any]) -> dict[str, Any] | None:
    rel = record.get("answer_template_path")
    if not rel:
        return None
    base = (_p4p5_import_dir(import_id) / str(rel)).parent
    for name in ["CUSTOMIZATION_DEFINITION.json", "CUSTOMIZATION_DEFINITION_V1.json"]:
        path = base / name
        if path.exists():
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                return obj if isinstance(obj, dict) else None
            except Exception:
                return None
    return None


def _p4p5_build_internal_design_export_data(
    import_id: str,
    *,
    app_key: str | None = None,
    theme_key: str | None = None,
    include_unanswered: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    catalog, validation, summary = _p4p5_load_import(import_id)
    answer_status = _p4p5_load_answer_status(import_id)
    answer_themes = answer_status.get("themes") if isinstance(answer_status.get("themes"), dict) else {}
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for theme in _p4p5_theme_list(catalog):
        key = _p4p5_theme_key(theme)
        if theme_key and key != theme_key:
            continue
        if app_key and _p4p5_theme_app(theme) != app_key and app_key not in ((theme.get("classification") or {}).get("target_apps") or []):
            continue

        record = answer_themes.get(key)
        answered = isinstance(record, dict) and record.get("answer_status") in ("answered", "answered_with_definition")
        if not answered and not include_unanswered:
            skipped.append({"development_theme_key": key, "reason": "not_answered"})
            continue

        record = record if isinstance(record, dict) else {}
        answer_template = _p4p5_read_answer_template_for_record(import_id, record) if record else {}
        customization_definition = _p4p5_read_customization_definition_for_record(import_id, record) if record else None

        rows.append({
            "development_theme_key": key,
            "app_key": _p4p5_theme_app(theme),
            "title_ja": _p4p5_theme_title(theme),
            "answer_record": record,
            "answer_template": answer_template,
            "customization_definition": customization_definition,
            "theme_source": theme,
            "p3_context_refs": theme.get("p3_context_refs") if isinstance(theme.get("p3_context_refs"), dict) else {},
            "odoo_mapping_seed": theme.get("odoo_mapping_seed") if isinstance(theme.get("odoo_mapping_seed"), dict) else {},
            "semantic_mapping_seed": theme.get("semantic_mapping_seed") if isinstance(theme.get("semantic_mapping_seed"), dict) else {},
            "governance_seed": theme.get("governance_seed") if isinstance(theme.get("governance_seed"), dict) else {},
        })

    meta = {
        "schema_name": "p4p5_internal_design_export_selection",
        "version": "v1",
        "import_id": import_id,
        "generated_at": _now_iso(),
        "app_key": app_key,
        "theme_key": theme_key,
        "include_unanswered": include_unanswered,
        "selected_theme_count": len(rows),
        "skipped": skipped,
        "catalog_summary": summary,
        "catalog_validation": validation,
        "answer_status_summary": {
            "answered_theme_count": answer_status.get("answered_theme_count", 0),
        },
    }
    return rows, meta


def _p4p5_export_internal_design_pack(
    import_id: str,
    *,
    app_key: str | None = None,
    theme_key: str | None = None,
    include_unanswered: bool = False,
) -> dict[str, Any]:
    rows, selection_meta = _p4p5_build_internal_design_export_data(
        import_id,
        app_key=app_key,
        theme_key=theme_key,
        include_unanswered=include_unanswered,
    )
    if not rows:
        raise HTTPException(status_code=400, detail="回答済みThemeがありません。回答済みPACKをImportしてからInternal Design PackをExportしてください。")

    pack_id = f"internal_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    out_dir = _p4p5_internal_design_pack_dir(import_id) / pack_id
    input_dir = out_dir / "input"
    prompt_dir = out_dir / "prompts"
    expected_dir = out_dir / "outputs_expected"
    output_dir = out_dir / "outputs"
    for d in [input_dir, prompt_dir, expected_dir, output_dir]:
        d.mkdir(parents=True, exist_ok=True)

    answer_summary = {
        "schema_name": "p4p5_internal_design_answer_summary",
        "version": "v1",
        "import_id": import_id,
        "pack_id": pack_id,
        "generated_at": _now_iso(),
        "theme_count": len(rows),
        "themes": [
            {
                "development_theme_key": r["development_theme_key"],
                "app_key": r["app_key"],
                "title_ja": r["title_ja"],
                "answer_status": (r.get("answer_record") or {}).get("answer_status"),
                "answered_count": (r.get("answer_record") or {}).get("answered_count"),
                "answer_total_count": (r.get("answer_record") or {}).get("answer_total_count"),
                "has_customization_definition": bool(r.get("customization_definition")),
                "customization_title_ja": (r.get("answer_record") or {}).get("customization_title_ja", ""),
            }
            for r in rows
        ],
    }
    theme_snapshot = {
        "schema_name": "p4p5_internal_design_theme_source_snapshot",
        "version": "v1",
        "themes": [{"development_theme_key": r["development_theme_key"], "theme_source": r["theme_source"]} for r in rows],
    }
    p3_snapshot = {
        "schema_name": "p4p5_internal_design_p3_context_snapshot",
        "version": "v1",
        "p3_usage_policy": "reference_only",
        "themes": [{"development_theme_key": r["development_theme_key"], "p3_context_refs": r.get("p3_context_refs") or {}} for r in rows],
    }
    answered_themes = {
        "schema_name": "p4p5_internal_design_answered_themes",
        "version": "v1",
        "project_key": (_p4p5_load_import(import_id)[0]).get("project_key"),
        "import_id": import_id,
        "pack_id": pack_id,
        "generated_at": _now_iso(),
        "theme_count": len(rows),
        "themes": rows,
    }

    _safe_json_dump(input_dir / "ANSWERED_THEMES.json", answered_themes)
    _safe_json_dump(input_dir / "ANSWER_SUMMARY.json", answer_summary)
    _safe_json_dump(input_dir / "THEME_SOURCE_SNAPSHOT.json", theme_snapshot)
    _safe_json_dump(input_dir / "P3_CONTEXT_SNAPSHOT.json", p3_snapshot)
    _safe_json_dump(input_dir / "EXPORT_SELECTION.json", selection_meta)

    for name, content in _p4p5_internal_prompt_files().items():
        (prompt_dir / name).write_text(content, encoding="utf-8")

    (expected_dir / "DOMAIN_GUIDE_PATCH_INTERNAL.md").write_text("# DOMAIN_GUIDE_PATCH_INTERNAL\n\nGenerated by ChatGPT in the internal execution step.\n", encoding="utf-8")
    _safe_json_dump(expected_dir / "ONTOLOGY_DELTA_INTERNAL.json", {"schema_name": "p4p5_ontology_delta_internal", "version": "v1", "themes": [], "entities": [], "relationships": [], "business_rules": [], "open_questions": [], "validation": {}})
    _safe_json_dump(expected_dir / "NEO4J_PROJECTION_SOURCE_INTERNAL.json", {"schema_name": "p4p5_neo4j_projection_source_internal", "version": "v1", "nodes": [], "relationships": [], "cypher_preview": [], "validation": {}})
    _safe_json_dump(expected_dir / "ODOO_CODEGEN_INPUT_INTERNAL.json", {"schema_name": "p4p5_odoo_codegen_input_internal", "version": "v1", "addons": [], "models": [], "fields": [], "views": [], "buttons": [], "reports": [], "safe_scope": [], "out_of_scope": [], "open_questions": {}})
    (expected_dir / "VALIDATION_REPORT_INTERNAL.md").write_text("# VALIDATION_REPORT_INTERNAL\n\nGenerated by ChatGPT after checking all internal outputs.\n", encoding="utf-8")
    (expected_dir / "ODOO_INCREMENTAL_CODEGEN_README.md").write_text("# ODOO_INCREMENTAL_CODEGEN_README\n\nUse prompts 06-08 to generate app-by-app Odoo codegen inputs under outputs/odoo_incremental/.\n", encoding="utf-8")
    (expected_dir / "odoo_incremental").mkdir(parents=True, exist_ok=True)
    (expected_dir / "odoo_incremental" / "README.md").write_text("# odoo_incremental\n\nExpected location for app-by-app Odoo codegen outputs created by prompts 06-08.\n", encoding="utf-8")

    start_here = """# START HERE - P4/P5 Internal Design Generation

This is an internal pack generated from answered P4/P5 Customer Decision Themes.

## How to use

1. Read `input/ANSWERED_THEMES.json`.
2. Run prompts in `prompts/` in numeric order.
3. Create the files listed in `outputs_expected/` under `outputs/`.
4. Run prompts 06-08 only when you want to prepare app-by-app Odoo codegen input.
5. Do not apply to Neo4j or Odoo in this step.
6. Keep unresolved or ambiguous items as GAP/open questions.

## Important

- Customer answers are the primary decision input.
- P3 context is `reference_only` unless explicitly approved by customer answers.
- Do not silently approve uncertain mappings.
- Keep `development_theme_key` and `decision_key` stable.
"""
    (out_dir / "START_HERE_FOR_CHATGPT.md").write_text(start_here, encoding="utf-8")

    readme = f"""# P4/P5 Internal Design Export Pack

Generated at: {selection_meta['generated_at']}
Import ID: {import_id}
Pack ID: {pack_id}
Theme count: {len(rows)}

This pack is for internal design generation with ChatGPT.

## Main input

- `input/ANSWERED_THEMES.json`
- `input/ANSWER_SUMMARY.json`
- `input/THEME_SOURCE_SNAPSHOT.json`
- `input/P3_CONTEXT_SNAPSHOT.json`

## Prompts

Run prompts 01-05 to create internal design artifacts.
Run prompts 06-08 if you want to create app-by-app Odoo codegen inputs gradually.

## Outputs to create

- `outputs/DOMAIN_GUIDE_PATCH_INTERNAL.md`
- `outputs/ONTOLOGY_DELTA_INTERNAL.json`
- `outputs/NEO4J_PROJECTION_SOURCE_INTERNAL.json`
- `outputs/ODOO_CODEGEN_INPUT_INTERNAL.json`
- `outputs/VALIDATION_REPORT_INTERNAL.md`
"""
    (out_dir / "README_INTERNAL.md").write_text(readme, encoding="utf-8")

    manifest = {
        "schema_name": "p4p5_internal_design_export_manifest",
        "version": "v1",
        "import_id": import_id,
        "pack_id": pack_id,
        "generated_at": selection_meta["generated_at"],
        "theme_count": len(rows),
        "files": [
            "README_INTERNAL.md",
            "START_HERE_FOR_CHATGPT.md",
            "MANIFEST.json",
            "input/ANSWERED_THEMES.json",
            "input/ANSWER_SUMMARY.json",
            "input/THEME_SOURCE_SNAPSHOT.json",
            "input/P3_CONTEXT_SNAPSHOT.json",
            "input/EXPORT_SELECTION.json",
            "prompts/01_generate_domain_guide_patch.md",
            "prompts/02_generate_ontology_delta.md",
            "prompts/03_generate_neo4j_projection_source.md",
            "prompts/04_generate_odoo_codegen_input.md",
            "prompts/05_validate_internal_outputs.md",
            "prompts/06_odoo_incremental_codegen_start.md",
            "prompts/07_odoo_incremental_codegen_next.md",
            "prompts/08_odoo_incremental_codegen_merge_validate.md",
            "outputs_expected/ODOO_INCREMENTAL_CODEGEN_README.md",
            "outputs_expected/odoo_incremental/README.md",
        ],
        "themes": answer_summary["themes"],
    }
    _safe_json_dump(out_dir / "MANIFEST.json", manifest)

    zip_path = out_dir.with_suffix(".zip")
    _zip_dir(out_dir, zip_path)

    # Mark exported themes so the UI can show status.
    internal_status = _p4p5_load_internal_design_export_status(import_id)
    theme_status = internal_status.get("themes") if isinstance(internal_status.get("themes"), dict) else {}
    now = _now_iso()
    for row in rows:
        theme_status[row["development_theme_key"]] = {
            "development_theme_key": row["development_theme_key"],
            "internal_design_export_status": "exported",
            "internal_design_pack_id": pack_id,
            "internal_design_exported_at": now,
            "app_key": row["app_key"],
            "title_ja": row["title_ja"],
        }
    internal_status["themes"] = theme_status
    exports = internal_status.get("exports") if isinstance(internal_status.get("exports"), list) else []
    exports.insert(0, {
        "pack_id": pack_id,
        "import_id": import_id,
        "generated_at": now,
        "theme_count": len(rows),
        "app_key": app_key,
        "theme_key": theme_key,
        "include_unanswered": include_unanswered,
        "download_url": f"/p4p5/imports/{import_id}/internal-design-packs/{pack_id}.zip",
    })
    internal_status["exports"] = exports[:50]
    _p4p5_save_internal_design_export_status(import_id, internal_status)

    result = {
        "schema_name": "p4p5_internal_design_export_result",
        "version": "v1",
        "status": "exported",
        "import_id": import_id,
        "pack_id": pack_id,
        "theme_count": len(rows),
        "generated_at": now,
        "download_url": f"/p4p5/imports/{import_id}/internal-design-packs/{pack_id}.zip",
        "zip_path": str(zip_path),
        "themes": answer_summary["themes"],
    }
    _safe_json_dump(out_dir / "EXPORT_RESULT.json", result)
    return result



def _p4p5_extract_answer_templates(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    result: list[tuple[Path, dict[str, Any]]] = []
    for p in root.rglob("ANSWER_TEMPLATE.json"):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("development_theme_key"):
            result.append((p, obj))
    if not result:
        for p in root.rglob("*.json"):
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("schema_name") == "p4p5_customer_decision_answer_template" and obj.get("development_theme_key"):
                result.append((p, obj))
    return result


def _p4p5_answered_count(answer_template: dict[str, Any]) -> tuple[int, int]:
    answers = answer_template.get("answers") if isinstance(answer_template.get("answers"), list) else []
    total = len([a for a in answers if isinstance(a, dict)])
    answered = len([
        a for a in answers
        if isinstance(a, dict)
        and (
            a.get("customer_answer") not in (None, "", "not_answered")
            or str(a.get("customer_comment") or "").strip()
        )
    ])
    return answered, total


def _p4p5_find_customization_definition(template_path: Path) -> dict[str, Any] | None:
    candidates = [
        template_path.parent / "CUSTOMIZATION_DEFINITION.json",
        template_path.parent / "CUSTOMIZATION_DEFINITION_V1.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
    return None


def _p4p5_answer_import_summary(import_id: str, answer_import_id: str, upload_filename: str, extracted_dir: Path) -> dict[str, Any]:
    templates = _p4p5_extract_answer_templates(extracted_dir)
    catalog, _validation, _summary = _p4p5_load_import(import_id)
    known_keys = {_p4p5_theme_key(t) for t in _p4p5_theme_list(catalog)}
    status = _p4p5_load_answer_status(import_id)
    theme_status = status.get("themes") if isinstance(status.get("themes"), dict) else {}
    imported_themes: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    now = _now_iso()

    if not templates:
        raise HTTPException(status_code=400, detail="ANSWER_TEMPLATE.json が見つかりません。回答済みPACK ZIPを指定してください。")

    for path, template in templates:
        theme_key = str(template.get("development_theme_key") or "")
        if not theme_key:
            issues.append({"severity": "error", "code": "MISSING_THEME_KEY", "path": str(path)})
            continue
        if known_keys and theme_key not in known_keys:
            issues.append({"severity": "warning", "code": "UNKNOWN_THEME_KEY", "theme_key": theme_key, "path": str(path)})
        answered, total = _p4p5_answered_count(template)
        definition = _p4p5_find_customization_definition(path)
        definition_title = ""
        definition_status = ""
        if isinstance(definition, dict):
            definition_title = str(definition.get("title_ja") or definition.get("display_name_ja") or definition.get("name") or "")
            definition_status = str(definition.get("definition_status") or "definition_present")

        answer_status = "answered_with_definition" if definition else ("answered" if answered > 0 or template.get("answer_status") == "answered" else "not_answered")
        target_dir = _p4p5_answered_pack_dir(import_id) / answer_import_id / re.sub(r"[^A-Za-z0-9_.-]+", "_", theme_key)
        target_dir.mkdir(parents=True, exist_ok=True)
        _safe_json_dump(target_dir / "ANSWER_TEMPLATE.json", template)
        if definition:
            _safe_json_dump(target_dir / "CUSTOMIZATION_DEFINITION.json", definition)

        record = {
            "development_theme_key": theme_key,
            "answer_status": answer_status,
            "answered_count": answered,
            "answer_total_count": total,
            "answered_ratio": (answered / total) if total else 0,
            "answer_import_id": answer_import_id,
            "answered_pack_filename": upload_filename,
            "imported_at": now,
            "answer_template_path": str((target_dir / "ANSWER_TEMPLATE.json").relative_to(_p4p5_import_dir(import_id))),
            "has_customization_definition": bool(definition),
            "customization_definition_status": definition_status,
            "customization_title_ja": definition_title,
        }
        theme_status[theme_key] = record
        imported_themes.append(record)

    status["themes"] = theme_status
    _p4p5_save_answer_status(import_id, status)

    summary = {
        "schema_name": "p4p5_customer_decision_answer_import_summary",
        "version": "v1",
        "import_id": import_id,
        "answer_import_id": answer_import_id,
        "filename": upload_filename,
        "imported_at": now,
        "status": "imported_with_warnings" if issues else "imported",
        "theme_count": len(imported_themes),
        "imported_themes": imported_themes,
        "issues": issues,
        "answered_theme_count": _p4p5_load_answer_status(import_id).get("answered_theme_count", 0),
    }
    _safe_json_dump(_p4p5_answered_pack_dir(import_id) / answer_import_id / "ANSWER_IMPORT_SUMMARY.json", summary)
    return summary


def _p4p5_dashboard(import_id: str, catalog: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    themes = _p4p5_theme_list(catalog)
    answer_status = _p4p5_load_answer_status(import_id)
    answer_themes = answer_status.get("themes") if isinstance(answer_status.get("themes"), dict) else {}
    internal_export_status = _p4p5_load_internal_design_export_status(import_id)
    internal_export_themes = internal_export_status.get("themes") if isinstance(internal_export_status.get("themes"), dict) else {}
    rows = []
    for theme in themes:
        cls = theme.get("classification") if isinstance(theme.get("classification"), dict) else {}
        gov = theme.get("governance_seed") if isinstance(theme.get("governance_seed"), dict) else {}
        ready = theme.get("readiness") if isinstance(theme.get("readiness"), dict) else {}
        seed = _p4p5_prompt_seed(theme)
        rows.append({
            "development_theme_key": _p4p5_theme_key(theme),
            "title_ja": _p4p5_theme_title(theme),
            "summary_ja": _p4p5_theme_summary(theme),
            "app_key": _p4p5_theme_app(theme),
            "target_apps": cls.get("target_apps") or [],
            "business_domain": cls.get("business_domain") or "",
            "process_stage": cls.get("process_stage") or "",
            "implementation_pattern": cls.get("implementation_pattern") or "",
            "risk_level": gov.get("risk_level") or "",
            "codegen_readiness": ready.get("codegen_readiness") or "",
            "customer_answer_status": (answer_themes.get(_p4p5_theme_key(theme)) or {}).get("answer_status") or ready.get("customer_answer_status") or "not_answered",
            "answered_count": (answer_themes.get(_p4p5_theme_key(theme)) or {}).get("answered_count", 0),
            "answer_total_count": (answer_themes.get(_p4p5_theme_key(theme)) or {}).get("answer_total_count", 0),
            "answer_import_id": (answer_themes.get(_p4p5_theme_key(theme)) or {}).get("answer_import_id", ""),
            "answered_pack_filename": (answer_themes.get(_p4p5_theme_key(theme)) or {}).get("answered_pack_filename", ""),
            "answered_at": (answer_themes.get(_p4p5_theme_key(theme)) or {}).get("imported_at", ""),
            "has_customization_definition": bool((answer_themes.get(_p4p5_theme_key(theme)) or {}).get("has_customization_definition")),
            "customization_title_ja": (answer_themes.get(_p4p5_theme_key(theme)) or {}).get("customization_title_ja", ""),
            "internal_design_export_status": (internal_export_themes.get(_p4p5_theme_key(theme)) or {}).get("internal_design_export_status", ""),
            "internal_design_pack_id": (internal_export_themes.get(_p4p5_theme_key(theme)) or {}).get("internal_design_pack_id", ""),
            "internal_design_exported_at": (internal_export_themes.get(_p4p5_theme_key(theme)) or {}).get("internal_design_exported_at", ""),
            "pack_export_readiness": ready.get("pack_export_readiness") or "",
            "question_count": _p4p5_question_count(theme),
            "scenario_count": _p4p5_scenario_count(theme),
            "hypothesis_item_count": _p4p5_hypothesis_item_count(theme),
            "question_seed_style": seed.get("style") or "",
            "p3_usage_policy": (theme.get("p3_context_refs") or {}).get("p3_usage_policy") if isinstance(theme.get("p3_context_refs"), dict) else "",
            "p3_context_counts": _p4p5_context_counts(theme),
        })
    return {
        "import_id": import_id,
        "generated_at": _now_iso(),
        "project_key": catalog.get("project_key"),
        "schema_name": catalog.get("schema_name"),
        "status": validation.get("status"),
        "summary": {
            "theme_count": validation.get("theme_count", 0),
            "app_count": validation.get("app_count", 0),
            "total_question_count": validation.get("total_question_count", 0),
            "total_hypothesis_item_count": validation.get("total_hypothesis_item_count", 0),
            "total_scenario_count": validation.get("total_scenario_count", 0),
            "p3_context_theme_count": validation.get("p3_context_theme_count", 0),
            "customer_pack_ready_theme_count": validation.get("customer_pack_ready_theme_count", 0),
            "answered_theme_count": answer_status.get("answered_theme_count", 0),
            "internal_exported_theme_count": internal_export_status.get("exported_theme_count", 0),
            "error_count": validation.get("error_count", 0),
            "warning_count": validation.get("warning_count", 0),
        },
        "app_counts": validation.get("app_counts", {}),
        "themes": rows,
    }


def _p4p5_load_import(import_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    base = _p4p5_import_dir(import_id)
    if not base.exists():
        raise HTTPException(status_code=404, detail="P4/P5 import not found")
    catalog = _load_json_path(_p4p5_catalog_path(import_id))
    validation = _load_json_path(_p4p5_validation_path(import_id)) if _p4p5_validation_path(import_id).exists() else _p4p5_validate_catalog(catalog)
    summary = _load_json_path(_p4p5_summary_path(import_id)) if _p4p5_summary_path(import_id).exists() else {}
    return catalog, validation, summary


def _p4p5_latest_import_id() -> str | None:
    candidates = []
    for p in P4P5_ROOT.glob("p4p5_*/IMPORT_SUMMARY.json"):
        candidates.append((p.stat().st_mtime, p.parent.name))
    return sorted(candidates, reverse=True)[0][1] if candidates else None


def _p4p5_format_list(values: Any, *, max_items: int = 20) -> list[str]:
    if not isinstance(values, list) or not values:
        return ["- なし"]
    lines: list[str] = []
    for v in values[:max_items]:
        if isinstance(v, dict):
            name = v.get("label_ja") or v.get("model") or v.get("logic_key") or v.get("report_key") or v.get("key") or v.get("node_key") or json.dumps(v, ensure_ascii=False)
            desc = v.get("usage_ja") or v.get("role_ja") or v.get("purpose_ja") or v.get("title_ja") or v.get("confidence") or ""
            lines.append(f"- `{name}` {desc}".rstrip())
        else:
            lines.append(f"- `{v}`")
    if len(values) > max_items:
        lines.append(f"- ... 他 {len(values) - max_items} 件")
    return lines


P4P5_CUSTOMER_FACING_TERM_REPLACEMENTS = {
    "Odoo実装": "業務システム側の対応",
    "DomainGuide": "社内の開発整理用データ",
    "OntologyDelta": "社内の開発整理用データ",
    "Cypher Projection": "社内の開発整理用データ",
    "Odoo Codegen Input": "社内の開発整理用データ",
    "Theme Resolution": "社内の開発整理用データ",
}


def _p4p5_sanitize_customer_facing_text(value: str) -> str:
    result = value
    for term, replacement in P4P5_CUSTOMER_FACING_TERM_REPLACEMENTS.items():
        result = result.replace(term, replacement)
    return result


def _p4p5_sanitize_customer_facing_obj(value: Any) -> Any:
    if isinstance(value, str):
        return _p4p5_sanitize_customer_facing_text(value)
    if isinstance(value, list):
        return [_p4p5_sanitize_customer_facing_obj(v) for v in value]
    if isinstance(value, dict):
        return {k: _p4p5_sanitize_customer_facing_obj(v) for k, v in value.items()}
    return value


def _p4p5_pack_markdown(theme: dict[str, Any]) -> str:
    key = _p4p5_theme_key(theme)
    title = _p4p5_theme_title(theme)
    summary = _p4p5_theme_summary(theme)
    cls = theme.get("classification") if isinstance(theme.get("classification"), dict) else {}
    odoo = theme.get("odoo_mapping_seed") if isinstance(theme.get("odoo_mapping_seed"), dict) else {}
    p3 = theme.get("p3_context_refs") if isinstance(theme.get("p3_context_refs"), dict) else {}
    seed = _p4p5_prompt_seed(theme)
    policy = seed.get("answer_policy") if isinstance(seed.get("answer_policy"), dict) else {}
    answer_options = policy.get("answer_options_default") if isinstance(policy.get("answer_options_default"), list) else ["合っている", "違う", "追加あり", "対象外", "要確認", "不明"]
    scenarios = seed.get("scenario_seed") if isinstance(seed.get("scenario_seed"), list) else []
    blocks = _p4p5_question_blocks(theme)

    lines: list[str] = []
    lines += [
        f"# {seed.get('pack_title_ja') or title}",
        "",
        "## 0. このPACKの目的",
        "",
        "このPACKは、P4/P5高カスタマイズ候補について、顧客・業務担当者・キーユーザーが不足情報を回答するための確認PACKです。",
        "ChatGPTやシステムが回答を自動補完してはいけません。各項目は、顧客回答後に社内の開発整理用データへ変換されます。",
        "",
        "## 1. 今回のテーマ",
        "",
        f"- development_theme_key: `{key}`",
        f"- テーマ: {title}",
        f"- 概要: {summary or '-'}",
        f"- 対象候補アプリ: {', '.join(map(str, cls.get('target_apps') or [])) or '-'}",
        f"- 業務領域: {cls.get('business_domain') or '-'}",
        f"- 工程: {cls.get('process_stage') or '-'}",
        f"- 実装パターン: {cls.get('implementation_pattern') or '-'}",
        "",
        "## 2. 業務シナリオ",
        "",
        "以下は回答者が業務イメージを持つための仮説シナリオです。確定仕様ではありません。違う場合は回答欄で修正してください。",
        "",
    ]
    if scenarios:
        for s in scenarios:
            if not isinstance(s, dict):
                continue
            lines += [f"### {s.get('title_ja') or s.get('scenario_key') or 'シナリオ'}", "", str(s.get("body_ja") or ""), ""]
    else:
        lines += ["- シナリオ未設定", ""]

    lines += [
        "## 3. 回答方法",
        "",
        "各質問では、こちらの仮説に対して次の選択肢で回答してください。必要に応じて補足コメントも記入してください。",
        "",
        "| 選択肢 | 意味 |",
        "|---|---|",
    ]
    option_desc = {
        "合っている": "こちらの仮説で概ね問題ない。",
        "違う": "仮説が違うため、正しい内容をコメントに記入する。",
        "追加あり": "仮説に追加したい対象・条件・例外がある。",
        "対象外": "このThemeでは扱わない。",
        "要確認": "現時点では判断できないため、顧客/現場/管理者確認が必要。",
        "不明": "情報がなく判断できない。",
    }
    for opt in answer_options:
        lines.append(f"| {opt} | {option_desc.get(str(opt), '')} |")
    lines += ["", "## 4. 現時点の前提情報", ""]
    lines += ["### 4.1 Odoo標準モデル候補", ""] + _p4p5_format_list(odoo.get("standard_models")) + [""]
    lines += ["### 4.2 P4/P5カスタムモデル候補", ""] + _p4p5_format_list(odoo.get("custom_model_candidates")) + [""]
    lines += ["### 4.3 P4/P5ロジック候補", ""] + _p4p5_format_list(odoo.get("custom_logic_candidates")) + [""]
    lines += ["### 4.4 帳票・一覧候補", ""] + _p4p5_format_list(odoo.get("report_candidates")) + [""]
    lines += ["### 4.5 P3からの参考候補", "", "P3情報は `reference_only` です。自動採用せず、Q7〜Q10で使う/拡張/使わない/要確認を回答してください。", ""]
    lines += ["#### P3補助マスタ", ""] + _p4p5_format_list(p3.get("related_p3_support_master_keys") or p3.get("related_support_masters")) + [""]
    lines += ["#### P3フィールド", ""] + _p4p5_format_list(p3.get("related_p3_overlay_field_keys") or p3.get("related_overlay_fields")) + [""]
    lines += ["#### P3 GAP/保留", ""] + _p4p5_format_list(p3.get("related_p3_gap_keys") or p3.get("related_p3_gap_items")) + [""]

    lines += ["## 5. 確認質問 Q1〜Q10", ""]
    for block in blocks:
        qid = block.get("question_id") or block.get("question_group") or block.get("group_key") or "Q"
        title_ja = block.get("title_ja") or block.get("group_title_ja") or qid
        lines += [f"### {qid}. {title_ja}", ""]
        if block.get("decision_purpose_ja"):
            lines += ["#### この質問で決めたいこと", "", str(block.get("decision_purpose_ja")), ""]
        if block.get("question_background_ja"):
            lines += ["#### 質問背景", "", str(block.get("question_background_ja")), ""]
        if block.get("hypothesis_intro_ja"):
            lines += ["#### こちらの仮説", "", str(block.get("hypothesis_intro_ja")), ""]
        items = block.get("hypothesis_items") if isinstance(block.get("hypothesis_items"), list) else []
        if items:
            lines += ["| decision_key | 確認項目 | こちらの仮説 | 回答選択肢 | 顧客回答 | コメント |", "|---|---|---|---|---|---|"]
            for item in items:
                if not isinstance(item, dict):
                    continue
                decision_key = str(item.get("decision_key") or "")
                label = str(item.get("label_ja") or item.get("question_id") or "")
                hypo = str(item.get("hypothesis_ja") or "").replace("\n", "<br>")
                opts = " / ".join(map(str, item.get("answer_options") or answer_options))
                lines.append(f"| `{decision_key}` | {label} | {hypo} | {opts} |  |  |")
            lines.append("")
        if block.get("answer_hint_ja"):
            lines += ["#### 回答ヒント", "", str(block.get("answer_hint_ja")), ""]
        if block.get("answer_example_ja"):
            lines += ["#### 回答例", "", str(block.get("answer_example_ja")), ""]
        else:
            lines += ["#### 回答例", "", "例: 概ね合っている。ただし、対象外にしたい処理や追加したい帳票がある場合はコメントに記入してください。", ""]
        if block.get("answer_record_template_ja"):
            lines += ["#### 回答記録テンプレート", "", "```text", str(block.get("answer_record_template_ja")), "```", ""]
        else:
            lines += ["#### 回答記録テンプレート", "", "```text", f"{qid}回答:\n- 顧客回答:\n- 修正内容:\n- 追加対象:\n- 対象外:\n- 要確認/不明:", "```", ""]
        used_for = []
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("used_for"), list):
                used_for.extend(map(str, item.get("used_for") or []))
        used_for = sorted(set(used_for))
        if used_for:
            lines += ["#### この回答の扱い", "", "この回答は社内の開発整理・確認資料作成に使います。", ""]

    lines += [
        "## 6. 対象外・保留にしたい内容",
        "",
        "今回のThemeでは扱わないもの、後続フェーズに回すもの、標準機能で代替するものを記入してください。",
        "",
        "```text",
        "- 対象外:",
        "- 後続フェーズ:",
        "- 標準機能で代替:",
        "- 理由:",
        "```",
        "",
        "## 7. 回答後の扱い",
        "",
        "回答後、システムにImportして、社内の開発整理用データを作成します。",
        "このPACKに回答しただけではOdooコードは自動生成されません。",
        "",
        "## 8. 注意事項",
        "",
        "- 分からない項目は「不明」または「要確認」で構いません。",
        "- すべてを埋める必要はありません。",
        "- P3情報は参考情報であり、自動採用しません。",
        "- 顧客回答前にCodegen対象として確定しません。",
        "- 標準Odooロジックの直接上書きは原則対象外です。",
        "",
    ]
    return _p4p5_sanitize_customer_facing_text("\n".join(lines).strip() + "\n")


def _p4p5_answer_template_json(theme: dict[str, Any]) -> dict[str, Any]:
    key = _p4p5_theme_key(theme)
    seed = _p4p5_prompt_seed(theme)
    policy = seed.get("answer_policy") if isinstance(seed.get("answer_policy"), dict) else {}
    answers: list[dict[str, Any]] = []
    for block in _p4p5_question_blocks(theme):
        question_group = str(block.get("question_id") or block.get("question_group") or block.get("group_key") or "")
        items = block.get("hypothesis_items") if isinstance(block.get("hypothesis_items"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            answers.append({
                "question_group": question_group,
                "question_id": item.get("question_id") or f"{question_group}-{len(answers)+1:03d}",
                "decision_key": item.get("decision_key"),
                "import_target": item.get("import_target"),
                "label_ja": item.get("label_ja"),
                "hypothesis_ja": item.get("hypothesis_ja"),
                "answer_options": item.get("answer_options") or policy.get("answer_options_default") or ["合っている", "違う", "追加あり", "対象外", "要確認", "不明"],
                "free_text_required": bool(item.get("free_text_required")),
                "customer_answer": None,
                "customer_comment": "",
                "confirmed_by": "",
                "answered_at": "",
            })
    return _p4p5_sanitize_customer_facing_obj({
        "schema_name": "p4p5_customer_decision_answer_template",
        "version": "v2.1",
        "development_theme_key": key,
        "title_ja": _p4p5_theme_title(theme),
        "answer_mode": seed.get("answer_mode") or "customer_decision_input",
        "answer_status": policy.get("default_answer_status") or "not_answered",
        "do_not_autofill_customer_answers": True,
        "p3_usage_policy": (theme.get("p3_context_refs") or {}).get("p3_usage_policy") if isinstance(theme.get("p3_context_refs"), dict) else "reference_only",
        "answers": answers,
    })




def _p4p5_customer_interview_prompt_diagram_first() -> str:
    return """# P4/P5 Customer Decision PACK Interview Prompt - Diagram First

あなたは、Odoo導入・カスタマイズ検討における顧客ヒアリング担当です。

添付された **P4/P5 Customer Decision PACK** を読み込み、対象テーマについて、最初にテーマ概要・関係図・関係テーブル・関係フィールドを説明したうえで、Q1から順番に質問してください。

## 0. 最重要方針

このPACKは顧客回答用です。

以下を厳守してください。

- 顧客回答を自動補完しない
- 顧客回答を推測しない
- 「合っている」と勝手に判断しない
- P3情報を確定仕様として扱わない
- P3情報は `reference_only` として扱う
- P4/P5の顧客回答、詳細ロジック、最終Odoo反映結果を勝手に作らない
- Odoo Codegen、Ontology Delta、Cypher、DomainGuideの生成はまだ行わない
- 顧客回答が完了するまでは、後続開発用データを確定しない
- 不明・未確認・判断保留は、そのまま不明/要確認として扱う

## 1. 読み込む対象ファイル

添付ZIP内から、対象テーマに関係する以下を必ず読み込んでください。

- `PACK_INDEX.json`
- `packs/<development_theme_key>/THEME_SOURCE.json`
- `packs/<development_theme_key>/ANSWER_TEMPLATE.json`
- `packs/<development_theme_key>/ANSWER_PACK.md`
- `packs/<development_theme_key>/index.html`
- `packs/<development_theme_key>/tables/affected_tables.html`
- `packs/<development_theme_key>/tables/affected_tables.csv`
- `packs/<development_theme_key>/tables/affected_fields_by_table.html`
- `packs/<development_theme_key>/tables/affected_fields_by_table.csv`
- `packs/<development_theme_key>/diagrams/table_connections.svg`
- `packs/<development_theme_key>/diagrams/table_connections.dot`
- `packs/<development_theme_key>/diagrams/table_connections.mmd`
- `packs/<development_theme_key>/data/theme_graph_binding.json`
- `packs/<development_theme_key>/data/affected_structural_elements.json`
- `packs/<development_theme_key>/data/theme_structural_subset.json`

`table_connections.mmd` が存在する場合は、最初の説明内で Mermaid 図として表示してください。

`table_connections.mmd` が存在しない場合は、`theme_structural_subset.json`、`theme_graph_binding.json`、`affected_structural_elements.json`、または `table_connections.dot` を参考に、会話用の簡易Mermaid図を作成して表示してください。

ただし、推測で存在しないテーブル・フィールドを追加してはいけません。

## 2. 禁止する開始動作

別スレッド開始直後に、以下のような検証レポートだけを出して待機してはいけません。

- 静的整合性検証レポート
- Phase 1 インテーク完了報告
- データ構造検証だけの報告
- 「テスト回答を入力してください」という待機
- `ANSWER_TEMPLATE.json` の整合性チェックだけの報告

整合性チェックは内部的に行ってよいですが、ユーザーに最初に見せる主内容にしないでください。

最初に行うべきことは、**顧客が回答しやすいようにテーマを説明し、関係図を表示し、関係テーブル・フィールドを説明し、そのままQ1へ進むこと**です。

## 3. 最初の応答で必ず行うこと

最初の応答では、いきなりQ1だけを出さないでください。

必ず次の順序で出してください。

1. 対象テーマの確認
2. このPACKの目的
3. 今回の業務テーマの説明
4. 関係図の表示
5. 図の見方
6. 関係する主なテーブル/モデル
7. 関係する主なフィールド候補
8. P3参考情報の扱い
9. 回答方法
10. Q1の質問

## 4. 最初の一文

最初の一文は、必ず以下にしてください。

```markdown
読み込みました。まず、このテーマの概要と関係する図・表を確認してから、Q1に進みます。
```

## 5. 最初の応答テンプレート

最初の応答は、以下の構成にしてください。

```markdown
読み込みました。まず、このテーマの概要と関係する図・表を確認してから、Q1に進みます。

# <テーマ名> - 顧客確認を開始します

対象テーマ:

`<development_theme_key>`

## 1. この確認で決めたいこと

<テーマ概要を顧客向けに説明>

## 2. 関係する業務イメージ

<業務シナリオを短く説明>

## 3. 関係図

以下は、このテーマに関係する可能性があるテーブル/モデルの参考図です。
確定仕様ではなく、回答のための確認材料です。

```mermaid
<table_connections.mmd の内容をここに表示>
```

## 4. 図・表の見方

このPACKには、回答の前提として以下の確認資料が含まれています。

- テーマ概要: `index.html`
- 関係テーブル一覧: `tables/affected_tables.html`
- 関係フィールド一覧: `tables/affected_fields_by_table.html`
- テーブル同士のつながり図: `diagrams/table_connections.svg`
- Mermaid図コード: `diagrams/table_connections.mmd`

図では、今回のテーマで関係する可能性があるモデルだけを表示しています。
関係テーブル一覧では、Odoo標準モデル、P3カスタムマスタ候補、missing_model を確認できます。
関係フィールド一覧では、P3で候補になっているカスタムフィールドや、関係する標準リレーションフィールドを確認できます。

## 5. 関係する主なテーブル/モデル

<affected_tables または theme_structural_subset から主要テーブルを抜粋して説明>

## 6. 関係する主なフィールド候補

<affected_fields_by_table または affected_structural_elements から主要フィールドを抜粋して説明>

## 7. P3参考情報の扱い

P3情報は `reference_only` です。
ここで表示されるテーブル・フィールド・マスタは、自動採用されません。

Q7〜Q10で、使う・拡張する・使わない・対象外・要確認を判断します。

## 8. 回答方法

これからQ1〜Q10を1問ずつ確認します。
回答は選択肢だけでも、箇条書きでも構いません。

不明なものは「不明」、あとで確認するものは「要確認」として扱います。
こちらでは顧客回答を推測して自動補完しません。

---

# Q1. <Q1タイトル>

## 決めたいこと

<Q1の目的>

## 質問背景

<Q1の背景>

## こちらの仮説

<Q1の仮説>

## decision_key

`<Q1 decision_key>`

## 回答してください

以下の形式で大丈夫です。

```text
Q1
- 回答選択: 合っている / 違う / 追加あり / 対象外 / 要確認 / 不明
- 修正・追加内容:
- 対象外にするもの:
- 要確認・不明な点:
- 補足:
```
```

## 6. Mermaid図の表示ルール

`table_connections.mmd` がある場合は、その内容を優先して表示してください。

図は詳細すぎないようにしてください。表示ノードが多すぎる場合は、テーマに直接関係する主要モデルを本文で抜粋し、詳細はHTML/CSVを見るように案内してください。

Mermaid図の直後に、必ず以下の趣旨を説明してください。

```markdown
この図は、P3内部Bindingから機械的に抽出された参考図です。
確定仕様ではありません。
表示されたテーブル・フィールド・マスタを採用するかどうかは、Q7〜Q10で確認します。
missing_model は、未導入またはDB未存在の候補として扱い、勝手に別モデルへ置き換えません。
```

## 7. テーブル・フィールド説明の制限

- 長くなりすぎる場合は、主要なものだけを抜粋してください
- 全件一覧をチャットに貼り出さないでください
- 詳細はHTMLファイルを見る前提で案内してください
- 日本語名がある場合は日本語名を優先してください
- 技術名は必要な場合だけ括弧で補足してください
- missing_model は補完・置換せず、そのまま表示してください

## 8. Q1以降の進め方

Q1からQ10まで、必ず1問ずつ進めてください。

- 一度にQ1〜Q10をすべて聞かない
- 顧客回答を受けたら、その回答を短く確認する
- 回答内容が不足している場合だけ、追加確認をする
- 回答が十分なら次の質問へ進む
- 顧客が「次」と言った場合は次の未回答質問へ進む
- 顧客がまとめて複数回答した場合は、該当するQに整理してよい
- ただし、不足や曖昧さがある場合は勝手に埋めない

## 9. 回答形式

各質問では、以下の形式を提示してください。

```text
Qx
- 回答選択: 合っている / 違う / 追加あり / 対象外 / 要確認 / 不明
- 修正・追加内容:
- 対象外にするもの:
- 要確認・不明な点:
- 補足:
```

## 10. Qごとの扱い

`ANSWER_TEMPLATE.json` の `answers` 配列を正としてください。

各質問では以下を使ってください。

- `question_group`
- `question_id`
- `decision_key`
- `label_ja`
- `hypothesis_ja`
- `answer_options`
- `import_target`

`ANSWER_PACK.md` により詳しい説明がある場合は、それも参照してください。

## 11. P3関連質問の扱い

Q7〜Q10など、P3情報の採否に関係する質問では、必ず以下を明記してください。

```markdown
P3情報は参考情報です。ここで「使う」と回答しても、まだ最終実装確定ではありません。
後続工程で、採用・拡張・対象外・要確認として整理します。
```

## 12. 最終出力

Q1〜Q10の回答が完了したら、以下を出力してください。

1. 回答サマリー
2. 未確認・要確認一覧
3. 対象外一覧
4. P3情報の採否整理
5. 更新済み `ANSWER_TEMPLATE.json` 相当のJSON
6. 次工程に渡すための `CUSTOMER_DECISION_ANSWER_RESULT.json`

ただし、顧客回答が完了するまでは、最終JSONを確定出力しないでください。

## 13. 不足ファイルがある場合

必要ファイルが不足している場合は、不足ファイルを短く示してください。

ただし、軽微な不足でQ1開始が可能な場合は、以下のように案内してから開始してください。

```markdown
一部の参考資料は不足していますが、質問PACK本体は読み込めています。
図・表がない部分は、ANSWER_PACK.md と ANSWER_TEMPLATE.json を元に確認を進めます。
```
""".strip() + "\n"


def _p4p5_write_customer_interview_assets(out_dir: Path, index: dict[str, Any]) -> None:
    prompts_dir = out_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompts_dir / "P4P5_CUSTOMER_DECISION_PACK_INTERVIEW_PROMPT_DIAGRAM_FIRST.md"
    prompt_path.write_text(_p4p5_customer_interview_prompt_diagram_first(), encoding="utf-8")

    themes = index.get("themes") if isinstance(index.get("themes"), list) else []
    first_theme = themes[0] if themes and isinstance(themes[0], dict) else {}
    theme_key = str(first_theme.get("development_theme_key") or index.get("theme_key") or "")
    title = str(first_theme.get("title_ja") or "P4/P5 Customer Decision PACK")
    mmd_path = str(((first_theme.get("p3_materials") or {}).get("table_connections_mmd_path") or "packs/<theme_key>/diagrams/table_connections.mmd"))
    next_msg = f"""# NEXT_THREAD_START_MESSAGE

添付したZIPは **P4/P5 Customer Decision PACK** です。

まず `prompts/P4P5_CUSTOMER_DECISION_PACK_INTERVIEW_PROMPT_DIAGRAM_FIRST.md` を読み、その指示に従って開始してください。

対象テーマ:

**{title}**
`{theme_key}`

重要:

- 最初に静的検証レポートだけを出して待機しないでください。
- まずテーマ概要、関係図、関係テーブル、関係フィールドを説明してください。
- `diagrams/table_connections.mmd` をMermaid図として本文に表示してください。
- Mermaid図が見つからない場合だけ、JSON/DOTから簡易Mermaid図を作成してください。
- P3情報は `reference_only` です。顧客回答を自動補完しないでください。
- その後、Q1から1問ずつ確認してください。

確認すべき主なファイル:

- `PACK_INDEX.json`
- `packs/*/THEME_SOURCE.json`
- `packs/*/ANSWER_TEMPLATE.json`
- `packs/*/ANSWER_PACK.md`
- `packs/*/index.html`
- `packs/*/tables/affected_tables.html`
- `packs/*/tables/affected_fields_by_table.html`
- `{mmd_path}`
- `packs/*/diagrams/table_connections.svg`
- `packs/*/data/theme_graph_binding.json`
- `packs/*/data/affected_structural_elements.json`
- `packs/*/data/theme_structural_subset.json`
"""
    (out_dir / "NEXT_THREAD_START_MESSAGE.md").write_text(next_msg, encoding="utf-8")
    start_here = """# START_HERE

このZIPだけで、別スレッドの顧客確認を開始できます。

別スレッドでは次の一文を送ってください。

```text
NEXT_THREAD_START_MESSAGE.mdを見て始めてください。
```

このPACKには、顧客確認用プロンプト、回答テンプレート、P3 reference_onlyのテーマ部分ER図、Mermaid図コード、関係テーブル/フィールド一覧が同梱されています。
"""
    (out_dir / "START_HERE.md").write_text(start_here, encoding="utf-8")
    req = """# Mermaid Export Requirement

Customer Decision PACK export must include theme-level Mermaid diagram code at:

```text
packs/<development_theme_key>/diagrams/table_connections.mmd
```

The Mermaid diagram is generated mechanically from imported P3 Internal Binding / theme structural subset data.
It must not add inferred models or fields.
Missing models must remain marked as missing_model.
P3 materials are reference_only and do not approve implementation scope.
"""
    docs_dir = out_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "MERMAID_EXPORT_REQUIREMENT.md").write_text(req, encoding="utf-8")

def _p4p5_export_customer_pack(
    import_id: str,
    *,
    app_key: str | None = None,
    theme_key: str | None = None,
    p3_binding_import_id: str | None = None,
    include_p3_diagrams: bool = True,
) -> dict[str, Any]:
    catalog, validation, _summary = _p4p5_load_import(import_id)
    themes = _p4p5_theme_list(catalog)
    if theme_key:
        themes = [t for t in themes if _p4p5_theme_key(t) == theme_key]
    if app_key:
        themes = [t for t in themes if _p4p5_theme_app(t) == app_key]
    if not themes:
        raise HTTPException(status_code=404, detail="No matching P4/P5 themes for customer decision pack export")

    pack_id = f"pack_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    out_dir = _p4p5_pack_dir(import_id) / pack_id
    out_dir.mkdir(parents=True, exist_ok=True)
    packs_dir = out_dir / "packs"
    packs_dir.mkdir(parents=True, exist_ok=True)

    p3_binding = _p4_load_p3_binding_data(p3_binding_import_id) if include_p3_diagrams else None
    index = {
        "schema_name": "p4p5_customer_decision_pack_export_index",
        "version": "v2.2_with_p3_internal_binding",
        "pack_id": pack_id,
        "import_id": import_id,
        "generated_at": _now_iso(),
        "app_key": app_key,
        "theme_key": theme_key,
        "theme_count": len(themes),
        "themes": [],
        "p3_usage_policy": "reference_only",
        "do_not_autofill_customer_answers": True,
        "p3_internal_binding": {
            "enabled": bool(p3_binding),
            "binding_import_id": (p3_binding or {}).get("binding_import_id"),
            "note_ja": "P3内部BindingはシステムにImport済みのデータを参照して、テーマ別の図・表を機械的に差し込みます。",
        },
    }
    for theme in themes:
        key = _p4p5_theme_key(theme)
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)
        theme_dir = packs_dir / safe_key
        theme_dir.mkdir(parents=True, exist_ok=True)
        md_path = theme_dir / "ANSWER_PACK.md"
        json_path = theme_dir / "ANSWER_TEMPLATE.json"
        source_path = theme_dir / "THEME_SOURCE.json"
        md_path.write_text(_p4p5_pack_markdown(theme), encoding="utf-8")
        _safe_json_dump(json_path, _p4p5_answer_template_json(theme))
        _safe_json_dump(source_path, theme)

        p3_materials: dict[str, Any] = {"enabled": False}
        if include_p3_diagrams:
            subset = _p4_build_theme_structural_subset(theme, p3_binding)
            data_dir = theme_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            _safe_json_dump(data_dir / "theme_structural_subset.json", subset)
            _safe_json_dump(data_dir / "theme_graph_binding.json", subset.get("theme_graph_binding") or {})
            _safe_json_dump(data_dir / "affected_structural_elements.json", subset.get("affected_structural_elements") or {})
            table_paths = _p4_write_theme_tables(theme_dir, subset)
            diagram_paths = _p4_write_theme_diagram(theme_dir, subset)
            _p4_write_theme_index_html(theme_dir, theme, subset, table_paths | diagram_paths)
            _p4_append_p3_section_to_answer_pack(md_path, subset)
            p3_materials = {
                "enabled": bool(p3_binding),
                "status": subset.get("status"),
                "model_count": len(subset.get("models") or []),
                "field_count": len(subset.get("fields") or []),
                "edge_count": len(subset.get("edges") or []),
                "missing_model_count": len(subset.get("missing_models") or []),
                "unmapped_ref_count": sum(len(v) for v in (subset.get("unmapped_refs") or {}).values()),
                "theme_index_path": str((theme_dir / "index.html").relative_to(out_dir)),
                "theme_graph_binding_path": str((data_dir / "theme_graph_binding.json").relative_to(out_dir)),
                "affected_structural_elements_path": str((data_dir / "affected_structural_elements.json").relative_to(out_dir)),
                "table_connections_svg_path": str((theme_dir / "diagrams" / "table_connections.svg").relative_to(out_dir)),
                "table_connections_mmd_path": str((theme_dir / "diagrams" / "table_connections.mmd").relative_to(out_dir)),
            }

        index["themes"].append({
            "development_theme_key": key,
            "title_ja": _p4p5_theme_title(theme),
            "app_key": _p4p5_theme_app(theme),
            "question_count": _p4p5_question_count(theme),
            "hypothesis_item_count": _p4p5_hypothesis_item_count(theme),
            "scenario_count": _p4p5_scenario_count(theme),
            "p3_context_counts": _p4p5_context_counts(theme),
            "p3_materials": p3_materials,
            "answer_pack_path": str(md_path.relative_to(out_dir)),
            "answer_template_path": str(json_path.relative_to(out_dir)),
            "theme_source_path": str(source_path.relative_to(out_dir)),
        })

    readme = "# P4/P5 Customer Decision PACK\n\nこのPACKは顧客回答用です。ChatGPTやシステムが回答を自動補完してはいけません。\n\n## Start\n\n別スレッドでは、次の一文で開始できます。\n\n```text\nNEXT_THREAD_START_MESSAGE.mdを見て始めてください。\n```\n\n## Included files\n\n- `NEXT_THREAD_START_MESSAGE.md`: 別スレッド開始メッセージです。\n- `START_HERE.md`: 利用手順です。\n- `prompts/P4P5_CUSTOMER_DECISION_PACK_INTERVIEW_PROMPT_DIAGRAM_FIRST.md`: 図を先に表示してからQ1へ進む顧客ヒアリング用プロンプトです。\n- `packs/*/ANSWER_PACK.md`: 背景・シナリオ・仮説付きの回答票です。\n- `packs/*/ANSWER_TEMPLATE.json`: decision_key単位の構造化回答テンプレートです。\n- `packs/*/THEME_SOURCE.json`: 生成元Themeデータです。\n- `packs/*/diagrams/table_connections.mmd`: チャット本文に表示するMermaid図コードです。\n- `packs/*/diagrams/table_connections.svg`: 顧客確認用の図ファイルです。\n\nP3情報はreference_onlyであり、自動採用しません。回答後はシステムへImportしてください。\n"
    (out_dir / "README.md").write_text(readme, encoding="utf-8")
    _safe_json_dump(out_dir / "PACK_INDEX.json", index)
    _p4p5_write_customer_interview_assets(out_dir, index)
    zip_path = out_dir.with_suffix(".zip")
    _zip_dir(out_dir, zip_path)
    result = {
        "schema_name": "p4p5_customer_decision_pack_export_result",
        "version": "v2.2_with_p3_internal_binding",
        "pack_id": pack_id,
        "import_id": import_id,
        "status": "exported",
        "theme_count": len(themes),
        "p3_binding_import_id": (p3_binding or {}).get("binding_import_id"),
        "download_url": f"/p4p5/imports/{import_id}/customer-packs/{pack_id}.zip",
        "zip_path": str(zip_path),
        "generated_at": _now_iso(),
        "index": index,
    }
    _safe_json_dump(out_dir / "EXPORT_RESULT.json", result)
    return result


@app.post("/p4p5/import-theme-catalog")
async def import_p4p5_theme_catalog(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")
    import_id = f"p4p5_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    out_dir = _p4p5_import_dir(import_id)
    upload_dir = out_dir / "uploaded"
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = file.filename or "uploaded"
    warnings: list[str] = []

    if filename.lower().endswith(".zip"):
        _p4p5_uploaded_pack_path(import_id).write_bytes(data)
        _p4p5_safe_extract_bytes(data, upload_dir)
        catalog_src = _p4p5_find_catalog(upload_dir)
        if not catalog_src:
            raise HTTPException(status_code=400, detail="P4/P5 Customer Decision Catalog JSON not found in ZIP")
        catalog = _load_json_path(catalog_src)
        source_catalog_path = str(catalog_src)
    else:
        catalog = _load_json_bytes(data)
        source_catalog_path = filename

    validation = _p4p5_validate_catalog(catalog)
    _safe_json_dump(_p4p5_catalog_path(import_id), catalog)
    _safe_json_dump(_p4p5_validation_path(import_id), validation)

    summary = {
        "schema_name": "p4p5_customer_decision_catalog_import_summary",
        "version": "v2.1",
        "import_id": import_id,
        "filename": filename,
        "source_catalog_path": source_catalog_path,
        "imported_at": _now_iso(),
        "status": validation.get("status"),
        "theme_count": validation.get("theme_count", 0),
        "app_count": validation.get("app_count", 0),
        "app_counts": validation.get("app_counts", {}),
        "total_question_count": validation.get("total_question_count", 0),
        "total_hypothesis_item_count": validation.get("total_hypothesis_item_count", 0),
        "total_scenario_count": validation.get("total_scenario_count", 0),
        "p3_context_theme_count": validation.get("p3_context_theme_count", 0),
        "customer_pack_ready_theme_count": validation.get("customer_pack_ready_theme_count", 0),
        "warnings": warnings,
        "links": {
            "self": f"/p4p5/imports/{import_id}",
            "dashboard": f"/p4p5/imports/{import_id}/dashboard",
            "themes": f"/p4p5/imports/{import_id}/themes",
            "validation": f"/p4p5/imports/{import_id}/validation",
            "export_customer_pack": f"/p4p5/imports/{import_id}/customer-pack/export",
        },
    }
    _safe_json_dump(_p4p5_summary_path(import_id), summary)
    _safe_json_dump(out_dir / "DASHBOARD.json", _p4p5_dashboard(import_id, catalog, validation))
    return summary


@app.get("/p4p5/imports")
def list_p4p5_imports() -> dict[str, Any]:
    items = []
    for p in sorted(P4P5_ROOT.glob("p4p5_*/IMPORT_SUMMARY.json"), reverse=True):
        try:
            items.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return {"schema_name": "p4p5_customer_decision_catalog_import_list", "count": len(items), "items": items}


@app.get("/p4p5/imports/latest")
def read_latest_p4p5_import() -> dict[str, Any]:
    latest = _p4p5_latest_import_id()
    if not latest:
        raise HTTPException(status_code=404, detail="No P4/P5 imports")
    return read_p4p5_import(latest)


@app.get("/p4p5/imports/{import_id}")
def read_p4p5_import(import_id: str) -> dict[str, Any]:
    _catalog, _validation, summary = _p4p5_load_import(import_id)
    return summary


@app.get("/p4p5/imports/{import_id}/validation")
def read_p4p5_validation(import_id: str) -> dict[str, Any]:
    _catalog, validation, _summary = _p4p5_load_import(import_id)
    return validation


@app.get("/p4p5/imports/{import_id}/dashboard")
def read_p4p5_dashboard(import_id: str) -> dict[str, Any]:
    catalog, validation, _summary = _p4p5_load_import(import_id)
    return _p4p5_dashboard(import_id, catalog, validation)


@app.get("/p4p5/imports/{import_id}/themes")
def list_p4p5_themes(import_id: str, app_key: str | None = None, q: str | None = None) -> dict[str, Any]:
    catalog, validation, _summary = _p4p5_load_import(import_id)
    rows = _p4p5_dashboard(import_id, catalog, validation)["themes"]
    if app_key:
        rows = [r for r in rows if r.get("app_key") == app_key or app_key in (r.get("target_apps") or [])]
    if q:
        qq = q.lower()
        rows = [r for r in rows if qq in json.dumps(r, ensure_ascii=False).lower()]
    return {"schema_name": "p4p5_customer_decision_theme_list", "import_id": import_id, "count": len(rows), "themes": rows}


@app.get("/p4p5/imports/{import_id}/themes/{theme_key:path}")
def read_p4p5_theme(import_id: str, theme_key: str) -> dict[str, Any]:
    catalog, _validation, _summary = _p4p5_load_import(import_id)
    for theme in _p4p5_theme_list(catalog):
        if _p4p5_theme_key(theme) == theme_key:
            return theme
    raise HTTPException(status_code=404, detail="Theme not found")


@app.post("/p4p5/imports/{import_id}/validate")
def validate_p4p5_import(import_id: str) -> dict[str, Any]:
    catalog, _validation, _summary = _p4p5_load_import(import_id)
    validation = _p4p5_validate_catalog(catalog)
    _safe_json_dump(_p4p5_validation_path(import_id), validation)
    _safe_json_dump(_p4p5_import_dir(import_id) / "DASHBOARD.json", _p4p5_dashboard(import_id, catalog, validation))
    return validation


@app.post("/p4p5/imports/{import_id}/customer-pack/export")
def export_p4p5_customer_pack(
    import_id: str,
    app_key: str | None = None,
    theme_key: str | None = None,
    p3_binding_import_id: str | None = None,
    include_p3_diagrams: bool = True,
) -> dict[str, Any]:
    return _p4p5_export_customer_pack(
        import_id,
        app_key=app_key,
        theme_key=theme_key,
        p3_binding_import_id=p3_binding_import_id,
        include_p3_diagrams=include_p3_diagrams,
    )




@app.post("/p4p5/imports/{import_id}/customer-pack/import")
async def import_p4p5_answered_customer_pack(import_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    _catalog, _validation, _summary = _p4p5_load_import(import_id)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")
    answer_import_id = f"answer_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    base_dir = _p4p5_answered_pack_dir(import_id) / answer_import_id
    upload_dir = base_dir / "uploaded"
    base_dir.mkdir(parents=True, exist_ok=True)
    filename = file.filename or "answered_pack"
    (base_dir / filename).write_bytes(data)

    if filename.lower().endswith(".zip"):
        _p4p5_safe_extract_bytes(data, upload_dir)
    elif filename.lower().endswith(".json"):
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / "ANSWER_TEMPLATE.json").write_bytes(data)
    else:
        raise HTTPException(status_code=400, detail="ZIPまたはANSWER_TEMPLATE.jsonを指定してください。")

    summary = _p4p5_answer_import_summary(import_id, answer_import_id, filename, upload_dir)
    catalog, validation, _summary = _p4p5_load_import(import_id)
    _safe_json_dump(_p4p5_import_dir(import_id) / "DASHBOARD.json", _p4p5_dashboard(import_id, catalog, validation))
    return summary


@app.get("/p4p5/imports/{import_id}/customer-pack/answers")
def list_p4p5_answered_customer_packs(import_id: str) -> dict[str, Any]:
    _catalog, _validation, _summary = _p4p5_load_import(import_id)
    return _p4p5_load_answer_status(import_id)



@app.post("/p4p5/imports/{import_id}/internal-design-pack/export")
def export_p4p5_internal_design_pack(
    import_id: str,
    app_key: str | None = None,
    theme_key: str | None = None,
    include_unanswered: bool = False,
) -> dict[str, Any]:
    return _p4p5_export_internal_design_pack(
        import_id,
        app_key=app_key,
        theme_key=theme_key,
        include_unanswered=include_unanswered,
    )


@app.get("/p4p5/imports/{import_id}/internal-design-packs")
def list_p4p5_internal_design_packs(import_id: str) -> dict[str, Any]:
    _catalog, _validation, _summary = _p4p5_load_import(import_id)
    status = _p4p5_load_internal_design_export_status(import_id)
    return status


@app.get("/p4p5/imports/{import_id}/internal-design-packs/{pack_id}.zip")
def download_p4p5_internal_design_pack(import_id: str, pack_id: str) -> FileResponse:
    zip_path = _p4p5_internal_design_pack_dir(import_id) / f"{pack_id}.zip"
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Internal design pack not found")
    return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip")


@app.get("/p4p5/imports/{import_id}/customer-packs/{pack_id}.zip")
def download_p4p5_customer_pack(import_id: str, pack_id: str) -> FileResponse:
    zip_path = _p4p5_pack_dir(import_id) / f"{pack_id}.zip"
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Customer pack not found")
    return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip")


# ---------------------------------------------------------------------------
# P5 Internal Design Import / Validate / Neo4j Apply
# ---------------------------------------------------------------------------
# This section imports the internal design artifacts generated by ChatGPT from
# P4/P5 answered themes. It intentionally treats the pack as an internal-only
# artifact and applies only the explicit NEO4J_PROJECTION_SOURCE_INTERNAL graph.

P5_INTERNAL_ROOT = STORAGE_ROOT / "p5_internal_design"
P5_INTERNAL_ROOT.mkdir(parents=True, exist_ok=True)

P5_INTERNAL_REQUIRED_FILES = {
    "domain_guide": "DOMAIN_GUIDE_PATCH_INTERNAL.md",
    "ontology_delta": "ONTOLOGY_DELTA_INTERNAL.json",
    "neo4j_projection": "NEO4J_PROJECTION_SOURCE_INTERNAL.json",
}
P5_INTERNAL_OPTIONAL_FILES = {
    "odoo_codegen_input": "ODOO_CODEGEN_INPUT_INTERNAL.json",
    "validation_report": "VALIDATION_REPORT_INTERNAL.md",
}


def _p5_internal_import_dir(design_import_id: str) -> Path:
    return P5_INTERNAL_ROOT / design_import_id


def _p5_internal_summary_path(design_import_id: str) -> Path:
    return _p5_internal_import_dir(design_import_id) / "IMPORT_SUMMARY.json"


def _p5_internal_validation_path(design_import_id: str) -> Path:
    return _p5_internal_import_dir(design_import_id) / "VALIDATION.json"


def _p5_internal_uploaded_zip_path(design_import_id: str) -> Path:
    return _p5_internal_import_dir(design_import_id) / "uploaded_internal_design_pack.zip"


def _p5_internal_find_file(root: Path, filename: str) -> Path | None:
    candidates = sorted(root.rglob(filename))
    return candidates[0] if candidates else None


def _p5_internal_load_optional_json(path: Path | None) -> dict[str, Any] | None:
    if not path or not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _p5_internal_neo4j_projection_path(design_import_id: str) -> Path:
    summary = _load_json_path(_p5_internal_summary_path(design_import_id))
    files = summary.get("files") if isinstance(summary.get("files"), dict) else {}
    rel = ((files.get("neo4j_projection") or {}).get("relative_path") if isinstance(files.get("neo4j_projection"), dict) else None)
    if not rel:
        raise HTTPException(status_code=404, detail="NEO4J_PROJECTION_SOURCE_INTERNAL.json not found in internal design import")
    path = _p5_internal_import_dir(design_import_id) / str(rel)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Neo4j projection file missing")
    return path


def _p5_internal_load_neo4j_payload(design_import_id: str) -> dict[str, Any]:
    path = _p5_internal_neo4j_projection_path(design_import_id)
    projection = _load_json_path(path)
    if isinstance(projection.get("neo4j_import_payload"), dict):
        payload = projection
    else:
        payload = {
            "schema_name": projection.get("schema_name") or "p4p5_neo4j_projection_source_internal",
            "version": projection.get("version") or "v1",
            "neo4j_import_payload": {
                "description": projection.get("description") or "P4/P5 internal design projection generated from customer answered themes.",
                "nodes": projection.get("nodes") or [],
                "relationships": projection.get("relationships") or projection.get("edges") or [],
            },
        }
    return payload


def _p5_internal_validate_payload(design_import_id: str) -> dict[str, Any]:
    base = _p5_internal_import_dir(design_import_id)
    if not base.exists():
        raise HTTPException(status_code=404, detail="P5 internal design import not found")
    summary = _load_json_path(_p5_internal_summary_path(design_import_id))
    issues: list[dict[str, Any]] = []
    files = summary.get("files") if isinstance(summary.get("files"), dict) else {}

    for file_key, filename in P5_INTERNAL_REQUIRED_FILES.items():
        meta = files.get(file_key) if isinstance(files.get(file_key), dict) else {}
        rel = meta.get("relative_path")
        if not rel or not (base / str(rel)).exists():
            issues.append({"severity": "error", "code": "MISSING_REQUIRED_FILE", "file_key": file_key, "filename": filename, "message": f"{filename} がありません。"})

    ontology = None
    neo_payload = None
    node_count = 0
    rel_count = 0
    dangling: list[dict[str, Any]] = []
    label_counts: dict[str, int] = {}
    rel_type_counts: dict[str, int] = {}

    ontology_meta = files.get("ontology_delta") if isinstance(files.get("ontology_delta"), dict) else {}
    ontology_path = base / str(ontology_meta.get("relative_path")) if ontology_meta.get("relative_path") else None
    ontology = _p5_internal_load_optional_json(ontology_path)
    if ontology is None:
        issues.append({"severity": "error", "code": "INVALID_ONTOLOGY_DELTA_JSON", "message": "ONTOLOGY_DELTA_INTERNAL.json をJSONとして読めません。"})
    else:
        if not isinstance(ontology.get("entities"), list):
            issues.append({"severity": "warning", "code": "ONTOLOGY_ENTITIES_NOT_LIST", "message": "OntologyDeltaのentitiesが配列ではありません。"})
        if not isinstance(ontology.get("relationships"), list):
            issues.append({"severity": "warning", "code": "ONTOLOGY_RELATIONSHIPS_NOT_LIST", "message": "OntologyDeltaのrelationshipsが配列ではありません。"})

    try:
        neo_payload = _p5_internal_load_neo4j_payload(design_import_id)
        nodes, rels = _extract_payload(neo_payload)
        node_count = len(nodes)
        rel_count = len(rels)
        dangling = _calc_dangling(nodes, rels)
        label_counts, rel_type_counts = _graph_counts(nodes, rels)
        if node_count == 0:
            issues.append({"severity": "error", "code": "NO_NEO4J_NODES", "message": "Neo4j Projectionにnodesがありません。"})
        if dangling:
            issues.append({"severity": "error", "code": "DANGLING_RELATIONSHIPS", "message": f"Neo4j Projectionにdangling relationshipsがあります: {len(dangling)}件", "count": len(dangling)})
    except HTTPException:
        raise
    except Exception as exc:
        issues.append({"severity": "error", "code": "INVALID_NEO4J_PROJECTION", "message": f"NEO4J_PROJECTION_SOURCE_INTERNAL.json を読めません: {exc}"})

    error_count = sum(1 for i in issues if i.get("severity") == "error")
    warning_count = sum(1 for i in issues if i.get("severity") == "warning")
    validation = {
        "schema_name": "p5_internal_design_validation",
        "version": "v1",
        "design_import_id": design_import_id,
        "validated_at": _now_iso(),
        "status": "valid" if error_count == 0 else "invalid",
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": issues,
        "node_count": node_count,
        "relationship_count": rel_count,
        "dangling_relationship_count": len(dangling),
        "label_counts": label_counts,
        "relationship_type_counts": rel_type_counts,
        "required_files_present": error_count == 0,
        "ready_for_neo4j_apply": error_count == 0 and node_count > 0,
    }
    _safe_json_dump(_p5_internal_validation_path(design_import_id), validation)
    return validation


def _p5_internal_preview(design_import_id: str) -> dict[str, Any]:
    summary = _load_json_path(_p5_internal_summary_path(design_import_id))
    validation = _load_json_path(_p5_internal_validation_path(design_import_id)) if _p5_internal_validation_path(design_import_id).exists() else _p5_internal_validate_payload(design_import_id)
    base = _p5_internal_import_dir(design_import_id)
    files = summary.get("files") if isinstance(summary.get("files"), dict) else {}
    domain_meta = files.get("domain_guide") if isinstance(files.get("domain_guide"), dict) else {}
    domain_text = ""
    if domain_meta.get("relative_path") and (base / str(domain_meta.get("relative_path"))).exists():
        domain_text = (base / str(domain_meta.get("relative_path"))).read_text(encoding="utf-8", errors="ignore")[:8000]
    ontology_meta = files.get("ontology_delta") if isinstance(files.get("ontology_delta"), dict) else {}
    ontology = _p5_internal_load_optional_json(base / str(ontology_meta.get("relative_path"))) if ontology_meta.get("relative_path") else None
    neo_payload = _p5_internal_load_neo4j_payload(design_import_id) if validation.get("ready_for_neo4j_apply") or validation.get("node_count") else {"neo4j_import_payload": {"nodes": [], "relationships": []}}
    nodes, rels = _extract_payload(neo_payload)
    return {
        "schema_name": "p5_internal_design_preview",
        "version": "v1",
        "design_import_id": design_import_id,
        "status": validation.get("status"),
        "summary": summary,
        "validation": validation,
        "domain_guide_preview_md": domain_text,
        "ontology_summary": {
            "entity_count": len(ontology.get("entities") or []) if isinstance(ontology, dict) else 0,
            "relationship_count": len(ontology.get("relationships") or []) if isinstance(ontology, dict) else 0,
            "business_rule_count": len(ontology.get("business_rules") or []) if isinstance(ontology, dict) else 0,
        },
        "graph_preview": {
            "nodes": nodes[:50],
            "relationships": rels[:80],
            "node_count": len(nodes),
            "relationship_count": len(rels),
        },
    }


@app.post("/p5/internal-design/import")
async def import_p5_internal_design_pack(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")
    design_import_id = f"p5_design_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    base = _p5_internal_import_dir(design_import_id)
    upload_dir = base / "uploaded"
    base.mkdir(parents=True, exist_ok=True)
    filename = file.filename or "internal_design_pack.zip"
    (_p5_internal_uploaded_zip_path(design_import_id) if filename.lower().endswith(".zip") else base / filename).write_bytes(data)

    if filename.lower().endswith(".zip"):
        _p4p5_safe_extract_bytes(data, upload_dir)
    else:
        raise HTTPException(status_code=400, detail="Internal Design Pack ZIPを指定してください。")

    files: dict[str, Any] = {}
    for k, name in {**P5_INTERNAL_REQUIRED_FILES, **P5_INTERNAL_OPTIONAL_FILES}.items():
        found = _p5_internal_find_file(upload_dir, name)
        if found:
            files[k] = {
                "filename": name,
                "relative_path": str(found.relative_to(base)),
                "size_bytes": found.stat().st_size,
            }
        else:
            files[k] = {"filename": name, "relative_path": None, "size_bytes": 0}

    manifest_path = _p5_internal_find_file(upload_dir, "MANIFEST.json")
    source_manifest = _p5_internal_load_optional_json(manifest_path)
    summary = {
        "schema_name": "p5_internal_design_import_summary",
        "version": "v1",
        "design_import_id": design_import_id,
        "filename": filename,
        "imported_at": _now_iso(),
        "status": "imported",
        "files": files,
        "source_manifest": source_manifest or {},
        "links": {
            "self": f"/p5/internal-design/imports/{design_import_id}",
            "validation": f"/p5/internal-design/imports/{design_import_id}/validation",
            "preview": f"/p5/internal-design/imports/{design_import_id}/preview",
            "neo4j_dry_run": f"/p5/internal-design/imports/{design_import_id}/neo4j-dry-run",
            "neo4j_apply": f"/p5/internal-design/imports/{design_import_id}/neo4j/apply",
        },
    }
    _safe_json_dump(_p5_internal_summary_path(design_import_id), summary)
    validation = _p5_internal_validate_payload(design_import_id)
    summary["status"] = "validated" if validation.get("status") == "valid" else "validation_failed"
    summary["validation"] = validation
    _safe_json_dump(_p5_internal_summary_path(design_import_id), summary)
    return summary


@app.get("/p5/internal-design/imports")
def list_p5_internal_design_imports() -> dict[str, Any]:
    items = []
    for p in sorted(P5_INTERNAL_ROOT.glob("p5_design_*/IMPORT_SUMMARY.json"), reverse=True):
        try:
            items.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return {"schema_name": "p5_internal_design_import_list", "count": len(items), "items": items}


@app.get("/p5/internal-design/imports/{design_import_id}")
def read_p5_internal_design_import(design_import_id: str) -> dict[str, Any]:
    return _load_json_path(_p5_internal_summary_path(design_import_id))


@app.get("/p5/internal-design/imports/{design_import_id}/validation")
def read_p5_internal_design_validation(design_import_id: str) -> dict[str, Any]:
    if _p5_internal_validation_path(design_import_id).exists():
        return _load_json_path(_p5_internal_validation_path(design_import_id))
    return _p5_internal_validate_payload(design_import_id)


@app.get("/p5/internal-design/imports/{design_import_id}/preview")
def read_p5_internal_design_preview(design_import_id: str) -> dict[str, Any]:
    return _p5_internal_preview(design_import_id)


@app.post("/p5/internal-design/imports/{design_import_id}/validate")
def validate_p5_internal_design_import(design_import_id: str) -> dict[str, Any]:
    return _p5_internal_validate_payload(design_import_id)


@app.post("/p5/internal-design/imports/{design_import_id}/neo4j-dry-run", response_model=Neo4jApplyResult)
def p5_internal_design_neo4j_dry_run(design_import_id: str) -> Neo4jApplyResult:
    validation = _p5_internal_validate_payload(design_import_id)
    if not validation.get("ready_for_neo4j_apply"):
        raise HTTPException(status_code=400, detail="Internal Design Pack is not ready for Neo4j dry-run")
    payload = _p5_internal_load_neo4j_payload(design_import_id)
    return _apply_graph_payload_to_neo4j(design_import_id, payload, True, _p5_internal_import_dir(design_import_id), "P5_INTERNAL_DESIGN")


@app.post("/p5/internal-design/imports/{design_import_id}/neo4j/apply", response_model=Neo4jApplyResult)
def p5_internal_design_apply_neo4j(design_import_id: str) -> Neo4jApplyResult:
    validation = _p5_internal_validate_payload(design_import_id)
    if not validation.get("ready_for_neo4j_apply"):
        raise HTTPException(status_code=400, detail="Internal Design Pack is not ready for Neo4j apply")
    payload = _p5_internal_load_neo4j_payload(design_import_id)
    result = _apply_graph_payload_to_neo4j(design_import_id, payload, False, _p5_internal_import_dir(design_import_id), "P5_INTERNAL_DESIGN")
    summary = _load_json_path(_p5_internal_summary_path(design_import_id))
    summary["status"] = "neo4j_applied"
    summary["neo4j_apply_result"] = result.model_dump()
    _safe_json_dump(_p5_internal_summary_path(design_import_id), summary)
    return result
