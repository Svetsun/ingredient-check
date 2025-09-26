# config.py
import os
import re
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# --- Keys (Gemini only; used for EN->SV translation of EU fields if present) ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "").strip()

# --- Project paths (absolute to avoid surprises) ---
PROJECT_ROOT = Path(__file__).resolve().parent  # this file is in the project root

PDF_PATH = (PROJECT_ROOT / "pdfs" / "food_ingredients_full_en_sv.pdf")
if not PDF_PATH.exists():
    # Fallback to your alternate filename if primary is missing
    PDF_PATH = (PROJECT_ROOT / "pdfs" / "food_ ingredients_ list.pdf")

INDEX_FOLDER = (PROJECT_ROOT / "faiss_indexes")
INDEX_FOLDER.mkdir(parents=True, exist_ok=True)

# Sanitize index dir name based on the PDF filename so each PDF gets its own index
safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", PDF_PATH.stem)
INDEX_PATH = INDEX_FOLDER / safe_stem
INDEX_PATH.mkdir(parents=True, exist_ok=True)

# --- Category taxonomy (PDF authority) ---
CATEGORIES = [
    "Preservatives",
    "Sweeteners",
    "Color additives",
    "Flavors",
    "Fat replacers",
    "Emulsifiers",
    "Stabilizers and thickeners",
    "Binders",
    "Texturizers",
    "Anti-caking agents",
    "Dough strengtheners and conditioners",
    "Nitrates & nitrites",
]
HARMFUL_CATEGORIES = set(CATEGORIES)
