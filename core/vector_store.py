# core/vector_store.py
from pathlib import Path
import shutil
import os
import streamlit as st
from typing import List
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from .pdf_utils import load_pdf_text, split_text

def get_embeddings():
    """
    Small, fast, good-quality sentence embeddings.
    """
    return HuggingFaceEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2",
        encode_kwargs={"normalize_embeddings": True},
    )

def faiss_files_exist(index_path: Path) -> bool:
    """
    Check for FAISS artifacts created by langchain FAISS:
    - index.faiss
    - index.pkl or docstore.pkl (different LC versions use one of these)
    """
    faiss_file = index_path / "index.faiss"
    pkl_index = index_path / "index.pkl"
    pkl_docstore = index_path / "docstore.pkl"
    return faiss_file.exists() and (pkl_index.exists() or pkl_docstore.exists())

def create_vector_store(chunks: List[str], save_path: Path) -> FAISS:
    """
    Build FAISS from chunks and persist to disk.
    """
    embeddings = get_embeddings()
    vs = FAISS.from_texts(chunks, embedding=embeddings)
    save_path.mkdir(parents=True, exist_ok=True)
    vs.save_local(str(save_path))
    return vs

def load_or_create_vector_store(
    pdf_path: Path,
    index_path: Path,
    force_rebuild: bool = False,
    verbose: bool = True,
) -> FAISS:
    """
    Load existing FAISS if possible, otherwise build once and reuse.
    Rebuild only if explicitly asked or if load fails (format mismatch).
    """
    embeddings = get_embeddings()

    if force_rebuild and index_path.exists():
        shutil.rmtree(index_path, ignore_errors=True)

    if faiss_files_exist(index_path):
        try:
            vs = FAISS.load_local(str(index_path), embeddings, allow_dangerous_deserialization=True)
            if verbose:
                st.info(f"Loaded FAISS index from disk: {index_path}")
            return vs
        except Exception as e:
            if verbose:
                st.warning(f"Failed to load existing FAISS ({e}); rebuilding…")
            shutil.rmtree(index_path, ignore_errors=True)

    # Build fresh
    text = load_pdf_text(pdf_path)
    chunks = split_text(text)
    if verbose:
        st.info(f"Building FAISS index (chunks={len(chunks)}) → {index_path}")
    return create_vector_store(chunks, index_path)

def similarity_docs(vs: FAISS, query: str, k: int = 4):
    """
    Vector search with defensive guards.
    """
    if not query or not vs:
        return []
    try:
        return vs.similarity_search(query, k=k)
    except Exception as e:
        st.error(f"Vector search error: {e}")
        return []
