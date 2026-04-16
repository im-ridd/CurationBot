import time
import logging
import threading
from collections import deque
from datetime import datetime
from dataclasses import dataclass, field

from beem.blockchain import Blockchain
from beem.comment import Comment

from backend.services.steem_client import SteemClient
from backend.database import SessionLocal
from backend.models import VoterAccount, TrailRule
from backend.config import get_fernet

logger = logging.getLogger(__name__)


@dataclass
class TrailRuleRuntime:
    """Runtime representation of a single trail rule."""
    rule_id: int
    leader_username: str
    weight_scale: float
    max_weight: float
    delay_seconds: int


class TrailEngine:
    """
    Monitors the blockchain stream for vote operations by one or more leaders,
    then replicates those votes for a specific follower account.

    One TrailEngine per follower (voter account). It watches ALL leaders
    configured for that follower in a single stream.
    """

    def __init__(self, voter_id: int):
        self.voter_id = voter_id
        self.voter_username: str = ""
        self.client: SteemClient | None = None
        self.running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # leader_username -> TrailRuleRuntime
        self.rules: dict[str, TrailRuleRuntime] = {}
        self.votes_replicated = 0
        self.ops_scanned = 0
        self._activity: deque[dict] = deque(maxlen=50)

    def _log_activity(self, event: str, detail: str = "", level: str = "info"):
        self._activity.appendleft({
            "ts": datetime.utcnow().strftime("%H:%M:%S"),
            "event": event,
            "detail": detail,
            "level": level,
        })

    # ── lifecycle ──

    def load_from_db(self) -> bool:
        db = SessionLocal()
        try:
            voter = db.query(VoterAccount).filter(VoterAccount.id == self.voter_id).first()
            if not voter:
                logger.error(f"Trail: Voter id={self.voter_id} not found")
                return False
            if not voter.enabled:
                logger.warning(f"Trail: Voter @{voter.username} is disabled")
                return False

            self.voter_username = voter.username

            fernet = get_fernet()
            posting_key = fernet.decrypt(voter.posting_key_encrypted.encode()).decode()
            self.client = SteemClient(posting_key)

            trail_rules = (
                db.query(TrailRule)
                .filter(TrailRule.follower_id == self.voter_id, TrailRule.enabled.is_(True))
                .all()
            )
            self.rules = {}
            for r in trail_rules:
                self.rules[r.leader_username] = TrailRuleRuntime(
                    rule_id=r.id,
                    leader_username=r.leader_username,
                    weight_scale=r.weight_scale,
                    max_weight=r.max_weight,
                    delay_seconds=r.delay_seconds,
                )

            logger.info(
                f"Trail: Loaded {len(self.rules)} leaders for @{self.voter_username}: "
                f"{list(self.rules.keys())}"
            )
            return len(self.rules) > 0
        finally:
            db.close()

    def reload_rules(self):
        db = SessionLocal()
        try:
            trail_rules = (
                db.query(TrailRule)
                .filter(TrailRule.follower_id == self.voter_id, TrailRule.enabled.is_(True))
                .all()
            )
            new_rules = {}
            for r in trail_rules:
                new_rules[r.leader_username] = TrailRuleRuntime(
                    rule_id=r.id,
                    leader_username=r.leader_username,
                    weight_scale=r.weight_scale,
                    max_weight=r.max_weight,
                    delay_seconds=r.delay_seconds,
                )
            with self._lock:
                self.rules = new_rules
            logger.info(f"Trail: Reloaded rules for @{self.voter_username}: {len(new_rules)} leaders")
        finally:
            db.close()

    def start(self) -> bool:
        if self.running:
            logger.warning(f"Trail for @{self.voter_username} already running")
            return False
        if not self.load_from_db():
            return False
        if not self.client.connect():
            return False

        self.running = True
        self._thread = threading.Thread(
            target=self._stream_loop, name=f"trail-{self.voter_username}", daemon=True
        )
        self._thread.start()
        logger.info(f"Trail engine started for @{self.voter_username}")
        self._log_activity("started", detail=f"Watching {len(self.rules)} leaders: {', '.join(self.rules.keys())}")
        return True

    def stop(self):
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=15)
        logger.info(f"Trail engine stopped for @{self.voter_username}")
        self._log_activity("stopped", level="warn")

    # ── stream loop ──

    def _stream_loop(self):
        logger.info(
            f"[trail-{self.voter_username}] Streaming votes, watching {len(self.rules)} leaders"
        )
        while self.running:
            try:
                bc = Blockchain(blockchain_instance=self.client.steem)
                for op in bc.stream(opNames=["vote"]):
                    if not self.running:
                        break

                    self.ops_scanned += 1
                    voter = op.get("voter", "")

                    with self._lock:
                        rule = self.rules.get(voter)

                    if rule is None:
                        continue

                    # Leader voted — replicate
                    author = op.get("author", "")
                    permlink = op.get("permlink", "")
                    leader_weight = op.get("weight", 0) / 100.0  # beem weight is in 0-10000

                    # Don't trail downvotes
                    if leader_weight <= 0:
                        continue

                    # Don't vote on our own posts
                    if author == self.voter_username:
                        continue

                    scaled_weight = min(leader_weight * rule.weight_scale, rule.max_weight)

                    logger.info(
                        f"[trail-{self.voter_username}] Leader @{voter} voted "
                        f"{leader_weight:.1f}% on @{author}/{permlink[:30]}... "
                        f"→ replicating at {scaled_weight:.1f}%"
                    )
                    self._log_activity("leader_vote", detail=f"@{voter} voted {leader_weight:.1f}% on @{author}/{permlink[:30]} → {scaled_weight:.1f}%")
                    if rule.delay_seconds > 0:
                        # Spawn a delayed vote in a separate thread
                        threading.Thread(
                            target=self._delayed_vote,
                            args=(author, permlink, scaled_weight, rule.delay_seconds),
                            daemon=True,
                        ).start()
                    else:
                        self._cast_vote(author, permlink, scaled_weight)

            except Exception as e:
                logger.error(f"[trail-{self.voter_username}] Stream error: {e}")
                if self.running:
                    time.sleep(5)  # backoff before reconnect

        logger.info(f"[trail-{self.voter_username}] Stream loop exited")

    def _delayed_vote(self, author: str, permlink: str, weight: float, delay: int):
        logger.info(
            f"[trail-{self.voter_username}] Waiting {delay}s before voting on @{author}/{permlink[:30]}"
        )
        deadline = time.time() + delay
        while time.time() < deadline:
            if not self.running:
                return
            time.sleep(1)
        self._cast_vote(author, permlink, weight)

    def _cast_vote(self, author: str, permlink: str, weight: float):
        try:
            identifier = f"@{author}/{permlink}"
            post = Comment(identifier, blockchain_instance=self.client.steem)

            if self.client.has_already_voted(post, self.voter_username):
                logger.info(f"[trail-{self.voter_username}] Already voted on {identifier}")
                return

            vp = self.client.get_voting_power(self.voter_username)
            if vp < 50.0:  # safety floor for trail votes
                logger.warning(f"[trail-{self.voter_username}] VP too low ({vp:.1f}%), skipping")
                return

            if self.client.upvote(post, weight=weight, voter=self.voter_username):
                self.votes_replicated += 1
                logger.info(
                    f"[trail-{self.voter_username}] Voted {weight:.1f}% on {identifier}"
                )
                self._log_activity("voted", detail=f"{weight:.1f}% on {identifier}")
        except Exception as e:
            logger.error(f"[trail-{self.voter_username}] Vote error on @{author}/{permlink}: {e}")

    # ── public status ──

    def get_status(self) -> dict:
        return {
            "voter": self.voter_username,
            "voter_id": self.voter_id,
            "running": self.running,
            "leaders": list(self.rules.keys()),
            "leaders_count": len(self.rules),
            "votes_replicated": self.votes_replicated,
            "ops_scanned": self.ops_scanned,
            "activity": list(self._activity),
        }
