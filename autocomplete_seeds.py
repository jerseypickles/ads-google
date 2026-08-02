"""Cosecha sugerencias del autocompletado público de Google como semillas long-tail.

No usa la Google Ads API ni credenciales: consulta el endpoint público de
sugerencias (el mismo que alimenta la caja de búsqueda) con términos de
pickles y expansiones a-z. Las candidatas se validan después con
GenerateKeywordHistoricalMetrics cuando tengamos Basic Access.

Uso:
    python3 autocomplete_seeds.py    # export a seed_candidates.csv
"""

import csv
import json
import time
import urllib.parse
import urllib.request
from datetime import date

CORE_SEEDS = [
    "pickles",
    "dill pickles",
    "spicy pickles",
    "gourmet pickles",
    "homemade pickles",
    "fermented pickles",
    "half sour pickles",
    "kosher dill pickles",
    "bread and butter pickles",
    "pickle gift",
    "buy pickles online",
    "pickled vegetables",
    "best pickles",
    "pickles near me",
    "jersey pickles",
]

ALPHABET_BASE = "pickles"  # se expande: "pickles a", "pickles b", ...

ENDPOINT = "https://suggestqueries.google.com/complete/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
PAUSE_SECONDS = 0.4


def fetch_suggestions(query: str) -> list[str]:
    params = urllib.parse.urlencode(
        {"client": "firefox", "hl": "en", "gl": "us", "q": query}
    )
    request = urllib.request.Request(f"{ENDPOINT}?{params}", headers=HEADERS)
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload[1]


def _dynamic_seeds() -> list[str]:
    """Semillas que Fable va añadiendo para explorar territorio nuevo."""
    import pathlib
    path = pathlib.Path(__file__).parent / "seeds_dynamic.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("seeds", [])
    except Exception:
        return []


def main() -> None:
    queries = list(dict.fromkeys(CORE_SEEDS + _dynamic_seeds()))
    queries += [f"{ALPHABET_BASE} {letter}" for letter in "abcdefghijklmnopqrstuvwxyz"]

    results: dict[str, str] = {}  # sugerencia -> query que la produjo
    for query in queries:
        try:
            suggestions = fetch_suggestions(query)
        except Exception as exc:
            print(f"  aviso: fallo con '{query}': {exc}")
            continue
        for suggestion in suggestions:
            results.setdefault(suggestion.strip().lower(), query)
        time.sleep(PAUSE_SECONDS)

    out_path = f"seed_candidates_{date.today().isoformat()}.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["keyword_candidata", "query_origen"])
        for keyword in sorted(results):
            writer.writerow([keyword, results[keyword]])

    print(f"Listo: {len(results)} candidatas únicas en {out_path}")


if __name__ == "__main__":
    main()
