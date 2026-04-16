from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from backend.database import get_db
from backend.models import VoterAccount, FanbaseEntry
from backend.schemas import VoterCreate, VoterUpdate, VoterOut
from backend.config import get_fernet

router = APIRouter(prefix="/voters", tags=["voters"])


def _encrypt_key(plain_key: str) -> str:
    return get_fernet().encrypt(plain_key.encode()).decode()


def _voter_to_out(voter: VoterAccount, fanbase_count: int) -> VoterOut:
    return VoterOut(
        id=voter.id,
        username=voter.username,
        min_voting_power=voter.min_voting_power,
        max_post_age_minutes=voter.max_post_age_minutes,
        interval_seconds=voter.interval_seconds,
        enabled=voter.enabled,
        created_at=voter.created_at,
        updated_at=voter.updated_at,
        fanbase_count=fanbase_count,
    )


@router.get("", response_model=list[VoterOut])
def list_voters(db: Session = Depends(get_db)):
    rows = (
        db.query(VoterAccount, func.count(FanbaseEntry.id))
        .outerjoin(FanbaseEntry)
        .group_by(VoterAccount.id)
        .all()
    )
    return [_voter_to_out(voter, cnt) for voter, cnt in rows]


@router.post("", response_model=VoterOut, status_code=201)
def create_voter(body: VoterCreate, db: Session = Depends(get_db)):
    existing = db.query(VoterAccount).filter(VoterAccount.username == body.username).first()
    if existing:
        raise HTTPException(400, f"Voter '{body.username}' already exists")

    voter = VoterAccount(
        username=body.username,
        posting_key_encrypted=_encrypt_key(body.posting_key),
        min_voting_power=body.min_voting_power,
        max_post_age_minutes=body.max_post_age_minutes,
        interval_seconds=body.interval_seconds,
        enabled=body.enabled,
    )
    db.add(voter)
    db.commit()
    db.refresh(voter)
    return _voter_to_out(voter, 0)


@router.get("/{voter_id}", response_model=VoterOut)
def get_voter(voter_id: int, db: Session = Depends(get_db)):
    voter = db.query(VoterAccount).filter(VoterAccount.id == voter_id).first()
    if not voter:
        raise HTTPException(404, "Voter not found")
    cnt = db.query(func.count(FanbaseEntry.id)).filter(FanbaseEntry.voter_id == voter_id).scalar()
    return _voter_to_out(voter, cnt)


@router.patch("/{voter_id}", response_model=VoterOut)
def update_voter(voter_id: int, body: VoterUpdate, db: Session = Depends(get_db)):
    voter = db.query(VoterAccount).filter(VoterAccount.id == voter_id).first()
    if not voter:
        raise HTTPException(404, "Voter not found")

    update_data = body.model_dump(exclude_unset=True)
    if "posting_key" in update_data:
        voter.posting_key_encrypted = _encrypt_key(update_data.pop("posting_key"))
    for field, value in update_data.items():
        setattr(voter, field, value)

    db.commit()
    db.refresh(voter)
    cnt = db.query(func.count(FanbaseEntry.id)).filter(FanbaseEntry.voter_id == voter_id).scalar()
    return _voter_to_out(voter, cnt)


@router.delete("/{voter_id}", status_code=204)
def delete_voter(voter_id: int, db: Session = Depends(get_db)):
    voter = db.query(VoterAccount).filter(VoterAccount.id == voter_id).first()
    if not voter:
        raise HTTPException(404, "Voter not found")
    db.delete(voter)
    db.commit()
