"""Start the web interface: python -m feecalc.web

Default: plain HTTP on 127.0.0.1:8000, no password; reach it through an SSH tunnel.
To listen on another address as well, set FEECALC_HOST / FEECALC_PORT. A non-loopback address requires
FEECALC_PASSWORD (HTTP Basic, any user name) and a TLS pair in FEECALC_TLS_CERT / FEECALC_TLS_KEY;
the plain-HTTP loopback listener on 127.0.0.1:FEECALC_LOCAL_PORT (default 8000) stays available
for SSH tunnels and asks for FEECALC_LOCAL_PASSWORD (default: the same password).
"""
import asyncio
import os

import uvicorn

from .app import create_app

LOOPBACK = ("127.0.0.1", "localhost", "::1")


async def serve(servers: list[uvicorn.Server]) -> None:
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for s in servers:  # one listener stopped (signal or failure): stop the other too
        s.should_exit = True
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    host = os.environ.get("FEECALC_HOST", "127.0.0.1")
    port = int(os.environ.get("FEECALC_PORT", "8000"))
    local_port = int(os.environ.get("FEECALC_LOCAL_PORT", "8000"))
    password = os.environ.get("FEECALC_PASSWORD", "")
    cert = os.environ.get("FEECALC_TLS_CERT")
    key = os.environ.get("FEECALC_TLS_KEY")
    local_password = os.environ.get("FEECALC_LOCAL_PASSWORD", password)
    # FEECALC_API_KEYS="billing:long-random-key,crm:another-key" - one key per integrating system
    api_keys = {key: name for name, _, key in (item.partition(":") for item in os.environ.get("FEECALC_API_KEYS", "").split(",")) if key}
    assert all(len(key) >= 24 for key in api_keys), "every key in FEECALC_API_KEYS must have 24+ characters"
    app = create_app(password=password, port_passwords={local_port: local_password} if host not in LOOPBACK else None,
                     api_keys=api_keys)
    if host in LOOPBACK:
        uvicorn.run(app, host=host, port=port, log_level="info")
    else:
        assert len(password) >= 16, "FEECALC_PASSWORD (16+ characters) is required on a non-loopback address"
        assert cert and key, "FEECALC_TLS_CERT and FEECALC_TLS_KEY are required on a non-loopback address"
        public = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info",
                                               ssl_certfile=cert, ssl_keyfile=key))
        local = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=local_port, log_level="info"))
        asyncio.run(serve([public, local]))
