# app.py
import os
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
os.environ["STREAMLIT_SERVER_FILE_WATCHER_TYPE"] = "poll"

from pathlib import Path
import json
from typing import List
import sqlite3

import streamlit as st
from PIL import Image

from config import (
    GOOGLE_API_KEY, PDF_PATH, INDEX_PATH, CATEGORIES, HARMFUL_CATEGORIES
)
from styles import inject_button_css
from core.pdf_utils import load_pdf_text
from core.vector_store import load_or_create_vector_store, faiss_files_exist
from core.ocr import ocr_image_to_text, extract_ingredient_list
from core.classify import classify_with_rag
from core.utils import group_by_category, make_downloads
from core.eu_additives import (
    enrich_items_with_eu,
    query_eu_additive,
    bulk_refresh_eu_codes,
    DB_PATH,  # from eu_additives for DB status view
)

# ----------------------------------------------------
# Streamlit page + styles
# ----------------------------------------------------
st.set_page_config(page_title="Food Ingredient Evaluation", page_icon="🍎", layout="wide")
inject_button_css()

st.title("🍎 Food Ingredient Evaluation")
st.caption(
    "Primary source: the provided PDF (English & Swedish names, E-codes). "
    "Classification (category/risk) uses ONLY the PDF. "
    "If an item is not found in the PDF, we enrich it with the EU official name/function via the EU Food Additives API "
    "(and optionally Swedish names via Gemini)."
)

# ----------------------------------------------------
# Session state (defaults)
# ----------------------------------------------------
defaults = {
    "ocr_lang": "eng+swe",
    "detected_text": "",
    "edit_text": "",
    "ingredients_confirmed_text": "",
    "ingredients_confirmed": False,
    "translate_sv": True,  # allow Swedish enrichment via Gemini when key exists
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ----------------------------------------------------
# Cache heavy operations (use string args so Streamlit hashing stays stable)
# ----------------------------------------------------
@st.cache_resource(show_spinner=False)
def _cached_vector_store(pdf_path_str: str, index_path_str: str):
    return load_or_create_vector_store(Path(pdf_path_str), Path(index_path_str), force_rebuild=False, verbose=False)

@st.cache_data(show_spinner=False)
def _cached_pdf_text(pdf_path_str: str) -> str:
    return load_pdf_text(Path(pdf_path_str))

# ----------------------------------------------------
# Load index & PDF (reused across reruns)
# ----------------------------------------------------
preexisting = faiss_files_exist(INDEX_PATH)
label = "Loading existing FAISS index" if preexisting else "Building FAISS index (first time)"
with st.spinner(f"{label}: {PDF_PATH.name}"):
    if not PDF_PATH.exists():
        st.error(f"PDF not found at: {PDF_PATH.resolve()}")
        st.stop()
    vector_store = _cached_vector_store(str(PDF_PATH), str(INDEX_PATH))

st.success(f"FAISS ready at: {INDEX_PATH}")
PDF_TEXT = _cached_pdf_text(str(PDF_PATH))

# Optional: show FAISS files present on disk (quick sanity check)
try:
    files = ", ".join(sorted([p.name for p in INDEX_PATH.iterdir()]))
    st.caption(f"FAISS files in `{INDEX_PATH.name}`: {files}")
except Exception:
    pass

# ----------------------------------------------------
# Sidebar: EU enrichment tools + DB status
# ----------------------------------------------------
with st.sidebar:
    st.header("EU Additives tools")

    st.toggle(
        "Translate EU names to Swedish (Gemini)",
        key="translate_sv",
        value=st.session_state.translate_sv,
        disabled=not bool(GOOGLE_API_KEY),
        help="Requires GOOGLE_API_KEY. If missing/disabled, enrichment still adds official EU (English) fields.",
    )

    st.divider()
    st.subheader("Quick E-code lookup")
    ecode_input = st.text_input("E-code (e.g., E250)", value="")
    if st.button("Lookup EU info"):
        if ecode_input.strip():
            eu = query_eu_additive(e_code=ecode_input.strip(), translate_sv=st.session_state.translate_sv)
            if eu:
                st.success("Found in EU registry.")
                st.json(eu)
            else:
                st.warning("No EU entry found for this code.")

    st.divider()
    st.subheader("Preload E-codes (cache/DB)")
    preload_txt = st.text_area("Comma/space separated (e.g., E211, E250, E951, E960)", height=90)
    if st.button("Preload now"):
        raw = preload_txt.replace(",", " ").split()
        codes = [c.strip() for c in raw if c.strip()]
        if codes:
            n = bulk_refresh_eu_codes(codes, translate_sv=st.session_state.translate_sv)
            st.success(f"Preloaded {n} codes into SQLite cache.")
        else:
            st.info("Provide at least one E-code.")

    st.divider()
    st.subheader("DB status")
    st.write(f"Path: `{Path(DB_PATH).resolve()}`")
    st.write("Exists:", Path(DB_PATH).exists())
    if st.button("Show recent EU rows"):
        if Path(DB_PATH).exists():
            try:
                import pandas as pd
                with sqlite3.connect(DB_PATH) as conn:
                    df = pd.read_sql_query(
                        "SELECT e_code, official_name_en, function_en, policy_item_id, updated_at "
                        "FROM eu_additives ORDER BY datetime(updated_at) DESC LIMIT 10",
                        conn,
                    )
                st.dataframe(df, use_container_width=True)
            except Exception as e:
                st.warning(f"Could not read DB: {e}")
        else:
            st.info("No DB yet. Do a lookup (e.g., E250) to populate.")

# ----------------------------------------------------
# OCR language
# ----------------------------------------------------
st.markdown("### 🌐 OCR language")
st.session_state.ocr_lang = st.selectbox(
    "Language for OCR:", ["eng", "swe", "eng+swe"],
    index=["eng", "swe", "eng+swe"].index(st.session_state.get("ocr_lang", "eng+swe"))
)
st.caption("Tip: use **eng+swe** for mixed labels.")

# ----------------------------------------------------
# Camera & upload
# ----------------------------------------------------
def _run_ocr_from_file(file) -> str:
    try:
        img = Image.open(file).convert("RGB")
        return ocr_image_to_text(img, lang=st.session_state.ocr_lang)
    except Exception as e:
        st.warning(f"Could not read image: {e}")
        return ""

with st.expander("📷 Add ingredients from camera", expanded=False):
    cam_img = st.camera_input("Take a photo", key="camera_input")
    if cam_img is not None:
        st.session_state.detected_text = _run_ocr_from_file(cam_img)

with st.expander("📤 Add ingredients from image", expanded=False):
    up_img = st.file_uploader("Upload a photo", type=["png","jpg","jpeg","webp"], key="upload_input")
    if up_img is not None:
        st.session_state.detected_text = _run_ocr_from_file(up_img)

# ----------------------------------------------------
# Detected vs editable text
# ----------------------------------------------------
st.markdown("### 🔎 Detected text (read-only)")
st.text_area(
    "Detected ingredients (read-only)",
    value=st.session_state.detected_text or "— Nothing detected yet —",
    height=140, disabled=True, label_visibility="collapsed", key="detected_text_area"
)

st.markdown("### ✍️ Edit text (paste or refine before analysis)")
if not st.session_state.edit_text and st.session_state.detected_text:
    st.session_state.edit_text = st.session_state.detected_text

st.session_state.edit_text = st.text_area(
    "Edit ingredients before confirming",
    value=st.session_state.edit_text,
    height=160,
    placeholder=("Ex: Ingredienser: vatten, natriumbensoat (E211), aspartam (E951)… / "
                 "Ingredients: water, sodium benzoate (E211), aspartame (E951)…"),
    label_visibility="collapsed",
    key="edit_text_area"
)

# Confirm text
if (st.session_state.edit_text or st.session_state.detected_text):
    if st.button("Confirm"):
        st.session_state.ingredients_confirmed_text = (
            st.session_state.edit_text or st.session_state.detected_text
        ).strip()
        st.session_state.ingredients_confirmed = bool(st.session_state.ingredients_confirmed_text)
        if st.session_state.ingredients_confirmed:
            st.success("Ingredients text confirmed.")
        else:
            st.warning("No text to confirm. Paste or upload first.")

# Parse into tokens
final_text = st.session_state.ingredients_confirmed_text if st.session_state.ingredients_confirmed else ""
ingredients: List[str] = extract_ingredient_list(final_text) if final_text else []

if ingredients:
    st.markdown("### ✅ Parsed ingredients")
    st.write(", ".join(ingredients))
else:
    st.write("_No ingredients parsed yet. Confirm ingredients text to proceed._")

# ----------------------------------------------------
# Analyze & report (with EU enrichment for NotInPDF)
# ----------------------------------------------------
def _render_eu_line(it: dict) -> None:
    """Show EU enrichment line for NotInPDF items."""
    if it.get("eu_enriched"):
        bits = []
        if it.get("eu_e_code"): bits.append(f"**{it['eu_e_code']}**")
        # Prefer SV if present, else EN
        name_sv = it.get("eu_official_name_sv", "").strip()
        name_en = it.get("eu_official_name_en", "").strip() or it.get("eu_official_name", "").strip()
        func_sv = it.get("eu_function_sv", "").strip()
        func_en = it.get("eu_function_en", "").strip() or it.get("eu_function", "").strip()
        if name_sv or name_en:
            bits.append(name_sv or name_en)
        if func_sv or func_en:
            bits.append(f"function: {func_sv or func_en}")
        if it.get("eu_policy_item_id"):
            bits.append(f"id: {it['eu_policy_item_id']}")
        st.markdown(f"- EU Additives (official): " + " — ".join(bits))
    elif it.get("source") != "PDF":
        st.markdown("- EU Additives (official): _no match_")

run_btn = st.button("🔬 Analyze & Create Report", disabled=not ingredients)

if run_btn:
    with st.spinner("Classifying from PDF and enriching NotInPDF items via EU registry…"):
        results = classify_with_rag(
            vector_store, ingredients, PDF_TEXT, GOOGLE_API_KEY
        )
        items = results.get("items", [])

        # Enrich NotInPDF via EU API (optional Swedish via Gemini)
        items = enrich_items_with_eu(items, translate_sv=st.session_state.translate_sv)

    # Coverage summary
    src_pdf = sum(1 for it in items if it.get("source") == "PDF")
    eu_yes = sum(1 for it in items if it.get("eu_enriched"))
    st.progress(
        max(1, src_pdf) / max(1, len(items)),
        text=f"PDF-matched: {src_pdf}/{len(items)} • EU-enriched NotInPDF: {eu_yes}"
    )

    # Human-friendly controversial list
    st.markdown("## ⚠️ Controversial ingredients (plain language)")
    controversial = [
        it for it in items
        if (str(it.get("red_flag","")).lower()=="true") or (it.get("risk","").lower()=="avoid")
    ]
    if not controversial:
        st.success("✅ No red-flag ingredients matched the PDF for this product.")
    else:
        for it in controversial:
            name = it.get("ingredient","?").strip()
            ecode = it.get("e_code","").strip()
            risk = it.get("risk","").strip() or "Avoid"
            reason = it.get("reason","").strip()
            ev = it.get("pdf_evidence","").strip()
            badge = "🛑 Avoid" if risk.lower()=="avoid" else f"⚠️ {risk}"
            code = f" ({ecode})" if ecode else ""
            st.markdown(f"**{name}{code}** — {badge}")
            if reason:
                st.write(f"- Why it’s flagged: {reason}")
            if ev and it.get("source") == "PDF":
                st.write(f"- Backed by PDF: “{ev}”")
            if it.get("source") != "PDF":
                _render_eu_line(it)
            st.markdown("---")

    # Structured breakdown
    st.markdown("## 📑 Full ingredient breakdown (by category)")
    st.caption(
        "Classification uses ONLY the PDF. Items marked NotInPDF had no supporting match in the document "
        "(EN/SV name or E-code) and were enriched with the EU Additives registry when available."
    )

    grouped = group_by_category(items, CATEGORIES)
    total = len(items)
    st.write(f"Analyzed items: **{total}**")

    display_order = CATEGORIES + ["None"]
    for cat in display_order:
        entries = grouped.get(cat, [])
        if not entries:
            continue
        with st.expander(f"{cat} ({len(entries)})", expanded=(cat in ["Preservatives","Sweeteners","Color additives"])):
            for it in entries:
                src = it.get("source","NotInPDF")
                badge_src = "🔹 PDF" if src == "PDF" else "⚪ NotInPDF"
                risk = it.get("risk","Unknown")
                red = str(it.get("red_flag","")).lower()=="true"
                ecode = it.get("e_code","")
                risk_badge = ("🛑 Avoid" if red or risk.lower()=="avoid" else f"⚠️ {risk}") if risk!="Unknown" else "❔ Unknown"
                head = f"**{it.get('ingredient','?')}**"
                if ecode:
                    head += f" *(E-code: {ecode})*"
                st.markdown(f"{head} — {risk_badge} — *{badge_src}*")
                if it.get("reason"):
                    st.markdown(f"- Reason: {it['reason']}")
                if it.get("pdf_evidence") and src == "PDF":
                    st.markdown(f"- PDF evidence: “{it['pdf_evidence']}”")

                if src != "PDF":
                    _render_eu_line(it)

                st.markdown("---")

    # Downloads (JSON/CSV). Ensure core/utils.py includes EU columns (already rewritten).
    json_bytes, csv_bytes = make_downloads(items)
    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "⬇️ Download JSON", data=json_bytes, file_name="ingredient_report.json",
            mime="application/json", key="dl_json"
        )
    with c2:
        st.download_button(
            "⬇️ Download CSV", data=csv_bytes, file_name="ingredient_report.csv",
            mime="text/csv", key="dl_csv"
        )
