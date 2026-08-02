"""Genera el refresh token de OAuth para la Google Ads API (flujo de escritorio).

Lee client_id/client_secret de google-ads.yaml, abre el navegador para
autorizar con info@jerseypickles.com y guarda el refresh token de vuelta
en google-ads.yaml automáticamente.

Uso:
    .venv/bin/python generate_refresh_token.py
"""

import pathlib
import re

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/adwords"]
YAML_PATH = pathlib.Path(__file__).parent / "google-ads.yaml"


def read_config() -> tuple[str, str, str]:
    text = YAML_PATH.read_text(encoding="utf-8")
    client_id = re.search(r'^client_id:\s*"([^"]+)"', text, re.M).group(1)
    client_secret = re.search(r'^client_secret:\s*"([^"]+)"', text, re.M).group(1)
    return text, client_id, client_secret


def main() -> None:
    text, client_id, client_secret = read_config()

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
    )

    print("Abriendo el navegador... autoriza con info@jerseypickles.com")
    credentials = flow.run_local_server(port=0, prompt="consent")

    new_text = re.sub(
        r'^refresh_token:\s*".*"',
        f'refresh_token: "{credentials.refresh_token}"',
        text,
        flags=re.M,
    )
    YAML_PATH.write_text(new_text, encoding="utf-8")

    print("\n=== LISTO ===")
    print("Refresh token guardado en google-ads.yaml")


if __name__ == "__main__":
    main()
