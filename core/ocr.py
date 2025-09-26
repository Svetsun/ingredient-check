# core/ocr.py
import re
import unicodedata
from typing import List
import streamlit as st
import pytesseract
from PIL import Image

# Adjust if Tesseract is installed elsewhere (Windows default shown)
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Patterns to preserve E-codes and multi-word tokens
_ECODE = r"(?:e|E)\s?-?\s?\d{3}[a-zA-Z]?"
_WORD = r"[a-zA-ZåäöÅÄÖ\-']{2,}"
_TOKEN_RE = re.compile(rf"{_ECODE}|{_WORD}", re.UNICODE)

def ocr_image_to_text(img: Image.Image, lang: str) -> str:
    """
    Runs Tesseract OCR for the given language(s).
    """
    try:
        return pytesseract.image_to_string(img, lang=lang)
    except Exception as e:
        st.error(f"OCR error: {e}")
        return ""

def _normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("\u2022", ",")  # bullet → comma
    return s

def extract_ingredient_list(raw_text: str) -> List[str]:
    """
    Extracts a de-duplicated list of ingredient tokens from free text.
    Preserves E-codes (E250, E-250, e 250, etc.) and joins multi-word names.
    """
    if not raw_text:
        return []
    text = _normalize_text(raw_text).lower()

    # Prefer lines mentioning ingredients keywords
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    candidate = ""
    for l in lines:
        if any(k in l for k in ["ingredient", "ingredients", "ingrediens", "ingredienser"]):
            candidate += " " + l
    if not candidate:
        candidate = text

    # Split on hard separators; then tokenize to keep E-codes intact
    chunks = re.split(r"[;()\[\]{}]| och | and |\n", candidate)
    seen, out = set(), []
    for ch in chunks:
        ch = re.sub(r"[\/|]", ",", ch)  # slashes/pipes → commas
        parts = [p.strip(" :.-") for p in ch.split(",")]
        for p in parts:
            if not p:
                continue
            tokens = _TOKEN_RE.findall(p)
            if not tokens:
                continue
            ing = " ".join(tokens).strip()
            ing = re.sub(r"\s+", " ", ing)
            if len(ing) > 1 and ing not in seen:
                out.append(ing)
                seen.add(ing)
    return out
