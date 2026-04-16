from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class VoterAccount(Base):
    """An account that casts votes (the curator)."""
    __tablename__ = "voter_accounts"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    posting_key_encrypted = Column(Text, nullable=False)
    min_voting_power = Column(Float, default=80.0)
    max_post_age_minutes = Column(Float, default=5.0)
    interval_seconds = Column(Integer, default=1)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # relationships
    fanbase_entries = relationship("FanbaseEntry", back_populates="voter", cascade="all, delete-orphan")
    trail_rules = relationship("TrailRule", back_populates="follower", cascade="all, delete-orphan")


class FanbaseEntry(Base):
    """An author in a voter's fanbase — the voter will auto-vote this author's posts."""
    __tablename__ = "fanbase_entries"

    id = Column(Integer, primary_key=True, index=True)
    voter_id = Column(Integer, ForeignKey("voter_accounts.id", ondelete="CASCADE"), nullable=False)
    author = Column(String(50), nullable=False, index=True)
    vote_percentage = Column(Float, default=50.0)
    post_delay_minutes = Column(Float, default=4.0)
    daily_vote_limit = Column(Integer, default=1)
    add_comment = Column(Boolean, default=False)
    comment_text = Column(Text, default="")
    add_image = Column(Boolean, default=False)
    image_path = Column(String(255), default="")
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # relationships
    voter = relationship("VoterAccount", back_populates="fanbase_entries")


class TrailRule(Base):
    """A rule: follower account replicates votes of leader_username."""
    __tablename__ = "trail_rules"

    id = Column(Integer, primary_key=True, index=True)
    follower_id = Column(Integer, ForeignKey("voter_accounts.id", ondelete="CASCADE"), nullable=False)
    leader_username = Column(String(50), nullable=False, index=True)
    weight_scale = Column(Float, default=1.0)   # multiply leader weight by this
    max_weight = Column(Float, default=100.0)    # cap on final vote weight
    delay_seconds = Column(Integer, default=0)   # wait N seconds after leader vote
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # relationships
    follower = relationship("VoterAccount", back_populates="trail_rules")
