import time
import logging
import threading
from collections import deque
from datetime import datetime, timedelta
from dataclasses import dataclass, field

from backend.services.steem_client import SteemClient
from backend.database import SessionLocal
from backend.models import VoterAccount, FanbaseEntry
from backend.config import get_fernet

logger = logging.getLogger(__name__)


@dataclass
class AuthorRuntime:
    """Runtime state for a fanbase author (not persisted)."""
    author: str
    vote_percentage: float
    post_delay_minutes: float
    daily_vote_limit: int
    add_comment: bool = False
    comment_text: str = ""
    add_image: bool = False
    image_path: str = ""
    # runtime counters
    votes_today: int = 0
    last_vote_time: datetime | None = None

    def can_vote(self) -> bool:
        now = datetime.now()
        if self.last_vote_time is None or now.date() > self.last_vote_time.date():
            self.votes_today = 0
        return self.votes_today < self.daily_vote_limit

    def record_vote(self):
        self.votes_today += 1
        self.last_vote_time = datetime.now()


@dataclass
class PendingPost:
    author: str
    post: object
    post_time: datetime
    vote_time: datetime
    runtime: AuthorRuntime
    attempts: int = 0


class CurationEngine:
    """One engine per voter account. Runs its own thread, reads fanbase from DB."""

    def __init__(self, voter_id: int):
        self.voter_id = voter_id
        self.voter_username: str = ""
        self.min_voting_power: float = 80.0
        self.max_post_age_minutes: float = 5.0
        self.interval_seconds: int = 1
        self.client: SteemClient | None = None
        self.running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # runtime state
        self.authors: dict[str, AuthorRuntime] = {}
        self.pending_posts: list[PendingPost] = []
        self.posts_checked = 0
        self.votes_made = 0
        self._activity: deque[dict] = deque(maxlen=50)

    def _log_activity(self, event: str, author: str = "", detail: str = "", level: str = "info"):
        self._activity.appendleft({
            "ts": datetime.utcnow().strftime("%H:%M:%S"),
            "event": event,
            "author": author,
            "detail": detail,
            "level": level,
        })

    # ── lifecycle ──

    def load_from_db(self) -> bool:
        """Load voter config + fanbase from DB. Returns False if voter not found."""
        db = SessionLocal()
        try:
            voter = db.query(VoterAccount).filter(VoterAccount.id == self.voter_id).first()
            if not voter:
                logger.error(f"Voter id={self.voter_id} not found in DB")
                return False
            if not voter.enabled:
                logger.warning(f"Voter @{voter.username} is disabled")
                return False

            self.voter_username = voter.username
            self.min_voting_power = voter.min_voting_power
            self.max_post_age_minutes = voter.max_post_age_minutes
            self.interval_seconds = voter.interval_seconds

            # Decrypt posting key
            fernet = get_fernet()
            posting_key = fernet.decrypt(voter.posting_key_encrypted.encode()).decode()
            self.client = SteemClient(posting_key)

            # Load fanbase
            entries = (
                db.query(FanbaseEntry)
                .filter(FanbaseEntry.voter_id == self.voter_id, FanbaseEntry.enabled.is_(True))
                .all()
            )
            self.authors = {}
            for e in entries:
                self.authors[e.author] = AuthorRuntime(
                    author=e.author,
                    vote_percentage=e.vote_percentage,
                    post_delay_minutes=e.post_delay_minutes,
                    daily_vote_limit=e.daily_vote_limit,
                    add_comment=e.add_comment,
                    comment_text=e.comment_text or "",
                    add_image=e.add_image,
                    image_path=e.image_path or "",
                )
            logger.info(f"Loaded {len(self.authors)} fanbase authors for @{self.voter_username}")
            return True
        finally:
            db.close()

    def reload_fanbase(self):
        """Hot-reload fanbase from DB without restarting the engine."""
        db = SessionLocal()
        try:
            entries = (
                db.query(FanbaseEntry)
                .filter(FanbaseEntry.voter_id == self.voter_id, FanbaseEntry.enabled.is_(True))
                .all()
            )
            new_authors: dict[str, AuthorRuntime] = {}
            for e in entries:
                # Preserve runtime counters for existing authors
                existing = self.authors.get(e.author)
                rt = AuthorRuntime(
                    author=e.author,
                    vote_percentage=e.vote_percentage,
                    post_delay_minutes=e.post_delay_minutes,
                    daily_vote_limit=e.daily_vote_limit,
                    add_comment=e.add_comment,
                    comment_text=e.comment_text or "",
                    add_image=e.add_image,
                    image_path=e.image_path or "",
                )
                if existing:
                    rt.votes_today = existing.votes_today
                    rt.last_vote_time = existing.last_vote_time
                new_authors[e.author] = rt

            with self._lock:
                self.authors = new_authors
            logger.info(f"Reloaded fanbase for @{self.voter_username}: {len(new_authors)} authors")
        finally:
            db.close()

    def start(self) -> bool:
        if self.running:
            logger.warning(f"Engine for @{self.voter_username} already running")
            return False
        if not self.load_from_db():
            return False
        if not self.client.connect():
            return False

        self.running = True
        self._thread = threading.Thread(
            target=self._main_loop, name=f"curation-{self.voter_username}", daemon=True
        )
        self._thread.start()
        logger.info(f"Engine started for @{self.voter_username}")
        self._log_activity("started", detail=f"Monitoring {len(self.authors)} authors")
        return True

    def stop(self):
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info(f"Engine stopped for @{self.voter_username}")
        self._log_activity("stopped", level="warn")

    # ── main loop ──

    def _main_loop(self):
        logger.info(
            f"[{self.voter_username}] Monitoring {len(self.authors)} authors"
        )
        while self.running:
            try:
                with self._lock:
                    authors_snapshot = dict(self.authors)

                for author_name, runtime in authors_snapshot.items():
                    if not self.running:
                        break
                    self._check_pending_posts()
                    if runtime.can_vote():
                        self._check_author(author_name, runtime)

                self._log_status()
                time.sleep(self.interval_seconds)
            except Exception as e:
                logger.error(f"[{self.voter_username}] Main loop error: {e}")
                time.sleep(self.interval_seconds)

        logger.info(f"[{self.voter_username}] Loop exited")

    # ── voting logic (ported from sniper_biz.py) ──

    def _has_voted_in_last_18h(self, author: str, daily_limit: int) -> bool:
        try:
            current_time = datetime.utcnow()
            cutoff_time = current_time - timedelta(hours=18)
            blog = self.client.get_blog(author, limit=5)
            votes_in_period = 0
            for post in blog:
                post_time = post['created'].replace(tzinfo=None)
                if post_time > cutoff_time:
                    if self.client.has_already_voted(post, self.voter_username):
                        votes_in_period += 1
            return votes_in_period >= daily_limit
        except Exception as e:
            logger.error(f"[{self.voter_username}] Error checking vote history for @{author}: {e}")
            return False

    def _analyze_competitor_timing(self, author: str, competitor: str = "karja") -> float | None:
        try:
            blog = self.client.get_blog(author, limit=2)
            if len(blog) > 1:
                last_post = blog[1]
                post_time = last_post['created'].replace(tzinfo=None)
                votes = last_post.get_votes()
                for vote in votes:
                    if vote['voter'] == competitor:
                        vote_time = vote['time'].replace(tzinfo=None)
                        delay = (vote_time - post_time).total_seconds() / 60
                        logger.info(
                            f"[{self.voter_username}] {competitor} voted after {delay:.1f}m on @{author}"
                        )
                        if delay > 4:
                            return delay - 0.25
                        return None
            return None
        except Exception as e:
            logger.error(f"[{self.voter_username}] Competitor analysis error: {e}")
            return None

    def _upvote_post(self, post, author: str, runtime: AuthorRuntime) -> bool:
        try:
            vp = self.client.get_voting_power(self.voter_username)
            if vp < self.min_voting_power:
                logger.warning(
                    f"[{self.voter_username}] Low VP ({vp:.1f}%) — skipping @{author}"
                )
                self._log_activity("low_vp", author=author, detail=f"VP {vp:.1f}% < {self.min_voting_power}%", level="warn")
                return False

            if self.client.has_already_voted(post, self.voter_username):
                return False

            if self.client.upvote(post, weight=runtime.vote_percentage * 1.0, voter=self.voter_username):
                self.votes_made += 1
                runtime.record_vote()
                title = getattr(post, 'title', '')[:60]
                logger.info(
                    f"[{self.voter_username}] Voted {runtime.vote_percentage}% on @{author}: "
                    f"{title}..."
                )
                self._log_activity("voted", author=author, detail=f"{runtime.vote_percentage}% — {title}")
                # Comment if configured
                if runtime.add_comment and runtime.comment_text:
                    body = runtime.comment_text
                    if runtime.add_image and runtime.image_path:
                        img_url = self.client.upload_image(runtime.image_path, self.voter_username)
                        if img_url:
                            body += f"\n\n![image]({img_url})"
                    self.client.comment_on_post(post, self.voter_username, body)
                return True
            return False
        except Exception as e:
            logger.error(f"[{self.voter_username}] Error voting on @{author}: {e}")
            return False

    def _check_author(self, author: str, runtime: AuthorRuntime):
        account = self.client.get_account(author)
        if not account:
            return

        if self._has_voted_in_last_18h(author, runtime.daily_vote_limit):
            return

        latest_post = self.client.get_latest_post(author)
        if not latest_post:
            return

        self.posts_checked += 1
        current_time = datetime.utcnow()
        post_time = latest_post['created']
        post_age = (current_time - post_time.replace(tzinfo=None)).total_seconds() / 60

        if post_age > self.max_post_age_minutes:
            return

        already_pending = any(
            p.post.identifier == latest_post.identifier for p in self.pending_posts
        )
        if already_pending:
            return

        # Competitor timing adjustment
        competitor_delay = self._analyze_competitor_timing(author)
        effective_delay = runtime.post_delay_minutes
        if competitor_delay is not None:
            effective_delay = min(effective_delay, competitor_delay)
            logger.info(
                f"[{self.voter_username}] Adjusted timing for @{author}: {effective_delay:.1f}m"
            )

        post_title = getattr(latest_post, 'title', '')[:60]
        logger.info(f"[{self.voter_username}] New post by @{author} (age {post_age:.1f}m)")
        self._log_activity("new_post", author=author, detail=f"{post_title} (age {post_age:.1f}m)")

        if post_age < effective_delay:
            vote_at = post_time.replace(tzinfo=None) + timedelta(minutes=effective_delay)
            self.pending_posts.append(PendingPost(
                author=author,
                post=latest_post,
                post_time=post_time.replace(tzinfo=None),
                vote_time=vote_at,
                runtime=runtime,
            ))
            wait_min = effective_delay - post_age
            logger.info(
                f"[{self.voter_username}] Queued @{author} — vote in {wait_min:.1f}m"
            )
            self._log_activity("queued", author=author, detail=f"{post_title} — voting in {wait_min:.1f}m")
        else:
            self._upvote_post(latest_post, author, runtime)

    def _check_pending_posts(self):
        current_time = datetime.utcnow()
        for pd in list(self.pending_posts):
            max_time = pd.post_time + timedelta(minutes=self.max_post_age_minutes)
            if current_time >= max_time:
                self.pending_posts.remove(pd)
                continue
            if current_time >= pd.vote_time:
                logger.info(f"[{self.voter_username}] Processing queued vote for @{pd.author}")
                if self._upvote_post(pd.post, pd.author, pd.runtime):
                    self.pending_posts.remove(pd)

    def _log_status(self):
        try:
            vp = self.client.get_voting_power(self.voter_username)
            vp_to_full = 100 - vp
            hours_to_full = (vp_to_full * 432000) / (100 * 3600)

            logger.info(
                f"[{self.voter_username}] VP={vp:.1f}% | "
                f"checked={self.posts_checked} voted={self.votes_made} "
                f"pending={len(self.pending_posts)} full_in={hours_to_full:.1f}h"
            )
        except Exception as e:
            logger.error(f"[{self.voter_username}] Status error: {e}")

    # ── public status ──

    def get_status(self) -> dict:
        return {
            "voter": self.voter_username,
            "voter_id": self.voter_id,
            "running": self.running,
            "authors_count": len(self.authors),
            "posts_checked": self.posts_checked,
            "votes_made": self.votes_made,
            "pending_posts": len(self.pending_posts),
            "pending_details": [
                {
                    "author": p.author,
                    "vote_time": p.vote_time.isoformat(),
                    "title": getattr(p.post, 'title', '')[:60],
                }
                for p in self.pending_posts
            ],
            "activity": list(self._activity),
        }
