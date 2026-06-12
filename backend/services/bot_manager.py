import logging
import time
import threading

from backend.services.curation_engine import CurationEngine
from backend.services.trail_engine import TrailEngine
from backend.database import SessionLocal
from backend.models import VoterAccount, TrailRule

logger = logging.getLogger(__name__)


class BotManager:
    """Singleton orchestrator — manages N CurationEngines + N TrailEngines."""

    _instance = None
    _init_lock = threading.Lock()

    def __new__(cls):
        with cls._init_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._engines: dict[int, CurationEngine] = {}
                cls._instance._trails: dict[int, TrailEngine] = {}
                cls._instance._lock = threading.Lock()
                # Watchdog: detects stalled engines and restarts them
                wdog = threading.Thread(
                    target=cls._instance._watchdog_loop,
                    name="bot-watchdog",
                    daemon=True,
                )
                wdog.start()
            return cls._instance

    # ── engine management ──

    def start_voter(self, voter_id: int) -> dict:
        with self._lock:
            if voter_id in self._engines and self._engines[voter_id].running:
                return {"ok": False, "error": "Already running"}

            engine = CurationEngine(voter_id)
            if engine.start():
                self._engines[voter_id] = engine
                return {"ok": True, "voter": engine.voter_username}
            return {"ok": False, "error": "Failed to start (check logs)"}

    def stop_voter(self, voter_id: int) -> dict:
        with self._lock:
            engine = self._engines.get(voter_id)
            if not engine or not engine.running:
                return {"ok": False, "error": "Not running"}
            engine.stop()
            return {"ok": True, "voter": engine.voter_username}

    def reload_voter_fanbase(self, voter_id: int) -> dict:
        with self._lock:
            engine = self._engines.get(voter_id)
            if not engine or not engine.running:
                return {"ok": False, "error": "Not running"}
            engine.reload_fanbase()
            return {"ok": True, "authors": len(engine.authors)}

    def get_voter_status(self, voter_id: int) -> dict | None:
        engine = self._engines.get(voter_id)
        if engine:
            return engine.get_status()
        return None

    def get_all_status(self) -> list[dict]:
        return [e.get_status() for e in self._engines.values()]

    def start_all_enabled(self) -> dict:
        """Start curation engines for all enabled, non-trail-only voters in DB."""
        db = SessionLocal()
        try:
            voters = (
                db.query(VoterAccount)
                .filter(VoterAccount.enabled.is_(True), VoterAccount.trail_only.is_(False))
                .all()
            )
            started = []
            failed = []
            for v in voters:
                result = self.start_voter(v.id)
                if result["ok"]:
                    started.append(v.username)
                else:
                    failed.append({"username": v.username, "error": result["error"]})
            return {"started": started, "failed": failed}
        finally:
            db.close()

    def stop_all(self) -> int:
        count = 0
        with self._lock:
            for engine in self._engines.values():
                if engine.running:
                    engine.stop()
                    count += 1
            for trail in self._trails.values():
                if trail.running:
                    trail.stop()
                    count += 1
        return count

    @property
    def running_count(self) -> int:
        return sum(1 for e in self._engines.values() if e.running)

    # ── trail management ──

    def start_trail(self, voter_id: int) -> dict:
        with self._lock:
            if voter_id in self._trails and self._trails[voter_id].running:
                return {"ok": False, "error": "Trail already running"}

            trail = TrailEngine(voter_id)
            if trail.start():
                self._trails[voter_id] = trail
                return {"ok": True, "voter": trail.voter_username, "leaders": list(trail.rules.keys())}
            return {"ok": False, "error": "Failed to start trail (check logs)"}

    def stop_trail(self, voter_id: int) -> dict:
        with self._lock:
            trail = self._trails.get(voter_id)
            if not trail or not trail.running:
                return {"ok": False, "error": "Trail not running"}
            trail.stop()
            return {"ok": True, "voter": trail.voter_username}

    def reload_trail_rules(self, voter_id: int) -> dict:
        with self._lock:
            trail = self._trails.get(voter_id)
            if not trail or not trail.running:
                return {"ok": False, "error": "Trail not running"}
            trail.reload_rules()
            return {"ok": True, "leaders": len(trail.rules)}

    def get_trail_status(self, voter_id: int) -> dict | None:
        trail = self._trails.get(voter_id)
        if trail:
            return trail.get_status()
        return None

    def get_all_trail_status(self) -> list[dict]:
        return [t.get_status() for t in self._trails.values()]

    def start_all_trails(self) -> dict:
        """Start trail engines for all voters that have enabled trail rules."""
        db = SessionLocal()
        try:
            voter_ids = (
                db.query(TrailRule.follower_id)
                .filter(TrailRule.enabled.is_(True))
                .distinct()
                .all()
            )
            started = []
            failed = []
            for (vid,) in voter_ids:
                result = self.start_trail(vid)
                if result["ok"]:
                    started.append(result["voter"])
                else:
                    failed.append({"voter_id": vid, "error": result["error"]})
            return {"started": started, "failed": failed}
        finally:
            db.close()

    @property
    def running_trail_count(self) -> int:
        return sum(1 for t in self._trails.values() if t.running)

    # ── watchdog ──

    def _watchdog_loop(self):
        """Background thread: restart engines that have gone silent."""
        # Trail engine: _last_op_ts is reset on every reconnect and updated on
        # every received op.  If it's older than STALL_THRESHOLD the internal
        # queue-based reconnect has itself gotten stuck — force a full restart.
        # Curation engine: _last_activity_ts is updated at the top of each
        # main-loop iteration.  Silence for STALL_THRESHOLD means it's frozen.
        STALL_THRESHOLD_TRAIL = 600    # 10 min — trail stream should always have ops
        STALL_THRESHOLD_CURATION = 120  # 2 min — heartbeat updated per-author now
        CHECK_INTERVAL = 60   # check once per minute

        while True:
            time.sleep(CHECK_INTERVAL)
            now = time.time()

            for voter_id, trail in list(self._trails.items()):
                if not trail.running:
                    continue
                if trail._last_op_ts > 0 and (now - trail._last_op_ts) > STALL_THRESHOLD_TRAIL:
                    logger.warning(
                        f"Watchdog: trail @{trail.voter_username} stalled "
                        f"({(now - trail._last_op_ts)/60:.1f}m no ops) — restarting"
                    )
                    self._restart_trail(voter_id)

            for voter_id, engine in list(self._engines.items()):
                if not engine.running:
                    continue
                if engine._last_activity_ts > 0 and (now - engine._last_activity_ts) > STALL_THRESHOLD_CURATION:
                    logger.warning(
                        f"Watchdog: curation @{engine.voter_username} stalled "
                        f"({(now - engine._last_activity_ts)/60:.1f}m inactive) — restarting"
                    )
                    self._restart_voter(voter_id)

    def _restart_trail(self, voter_id: int):
        with self._lock:
            trail = self._trails.get(voter_id)
            if not trail:
                return
            trail.stop()  # join with timeout=15; old thread is daemon, will die
            new_trail = TrailEngine(voter_id)
            if new_trail.start():
                self._trails[voter_id] = new_trail
                logger.info(f"Watchdog: trail @{new_trail.voter_username} restarted")
            else:
                logger.error(f"Watchdog: failed to restart trail id={voter_id}")
                self._trails.pop(voter_id, None)

    def _restart_voter(self, voter_id: int):
        with self._lock:
            engine = self._engines.get(voter_id)
            if not engine:
                return
            engine.stop()  # join with timeout=10
            new_engine = CurationEngine(voter_id)
            if new_engine.start():
                self._engines[voter_id] = new_engine
                logger.info(f"Watchdog: curation @{new_engine.voter_username} restarted")
            else:
                logger.error(f"Watchdog: failed to restart curation id={voter_id}")
                self._engines.pop(voter_id, None)
