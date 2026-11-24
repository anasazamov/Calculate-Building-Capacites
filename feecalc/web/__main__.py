"""Start the web interface: python -m feecalc.web  (127.0.0.1:8000 only; reach it through an SSH tunnel)."""
import uvicorn

from .app import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host="127.0.0.1", port=8000, log_level="info")
