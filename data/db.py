# data/db.py
# Database connection — reads from environment, never hardcoded

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable not set. Check your .env file.")

# NullPool is recommended for scripts and batch jobs — avoids connection leaks.
# The FastAPI app (api/db.py) uses a regular pool instead.  The connect args
# below keep long COPY-based loads stable across multiple child-table ingests.
engine = create_engine(
    DATABASE_URL,
    poolclass=NullPool,
    connect_args={
        "sslmode": "require",       # Supabase requires SSL
        "options": "-c statement_timeout=0",  # disable per-statement timeout for bulk ingest
        # TCP keepalives — prevent routers/Supabase from silently dropping long-running
        # connections during bulk COPY operations (critical on Windows where the default
        # OS keepalive fires only after 2 hours).
        "keepalives": 1,
        "keepalives_idle": 60,      # send first keepalive probe after 60s of silence
        "keepalives_interval": 10,  # resend every 10s if no ACK
        "keepalives_count": 5,      # give up after 5 unanswered probes
    },
)


def get_engine():
    return engine


def test_connection():
    """Run this first to confirm the connection works before ingesting."""
    with engine.connect() as conn:
        result = conn.execute(text("SELECT version()"))
        print(f"Connected: {result.fetchone()[0]}")


if __name__ == "__main__":
    test_connection()
