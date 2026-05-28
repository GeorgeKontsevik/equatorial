import os

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = "gpt-5.4-mini"

# ============================================================
# Minimal precipitation-impact case collector
# Dependencies:
#   pip install requests pandas
#
# Notebook:
#   from collect_precipitation_impact_cases_minimal_fixed import run_collection
#   df = run_collection(countries_list=["Côte d’Ivoire", "Malaysia"], target_per_country=20)
#
# Saves after every request:
#   outputs/cases_raw.jsonl
#   outputs/cases_deduped.csv
# ============================================================

import argparse
import datetime as dt
import difflib
import hashlib
import importlib.util
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

for module in ["requests", "pandas"]:
    if importlib.util.find_spec(module) is None:
        raise RuntimeError(f"Missing package: {module}. Install: pip install requests pandas")

import pandas as pd
import requests


DEFAULT_COUNTRIES = [
    "Côte d’Ivoire", "Ghana", "Nigeria", "Cameroon", "Gabon", "Republic of Congo",
    "Democratic Republic of Congo", "Uganda", "Kenya", "Somalia", "Ethiopia",
    "Burundi", "Rwanda", "Tanzania", "Angola", "Zambia", "Mozambique", "Madagascar",
    "Sudan", "South Sudan", "Chad", "Niger", "Mali", "Indonesia", "Malaysia",
    "Brunei", "Papua New Guinea", "Philippines", "Ecuador", "Colombia", "Brazil",
    "Peru", "Guyana", "Suriname",
]

CASE_SCHEMA = {
    "type": "object",
    "properties": {
        "cases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "country": {"type": "string"},
                    "where": {"type": "string"},
                    "when": {"type": "string"},
                    "what_happened": {"type": "string"},
                    "precipitation_impact": {"type": "string"},
                    "source_url": {"type": "string"},
                },
                "required": ["country", "where", "when", "what_happened", "precipitation_impact", "source_url"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cases"],
    "additionalProperties": False,
}

QUERY_FAMILIES = [
    ['"rain-damaged roads" crops 2024', '"trucks stuck" crops rain 2024'],
    ['"roads impassable" crops flood 2024', '"market supply" "roads damaged" floods 2024'],
    ['"harvest delayed" "roads" "floods" 2024', '"transport to ports" "heavy rains" 2024'],
    ['site:fews.net floods roads markets crops 2024', 'site:wfp.org flooded roads trucks food 2024'],
]


def norm(x: Optional[str]) -> str:
    x = x or ""
    x = x.lower().strip()
    x = re.sub(r"[^\w\s\-/'’]", " ", x, flags=re.UNICODE)
    x = re.sub(r"\s+", " ", x)
    return x


def norm_url(url: Optional[str]) -> str:
    if not url:
        return ""
    p = urlparse(url.strip())
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", "", ""))


def month_key(case: Dict[str, Any]) -> str:
    m = re.search(r"2024[-\s/]*(\d{1,2})?", case.get("when", ""))
    if not m:
        return "2024"
    if m.group(1):
        return f"2024-{int(m.group(1)):02d}"
    return "2024"


def event_id(case: Dict[str, Any]) -> str:
    raw = "|".join([
        norm(case.get("country")),
        norm(case.get("where")),
        month_key(case),
        norm(case.get("precipitation_impact")),
        norm_url(case.get("source_url")),
    ])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio() * 100


def is_dup(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    if norm(a.get("country")) != norm(b.get("country")):
        return False
    if month_key(a) != month_key(b):
        return False
    if norm_url(a.get("source_url")) == norm_url(b.get("source_url")):
        return True
    return (
        similarity(a.get("where", ""), b.get("where", "")) > 75
        and similarity(a.get("what_happened", ""), b.get("what_happened", "")) > 75
    )


def dedup(cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for c in cases:
        if not any(is_dup(c, old) for old in out):
            out.append(c)
    return out


def enrich(case: Dict[str, Any]) -> Dict[str, Any]:
    c = dict(case)
    c["event_id"] = event_id(c)
    c["source_domain"] = urlparse(c.get("source_url", "")).netloc.lower().replace("www.", "")
    c["collected_at"] = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    return c


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def append_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def export_csv(cases: List[Dict[str, Any]], path: Path) -> pd.DataFrame:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(cases)
    cols = ["event_id", "country", "where", "when", "what_happened", "precipitation_impact", "source_url", "source_domain", "collected_at"]
    if not df.empty:
        df = df[[c for c in cols if c in df.columns]]
    df.to_csv(path, index=False, encoding="utf-8")
    print(f"[saved] {path} | rows={len(df)}")
    return df


def read_countries(path: Optional[str]) -> List[str]:
    if not path or not Path(path).exists():
        return DEFAULT_COUNTRIES
    df = pd.read_csv(path)
    return [str(x).strip() for x in df["country"].dropna() if str(x).strip()]


def blacklist(existing: List[Dict[str, Any]], country: str) -> str:
    rows = []
    for c in existing:
        if norm(c.get("country")) == norm(country):
            rows.append({
                "where": c.get("where"),
                "when": c.get("when"),
                "precipitation_impact": c.get("precipitation_impact"),
                "url": c.get("source_url"),
            })
    return json.dumps(rows[-60:], ensure_ascii=False)


def build_prompt(country: str, batch_id: int, target: int, existing: List[Dict[str, Any]]) -> str:
    queries = QUERY_FAMILIES[batch_id % len(QUERY_FAMILIES)]
    return f"""
Find up to {target} unique 2024 cases in {country}.

Keep only cases where source explicitly says:
rain/flood/heavy rainfall -> roads/transport/logistics disruption -> road/logistics/market/transport impact.

Return only JSON.
No summaries.
No markdown.
No inferred cases.
No duplicates.
Every case must have URL.

Fields:
country, where, when, what_happened, precipitation_impact, source_url.

what_happened: one sentence, max 7 words.
precipitation_impact: short label of the effect, e.g. "roads impassable", "trucks stuck", "bridge washed away", "market supply disrupted".

Search queries:
{chr(10).join("- " + q + " " + country for q in queries)}

Already collected:
{blacklist(existing, country)}
""".strip()


def call_api(prompt: str, model: str, api_key: str, retries: int = 2) -> Dict[str, Any]:
    payload = {
        "model": model,
        "reasoning": {"effort": "low"},
        "tools": [{"type": "web_search"}],
        "input": prompt,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "minimal_crop_logistics_cases",
                "strict": True,
                "schema": CASE_SCHEMA,
            }
        },
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    for i in range(retries):
        r = requests.post("https://api.openai.com/v1/responses", headers=headers, json=payload, timeout=180)
        if r.status_code < 400:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 60
            print(f"[retry] HTTP {r.status_code}; sleep={wait}s")
            time.sleep(wait)
            continue
        raise RuntimeError(f"OpenAI API error {r.status_code}: {r.text[:1500]}")

    raise RuntimeError("API failed after retries")


def output_text(resp: Dict[str, Any]) -> str:
    if resp.get("output_text"):
        return resp["output_text"]
    chunks = []
    for item in resp.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    chunks.append(c.get("text", ""))
    return "\n".join(chunks)


def parse_cases(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    text = output_text(resp)
    data = json.loads(text)
    return [enrich(c) for c in data.get("cases", [])]


def run_collection(
    countries_path: str = "countries.csv",
    countries_list: Optional[List[str]] = None,
    target_per_country: int = 50,
    batches: int = 4,
    outputs_dir: str = "outputs",
    resume: bool = True,
    sleep: float = 2.0,
) -> pd.DataFrame:
    if OPENAI_API_KEY == "PASTE_YOUR_OPENAI_API_KEY_HERE":
        raise RuntimeError("Paste your API key into OPENAI_API_KEY at the top of this file.")

    countries = countries_list if countries_list is not None else read_countries(countries_path)
    out_dir = Path(outputs_dir)
    raw_path = out_dir / "cases_raw_2.jsonl"
    csv_path = out_dir / "cases_deduped_2.csv"

    all_cases = read_jsonl(raw_path) if resume else []

    for country in countries:
        print(f"\n[country] {country}")
        for batch_id in range(batches):
            country_count = sum(1 for c in dedup(all_cases) if norm(c.get("country")) == norm(country))
            if country_count >= target_per_country:
                print(f"[done] target reached: {country_count}")
                break

            prompt = build_prompt(country, batch_id, target_per_country, all_cases)
            print(f"[api] batch={batch_id + 1}/{batches}; current={country_count}")

            resp = call_api(prompt, OPENAI_MODEL, OPENAI_API_KEY)
            new_cases = parse_cases(resp)
            print(f"[new] {len(new_cases)}")

            append_jsonl(raw_path, new_cases)
            all_cases.extend(new_cases)

            deduped = dedup(all_cases)
            df = export_csv(deduped, csv_path)
            time.sleep(sleep)

    return export_csv(dedup(all_cases), csv_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--countries", default="countries.csv")
    parser.add_argument("--target-per-country", type=int, default=50)
    parser.add_argument("--batches", type=int, default=4)
    parser.add_argument("--outputs-dir", default="outputs")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    run_collection(
        countries_path=args.countries,
        target_per_country=args.target_per_country,
        batches=args.batches,
        outputs_dir=args.outputs_dir,
        resume=args.resume,
    )


if __name__ == "__main__":
    main()
