"""Start the web interface: python -m feecalc.web

Default: 127.0.0.1:8000, no password; reach it through an SSH tunnel.
To listen on another address set FEECALC_HOST / FEECALC_PORT. A non-loopback address requires
FEECALC_PASSWORD (HTTP Basic, any user name) and a TLS pair in FEECALC_TLS_CERT / FEECALC_TLS_KEY.
"""
import os

import uvicorn

from .app import create_app

if __name__ == "__main__":
    host = os.environ.get("FEECALC_HOST", "127.0.0.1")
    port = int(os.environ.get("FEECALC_PORT", "8000"))
    password = os.environ.get("FEECALC_PASSWORD", "")
    cert = os.environ.get("FEECALC_TLS_CERT")
    key = os.environ.get("FEECALC_TLS_KEY")
    if host not in ("127.0.0.1", "localhost", "::1"):
        assert len(password) >= 16, "FEECALC_PASSWORD (16+ characters) is required on a non-loopback address"
        assert cert and key, "FEECALC_TLS_CERT and FEECALC_TLS_KEY are required on a non-loopback address"
    uvicorn.run(create_app(password=password), host=host, port=port, log_level="info",
                ssl_certfile=cert, ssl_keyfile=key)
