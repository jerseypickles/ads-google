"""Genera ideas de keywords para jerseypickles.com con la Google Ads API.

Usa KeywordPlanIdeaService.GenerateKeywordIdeas con semillas + URL del sitio,
mercado EE.UU. en inglés, y exporta a CSV con volumen mensual, competencia
y rangos de puja.

Uso:
    python3 keyword_ideas.py            # export a keyword_ideas.csv
"""

import csv
import sys
from datetime import date

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

CUSTOMER_ID = "4888823590"  # Jersey Pickles (488-882-3590 sin guiones)
SITE_URL = "https://www.jerseypickles.com"

# Semillas (máx. ~20). Ajustar libremente.
SEED_KEYWORDS = [
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
]

GEO_US = "geoTargetConstants/2840"       # Estados Unidos
LANGUAGE_EN = "languageConstants/1000"   # Inglés

COMPETITION_ES = {
    "LOW": "baja",
    "MEDIUM": "media",
    "HIGH": "alta",
    "UNSPECIFIED": "",
    "UNKNOWN": "",
}


def _all_seeds() -> list[str]:
    """Núcleo fijo + las últimas semillas dinámicas de Fable (máx. 20 total)."""
    import json as _json
    import pathlib

    dyn = []
    path = pathlib.Path(__file__).parent / "seeds_dynamic.json"
    try:
        dyn = _json.loads(path.read_text(encoding="utf-8")).get("seeds", [])
    except Exception:
        pass
    return list(dict.fromkeys(SEED_KEYWORDS[:10] + dyn[-10:]))[:20]


def main() -> None:
    client = GoogleAdsClient.load_from_storage("google-ads.yaml")
    service = client.get_service("KeywordPlanIdeaService")

    request = client.get_type("GenerateKeywordIdeasRequest")
    request.customer_id = CUSTOMER_ID
    request.language = LANGUAGE_EN
    request.geo_target_constants.append(GEO_US)
    request.include_adult_keywords = False
    request.keyword_plan_network = (
        client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
    )
    request.keyword_and_url_seed.url = SITE_URL
    request.keyword_and_url_seed.keywords.extend(_all_seeds())

    try:
        ideas = service.generate_keyword_ideas(request=request)
    except GoogleAdsException as ex:
        for error in ex.failure.errors:
            print(f"Error de la API: {error.message}", file=sys.stderr)
        print(
            "\nSi el error menciona el developer token: la solicitud de "
            "Basic Access aún no está aprobada.",
            file=sys.stderr,
        )
        sys.exit(1)

    out_path = f"keyword_ideas_{date.today().isoformat()}.csv"
    rows = 0
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "keyword",
                "busquedas_mensuales_promedio",
                "competencia",
                "puja_top_baja_usd",
                "puja_top_alta_usd",
            ]
        )
        for idea in ideas:
            metrics = idea.keyword_idea_metrics
            writer.writerow(
                [
                    idea.text,
                    metrics.avg_monthly_searches,
                    COMPETITION_ES.get(metrics.competition.name, ""),
                    round(metrics.low_top_of_page_bid_micros / 1_000_000, 2),
                    round(metrics.high_top_of_page_bid_micros / 1_000_000, 2),
                ]
            )
            rows += 1

    print(f"Listo: {rows} keywords exportadas a {out_path}")


if __name__ == "__main__":
    main()
