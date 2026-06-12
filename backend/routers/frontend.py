"""Frontend router — serves HTML pages via Jinja2 templates."""

import json
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from beem import Steem
from beem.account import Account

from backend.database import get_db
from backend.models import VoterAccount, FanbaseEntry, TrailRule
from backend.config import get_fernet, STEEM_NODES
from backend.services.bot_manager import BotManager
from backend.services.steem_client import verify_posting_key

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["frontend"])

_template_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))


def _mgr() -> BotManager:
    return BotManager()


def _encrypt_key(plain_key: str) -> str:
    return get_fernet().encrypt(plain_key.encode()).decode()


# ────────────────────── Auth ──────────────────────


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    error = request.query_params.get("error")
    return templates.TemplateResponse(request, "login.html", {"error": error})


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    from backend.config import ADMIN_USER, ADMIN_PASS
    from backend.auth import make_auth_token
    import hmac
    if hmac.compare_digest(username, ADMIN_USER) and hmac.compare_digest(password, ADMIN_PASS):
        token = make_auth_token(username)
        response = RedirectResponse("/ui", status_code=303)
        response.set_cookie("session", token, httponly=True, samesite="lax", max_age=60*60*24*7)
        return response
    return RedirectResponse("/ui/login?error=1", status_code=303)


@router.get("/logout")
def logout():
    response = RedirectResponse("/ui/login", status_code=303)
    response.delete_cookie("session")
    return response


# ────────────────────── Pages ──────────────────────


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    # Stats — curation side only
    voters_total = db.query(func.count(VoterAccount.id)).filter(VoterAccount.trail_only.is_(False)).scalar()
    voters_enabled = db.query(func.count(VoterAccount.id)).filter(VoterAccount.enabled.is_(True), VoterAccount.trail_only.is_(False)).scalar()
    fb_total = db.query(func.count(FanbaseEntry.id)).scalar()
    fb_enabled = db.query(func.count(FanbaseEntry.id)).filter(FanbaseEntry.enabled.is_(True)).scalar()

    voters = (
        db.query(VoterAccount, func.count(FanbaseEntry.id))
        .outerjoin(FanbaseEntry)
        .filter(VoterAccount.trail_only.is_(False))
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
    }
    flash = request.query_params.get("flash")
    flash_error = request.query_params.get("error")
    return templates.TemplateResponse(request, "dashboard.html", {
        "stats": stats, "voters": voter_list,
        "flash": flash, "flash_error": flash_error,
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
    all_voters = db.query(VoterAccount).order_by(VoterAccount.username).all()

    # Voter IDs that have at least one trail rule
    voter_ids_with_rules = {r.follower_id for r in rules}

    # Trail accounts = trail-only accounts OR curation voters that also have trail rules
    trail_accounts_raw = [
        v for v in all_voters
        if v.trail_only or v.id in voter_ids_with_rules
    ]

    # Count trail rules per account for display
    trail_rule_counts = {}
    for r in rules:
        trail_rule_counts[r.follower_id] = trail_rule_counts.get(r.follower_id, 0) + 1

    trail_account_list = [{
        "id": v.id,
        "username": v.username,
        "enabled": v.enabled,
        "rule_count": trail_rule_counts.get(v.id, 0),
        "trail_only": v.trail_only,  # False = also a curation voter
    } for v in trail_accounts_raw]

    # Resolve follower names for rules table
    voter_map = {v.id: v.username for v in all_voters}
    trail_list = []
    for r in rules:
        trail_list.append({
            "id": r.id,
            "follower_id": r.follower_id,
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
        "trail_accounts": trail_account_list,
        "trail_engines": _mgr().get_all_trail_status(),
        # all voters available as trail followers (both trail-only and curation)
        "voters": [{"id": v.id, "username": v.username} for v in all_voters],
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
    """Dashboard partial: curation engines only."""
    mgr = _mgr()
    return templates.TemplateResponse(request, "partials/runtime_status.html", {
        "curation": mgr.get_all_status(),
    })


@router.get("/partials/trail-status", response_class=HTMLResponse)
def partial_trail_status(request: Request):
    mgr = _mgr()
    return templates.TemplateResponse(request, "partials/trail_status.html", {
        "trails": mgr.get_all_trail_status(),
    })


@router.get("/partials/activity", response_class=HTMLResponse)
def partial_activity(request: Request):
    """HTMX partial: curation-only activity feed (dashboard)."""
    mgr = _mgr()
    events = []
    for s in mgr.get_all_status():
        for ev in s.get("activity", []):
            events.append({**ev, "source": s["voter"], "type": "curation"})
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
    posting_key: str = Form(None),
    min_voting_power: float = Form(80.0),
    max_post_age_minutes: float = Form(5.0),
    db: Session = Depends(get_db),
):
    username = username.strip().lower()
    key = posting_key.strip() if posting_key and posting_key.strip() else None

    # Validate key if provided
    if key:
        ok, err = verify_posting_key(username, key)
        if not ok:
            return RedirectResponse(f"/ui?flash={err.replace(' ', '+')}&error=1", status_code=303)

    existing = db.query(VoterAccount).filter(VoterAccount.username == username).first()
    if existing:
        # Account already exists — enable curation if not already, update key only if provided
        changed = False
        if key:
            existing.posting_key_encrypted = _encrypt_key(key)
            changed = True
        if existing.trail_only:
            existing.trail_only = False
            existing.min_voting_power = min_voting_power
            existing.max_post_age_minutes = max_post_age_minutes
            existing.enabled = True
            changed = True
        if changed:
            db.commit()
        return RedirectResponse(
            f"/ui/voters/{existing.id}?flash=Voter+abilitato+per+curation",
            status_code=303,
        )
    if not key:
        return RedirectResponse("/ui?flash=Posting+key+richiesta+per+nuovo+account&error=1", status_code=303)
    voter = VoterAccount(
        username=username,
        posting_key_encrypted=_encrypt_key(key),
        min_voting_power=min_voting_power,
        max_post_age_minutes=max_post_age_minutes,
        enabled=True,
        trail_only=False,
    )
    db.add(voter)
    db.commit()
    return RedirectResponse(f"/ui/voters/{voter.id}?flash=Voter+created", status_code=303)


# ── Trail accounts (trail-only voters managed from the Trails page) ──

@router.post("/trail-accounts/add")
def form_add_trail_account(
    username: str = Form(...),
    posting_key: str = Form(None),
    db: Session = Depends(get_db),
):
    username = username.strip().lower()
    key = posting_key.strip() if posting_key and posting_key.strip() else None

    # Validate key if provided
    if key:
        ok, err = verify_posting_key(username, key)
        if not ok:
            return RedirectResponse(f"/ui/trails?flash={err.replace(' ', '+')}&error=1", status_code=303)

    existing = db.query(VoterAccount).filter(VoterAccount.username == username).first()
    if existing:
        if not existing.trail_only:
            # Already a curation voter — update key if provided
            if key:
                existing.posting_key_encrypted = _encrypt_key(key)
                db.commit()
            return RedirectResponse(
                f"/ui/trails?flash=@{username}+e+gia+un+voter+curation%2C+aggiungi+le+regole+trail+qui+sotto",
                status_code=303,
            )
        return RedirectResponse("/ui/trails?flash=Account+gia+esistente&error=1", status_code=303)
    if not key:
        return RedirectResponse("/ui/trails?flash=Posting+key+richiesta+per+nuovo+account&error=1", status_code=303)
    voter = VoterAccount(
        username=username,
        posting_key_encrypted=_encrypt_key(key),
        enabled=True,
        trail_only=True,
    )
    db.add(voter)
    db.commit()
    return RedirectResponse("/ui/trails?flash=Trail+account+aggiunto", status_code=303)


@router.post("/trail-accounts/{voter_id}/delete")
def form_delete_trail_account(voter_id: int, db: Session = Depends(get_db)):
    voter = db.query(VoterAccount).filter(VoterAccount.id == voter_id).first()
    if voter:
        _mgr().stop_trail(voter_id)
        if voter.trail_only:
            # Pure trail account — delete entirely (cascade removes trail rules)
            db.delete(voter)
        else:
            # Curation voter — only remove trail rules, keep the curation account
            db.query(TrailRule).filter(TrailRule.follower_id == voter_id).delete()
        db.commit()
    return RedirectResponse("/ui/trails?flash=Trail+rules+removed", status_code=303)


@router.post("/trail-accounts/{voter_id}/toggle")
def form_toggle_trail_account(voter_id: int, db: Session = Depends(get_db)):
    voter = db.query(VoterAccount).filter(VoterAccount.id == voter_id).first()
    if voter:
        voter.enabled = not voter.enabled
        db.commit()
    return RedirectResponse("/ui/trails", status_code=303)


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


@router.post("/voters/{voter_id}/delete")
def form_delete_voter(voter_id: int, db: Session = Depends(get_db)):
    voter = db.query(VoterAccount).filter(
        VoterAccount.id == voter_id, VoterAccount.trail_only.is_(False)
    ).first()
    if not voter:
        return RedirectResponse("/ui", status_code=303)
    _mgr().stop_voter(voter_id)
    # Check if this account also has trail rules — preserve them
    has_trail = db.query(TrailRule).filter(TrailRule.follower_id == voter_id).count() > 0
    if has_trail:
        # Remove only curation data; convert account to trail-only
        db.query(FanbaseEntry).filter(FanbaseEntry.voter_id == voter_id).delete()
        voter.trail_only = True
        db.commit()
        return RedirectResponse("/ui?flash=Curation+rimossa+%E2%80%94+le+trail+rules+sono+state+conservate", status_code=303)
    db.delete(voter)
    db.commit()
    return RedirectResponse("/ui?flash=Voter+eliminato", status_code=303)


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


@router.get("/voters/{voter_id}/fanbase/export")
def export_fanbase(voter_id: int, db: Session = Depends(get_db)):
    """Download fanbase as JSON file."""
    voter = db.query(VoterAccount).filter(VoterAccount.id == voter_id).first()
    if not voter:
        return RedirectResponse("/ui", status_code=303)
    entries = (
        db.query(FanbaseEntry)
        .filter(FanbaseEntry.voter_id == voter_id)
        .order_by(FanbaseEntry.author)
        .all()
    )
    data = [{
        "author": e.author,
        "vote_percentage": e.vote_percentage,
        "post_delay_minutes": e.post_delay_minutes,
        "daily_vote_limit": e.daily_vote_limit,
        "add_comment": e.add_comment,
        "comment_text": e.comment_text,
        "enabled": e.enabled,
    } for e in entries]
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": f'attachment; filename="fanbase_{voter.username}.json"'},
    )


@router.post("/voters/{voter_id}/fanbase/import")
async def import_fanbase(voter_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Import fanbase from JSON file. Skips duplicates, adds new entries."""
    voter = db.query(VoterAccount).filter(VoterAccount.id == voter_id).first()
    if not voter:
        return RedirectResponse("/ui", status_code=303)
    try:
        content = await file.read()
        entries = json.loads(content)
        if not isinstance(entries, list):
            return RedirectResponse(
                f"/ui/voters/{voter_id}?flash=Invalid+file+format&error=1", status_code=303,
            )
    except (json.JSONDecodeError, UnicodeDecodeError):
        return RedirectResponse(
            f"/ui/voters/{voter_id}?flash=Invalid+JSON+file&error=1", status_code=303,
        )

    added = 0
    skipped = 0
    for item in entries:
        author = item.get("author", "").strip().lower()
        if not author:
            continue
        existing = (
            db.query(FanbaseEntry)
            .filter(FanbaseEntry.voter_id == voter_id, FanbaseEntry.author == author)
            .first()
        )
        if existing:
            skipped += 1
            continue
        entry = FanbaseEntry(
            voter_id=voter_id,
            author=author,
            vote_percentage=float(item.get("vote_percentage", 10.0)),
            post_delay_minutes=float(item.get("post_delay_minutes", 4.0)),
            daily_vote_limit=int(item.get("daily_vote_limit", 1)),
            add_comment=bool(item.get("add_comment", False)),
            comment_text=str(item.get("comment_text", "")),
            enabled=bool(item.get("enabled", True)),
        )
        db.add(entry)
        added += 1
    db.commit()
    return RedirectResponse(
        f"/ui/voters/{voter_id}?flash=Imported+{added}+authors+({skipped}+skipped)", status_code=303,
    )


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


@router.post("/bot/trails/{voter_id}/start")
def form_start_trail(voter_id: int):
    _mgr().start_trail(voter_id)
    return RedirectResponse("/ui/trails?flash=Trail+started", status_code=303)


@router.post("/bot/trails/{voter_id}/stop")
def form_stop_trail(voter_id: int):
    _mgr().stop_trail(voter_id)
    return RedirectResponse("/ui/trails?flash=Trail+stopped", status_code=303)
