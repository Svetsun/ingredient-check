# core/classify.py
from typing import List, Dict
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_google_genai import ChatGoogleGenerativeAI

from .vector_store import similarity_docs
from .json_utils import parse_json_strict
from .prompts import USER_PROMPT_TMPL
from .eu_additives import enrich_items_with_eu  # optional enrichment hook

# Swedish context anchors to help retrieval for SV terms
SV_KEYS = [
    "konserveringsmedel", "sötningsmedel", "färgämn", "arom", "smakämn",
    "emulgeringsmedel", "stabiliseringsmedel", "förtjockningsmedel", "bindemedel",
    "texturmedel", "klumpförebyggande", "degförbättringsmedel", "nitrit", "nitrat"
]

def get_llm(temp: float, google_api_key: str):
    """
    Deterministic Gemini LLM for classification (PDF-only authority).
    """
    return ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        temperature=temp,
        google_api_key=google_api_key,
    )

def classify_with_rag(
    vs,
    ingredients: List[str],
    pdf_text: str,
    google_api_key: str,
    do_eu_enrichment: bool = False,
    translate_sv: bool = True,
) -> Dict:
    """
    RAG classification using ONLY the PDF as authority. Optionally enrich NotInPDF via EU registry.
    """
    # Build retrieval context per term (EN+SV probes)
    context_blobs = []
    for term in ingredients:
        docs = similarity_docs(vs, term, k=3)
        if docs:
            snippet = "\n---\n".join(d.page_content[:800] for d in docs)
            context_blobs.append(f"### {term}\n{snippet}")

        for sv_key in SV_KEYS:
            docs_sv = similarity_docs(vs, f"{term} {sv_key}", k=1)
            if docs_sv:
                context_blobs.append(f"### {term} (sv match: {sv_key})\n{docs_sv[0].page_content[:800]}")
    context_joined = "\n\n".join(context_blobs) if context_blobs else "(no relevant PDF passages found)"

    prompt = PromptTemplate(
        template=USER_PROMPT_TMPL,
        input_variables=["context", "ingredients", "pdf_risk_guide"],
        template_format="jinja2",
    )
    llm = get_llm(temp=0.0, google_api_key=google_api_key or "")

    chain = (
        {
            "context": lambda x: x["context"],
            "ingredients": lambda x: x["ingredients"],
            "pdf_risk_guide": lambda x: x["pdf_risk_guide"],
        }
        | prompt
        | llm
        | StrOutputParser()
    )

    raw = chain.invoke({
        "context": context_joined,
        "ingredients": "\n".join(ingredients),
        "pdf_risk_guide": pdf_text,
    })

    # Parse or attempt a single repair
    try:
        data = parse_json_strict(raw)
        if "items" not in data or not isinstance(data["items"], list):
            raise ValueError("Missing or invalid 'items'")
    except Exception:
        fixer = PromptTemplate(
            template=(
                "Return ONLY valid JSON matching this schema (no prose, no fences):\n"
                "{% raw %}{\"items\": [{\"ingredient\": \"<name>\", \"e_code\": \"<E#|''>\", \"category\": \"<PDF category|None>\", \"risk\": \"<PDF term|Unknown>\", \"red_flag\": true|false, \"reason\": \"<short>\", \"source\":\"PDF|NotInPDF\", \"pdf_evidence\":\"<short>\"}] }{% endraw %}\n\n"
                "Rewrite the following as valid JSON only:\n\n{{ raw }}"
            ),
            input_variables=["raw"],
            template_format="jinja2",
        )
        repaired = (fixer | llm | StrOutputParser()).invoke({"raw": raw})
        data = parse_json_strict(repaired)
        if "items" not in data or not isinstance(data["items"], list):
            raise ValueError("Missing or invalid 'items' after fix")

    # Optional EU enrichment for NotInPDF items
    if do_eu_enrichment:
        data["items"] = enrich_items_with_eu(data["items"], translate_sv=translate_sv)

    return data
