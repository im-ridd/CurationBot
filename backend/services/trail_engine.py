import queue
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

        # Serialize broadcast votes: Steem allows 1 vote per account every 3s
        self._vote_lock = threading.Lock()
        self._last_vote_ts = 0.0
        self.MIN_VOTE_INTERVAL = 3.5  # seconds, small margin over chain limit

        # Heartbeat updated by stream loop; used by external watchdog
        self._last_op_ts: float = 0.0

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
        # Steem produces a block every 3s.  If no op arrives for 5 minutes either
        # the node is dead (half-open TCP) or all nodes are unreachable.  We detect
        # this via a queue timeout and reconnect gracefully.
        STREAM_TIMEOUT = 300  # seconds

        logger.info(
            f"[trail-{self.voter_username}] Streaming votes, watching {len(self.rules)} leaders"
        )
        while self.running:
            op_queue: queue.Queue = queue.Queue(maxsize=500)
            stop_event = threading.Event()
            producer = threading.Thread(
                target=self._stream_producer,
                args=(op_queue, stop_event),
                daemon=True,
                name=f"trail-producer-{self.voter_username}",
            )
            producer.start()
            self._last_op_ts = time.time()

            try:
                while self.running:
                    try:
                        op = op_queue.get(timeout=STREAM_TIMEOUT)
                    except queue.Empty:
                        logger.warning(
                            f"[trail-{self.voter_username}] Stream idle for {STREAM_TIMEOUT}s "
                            "— reconnecting"
                        )
                        self._log_activity(
                            "stream_stall",
                            detail=f"No ops for {STREAM_TIMEOUT}s, reconnecting",
                            level="warn",
                        )
                        break  # exit inner loop → reconnect below

                    if op is None:  # producer signalled a stream error → reconnect
                        logger.warning(
                            f"[trail-{self.voter_username}] Stream disconnected — reconnecting in 5s"
                        )
                        break

                    self._last_op_ts = time.time()
                    self.ops_scanned += 1
                    voter = op.get("voter", "")

                    with self._lock:
                        rule = self.rules.get(voter)

                    if rule is None:
                        continue

                    # Leader voted — replicate
                    author = op.get("author", "")
                    permlink = op.get("permlink", "")
                    leader_weight = op.get("weight", 0) / 100.0  # beem: 0-10000 → 0-100

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
                    self._log_activity(
                        "leader_vote",
                        detail=(
                            f"@{voter} voted {leader_weight:.1f}% on "
                            f"@{author}/{permlink[:30]} → {scaled_weight:.1f}%"
                        ),
                    )
                    if rule.delay_seconds > 0:
                        threading.Thread(
                            target=self._delayed_vote,
                            args=(author, permlink, scaled_weight, rule.delay_seconds),
                            daemon=True,
                        ).start()
                    else:
                        self._cast_vote(author, permlink, scaled_weight)

            finally:
                stop_event.set()  # ask producer to exit on next op

            if self.running:
                time.sleep(5)
                self.client.connect()  # fresh Steem instance for next iteration

        logger.info(f"[trail-{self.voter_username}] Stream loop exited")

    def _stream_producer(self, op_queue: queue.Queue, stop_event: threading.Event):
        """Runs in a daemon thread; feeds vote ops into op_queue.

        Signals the consumer by putting ``None`` on error so it can break out
        instead of waiting for the full STREAM_TIMEOUT.
        """
        try:
            bc = Blockchain(blockchain_instance=self.client.steem)
            for op in bc.stream(opNames=["vote"]):
                if stop_event.is_set():
                    return
                try:
                    op_queue.put_nowait(op)
                except queue.Full:
                    pass  # rare backpressure — drop the op
        except Exception as e:
            logger.warning(f"[trail-{self.voter_username}] Stream interrupted: {e}")
            if not stop_event.is_set():
                try:
                    op_queue.put_nowait(None)  # wake consumer
                except queue.Full:
                    pass

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
                self._log_activity("voted", detail=f"già votato: {identifier}")
                return

            vp = self.client.get_voting_power(self.voter_username)
            if vp is None:
                logger.warning(f"[trail-{self.voter_username}] Could not fetch VP, skipping vote")
                return
            if vp < 50.0:  # safety floor for trail votes
                logger.warning(f"[trail-{self.voter_username}] VP too low ({vp:.1f}%), skipping")
                return

            # Enforce the 3-second chain rule WITHOUT holding the lock during the
            # actual network call — otherwise every concurrent delayed-vote thread
            # would be blocked for the full round-trip time of the upvote RPC.
            with self._vote_lock:
                wait = self._last_vote_ts + self.MIN_VOTE_INTERVAL - time.time()
                if wait > 0:
                    time.sleep(wait)
                self._last_vote_ts = time.time()  # reserve this slot
            # Lock released — upvote is made outside it
            logger.info(f"[trail-{self.voter_username}] Casting vote as @{self.voter_username} on {identifier}")
            success = self.client.upvote(post, weight=weight, voter=self.voter_username)

            if success:
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
