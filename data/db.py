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
# The FastAPI app (api/db.py) uses a regular pool instead.
engine = create_engine(
    DATABASE_URL,
    poolclass=NullPool,
    connect_args={
        "sslmode": "require",       # Supabase requires SSL
        "options": "-c statement_timeout=0",  # disable per-statement timeout for bulk ingest
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
