from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.database import get_db
from backend.models import VoterAccount, FanbaseEntry, TrailRule

router = APIRouter(prefix="/status", tags=["status"])


@router.get("")
def global_status(db: Session = Depends(get_db)):
    voters_total = db.query(func.count(VoterAccount.id)).scalar()
    voters_enabled = (
        db.query(func.count(VoterAccount.id))
        .filter(VoterAccount.enabled.is_(True))
        .scalar()
    )
    fanbase_total = db.query(func.count(FanbaseEntry.id)).scalar()
    fanbase_enabled = (
        db.query(func.count(FanbaseEntry.id))
        .filter(FanbaseEntry.enabled.is_(True))
        .scalar()
    )
    trails_total = db.query(func.count(TrailRule.id)).scalar()
    trails_enabled = (
        db.query(func.count(TrailRule.id))
        .filter(TrailRule.enabled.is_(True))
        .scalar()
    )
    return {
        "voters": {"total": voters_total, "enabled": voters_enabled},
        "fanbase_entries": {"total": fanbase_total, "enabled": fanbase_enabled},
        "trail_rules": {"total": trails_total, "enabled": trails_enabled},
    }


@router.get("/{voter_id}")
def voter_status(voter_id: int, db: Session = Depends(get_db)):
    voter = db.query(VoterAccount).filter(VoterAccount.id == voter_id).first()
    if not voter:
        return {"error": "Voter not found"}

    fanbase = (
        db.query(FanbaseEntry)
        .filter(FanbaseEntry.voter_id == voter_id, FanbaseEntry.enabled.is_(True))
        .all()
    )
    trails = (
        db.query(TrailRule)
        .filter(TrailRule.follower_id == voter_id, TrailRule.enabled.is_(True))
        .all()
    )
    return {
        "voter": voter.username,
        "enabled": voter.enabled,
        "min_voting_power": voter.min_voting_power,
        "max_post_age_minutes": voter.max_post_age_minutes,
        "fanbase_authors": [
            {"author": e.author, "vote_pct": e.vote_percentage, "delay_min": e.post_delay_minutes}
            for e in fanbase
        ],
        "trailing": [
            {"leader": t.leader_username, "scale": t.weight_scale, "max_w": t.max_weight}
            for t in trails
        ],
    }
