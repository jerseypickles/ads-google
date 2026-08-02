"""Chequeo del seguimiento de conversiones (pixel) de Jersey Pickles.

Lista las acciones de conversión de la cuenta (estado, tipo, categoría) y
cuántas conversiones registró cada una en los últimos 12 meses.

Uso:
    .venv/bin/python conversion_check.py
"""

import sys
from datetime import date, timedelta

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

CUSTOMER_ID = "4888823590"

_END = date.today()
_START = _END - timedelta(days=365)

ACTIONS_QUERY = """
    SELECT
        conversion_action.id,
        conversion_action.name,
        conversion_action.status,
        conversion_action.type,
        conversion_action.category,
        conversion_action.primary_for_goal
    FROM conversion_action
    ORDER BY conversion_action.status
"""

VOLUME_QUERY = f"""
    SELECT
        segments.conversion_action_name,
        metrics.all_conversions,
        metrics.all_conversions_value
    FROM conversion_action
    WHERE segments.date BETWEEN '{_START.isoformat()}' AND '{_END.isoformat()}'
"""


def main() -> None:
    client = GoogleAdsClient.load_from_storage("google-ads.yaml")
    service = client.get_service("GoogleAdsService")

    try:
        print("=== ACCIONES DE CONVERSIÓN CONFIGURADAS ===")
        for batch in service.search_stream(customer_id=CUSTOMER_ID, query=ACTIONS_QUERY):
            for row in batch.results:
                a = row.conversion_action
                print(
                    f"[{a.status.name}] {a.name} | tipo={a.type_.name} "
                    f"| categoria={a.category.name} | primaria={a.primary_for_goal}"
                )

        print("\n=== CONVERSIONES REGISTRADAS (últimos 12 meses) ===")
        totals = {}
        for batch in service.search_stream(customer_id=CUSTOMER_ID, query=VOLUME_QUERY):
            for row in batch.results:
                name = row.segments.conversion_action_name
                t = totals.setdefault(name, [0.0, 0.0])
                t[0] += row.metrics.all_conversions
                t[1] += row.metrics.all_conversions_value
        if not totals:
            print("(ninguna conversión registrada en el periodo)")
        for name, (conv, value) in sorted(totals.items(), key=lambda x: -x[1][0]):
            print(f"{name}: {conv:.1f} conversiones | valor ${value:,.2f}")
    except GoogleAdsException as ex:
        for error in ex.failure.errors:
            print(f"Error de la API: {error.message}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
