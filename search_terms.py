"""Mina los términos de búsqueda reales de las campañas de Jersey Pickles.

Consulta search_term_view vía GAQL (últimos 12 meses): qué buscó la gente
cuando salieron nuestros anuncios, con clics, impresiones y coste. Sirve
para encontrar keywords nuevas que añadir y negativas que excluir.

Requiere Basic Access aprobado.

Uso:
    .venv/bin/python search_terms.py    # export a search_terms.csv
"""

import csv
import sys
from datetime import date, timedelta

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

CUSTOMER_ID = "4888823590"

_END = date.today()
_START = _END - timedelta(days=365)

QUERY = f"""
    SELECT
        search_term_view.search_term,
        search_term_view.status,
        campaign.name,
        ad_group.name,
        metrics.impressions,
        metrics.clicks,
        metrics.ctr,
        metrics.average_cpc,
        metrics.cost_micros,
        metrics.conversions
    FROM search_term_view
    WHERE segments.date BETWEEN '{_START.isoformat()}' AND '{_END.isoformat()}'
    ORDER BY metrics.impressions DESC
"""


def main() -> None:
    client = GoogleAdsClient.load_from_storage("google-ads.yaml")
    service = client.get_service("GoogleAdsService")

    try:
        stream = service.search_stream(customer_id=CUSTOMER_ID, query=QUERY)

        out_path = f"search_terms_{date.today().isoformat()}.csv"
        rows = 0
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "termino_de_busqueda",
                    "estado",
                    "campana",
                    "grupo_de_anuncios",
                    "impresiones",
                    "clics",
                    "ctr",
                    "cpc_promedio_usd",
                    "coste_usd",
                    "conversiones",
                ]
            )
            for batch in stream:
                for row in batch.results:
                    metrics = row.metrics
                    writer.writerow(
                        [
                            row.search_term_view.search_term,
                            row.search_term_view.status.name,
                            row.campaign.name,
                            row.ad_group.name,
                            metrics.impressions,
                            metrics.clicks,
                            round(metrics.ctr, 4),
                            round(metrics.average_cpc / 1_000_000, 2),
                            round(metrics.cost_micros / 1_000_000, 2),
                            metrics.conversions,
                        ]
                    )
                    rows += 1

        print(f"Listo: {rows} términos exportados a {out_path}")
        if rows == 0:
            print(
                "0 filas: las campañas llevan pausadas demasiado tiempo o no "
                "hay historial en los últimos 12 meses."
            )
    except GoogleAdsException as ex:
        for error in ex.failure.errors:
            print(f"Error de la API: {error.message}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
