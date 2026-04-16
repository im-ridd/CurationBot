"""Frontend router — serves HTML pages via Jinja2 templates."""

import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from beem import Steem
from beem.account import Account

from backend.database import get_db
from backend.models import VoterAccount, FanbaseEntry, TrailRule
from backend.config import get_fernet, STEEM_NODES
from backend.services.bot_manager import BotManager

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["frontend"])

_template_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))


def _mgr() -> BotManager:
    return BotManager()


def _encrypt_key(plain_key: str) -> str:
    return get_fernet().encrypt(plain_key.encode()).decode()


# ────────────────────── Pages ──────────────────────


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    voters_total = db.query(func.count(VoterAccount.id)).scalar()
    voters_enabled = db.query(func.count(VoterAccount.id)).filter(VoterAccount.enabled.is_(True)).scalar()
    fb_total = db.query(func.count(FanbaseEntry.id)).scalar()
    fb_enabled = db.query(func.count(FanbaseEntry.id)).filter(FanbaseEntry.enabled.is_(True)).scalar()
    tr_total = db.query(func.count(TrailRule.id)).scalar()
    tr_enabled = db.query(func.count(TrailRule.id)).filter(TrailRule.enabled.is_(True)).scalar()

    voters = (
        db.query(VoterAccount, func.count(FanbaseEntry.id))
        .outerjoin(FanbaseEntry)
        .group_by(VoterAccount.id)
        .all()
    )
    voter_list = []
    for v, cnt in voters:
        voter_list.append({
            "id": v.id, "username": v.username, "enabled": v.enabled,
            "min_voting_power": v.min_voting_power,
            "max_post_age_minutes": v.max_post_age_minutes,
            "fanbase_count": cnt,
        })

    stats = {
        "voters": {"total": voters_total, "enabled": voters_enabled},
        "fanbase_entries": {"total": fb_total, "enabled": fb_enabled},
        "trail_rules": {"total": tr_total, "enabled": tr_enabled},
    }
    return templates.TemplateResponse(request, "dashboard.html", {
        "stats": stats, "voters": voter_list,
    })


@router.get("/voters/{voter_id}", response_class=HTMLResponse)
def voter_detail(request: Request, voter_id: int, db: Session = Depends(get_db)):
    voter = db.query(VoterAccount).filter(VoterAccount.id == voter_id).first()
    if not voter:
        return RedirectResponse("/ui", status_code=303)
    cnt = db.query(func.count(FanbaseEntry.id)).filter(FanbaseEntry.voter_id == voter_id).scalar()
    fanbase = (
        db.query(FanbaseEntry)
        .filter(FanbaseEntry.voter_id == voter_id)
        .order_by(FanbaseEntry.author)
        .all()
    )
    flash = request.query_params.get("flash")
    flash_error = request.query_params.get("error")
    return templates.TemplateResponse(request, "voter_detail.html", {
        "voter": {
            "id": voter.id, "username": voter.username, "enabled": voter.enabled,
            "min_voting_power": voter.min_voting_power,
            "max_post_age_minutes": voter.max_post_age_minutes,
            "fanbase_count": cnt,
        },
        "fanbase": fanbase,
        "flash": flash, "flash_error": flash_error,
    })


@router.get("/trails", response_class=HTMLResponse)
def trails_page(request: Request, db: Session = Depends(get_db)):
    rules = db.query(TrailRule).order_by(TrailRule.id).all()
    voters = db.query(VoterAccount).order_by(VoterAccount.username).all()

    # Resolve follower names
    voter_map = {v.id: v.username for v in voters}
    trail_list = []
    for r in rules:
        trail_list.append({
            "id": r.id,
            "follower_name": voter_map.get(r.follower_id, f"id:{r.follower_id}"),
            "leader_username": r.leader_username,
            "weight_scale": r.weight_scale,
            "max_weight": r.max_weight,
            "delay_seconds": r.delay_seconds,
            "enabled": r.enabled,
        })

    flash = request.query_params.get("flash")
    flash_error = request.query_params.get("error")
    return templates.TemplateResponse(request, "trails.html", {
        "trails": trail_list,
        "voters": [{"id": v.id, "username": v.username} for v in voters],
        "flash": flash, "flash_error": flash_error,
    })


# ────────────────────── HTMX partials ──────────────────────


def _fetch_account_info(username: str) -> dict:
    """Fetch live account data from Steem blockchain."""
    try:
        steem = Steem(node=STEEM_NODES)
        acc = Account(username, blockchain_instance=steem)
        vp = acc.get_voting_power()
        sp = float(acc.get_steem_power())
        rep = float(acc.get_reputation())
        rc_pct = 100.0
        try:
            rc = acc.get_rc_manabar()
            rc_pct = rc.get("current_pct", 100.0) if isinstance(rc, dict) else 100.0
        except Exception:
            pass
        balance_steem = str(acc.balances["available"][0])
        balance_sbd = str(acc.balances["available"][1])
        return {
            "username": username,
            "vp": round(vp, 2),
            "sp": round(sp, 2),
            "reputation": round(rep, 1),
            "rc_pct": round(rc_pct, 2),
            "balance_steem": balance_steem,
            "balance_sbd": balance_sbd,
            "ok": True,
        }
    except Exception as e:
        log.error(f"Failed to fetch account info for @{username}: {e}")
        return {"username": username, "ok": False, "error": str(e)}


@router.get("/partials/account-cards", response_class=HTMLResponse)
def partial_account_cards(request: Request, db: Session = Depends(get_db)):
    """HTMX partial: live blockchain account data for all voters."""
    voters = db.query(VoterAccount).order_by(VoterAccount.id).all()
    usernames = [v.username for v in voters]

    accounts = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_account_info, u): u for u in usernames}
        for f in as_completed(futures):
            accounts.append(f.result())
    accounts.sort(key=lambda a: a["username"])

    return templates.TemplateResponse(request, "partials/account_cards.html", {
        "accounts": accounts,
    })


@router.get("/partials/account-info/{username}", response_class=HTMLResponse)
def partial_single_account(request: Request, username: str):
    """HTMX partial: live blockchain data for one voter."""
    info = _fetch_account_info(username)
    return templates.TemplateResponse(request, "partials/account_info_single.html", {
        "acc": info,
    })


@router.get("/partials/runtime-status", response_class=HTMLResponse)
def partial_runtime_status(request: Request):
    mgr = _mgr()
    return templates.TemplateResponse(request, "partials/runtime_status.html", {
        "curation": mgr.get_all_status(),
        "trails": mgr.get_all_trail_status(),
    })


@router.get("/partials/trail-status", response_class=HTMLResponse)
def partial_trail_status(request: Request):
    mgr = _mgr()
    return templates.TemplateResponse(request, "partials/trail_status.html", {
        "trails": mgr.get_all_trail_status(),
    })


@router.get("/partials/activity", response_class=HTMLResponse)
def partial_activity(request: Request):
    """HTMX partial: merged activity feed from all curation + trail engines."""
    mgr = _mgr()
    events = []
    for s in mgr.get_all_status():
        for ev in s.get("activity", []):
            events.append({**ev, "source": s["voter"], "type": "curation"})
    for s in mgr.get_all_trail_status():
        for ev in s.get("activity", []):
            events.append({**ev, "source": s["voter"], "author": "", "type": "trail"})
    # Sort newest first (already newest-first per engine, merge sort by timestamp)
    events.sort(key=lambda e: e["ts"], reverse=True)
    return templates.TemplateResponse(request, "partials/activity_feed.html", {
        "events": events[:80],
    })


@router.get("/partials/trail-activity", response_class=HTMLResponse)
def partial_trail_activity(request: Request):
    """HTMX partial: trail-only activity feed."""
    mgr = _mgr()
    events = []
    for s in mgr.get_all_trail_status():
        for ev in s.get("activity", []):
            events.append({**ev, "source": s["voter"]})
    events.sort(key=lambda e: e["ts"], reverse=True)
    return templates.TemplateResponse(request, "partials/trail_activity.html", {
        "events": events[:50],
    })


# ────────────────────── Form actions: Voters ──────────────────────


@router.post("/voters/add")
def form_add_voter(
    username: str = Form(...),
    posting_key: str = Form(...),
    min_voting_power: float = Form(80.0),
    max_post_age_minutes: float = Form(5.0),
    db: Session = Depends(get_db),
):
    existing = db.query(VoterAccount).filter(VoterAccount.username == username).first()
    if existing:
        return RedirectResponse("/ui?flash=Voter+already+exists&error=1", status_code=303)
    voter = VoterAccount(
        username=username,
        posting_key_encrypted=_encrypt_key(posting_key),
        min_voting_power=min_voting_power,
        max_post_age_minutes=max_post_age_minutes,
        enabled=True,
    )
    db.add(voter)
    db.commit()
    return RedirectResponse(f"/ui/voters/{voter.id}?flash=Voter+created", status_code=303)


@router.post("/voters/{voter_id}/edit")
def form_edit_voter(
    voter_id: int,
    min_voting_power: float = Form(...),
    max_post_age_minutes: float = Form(...),
    enabled: str = Form("true"),
    posting_key: str = Form(""),
    db: Session = Depends(get_db),
):
    voter = db.query(VoterAccount).filter(VoterAccount.id == voter_id).first()
    if not voter:
        return RedirectResponse("/ui", status_code=303)
    voter.min_voting_power = min_voting_power
    voter.max_post_age_minutes = max_post_age_minutes
    voter.enabled = enabled.lower() == "true"
    if posting_key.strip():
        voter.posting_key_encrypted = _encrypt_key(posting_key.strip())
    db.commit()
    return RedirectResponse(f"/ui/voters/{voter_id}?flash=Settings+saved", status_code=303)


# ────────────────────── Form actions: Fanbase ──────────────────────


@router.post("/voters/{voter_id}/fanbase/add")
def form_add_fanbase(
    voter_id: int,
    author: str = Form(...),
    vote_percentage: float = Form(10.0),
    post_delay_minutes: float = Form(4.0),
    daily_vote_limit: int = Form(1),
    add_comment: str = Form(""),
    comment_text: str = Form(""),
    db: Session = Depends(get_db),
):
    existing = (
        db.query(FanbaseEntry)
        .filter(FanbaseEntry.voter_id == voter_id, FanbaseEntry.author == author)
        .first()
    )
    if existing:
        return RedirectResponse(
            f"/ui/voters/{voter_id}?flash=Author+already+exists&error=1", status_code=303,
        )
    entry = FanbaseEntry(
        voter_id=voter_id,
        author=author.strip().lower(),
        vote_percentage=vote_percentage,
        post_delay_minutes=post_delay_minutes,
        daily_vote_limit=daily_vote_limit,
        add_comment=add_comment == "true",
        comment_text=comment_text,
    )
    db.add(entry)
    db.commit()
    return RedirectResponse(f"/ui/voters/{voter_id}?flash=Author+added", status_code=303)


@router.post("/voters/{voter_id}/fanbase/{entry_id}/edit")
def form_edit_fanbase(
    voter_id: int,
    entry_id: int,
    vote_percentage: float = Form(...),
    post_delay_minutes: float = Form(...),
    daily_vote_limit: int = Form(...),
    add_comment: str = Form(""),
    comment_text: str = Form(""),
    enabled: str = Form("true"),
    db: Session = Depends(get_db),
):
    entry = (
        db.query(FanbaseEntry)
        .filter(FanbaseEntry.id == entry_id, FanbaseEntry.voter_id == voter_id)
        .first()
    )
    if not entry:
        return RedirectResponse(f"/ui/voters/{voter_id}?flash=Entry+not+found&error=1", status_code=303)
    entry.vote_percentage = vote_percentage
    entry.post_delay_minutes = post_delay_minutes
    entry.daily_vote_limit = daily_vote_limit
    entry.add_comment = add_comment == "true"
    entry.comment_text = comment_text
    entry.enabled = enabled.lower() == "true"
    db.commit()
    return RedirectResponse(f"/ui/voters/{voter_id}?flash=Author+updated", status_code=303)


@router.post("/voters/{voter_id}/fanbase/{entry_id}/delete")
def form_delete_fanbase(voter_id: int, entry_id: int, db: Session = Depends(get_db)):
    entry = (
        db.query(FanbaseEntry)
        .filter(FanbaseEntry.id == entry_id, FanbaseEntry.voter_id == voter_id)
        .first()
    )
    if entry:
        db.delete(entry)
        db.commit()
    return RedirectResponse(f"/ui/voters/{voter_id}?flash=Author+removed", status_code=303)


# ────────────────────── Form actions: Trails ──────────────────────


@router.post("/trails/add")
def form_add_trail(
    follower_id: int = Form(...),
    leader_username: str = Form(...),
    weight_scale: float = Form(1.0),
    max_weight: float = Form(100.0),
    delay_seconds: int = Form(0),
    db: Session = Depends(get_db),
):
    follower = db.query(VoterAccount).filter(VoterAccount.id == follower_id).first()
    if not follower:
        return RedirectResponse("/ui/trails?flash=Follower+not+found&error=1", status_code=303)
    existing = (
        db.query(TrailRule)
        .filter(TrailRule.follower_id == follower_id, TrailRule.leader_username == leader_username)
        .first()
    )
    if existing:
        return RedirectResponse("/ui/trails?flash=Trail+already+exists&error=1", status_code=303)
    rule = TrailRule(
        follower_id=follower_id,
        leader_username=leader_username.strip().lower(),
        weight_scale=weight_scale,
        max_weight=max_weight,
        delay_seconds=delay_seconds,
    )
    db.add(rule)
    db.commit()
    return RedirectResponse("/ui/trails?flash=Trail+created", status_code=303)


@router.post("/trails/{trail_id}/edit")
def form_edit_trail(
    trail_id: int,
    weight_scale: float = Form(...),
    max_weight: float = Form(...),
    delay_seconds: int = Form(...),
    enabled: str = Form("true"),
    db: Session = Depends(get_db),
):
    rule = db.query(TrailRule).filter(TrailRule.id == trail_id).first()
    if not rule:
        return RedirectResponse("/ui/trails?flash=Trail+not+found&error=1", status_code=303)
    rule.weight_scale = weight_scale
    rule.max_weight = max_weight
    rule.delay_seconds = delay_seconds
    rule.enabled = enabled.lower() == "true"
    db.commit()
    return RedirectResponse("/ui/trails?flash=Trail+updated", status_code=303)


@router.post("/trails/{trail_id}/delete")
def form_delete_trail(trail_id: int, db: Session = Depends(get_db)):
    rule = db.query(TrailRule).filter(TrailRule.id == trail_id).first()
    if rule:
        db.delete(rule)
        db.commit()
    return RedirectResponse("/ui/trails?flash=Trail+deleted", status_code=303)


# ────────────────────── Bot control (form POST) ──────────────────────


@router.post("/bot/start-all")
def form_start_all():
    _mgr().start_all_enabled()
    return RedirectResponse("/ui?flash=All+engines+started", status_code=303)


@router.post("/bot/stop-all")
def form_stop_all():
    _mgr().stop_all()
    return RedirectResponse("/ui?flash=All+engines+stopped", status_code=303)


@router.post("/bot/voters/{voter_id}/start")
def form_start_voter(voter_id: int):
    _mgr().start_voter(voter_id)
    return RedirectResponse(f"/ui?flash=Voter+{voter_id}+started", status_code=303)


@router.post("/bot/voters/{voter_id}/stop")
def form_stop_voter(voter_id: int):
    _mgr().stop_voter(voter_id)
    return RedirectResponse(f"/ui?flash=Voter+{voter_id}+stopped", status_code=303)


@router.post("/bot/voters/{voter_id}/reload")
def form_reload_voter(voter_id: int):
    _mgr().reload_voter_fanbase(voter_id)
    return RedirectResponse(f"/ui/voters/{voter_id}?flash=Fanbase+reloaded", status_code=303)


@router.post("/bot/trails/start-all")
def form_start_all_trails():
    _mgr().start_all_trails()
    return RedirectResponse("/ui/trails?flash=All+trails+started", status_code=303)
