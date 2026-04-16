from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


# ── VoterAccount ──

class VoterCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    posting_key: str = Field(..., min_length=1, description="WIF posting key (will be encrypted at rest)")
    min_voting_power: float = 80.0
    max_post_age_minutes: float = 5.0
    interval_seconds: int = 1
    enabled: bool = True


class VoterUpdate(BaseModel):
    posting_key: Optional[str] = None
    min_voting_power: Optional[float] = None
    max_post_age_minutes: Optional[float] = None
    interval_seconds: Optional[int] = None
    enabled: Optional[bool] = None


class VoterOut(BaseModel):
    id: int
    username: str
    min_voting_power: float
    max_post_age_minutes: float
    interval_seconds: int
    enabled: bool
    created_at: datetime
    updated_at: datetime
    fanbase_count: int = 0

    model_config = {"from_attributes": True}


# ── FanbaseEntry ──

class FanbaseCreate(BaseModel):
    author: str = Field(..., min_length=1, max_length=50)
    vote_percentage: float = Field(50.0, gt=0, le=100)
    post_delay_minutes: float = Field(4.0, ge=0)
    daily_vote_limit: int = Field(1, ge=1)
    add_comment: bool = False
    comment_text: str = ""
    add_image: bool = False
    image_path: str = ""
    enabled: bool = True


class FanbaseUpdate(BaseModel):
    vote_percentage: Optional[float] = Field(None, gt=0, le=100)
    post_delay_minutes: Optional[float] = Field(None, ge=0)
    daily_vote_limit: Optional[int] = Field(None, ge=1)
    add_comment: Optional[bool] = None
    comment_text: Optional[str] = None
    add_image: Optional[bool] = None
    image_path: Optional[str] = None
    enabled: Optional[bool] = None


class FanbaseOut(BaseModel):
    id: int
    voter_id: int
    author: str
    vote_percentage: float
    post_delay_minutes: float
    daily_vote_limit: int
    add_comment: bool
    comment_text: str
    add_image: bool
    image_path: str
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── TrailRule ──

class TrailCreate(BaseModel):
    follower_id: int
    leader_username: str = Field(..., min_length=1, max_length=50)
    weight_scale: float = Field(1.0, gt=0)
    max_weight: float = Field(100.0, gt=0, le=100)
    delay_seconds: int = Field(0, ge=0)
    enabled: bool = True


class TrailUpdate(BaseModel):
    weight_scale: Optional[float] = Field(None, gt=0)
    max_weight: Optional[float] = Field(None, gt=0, le=100)
    delay_seconds: Optional[int] = Field(None, ge=0)
    enabled: Optional[bool] = None


class TrailOut(BaseModel):
    id: int
    follower_id: int
    leader_username: str
    weight_scale: float
    max_weight: float
    delay_seconds: int
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
