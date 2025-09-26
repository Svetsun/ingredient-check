# core/eu_additives.py
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from .json_utils import parse_json_strict
from config import GOOGLE_API_KEY
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

# =============================================================================
# EU API (keys OPTIONAL; code works without them)
# =============================================================================
BASE_URL = "https://api.datalake.sante.service.ec.europa.eu/food-additives/food_additives_details"
API_VERSION = "v1.0"

DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Optional keys: keep exactly like your working probe script
HDR_API_KEY = os.getenv("OCP_APIM_SUBSCRIPTION_KEY", "").strip()
QRY_API_KEY = os.getenv("EU_FOOD_ADD_SUBSCRIPTION_KEY", "").strip()

DEFAULT_HEADERS = {
    "User-Agent": "FoodAdditivesClient/1.0 (+your-app)",
    "Accept": "*/*",
    "Content-Type": "application/json",
}

# =============================================================================
# SQLite cache
# =============================================================================
DB_PATH = DATA_DIR / "eu_additives.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# L1 in-memory cache for this process
L1_CACHE: dict[str, dict] = {}
TTL_DAYS = 180  # refresh API rows after ~6 months

# =============================================================================
# E-code utilities
# =============================================================================
_ECODE_RE = re.compile(r"(?i)\bE\s*[- ]?\s*\d{3}[a-z]?\b")

def normalize_e_code_storage(s: str) -> str:
    """
    Storage-normalized canonical form: E250, E211a (no spaces/hyphens).
    """
    if not s:
        return ""
    s = s.strip().upper()
    s = re.sub(r"[\s-]+", "", s)
    if not s.startswith("E"):
        s = "E" + s
    return s

def normalize_e_code_query_variants(s: str) -> List[str]:
    """
    EU endpoint often expects 'E 250' but sometimes 'E250' or 'E-250'.
    Try all variants.
    """
    s = s.strip().upper()
    m = re.search(r"(?i)E\s*[- ]?\s*(\d{3}[A-Z]?)", s)
    if not m:
        # If user passed just numbers like "250", still try variants
        core = re.sub(r"\D", "", s)
        return [f"E {core}", f"E{core}", f"E-{core}"] if core else [s]
    core = m.group(1)
    return [f"E {core}", f"E{core}", f"E-{core}"]

def extract_e_code_from_text(text: str) -> str:
    if not text:
        return ""
    m = _ECODE_RE.search(text)
    return normalize_e_code_storage(m.group(0)) if m else ""

# =============================================================================
# DB helpers
# =============================================================================
def _utcnow() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")

def _is_expired(ts: Optional[str], days: int = TTL_DAYS) -> bool:
    if not ts:
        return True
    try:
        return datetime.utcnow() - datetime.fromisoformat(ts) > timedelta(days=days)
    except Exception:
        return True

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS eu_additives (
              e_code TEXT PRIMARY KEY,             -- storage-normalized (E250)
              official_name_en TEXT,
              function_en TEXT,
              policy_item_id TEXT,
              payload_json TEXT,                  -- raw API row
              name_sv TEXT,
              function_sv TEXT,
              updated_at TEXT
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_official_name_en ON eu_additives(official_name_en)")
        conn.commit()

def _row_to_dict(row) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    (e_code, official_name_en, function_en, policy_item_id, payload_json,
     name_sv, function_sv, updated_at) = row
    return {
        "eu_e_code": e_code or "",
        "eu_official_name": (official_name_en or ""),   # legacy EN alias
        "eu_function": (function_en or ""),             # legacy EN alias
        "eu_official_name_en": (official_name_en or ""),
        "eu_function_en": (function_en or ""),
        "eu_official_name_sv": (name_sv or ""),
        "eu_function_sv": (function_sv or ""),
        "eu_policy_item_id": (policy_item_id or ""),
        "eu_raw": json.loads(payload_json) if payload_json else {},
        "updated_at": updated_at or "",
    }

def db_get_by_e_code(e_code_storage: str) -> Optional[Dict[str, Any]]:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            "SELECT e_code, official_name_en, function_en, policy_item_id, payload_json, "
            "name_sv, function_sv, updated_at "
            "FROM eu_additives WHERE e_code=?",
            (e_code_storage,),
        )
        return _row_to_dict(c.fetchone())

def db_upsert(eu: Dict[str, Any]) -> None:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO eu_additives
              (e_code, official_name_en, function_en, policy_item_id, payload_json, name_sv, function_sv, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(e_code) DO UPDATE SET
              official_name_en=excluded.official_name_en,
              function_en=excluded.function_en,
              policy_item_id=excluded.policy_item_id,
              payload_json=excluded.payload_json,
              name_sv=excluded.name_sv,
              function_sv=excluded.function_sv,
              updated_at=excluded.updated_at
            """,
            (
                eu.get("eu_e_code", ""),
                eu.get("eu_official_name_en", ""),
                eu.get("eu_function_en", ""),
                eu.get("eu_policy_item_id", ""),
                json.dumps(eu.get("eu_raw", {}), ensure_ascii=False),
                eu.get("eu_official_name_sv", ""),
                eu.get("eu_function_sv", ""),
                eu.get("updated_at", _utcnow()),
            ),
        )
        conn.commit()

# =============================================================================
# EU API calls (JSON first, CSV fallback)
# =============================================================================
def _http_get(params: Dict[str, Any], timeout: int = 45) -> requests.Response:
    merged = {"format": "json", "api-version": API_VERSION, **params}
    if QRY_API_KEY:
        merged["subscription-key"] = QRY_API_KEY
    headers = DEFAULT_HEADERS.copy()
    if HDR_API_KEY:
        headers["Ocp-Apim-Subscription-Key"] = HDR_API_KEY
    resp = requests.get(BASE_URL, params=merged, headers=headers, timeout=timeout)
    # Save raw for debugging
    (DATA_DIR / "food_additives_details_raw.json").write_bytes(resp.content)
    return resp

def _http_get_csv(params: Dict[str, Any], timeout: int = 45) -> requests.Response:
    merged = {"format": "csv", "api-version": API_VERSION, **params}
    if QRY_API_KEY:
        merged["subscription-key"] = QRY_API_KEY
    headers = DEFAULT_HEADERS.copy()
    if HDR_API_KEY:
        headers["Ocp-Apim-Subscription-Key"] = HDR_API_KEY
    resp = requests.get(BASE_URL, params=merged, headers=headers, timeout=timeout)
    (DATA_DIR / "food_additives_details_raw.csv").write_bytes(resp.content)
    return resp

def _extract_rows_from_json(data: Any) -> List[Dict[str, Any]]:
    """
    EU API shapes:
      - {"value": [ ... ]}
      - {"items": [ ... ]}  (rare)
      - [ ... ]             (already a list)
      - { ... }             (single row dict)
    """
    if data is None:
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("value", "items", "data", "results"):
            if key in data and isinstance(data[key], list):
                return [r for r in data[key] if isinstance(r, dict)]
        return [data]
    return []

def _api_get_rows_json(params: Dict[str, Any], timeout: int = 45) -> List[Dict[str, Any]]:
    try:
        resp = _http_get(params, timeout=timeout)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return _extract_rows_from_json(data)
    except Exception:
        return []

def _api_get_rows_csv(params: Dict[str, Any], timeout: int = 45) -> List[Dict[str, Any]]:
    try:
        resp = _http_get_csv(params, timeout=timeout)
        if resp.status_code != 200:
            return []
        text = resp.text
        return list(csv.DictReader(StringIO(text)))
    except Exception:
        return []

def _prefer_substance_match(rows: List[Dict[str, Any]], e_code_norm: str) -> Optional[Dict[str, Any]]:
    """
    Choose the best row for a given E-code:
      1) exact normalized e_code + additive_type == 'substanceFAD'
      2) exact normalized e_code (any type)
      3) any 'substanceFAD'
      4) fallback: first row
    """
    def norm(val: str) -> str:
        return normalize_e_code_storage(val or "")
    ecode_keys = ["additive_e_code", "e_code", "E_number", "e_number", "code"]

    enriched: List[Tuple[Dict[str, Any], str, str]] = []
    for r in rows:
        e = ""
        for k in ecode_keys:
            v = r.get(k)
            if v not in (None, ""):
                e = str(v)
                break
        e_norm = norm(e)
        t = str(r.get("additive_type", "") or r.get("type", "") or "")
        enriched.append((r, e_norm, t))

    for r, e_norm, t in enriched:
        if e_norm and e_norm == e_code_norm and t.lower() == "substancefad":
            return r
    for r, e_norm, _ in enriched:
        if e_norm and e_norm == e_code_norm:
            return r
    for r, _, t in enriched:
        if t.lower() == "substancefad":
            return r
    return rows[0] if rows else None

def _query_api_and_normalize(e_code: Optional[str], name: Optional[str], timeout: int = 45) -> Optional[Dict[str, Any]]:
    """
    Returns normalized dict with EN fields (name/function) and IDs.
    """
    chosen_row: Optional[Dict[str, Any]] = None

    if e_code:
        variants = normalize_e_code_query_variants(e_code)
        for variant in variants:
            params = {"additive_e_code": variant}
            rows = _api_get_rows_json(params, timeout=timeout)
            if not rows:
                rows = _api_get_rows_csv(params, timeout=timeout)
            if rows:
                chosen_row = _prefer_substance_match(rows, normalize_e_code_storage(variant))
                if chosen_row:
                    break

    elif name:
        params = {"additive_name": name}
        rows = _api_get_rows_json(params, timeout=timeout)
        if not rows:
            rows = _api_get_rows_csv(params, timeout=timeout)
        if rows:
            subs = [r for r in rows if str(r.get("additive_type","")).lower() == "substancefad"]
            chosen_row = subs[0] if subs else rows[0]

    if not chosen_row:
        return None

    def pick(d: Dict[str, Any], *keys) -> str:
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return str(d[k])
        return ""

    eu_e_raw   = pick(chosen_row, "additive_e_code", "e_code", "E_number", "e_number", "code")
    eu_name_en = pick(chosen_row, "additive_name", "name", "Name")
    eu_func_en = pick(chosen_row, "functional_class", "function", "category")
    pol_id     = pick(chosen_row, "policy_item_id", "policy_id", "id")
    fip_url    = pick(chosen_row, "fip_url", "url")

    return {
        "eu_e_code": normalize_e_code_storage(eu_e_raw),
        "eu_official_name_en": eu_name_en,
        "eu_function_en": eu_func_en,
        "eu_policy_item_id": pol_id,
        "eu_raw": chosen_row,
        "eu_fip_url": fip_url,
        "updated_at": _utcnow(),
    }

# =============================================================================
# Swedish enrichment (Gemini + deterministic fallbacks)
# =============================================================================
def _get_llm() -> Optional[ChatGoogleGenerativeAI]:
    if not GOOGLE_API_KEY:
        return None
    try:
        return ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            temperature=0.0,
            google_api_key=GOOGLE_API_KEY,
        )
    except Exception:
        return None

def _is_placeholder_sv(s: str) -> bool:
    s = (s or "").strip().lower()
    # Empty or generic placeholders; also any angle brackets or mention of "svensk/svenska"
    return (
        s in {"", "namn", "funktion"} or
        "<" in s or ">" in s or
        "svensk" in s or "svenska" in s
    )

# IMPORTANT: use Jinja2 + raw block so JSON braces aren't treated as variables
_TRANSLATE_TMPL = PromptTemplate(
    template=(
        "Translate the EU additive fields to Swedish.\n"
        "Return ONLY this JSON (no code fences, no comments):\n"
        "{% raw %}{\"name_sv\":\"...\", \"function_sv\":\"...\"}{% endraw %}\n"
        "- If you are not sure, return an empty string for that field.\n"
        "- Do NOT use angle brackets. Do NOT return placeholders.\n"
        "- Keep chemical names natural for Swedish labeling.\n\n"
        "English name: {{ name_en }}\n"
        "English function: {{ function_en }}\n"
    ),
    input_variables=["name_en", "function_en"],
    template_format="jinja2",  # <-- critical
)

def _translate_to_sv(name_en: str, function_en: str) -> Dict[str, str]:
    llm = _get_llm()
    if not llm or (not name_en and not function_en):
        return {"name_sv": "", "function_sv": ""}

    chain = _TRANSLATE_TMPL | llm | StrOutputParser()
    raw = chain.invoke({"name_en": name_en or "", "function_en": function_en or ""})
    try:
        data = parse_json_strict(raw)
    except Exception:
        return {"name_sv": "", "function_sv": ""}

    name_sv = (data.get("name_sv") or "").strip()
    function_sv = (data.get("function_sv") or "").strip()

    # Strip placeholders if any slipped through
    if _is_placeholder_sv(name_sv):
        name_sv = ""
    if _is_placeholder_sv(function_sv):
        function_sv = ""

    return {"name_sv": name_sv, "function_sv": function_sv}


# Deterministic overrides to avoid junk + improve UX
SWEDISH_NAME_OVERRIDES = {
    "E903": "Karnaubavax",                          # Carnauba wax
    "E414": "Gummi arabicum (akaciagummi)",         # Gum arabic
    "E300": "Askorbinsyra",                         # Ascorbic acid
    "E967": "Xylitol (björksocker)",                # Xylitol
}
FUNCTION_SV_MAP = {
    "Antioxidant": "Antioxidationsmedel",
    "Glazing agent": "Ytbehandlingsmedel",
    "Glazing agents": "Ytbehandlingsmedel",
    "Colour": "Färgämne",
    "Color": "Färgämne",
    "Sweetener": "Sötningsmedel",
    "Stabiliser": "Stabiliseringsmedel",
    "Stabilizer": "Stabiliseringsmedel",
    "Thickener": "Förtjockningsmedel",
    "Emulsifier": "Emulgeringsmedel",
    "Preservative": "Konserveringsmedel",
    "Raising agent": "Jäsmedel",
    "Acidity regulator": "Surhetsreglerande medel",
    "Anti-caking agent": "Klumpförebyggande medel",
    "Flavour enhancer": "Smakförstärkare",
    "Flavouring": "Aromämne",
}
FUNCTION_OVERRIDES_BY_ECODE_SV = {
    "E903": "Ytbehandlingsmedel",
    "E967": "Sötningsmedel",
    "E300": "Antioxidationsmedel",
    "E414": "Stabiliserings-/Förtjockningsmedel",
}

def _sv_fallbacks(eu: Dict[str, Any]) -> None:
    ecode   = eu.get("eu_e_code", "")
    func_en = eu.get("eu_function_en", "") or ""

    # Name: if LLM produced empty/placeholder, try override
    if _is_placeholder_sv(eu.get("eu_official_name_sv", "")):
        eu["eu_official_name_sv"] = ""
    if not eu.get("eu_official_name_sv") and ecode in SWEDISH_NAME_OVERRIDES:
        eu["eu_official_name_sv"] = SWEDISH_NAME_OVERRIDES[ecode]

    # Function: clean LLM, then map EN→SV, then code-specific override
    if _is_placeholder_sv(eu.get("eu_function_sv", "")):
        eu["eu_function_sv"] = ""
    if not eu.get("eu_function_sv") and func_en:
        for k, v in FUNCTION_SV_MAP.items():
            if k.lower() in func_en.lower():
                eu["eu_function_sv"] = v
                break
    if not eu.get("eu_function_sv") and ecode in FUNCTION_OVERRIDES_BY_ECODE_SV:
        eu["eu_function_sv"] = FUNCTION_OVERRIDES_BY_ECODE_SV[ecode]

# =============================================================================
# Public API
# =============================================================================
def query_eu_additive(
    e_code: Optional[str] = None,
    name: Optional[str] = None,
    timeout: int = 45,
    translate_sv: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    Query by E-code (preferred) or name.
    Layered lookup:
      1) L1 cache (storage-normalized E-code)
      2) SQLite (with TTL)
      3) EU API (JSON then CSV) + optional Swedish enrichment
      4) Upsert & cache

    Returns a dict with keys:
      eu_e_code, eu_official_name_en, eu_function_en, eu_policy_item_id,
      eu_official_name_sv, eu_function_sv, eu_fip_url, eu_raw, updated_at, ...
    """
    init_db()

    if e_code:
        e_store = normalize_e_code_storage(e_code)

        # L1 cache
        hit = L1_CACHE.get(e_store)
        if hit and not _is_expired(hit.get("updated_at")):
            return hit

        # SQLite
        row = db_get_by_e_code(e_store)
        if row and not _is_expired(row.get("updated_at")):
            L1_CACHE[e_store] = row
            return row

        # EU API
        eu = _query_api_and_normalize(e_code=e_store, name=None, timeout=timeout)
        if eu:
            if translate_sv:
                sv = _translate_to_sv(eu.get("eu_official_name_en", ""), eu.get("eu_function_en", ""))
                eu["eu_official_name_sv"] = sv.get("name_sv", "")
                eu["eu_function_sv"] = sv.get("function_sv", "")
            else:
                eu["eu_official_name_sv"] = ""
                eu["eu_function_sv"] = ""

            # Deterministic cleanups/overrides
            _sv_fallbacks(eu)

            # Back-compat English aliases (for CSV/JSON export)
            eu["eu_official_name"] = eu.get("eu_official_name_en", "")
            eu["eu_function"]      = eu.get("eu_function_en", "")

            db_upsert(eu)
            L1_CACHE[e_store] = eu
            time.sleep(0.2)  # polite pacing if user loops many codes
            return eu
        return None

    if name:
        eu = _query_api_and_normalize(e_code=None, name=name, timeout=timeout)
        if eu:
            if translate_sv:
                sv = _translate_to_sv(eu.get("eu_official_name_en", ""), eu.get("eu_function_en", ""))
                eu["eu_official_name_sv"] = sv.get("name_sv", "")
                eu["eu_function_sv"] = sv.get("function_sv", "")
            else:
                eu["eu_official_name_sv"] = ""
                eu["eu_function_sv"] = ""

            _sv_fallbacks(eu)

            eu["eu_official_name"] = eu.get("eu_official_name_en", "")
            eu["eu_function"]      = eu.get("eu_function_en", "")

            e_store = eu.get("eu_e_code") or ""
            if e_store:
                db_upsert(eu)
                L1_CACHE[e_store] = eu
            time.sleep(0.2)
            return eu
        return None

    return None

def enrich_items_with_eu(items: List[Dict[str, Any]], translate_sv: bool = True) -> List[Dict[str, Any]]:
    """
    Attach EU metadata to items with source != PDF.
    (UI decides whether to display EU line for PDF-backed items.)
    """
    out: List[Dict[str, Any]] = []
    for it in items:
        if it.get("source") == "PDF":
            out.append(it)
            continue

        name = (it.get("ingredient") or "").strip()
        e_code = (it.get("e_code") or "").strip()
        if not e_code:
            e_code = extract_e_code_from_text(name)

        eu = query_eu_additive(e_code=e_code or None, name=None if e_code else name, translate_sv=translate_sv)

        if eu:
            it["eu_enriched"] = True
            it["eu_source"] = "EU_API"
            it["eu_e_code"] = eu.get("eu_e_code", "")
            it["eu_official_name"] = eu.get("eu_official_name", "")
            it["eu_function"] = eu.get("eu_function", "")
            it["eu_policy_item_id"] = eu.get("eu_policy_item_id", "")
            it["eu_official_name_en"] = eu.get("eu_official_name_en", "")
            it["eu_function_en"] = eu.get("eu_function_en", "")
            it["eu_official_name_sv"] = eu.get("eu_official_name_sv", "")
            it["eu_function_sv"] = eu.get("eu_function_sv", "")
            if eu.get("eu_fip_url"):
                it["eu_fip_url"] = eu.get("eu_fip_url")
        else:
            it["eu_enriched"] = False
            it["eu_source"] = "None"

        out.append(it)
    return out

def bulk_refresh_eu_codes(codes: List[str], translate_sv: bool = True, timeout: int = 45) -> int:
    """
    Pre-fetch & cache a set of E-codes into SQLite (with optional SV enrichment).
    """
    init_db()
    ok = 0
    for raw in codes:
        e_store = normalize_e_code_storage(raw)
        eu = query_eu_additive(e_code=e_store, name=None, translate_sv=translate_sv)
        if eu:
            ok += 1
    return ok

# =============================================================================
# Debug / probe helpers
# =============================================================================
def eu_api_probe(e_code: str = "E250", timeout: int = 30) -> Dict[str, Any]:
    """
    Health check: tries E-code variants and reports number of rows returned.
    """
    out = {"ecode": e_code, "attempts": [], "ok": False}
    for variant in normalize_e_code_query_variants(e_code):
        params = {"additive_e_code": variant}
        rows = _api_get_rows_json(params, timeout=timeout)
        if not rows:
            rows = _api_get_rows_csv(params, timeout=timeout)
        out["attempts"].append({"variant": variant, "rows": len(rows)})
        if rows:
            out["ok"] = True
            break
    return out

def db_recent_rows(limit: int = 10) -> List[Tuple]:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT e_code, official_name_en, function_en, policy_item_id, updated_at "
            "FROM eu_additives ORDER BY datetime(updated_at) DESC LIMIT ?",
            (int(limit),),
        )
        return cur.fetchall()

def db_get_row(e_code: str) -> Optional[Dict[str, Any]]:
    return db_get_by_e_code(normalize_e_code_storage(e_code))

# =============================================================================
# CLI (optional)
# =============================================================================
if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser(description="EU Additives API checker")
    ap.add_argument("--probe", action="store_true", help="Run raw API probe (no DB write)")
    ap.add_argument("--ecode", default="E250", help="E-code to check (e.g., E211)")
    ap.add_argument("--lookup", action="store_true", help="Lookup via query_eu_additive (persists to DB)")
    ap.add_argument("--nosv", action="store_true", help="Disable Swedish translation")
    args = ap.parse_args()

    if args.probe:
        print(json.dumps(eu_api_probe(args.ecode), ensure_ascii=False, indent=2)); sys.exit(0)
    if args.lookup:
        res = query_eu_additive(e_code=args.ecode, translate_sv=not args.nosv)
        print(json.dumps(res, ensure_ascii=False, indent=2)); sys.exit(0)
    ap.print_help()
