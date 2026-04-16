import os
from cryptography.fernet import Fernet

# --- Steem Nodes ---
STEEM_NODES = [
    "https://api.steemit.com",
    "https://api.moecki.online",
    "https://steemapi.boylikegirl.club",
    "https://cn.steems.top",
    "https://api.worldofxpilar.com",
    "https://api.upvu.org",
]

# --- Database ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./curation_bot.db")

# --- Encryption key for posting keys at rest ---
# Generate once with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Then store in env var FERNET_KEY
FERNET_KEY = os.getenv("FERNET_KEY", "")

def get_fernet() -> Fernet:
    if not FERNET_KEY:
        raise RuntimeError(
            "FERNET_KEY env var not set. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(FERNET_KEY.encode())

# --- API ---
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8000"))
