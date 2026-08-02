"""Valida keywords candidatas con métricas históricas reales de Google Ads.

Lee las candidatas de un CSV (por defecto el seed_candidates_*.csv más
reciente, columna 1) y pide a GenerateKeywordHistoricalMetrics el volumen
mensual de los últimos 12 meses, competencia y pujas. Así separamos las
candidatas del autocompletado que tienen volumen real de las que no.

Requiere Basic Access aprobado.

Uso:
    .venv/bin/python historical_metrics.py [candidatas.csv]
"""

import csv
import glob
import sys
from datetime import date

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

CUSTOMER_ID = "4888823590"
GEO_US = "geoTargetConstants/2840"
LANGUAGE_EN = "languageConstants/1000"
BATCH_SIZE = 500  # límite de keywords por petición razonable

# Filtro rápido: descartar candidatas claramente fuera de mercado/intención
EXCLUDE_SUBSTRINGS = [
    "india", "hyderabad", "australia", "canada", " uk", "recipe", "rugrats",
    "in spanish", "calories", "nutrition", "walmart", "grillo",
]


def newest_candidates_file() -> str:
    files = sorted(glob.glob("seed_candidates_*.csv"))
    if not files:
        print("No hay seed_candidates_*.csv; pasa un CSV como argumento.")
        sys.exit(1)
    return files[-1]


def load_keywords(path: str) -> list[str]:
    keywords = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # cabecera
        for row in reader:
            if not row:
                continue
            keyword = row[0].strip().lower()
            if any(bad in keyword for bad in EXCLUDE_SUBSTRINGS):
                continue
            keywords.append(keyword)
    return sorted(set(keywords))


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else newest_candidates_file()
    keywords = load_keywords(path)
    print(f"{len(keywords)} keywords a validar (de {path})")

    client = GoogleAdsClient.load_from_storage("google-ads.yaml")
    service = client.get_service("KeywordPlanIdeaService")

    out_path = f"historical_metrics_{date.today().isoformat()}.csv"
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
                "volumen_por_mes_12m",
            ]
        )

        for start in range(0, len(keywords), BATCH_SIZE):
            batch = keywords[start : start + BATCH_SIZE]

            request = client.get_type("GenerateKeywordHistoricalMetricsRequest")
            request.customer_id = CUSTOMER_ID
            request.keywords.extend(batch)
            request.language = LANGUAGE_EN
            request.geo_target_constants.append(GEO_US)
            request.keyword_plan_network = (
                client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
            )

            try:
                response = service.generate_keyword_historical_metrics(
                    request=request
                )
            except GoogleAdsException as ex:
                for error in ex.failure.errors:
                    print(f"Error de la API: {error.message}", file=sys.stderr)
                sys.exit(1)

            for result in response.results:
                metrics = result.keyword_metrics
                # El enum MonthOfYear va de JANUARY=2 a DECEMBER=13 → restar 1
                monthly = "; ".join(
                    f"{volume.year}-{volume.month - 1:02d}:{volume.monthly_searches}"
                    for volume in metrics.monthly_search_volumes
                )
                writer.writerow(
                    [
                        result.text,
                        metrics.avg_monthly_searches,
                        metrics.competition.name,
                        round(metrics.low_top_of_page_bid_micros / 1_000_000, 2),
                        round(metrics.high_top_of_page_bid_micros / 1_000_000, 2),
                        monthly,
                    ]
                )
                rows += 1

    print(f"Listo: {rows} keywords con métricas en {out_path}")


if __name__ == "__main__":
    main()
