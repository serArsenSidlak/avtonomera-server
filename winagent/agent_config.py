"""Agent configuration. The CI build overwrites this file with real values from repo secrets.

For local testing you can set env vars AGENT_SERVER_URL / AGENT_INGEST_SECRET instead.
"""
import os

SERVER_URL = os.environ.get("AGENT_SERVER_URL", "https://34.123.136.171.nip.io")
INGEST_SECRET = os.environ.get("AGENT_INGEST_SECRET", "")
