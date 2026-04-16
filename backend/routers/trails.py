from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import VoterAccount, TrailRule
from backend.schemas import TrailCreate, TrailUpdate, TrailOut

router = APIRouter(prefix="/trails", tags=["trails"])


@router.get("", response_model=list[TrailOut])
def list_trails(db: Session = Depends(get_db)):
    return db.query(TrailRule).order_by(TrailRule.id).all()


@router.post("", response_model=TrailOut, status_code=201)
def create_trail(body: TrailCreate, db: Session = Depends(get_db)):
    follower = db.query(VoterAccount).filter(VoterAccount.id == body.follower_id).first()
    if not follower:
        raise HTTPException(404, "Follower voter account not found")

    existing = (
        db.query(TrailRule)
        .filter(
            TrailRule.follower_id == body.follower_id,
            TrailRule.leader_username == body.leader_username,
        )
        .first()
    )
    if existing:
        raise HTTPException(400, f"Trail rule already exists for {follower.username} -> {body.leader_username}")

    rule = TrailRule(**body.model_dump())
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.get("/{trail_id}", response_model=TrailOut)
def get_trail(trail_id: int, db: Session = Depends(get_db)):
    rule = db.query(TrailRule).filter(TrailRule.id == trail_id).first()
    if not rule:
        raise HTTPException(404, "Trail rule not found")
    return rule


@router.patch("/{trail_id}", response_model=TrailOut)
def update_trail(trail_id: int, body: TrailUpdate, db: Session = Depends(get_db)):
    rule = db.query(TrailRule).filter(TrailRule.id == trail_id).first()
    if not rule:
        raise HTTPException(404, "Trail rule not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(rule, field, value)

    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/{trail_id}", status_code=204)
def delete_trail(trail_id: int, db: Session = Depends(get_db)):
    rule = db.query(TrailRule).filter(TrailRule.id == trail_id).first()
    if not rule:
        raise HTTPException(404, "Trail rule not found")
    db.delete(rule)
    db.commit()
