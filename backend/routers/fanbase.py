from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import VoterAccount, FanbaseEntry
from backend.schemas import FanbaseCreate, FanbaseUpdate, FanbaseOut

router = APIRouter(prefix="/voters/{voter_id}/fanbase", tags=["fanbase"])


def _get_voter_or_404(voter_id: int, db: Session) -> VoterAccount:
    voter = db.query(VoterAccount).filter(VoterAccount.id == voter_id).first()
    if not voter:
        raise HTTPException(404, "Voter not found")
    return voter


@router.get("", response_model=list[FanbaseOut])
def list_fanbase(voter_id: int, db: Session = Depends(get_db)):
    _get_voter_or_404(voter_id, db)
    entries = (
        db.query(FanbaseEntry)
        .filter(FanbaseEntry.voter_id == voter_id)
        .order_by(FanbaseEntry.author)
        .all()
    )
    return entries


@router.post("", response_model=FanbaseOut, status_code=201)
def add_fanbase_entry(voter_id: int, body: FanbaseCreate, db: Session = Depends(get_db)):
    _get_voter_or_404(voter_id, db)

    existing = (
        db.query(FanbaseEntry)
        .filter(FanbaseEntry.voter_id == voter_id, FanbaseEntry.author == body.author)
        .first()
    )
    if existing:
        raise HTTPException(400, f"Author '{body.author}' already in fanbase for this voter")

    entry = FanbaseEntry(voter_id=voter_id, **body.model_dump())
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@router.get("/{entry_id}", response_model=FanbaseOut)
def get_fanbase_entry(voter_id: int, entry_id: int, db: Session = Depends(get_db)):
    _get_voter_or_404(voter_id, db)
    entry = (
        db.query(FanbaseEntry)
        .filter(FanbaseEntry.id == entry_id, FanbaseEntry.voter_id == voter_id)
        .first()
    )
    if not entry:
        raise HTTPException(404, "Fanbase entry not found")
    return entry


@router.patch("/{entry_id}", response_model=FanbaseOut)
def update_fanbase_entry(voter_id: int, entry_id: int, body: FanbaseUpdate, db: Session = Depends(get_db)):
    _get_voter_or_404(voter_id, db)
    entry = (
        db.query(FanbaseEntry)
        .filter(FanbaseEntry.id == entry_id, FanbaseEntry.voter_id == voter_id)
        .first()
    )
    if not entry:
        raise HTTPException(404, "Fanbase entry not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(entry, field, value)

    db.commit()
    db.refresh(entry)
    return entry


@router.delete("/{entry_id}", status_code=204)
def delete_fanbase_entry(voter_id: int, entry_id: int, db: Session = Depends(get_db)):
    _get_voter_or_404(voter_id, db)
    entry = (
        db.query(FanbaseEntry)
        .filter(FanbaseEntry.id == entry_id, FanbaseEntry.voter_id == voter_id)
        .first()
    )
    if not entry:
        raise HTTPException(404, "Fanbase entry not found")
    db.delete(entry)
    db.commit()
