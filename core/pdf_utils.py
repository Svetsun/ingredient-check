# core/pdf_utils.py
from pathlib import Path
from typing import List
from PyPDF2 import PdfReader
import streamlit as st
from langchain.text_splitter import RecursiveCharacterTextSplitter

def load_pdf_text(file_path: Path) -> str:
    """
    Loads plaintext from a PDF (page-by-page).
    """
    text = ""
    try:
        reader = PdfReader(str(file_path))
        for p in reader.pages:
            t = p.extract_text()
            if t:
                text += t + "\n"
    except Exception as e:
        st.error(f"PDF read error: {e}")
    return text

def split_text(text: str, chunk_size: int = 2200, chunk_overlap: int = 200) -> List[str]:
    """
    Splits text for vector indexing. Slightly larger chunks help with tables.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    return splitter.split_text(text)
