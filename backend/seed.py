"""
Seed script — migrates the existing cur8 fanbase from curation_bot.py into the DB.

Usage:
    1. Set env vars FERNET_KEY and CUR8_POSTING_KEY
    2. python -m backend.seed
"""
import os
import sys

from backend.database import engine, SessionLocal
from backend.models import Base, VoterAccount, FanbaseEntry
from backend.config import get_fernet

# ── Current fanbase for voter "cur8" (extracted from curation_bot.py) ──
# Only active (uncommented) entries included.
CUR8_FANBASE = [
    {"author": "im-ridd", "vote_percentage": 50, "post_delay_minutes": 4.9, "daily_vote_limit": 1},
    {"author": "stefano.massari", "vote_percentage": 5, "post_delay_minutes": 4, "daily_vote_limit": 1},
    {"author": "mikitaly", "vote_percentage": 5, "post_delay_minutes": 4, "daily_vote_limit": 1},
    {"author": "frafiomatale", "vote_percentage": 5, "post_delay_minutes": 4, "daily_vote_limit": 1},
    {"author": "rp-k-0", "vote_percentage": 25, "post_delay_minutes": 4, "daily_vote_limit": 1},
    {"author": "tangera", "vote_percentage": 7, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "davilo", "vote_percentage": 15, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "vingroup", "vote_percentage": 15, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "karianaporras", "vote_percentage": 7, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "patjewell", "vote_percentage": 20, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "trafalgar", "vote_percentage": 30, "post_delay_minutes": 1.8, "daily_vote_limit": 1},
    {"author": "rme", "vote_percentage": 5, "post_delay_minutes": 2.5, "daily_vote_limit": 1},
    {"author": "kabula", "vote_percentage": 20, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "happycapital", "vote_percentage": 25, "post_delay_minutes": 4.4, "daily_vote_limit": 1},
    {"author": "zumed", "vote_percentage": 30, "post_delay_minutes": 4, "daily_vote_limit": 1},
    {"author": "cjsdns", "vote_percentage": 10, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "mimes", "vote_percentage": 20, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "gogikr", "vote_percentage": 20, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "arbitration", "vote_percentage": 7, "post_delay_minutes": 4, "daily_vote_limit": 1},
    {"author": "alvinwales", "vote_percentage": 7, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "indicatormode", "vote_percentage": 40, "post_delay_minutes": 4, "daily_vote_limit": 1},
    {"author": "ritzy-writer", "vote_percentage": 40, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "powergogo", "vote_percentage": 7, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "jgwonkim", "vote_percentage": 25, "post_delay_minutes": 3.8, "daily_vote_limit": 1},
    {"author": "sagor1233", "vote_percentage": 7, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "pmart", "vote_percentage": 10, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "anggali", "vote_percentage": 10, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "fxsajol", "vote_percentage": 10, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "iostkr", "vote_percentage": 7, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "lieutenantdan", "vote_percentage": 30, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "ini4909", "vote_percentage": 15, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "inchonbitcoin", "vote_percentage": 15, "post_delay_minutes": 4, "daily_vote_limit": 1},
    {"author": "nazmulhudaa", "vote_percentage": 15, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "pomeline", "vote_percentage": 7.5, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "saikat890", "vote_percentage": 5, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "amarbanglablog", "vote_percentage": 4.5, "post_delay_minutes": 3.5, "daily_vote_limit": 1},
    {"author": "optv1", "vote_percentage": 15, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "justyy", "vote_percentage": 10, "post_delay_minutes": 4, "daily_vote_limit": 1},
    {"author": "arsalaan", "vote_percentage": 25, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "colds", "vote_percentage": 45, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "steem-articles", "vote_percentage": 20, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "countach.rico", "vote_percentage": 45, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "abbc-reports", "vote_percentage": 20, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "successgr", "vote_percentage": 15, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "bbn1", "vote_percentage": 5, "post_delay_minutes": 2.9, "daily_vote_limit": 1},
    {"author": "thinking.element", "vote_percentage": 30, "post_delay_minutes": 4.3, "daily_vote_limit": 1},
    {"author": "abb-featured", "vote_percentage": 30, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "t-s-k", "vote_percentage": 40, "post_delay_minutes": 4, "daily_vote_limit": 1},
    {"author": "boc-reports", "vote_percentage": 15, "post_delay_minutes": 4.3, "daily_vote_limit": 1},
    {"author": "bumblecat", "vote_percentage": 20, "post_delay_minutes": 4, "daily_vote_limit": 1},
    {"author": "jihad75", "vote_percentage": 20, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "leguna", "vote_percentage": 20, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "lyh5926", "vote_percentage": 25, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "nilaymajumder", "vote_percentage": 5, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "sa-reports", "vote_percentage": 20, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "un-stoppable", "vote_percentage": 35, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "uco.bnb-d", "vote_percentage": 10, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "uco.btc-l", "vote_percentage": 15, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "hotspotitaly", "vote_percentage": 10, "post_delay_minutes": 4.8, "daily_vote_limit": 1},
    {"author": "mia.fobos", "vote_percentage": 5, "post_delay_minutes": 4.3, "daily_vote_limit": 1},
    {"author": "luciojolly", "vote_percentage": 30, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "tasubot", "vote_percentage": 7.5, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "fahriz4l", "vote_percentage": 25, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "wildnature1", "vote_percentage": 5, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "dexpartacus", "vote_percentage": 15, "post_delay_minutes": 4.8, "daily_vote_limit": 1},
    {"author": "centering", "vote_percentage": 3, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "rtytf2", "vote_percentage": 5, "post_delay_minutes": 2.6, "daily_vote_limit": 1},
    {"author": "boddhisattva", "vote_percentage": 10, "post_delay_minutes": 3.8, "daily_vote_limit": 1},
    {"author": "steemhop.org", "vote_percentage": 10, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "raintears", "vote_percentage": 40, "post_delay_minutes": 3.8, "daily_vote_limit": 1},
    {"author": "steemzzang", "vote_percentage": 25, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "diamond31", "vote_percentage": 15, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "upvu.witness", "vote_percentage": 30, "post_delay_minutes": 4, "daily_vote_limit": 1},
    {"author": "khaiyoui", "vote_percentage": 10, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "dodoim", "vote_percentage": 10, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "hodolbak", "vote_percentage": 10, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "ety001", "vote_percentage": 20, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "jjy", "vote_percentage": 15, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "fj1", "vote_percentage": 5, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "pennsif", "vote_percentage": 15, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "bigram13", "vote_percentage": 10, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "jrcornel", "vote_percentage": 15, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "thedogekid", "vote_percentage": 10, "post_delay_minutes": 2.8, "daily_vote_limit": 1},
    {"author": "threadstream", "vote_percentage": 15, "post_delay_minutes": 4, "daily_vote_limit": 1},
    {"author": "feuerelfe", "vote_percentage": 15, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "puss.coin", "vote_percentage": 20, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "tfc-reports", "vote_percentage": 15, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "ocean-trench", "vote_percentage": 25, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "georgia14", "vote_percentage": 5, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "abb-fun", "vote_percentage": 30, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "blackeyedm", "vote_percentage": 20, "post_delay_minutes": 3.9, "daily_vote_limit": 1},
    {"author": "blacks", "vote_percentage": 30, "post_delay_minutes": 4, "daily_vote_limit": 1},
    {"author": "whoisjohn", "vote_percentage": 15, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "curious.mind", "vote_percentage": 30, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "darklights", "vote_percentage": 35, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "heartwarming", "vote_percentage": 15, "post_delay_minutes": 4.4, "daily_vote_limit": 1},
    {"author": "ciusk", "vote_percentage": 5, "post_delay_minutes": 4.7, "daily_vote_limit": 1},
    {"author": "jejudomin", "vote_percentage": 15, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "winkles", "vote_percentage": 15, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "girolamomarotta", "vote_percentage": 10, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "mad-runner", "vote_percentage": 10, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "parkname", "vote_percentage": 15, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "luna88", "vote_percentage": 10, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "libera-tor", "vote_percentage": 5, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "lee1004", "vote_percentage": 10, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "xiguang", "vote_percentage": 5, "post_delay_minutes": 4.9, "daily_vote_limit": 1},
    {"author": "oswaldocuarta", "vote_percentage": 5, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "ppomppu", "vote_percentage": 20, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "realrobinhood", "vote_percentage": 10, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "krishiv-g", "vote_percentage": 5, "post_delay_minutes": 4.9, "daily_vote_limit": 1},
    {"author": "cancerdoctor", "vote_percentage": 5, "post_delay_minutes": 3, "daily_vote_limit": 1},
    {"author": "yela", "vote_percentage": 15, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "abuse-watcher", "vote_percentage": 15, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "alimkhan", "vote_percentage": 5, "post_delay_minutes": 4.8, "daily_vote_limit": 1},
    {"author": "arman-alif", "vote_percentage": 15, "post_delay_minutes": 4, "daily_vote_limit": 1},
    {"author": "anailuj1992", "vote_percentage": 25, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "dove11", "vote_percentage": 5, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "sualeha", "vote_percentage": 5, "post_delay_minutes": 3.2, "daily_vote_limit": 1},
    {"author": "zzan7", "vote_percentage": 15, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "blaze.apps", "vote_percentage": 30, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "cur8.witness", "vote_percentage": 30, "post_delay_minutes": 4.9, "daily_vote_limit": 1},
    {"author": "solaymann", "vote_percentage": 2, "post_delay_minutes": 4.9, "daily_vote_limit": 1},
    {"author": "samhunnahar", "vote_percentage": 2, "post_delay_minutes": 4.9, "daily_vote_limit": 1},
    {"author": "max-pro", "vote_percentage": 5, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "afzalqamar", "vote_percentage": 5, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "ahsansharif", "vote_percentage": 5, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "narocky71", "vote_percentage": 5, "post_delay_minutes": 4.5, "daily_vote_limit": 1},
    {"author": "issambashir", "vote_percentage": 25, "post_delay_minutes": 3.8, "daily_vote_limit": 2},
    {"author": "new.chain-stats", "vote_percentage": 35, "post_delay_minutes": 3.9, "daily_vote_limit": 1},
    {"author": "rokonn", "vote_percentage": 10, "post_delay_minutes": 4.8, "daily_vote_limit": 1},
]


def seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        # Check if cur8 voter already exists
        existing = db.query(VoterAccount).filter(VoterAccount.username == "cur8").first()
        if existing:
            print(f"Voter 'cur8' already exists (id={existing.id}). Skipping voter creation.")
            voter = existing
        else:
            posting_key = os.getenv("CUR8_POSTING_KEY", "")
            if not posting_key:
                print("WARNING: CUR8_POSTING_KEY env var not set. Using placeholder.")
                posting_key = "PLACEHOLDER_SET_ME_LATER"

            fernet = get_fernet()
            voter = VoterAccount(
                username="cur8",
                posting_key_encrypted=fernet.encrypt(posting_key.encode()).decode(),
                min_voting_power=80.0,
                max_post_age_minutes=5.0,
                interval_seconds=1,
                enabled=True,
            )
            db.add(voter)
            db.commit()
            db.refresh(voter)
            print(f"Created voter 'cur8' (id={voter.id})")

        # Seed fanbase entries
        added = 0
        skipped = 0
        for entry_data in CUR8_FANBASE:
            exists = (
                db.query(FanbaseEntry)
                .filter(
                    FanbaseEntry.voter_id == voter.id,
                    FanbaseEntry.author == entry_data["author"],
                )
                .first()
            )
            if exists:
                skipped += 1
                continue

            entry = FanbaseEntry(
                voter_id=voter.id,
                author=entry_data["author"],
                vote_percentage=entry_data["vote_percentage"],
                post_delay_minutes=entry_data["post_delay_minutes"],
                daily_vote_limit=entry_data["daily_vote_limit"],
                add_comment=False,
                enabled=True,
            )
            db.add(entry)
            added += 1

        db.commit()
        print(f"Fanbase seed complete: {added} added, {skipped} already existed.")
        print(f"Total fanbase entries for cur8: {db.query(FanbaseEntry).filter(FanbaseEntry.voter_id == voter.id).count()}")

    finally:
        db.close()


if __name__ == "__main__":
    seed()
