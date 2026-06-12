from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from backend.config import DATABASE_URL

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def run_migrations():
    """Apply lightweight SQLite migrations for new columns."""
    with engine.begin() as conn:
        # Add trail_only if the column does not yet exist (SQLite has no IF NOT EXISTS for ADD COLUMN)
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(voter_accounts)"))}
        if "trail_only" not in cols:
            conn.execute(text("ALTER TABLE voter_accounts ADD COLUMN trail_only INTEGER NOT NULL DEFAULT 0"))


def get_db() -> Session:
    """FastAPI dependency — yields a DB session and closes it after."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
