from fastapi import APIRouter

from backend.services.bot_manager import BotManager

router = APIRouter(prefix="/bot", tags=["bot-control"])


def _mgr() -> BotManager:
    return BotManager()


@router.post("/start-all")
def start_all():
    """Start curation engines for all enabled voters."""
    return _mgr().start_all_enabled()


@router.post("/stop-all")
def stop_all():
    stopped = _mgr().stop_all()
    return {"stopped": stopped}


@router.post("/voters/{voter_id}/start")
def start_voter(voter_id: int):
    return _mgr().start_voter(voter_id)


@router.post("/voters/{voter_id}/stop")
def stop_voter(voter_id: int):
    return _mgr().stop_voter(voter_id)


@router.post("/voters/{voter_id}/reload")
def reload_voter_fanbase(voter_id: int):
    """Hot-reload fanbase from DB without restarting the engine."""
    return _mgr().reload_voter_fanbase(voter_id)


@router.get("/status")
def bot_runtime_status():
    """Runtime status of all running engines."""
    mgr = _mgr()
    return {
        "curation_engines": mgr.running_count,
        "trail_engines": mgr.running_trail_count,
        "curation": mgr.get_all_status(),
        "trails": mgr.get_all_trail_status(),
    }


@router.get("/status/{voter_id}")
def bot_voter_runtime_status(voter_id: int):
    """Runtime status of a specific engine."""
    status = _mgr().get_voter_status(voter_id)
    if status is None:
        return {"error": "Engine not found or never started"}
    return status


# ── Trail control ──

@router.post("/trails/start-all")
def start_all_trails():
    """Start trail engines for all voters with enabled trail rules."""
    return _mgr().start_all_trails()


@router.post("/trails/{voter_id}/start")
def start_trail(voter_id: int):
    return _mgr().start_trail(voter_id)


@router.post("/trails/{voter_id}/stop")
def stop_trail(voter_id: int):
    return _mgr().stop_trail(voter_id)


@router.post("/trails/{voter_id}/reload")
def reload_trail_rules(voter_id: int):
    """Hot-reload trail rules from DB without restarting."""
    return _mgr().reload_trail_rules(voter_id)


@router.get("/trails/status")
def trail_runtime_status():
    """Runtime status of all trail engines."""
    mgr = _mgr()
    return {
        "running_trails": mgr.running_trail_count,
        "trails": mgr.get_all_trail_status(),
    }


@router.get("/trails/status/{voter_id}")
def trail_voter_runtime_status(voter_id: int):
    status = _mgr().get_trail_status(voter_id)
    if status is None:
        return {"error": "Trail engine not found or never started"}
    return status
