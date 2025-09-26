# core/json_utils.py
import json
import re

def parse_json_strict(raw: str) -> dict:
    """
    Attempts to coerce an LLM response into valid JSON.
    Tries: direct JSON → fenced ```json → brace slice → light repairs.
    """
    # direct
    try:
        return json.loads(raw)
    except Exception:
        pass

    # fenced
    m = re.search(r"```json\s*(.+?)\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
    if m:
        block = m.group(1).strip()
        try:
            return json.loads(block)
        except Exception:
            raw = block

    # slice by outermost braces
    if "{" in raw and "}" in raw:
        start, end = raw.find("{"), raw.rfind("}")
        if start < end:
            candidate = raw[start:end + 1]
            try:
                return json.loads(candidate)
            except Exception:
                repaired = candidate.replace("\ufeff", "")
                repaired = re.sub(r"```.*?```", "", repaired, flags=re.DOTALL)
                repaired = re.sub(r"//.*?$", "", repaired, flags=re.MULTILINE)
                repaired = re.sub(r"/\*.*?\*/", "", repaired, flags=re.DOTALL)
                repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
                if repaired.count("'") > repaired.count('"'):
                    repaired = re.sub(r"(?<!\\)'", '"', repaired)
                repaired = repaired.strip()
                try:
                    return json.loads(repaired)
                except Exception:
                    pass
    raise ValueError("Could not coerce model output to JSON")
