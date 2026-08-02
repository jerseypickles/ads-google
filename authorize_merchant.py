"""Autorización one-shot para Merchant API (scope content).

Abre el navegador, pides consentimiento con info@jerseypickles.com y guarda
el refresh token en .merchant_token.json (chmod 600). El token de Google Ads
no se toca.
"""

import json
import os
from pathlib import Path

import yaml
from google_auth_oauthlib.flow import InstalledAppFlow

BASE = Path(__file__).parent
SCOPES = ["https://www.googleapis.com/auth/content"]
OUT = BASE / ".merchant_token.json"


def main() -> None:
    cfg = yaml.safe_load((BASE / "google-ads.yaml").read_text())
    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        scopes=SCOPES,
    )
    creds = flow.run_local_server(port=8090, prompt="consent",
                                  authorization_prompt_message="")
    OUT.write_text(json.dumps({
        "refresh_token": creds.refresh_token,
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "scopes": SCOPES,
    }))
    os.chmod(OUT, 0o600)
    print(f"✓ Token de Merchant guardado en {OUT.name}")


if __name__ == "__main__":
    main()
