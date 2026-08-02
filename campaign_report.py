"""Reporte de rendimiento de campañas y keywords de Jersey Pickles.

Genera dos CSV con métricas de los últimos 30 días (por día):
  - report_campanas_*.csv : rendimiento por campaña
  - report_keywords_*.csv : rendimiento por keyword, con Quality Score
                            y cuota de impresiones

Ejecutado cada semana va acumulando el histórico para ver tendencias.
Requiere Basic Access aprobado.

Uso:
    .venv/bin/python campaign_report.py           # últimos 30 días
    .venv/bin/python campaign_report.py 90        # últimos 90 días
"""

import csv
import sys
from datetime import date, timedelta

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

CUSTOMER_ID = "4888823590"

CAMPAIGN_QUERY = """
    SELECT
        segments.date,
        campaign.name,
        campaign.status,
        metrics.impressions,
        metrics.clicks,
        metrics.ctr,
        metrics.average_cpc,
        metrics.cost_micros,
        metrics.conversions,
        metrics.cost_per_conversion
    FROM campaign
    WHERE segments.date BETWEEN '{start}' AND '{end}'
        AND metrics.impressions > 0
    ORDER BY segments.date DESC
"""

KEYWORD_QUERY = """
    SELECT
        segments.date,
        campaign.name,
        ad_group.name,
        ad_group_criterion.keyword.text,
        ad_group_criterion.keyword.match_type,
        ad_group_criterion.quality_info.quality_score,
        metrics.impressions,
        metrics.clicks,
        metrics.ctr,
        metrics.average_cpc,
        metrics.cost_micros,
        metrics.conversions,
        metrics.search_impression_share
    FROM keyword_view
    WHERE segments.date BETWEEN '{start}' AND '{end}'
        AND metrics.impressions > 0
    ORDER BY metrics.cost_micros DESC
"""


def run_query(service, query: str, out_path: str, header: list[str], row_fn) -> int:
    rows = 0
    stream = service.search_stream(customer_id=CUSTOMER_ID, query=query)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for batch in stream:
            for row in batch.results:
                writer.writerow(row_fn(row))
                rows += 1
    return rows


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    end = date.today()
    start = end - timedelta(days=days)
    today = end.isoformat()

    client = GoogleAdsClient.load_from_storage("google-ads.yaml")
    service = client.get_service("GoogleAdsService")

    try:
        n = run_query(
            service,
            CAMPAIGN_QUERY.format(start=start.isoformat(), end=today),
            f"report_campanas_{today}.csv",
            [
                "fecha", "campana", "estado", "impresiones", "clics", "ctr",
                "cpc_promedio_usd", "coste_usd", "conversiones",
                "coste_por_conversion_usd",
            ],
            lambda r: [
                r.segments.date,
                r.campaign.name,
                r.campaign.status.name,
                r.metrics.impressions,
                r.metrics.clicks,
                round(r.metrics.ctr, 4),
                round(r.metrics.average_cpc / 1_000_000, 2),
                round(r.metrics.cost_micros / 1_000_000, 2),
                r.metrics.conversions,
                round(r.metrics.cost_per_conversion / 1_000_000, 2),
            ],
        )
        print(f"Campañas: {n} filas → report_campanas_{today}.csv")

        n = run_query(
            service,
            KEYWORD_QUERY.format(start=start.isoformat(), end=today),
            f"report_keywords_{today}.csv",
            [
                "fecha", "campana", "grupo_de_anuncios", "keyword",
                "concordancia", "quality_score", "impresiones", "clics",
                "ctr", "cpc_promedio_usd", "coste_usd", "conversiones",
                "cuota_impresiones",
            ],
            lambda r: [
                r.segments.date,
                r.campaign.name,
                r.ad_group.name,
                r.ad_group_criterion.keyword.text,
                r.ad_group_criterion.keyword.match_type.name,
                r.ad_group_criterion.quality_info.quality_score,
                r.metrics.impressions,
                r.metrics.clicks,
                round(r.metrics.ctr, 4),
                round(r.metrics.average_cpc / 1_000_000, 2),
                round(r.metrics.cost_micros / 1_000_000, 2),
                r.metrics.conversions,
                round(r.metrics.search_impression_share, 4),
            ],
        )
        print(f"Keywords: {n} filas → report_keywords_{today}.csv")

        if n == 0:
            print(
                "0 filas con impresiones: normal mientras las campañas "
                "sigan pausadas. Reactívalas y este reporte cobra vida."
            )
    except GoogleAdsException as ex:
        for error in ex.failure.errors:
            print(f"Error de la API: {error.message}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
