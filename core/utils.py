# core/utils.py
from typing import List, Dict, Tuple
import io
import csv
import json

def group_by_category(items: List[Dict], categories: List[str]) -> Dict[str, List[Dict]]:
    """
    Groups items by category (with a fallback 'None').
    """
    groups = {cat: [] for cat in categories}
    groups["None"] = []
    for it in items:
        cat = it.get("category") or "None"
        if cat not in groups:
            cat = "None"
        groups[cat].append(it)
    return groups

def make_downloads(items: List[Dict]) -> Tuple[bytes, bytes]:
    """
    Returns (json_bytes, csv_bytes) for the report, including EU enrichment fields.
    """
    # JSON
    json_bytes = json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8")

    # CSV
    csv_io = io.StringIO()
    fieldnames = [
        "ingredient",
        "e_code",
        "category",
        "risk",
        "red_flag",
        "source",
        "reason",
        "pdf_evidence",
        # EU enrichment fields:
        "eu_enriched",
        "eu_source",
        "eu_e_code",
        "eu_official_name",      # EN (legacy)
        "eu_function",           # EN (legacy)
        "eu_official_name_en",
        "eu_function_en",
        "eu_official_name_sv",
        "eu_function_sv",
        "eu_policy_item_id",
    ]
    writer = csv.DictWriter(csv_io, fieldnames=fieldnames)
    writer.writeheader()
    for it in items:
        writer.writerow({k: it.get(k, "") for k in fieldnames})

    return json_bytes, csv_io.getvalue().encode("utf-8")
