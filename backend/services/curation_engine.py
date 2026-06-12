import time
import logging
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
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
    next_retry: datetime | None = None  # set after a failed vote attempt

    RETRY_DELAY_SECS: int = 15
    MAX_ATTEMPTS: int = 5


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
        self._state_lock = threading.Lock()  # protects pending_posts, votes_made, _activity
        self._pending_event = threading.Event()  # wakes pending thread when a new post is queued

        # Cache for _has_voted_in_last_18h: author -> (timestamp, result)
        # Avoids N heavy API calls per loop tick when the fanbase is large.
        self._vote_history_cache: dict[str, tuple[float, bool]] = {}
        _VOTE_HISTORY_TTL = 600  # seconds (10 min)
        self._vote_history_ttl = _VOTE_HISTORY_TTL

        # Heartbeat updated every main-loop iteration; used by watchdog.
        self._last_activity_ts: float = 0.0
        self._last_scan_duration: float = 0.0  # seconds for last full scan cycle

        # Parallel scan: how many authors to check concurrently.
        # 5 workers keeps connection pool pressure low while still scanning
        # 130 authors in ~40-60s under normal node conditions.
        self.SCAN_WORKERS = 5
        self.AUTHOR_TIMEOUT = 90  # seconds per author before skip (10s/node × 6 nodes × some slack)

    def _log_activity(self, event: str, author: str = "", detail: str = "", level: str = "info"):
        with self._state_lock:
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
        # Dedicated thread for pending posts — checks every 10s independently of the scan cycle
        self._pending_thread = threading.Thread(
            target=self._pending_loop, name=f"pending-{self.voter_username}", daemon=True
        )
        self._pending_thread.start()
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

    def _pending_loop(self):
        """Wakes at exactly the right moment to cast each queued vote."""
        while self.running:
            with self._state_lock:
                pending_snapshot = list(self.pending_posts)

            now = datetime.utcnow()
            next_wake = None
            for pd in pending_snapshot:
                expire = pd.post_time + timedelta(minutes=self.max_post_age_minutes)
                # Wake at vote_time, or at next_retry if a previous attempt failed
                target = pd.next_retry if pd.next_retry else pd.vote_time
                target = min(target, expire)
                if target > now:
                    if next_wake is None or target < next_wake:
                        next_wake = target

            sleep_secs = max(0.0, (next_wake - datetime.utcnow()).total_seconds()) if next_wake else 60

            self._pending_event.wait(timeout=sleep_secs)
            self._pending_event.clear()

            try:
                self._check_pending_posts()
            except Exception as e:
                logger.error(f"[{self.voter_username}] Pending loop error: {e}")

    def _main_loop(self):
        logger.info(
            f"[{self.voter_username}] Monitoring {len(self.authors)} authors "
            f"(parallel scan, {self.SCAN_WORKERS} workers)"
        )
        with ThreadPoolExecutor(
            max_workers=self.SCAN_WORKERS,
            thread_name_prefix=f"cur-{self.voter_username}"
        ) as pool:
            while self.running:
                try:
                    self._last_activity_ts = time.time()

                    with self._lock:
                        authors_snapshot = dict(self.authors)

                    # Submit all eligible authors in parallel
                    scan_start = time.time()
                    futures = {}
                    for author_name, runtime in authors_snapshot.items():
                        if not self.running:
                            break
                        if runtime.can_vote():
                            futures[pool.submit(self._check_author, author_name, runtime)] = author_name

                    # Collect results with per-author timeout
                    for future, author_name in futures.items():
                        self._last_activity_ts = time.time()
                        try:
                            future.result(timeout=self.AUTHOR_TIMEOUT)
                        except FuturesTimeoutError:
                            logger.warning(
                                f"[{self.voter_username}] Timeout on @{author_name} "
                                f"({self.AUTHOR_TIMEOUT}s) — skipping"
                            )
                            future.cancel()
                        except Exception as e:
                            logger.error(f"[{self.voter_username}] Error on @{author_name}: {e}")

                    self._log_status()
                    self._last_scan_duration = time.time() - scan_start
                    time.sleep(self.interval_seconds)
                except Exception as e:
                    logger.error(f"[{self.voter_username}] Main loop error: {e}")
                    time.sleep(self.interval_seconds)

        logger.info(f"[{self.voter_username}] Loop exited")

    # ── voting logic (ported from sniper_biz.py) ──

    def _has_voted_in_last_18h(self, author: str, daily_limit: int) -> bool:
        # Use cached result if fresh enough to avoid hammering the API every tick
        cached = self._vote_history_cache.get(author)
        if cached and time.time() - cached[0] < self._vote_history_ttl:
            return cached[1]
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
            result = votes_in_period >= daily_limit
            self._vote_history_cache[author] = (time.time(), result)
            return result
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

    def _has_voted_in_last_18h_from_blog(self, author: str, daily_limit: int, blog: list) -> bool:
        """Check vote history using an already-fetched blog list — no extra RPC.
        active_votes is included in get_discussions_by_blog responses."""
        try:
            current_time = datetime.utcnow()
            cutoff_time = current_time - timedelta(hours=18)
            votes_in_period = 0
            for post in blog:
                post_time = post['created'].replace(tzinfo=None)
                if post_time > cutoff_time:
                    active_votes = post.get('active_votes', [])
                    if any(v['voter'] == self.voter_username for v in active_votes):
                        votes_in_period += 1
            result = votes_in_period >= daily_limit
            self._vote_history_cache[author] = (time.time(), result)
            return result
        except Exception as e:
            logger.error(f"[{self.voter_username}] Error checking vote history for @{author}: {e}")
            return False

    def _competitor_delay_from_blog(self, author: str, blog: list, competitor: str = "karja") -> float | None:
        """Analyze competitor timing on the previous post.
        get_discussions_by_blog active_votes has no timestamps, so we make one
        targeted get_active_votes call — only when a new post is detected (rare)."""
        try:
            if len(blog) > 1:
                last_post = blog[1]
                post_time = last_post['created'].replace(tzinfo=None)
                permlink = last_post.get('permlink', '') or getattr(last_post, 'permlink', '')
                votes = self.client.get_active_votes(author, permlink)
                for vote in votes:
                    if vote.get('voter') == competitor:
                        vote_time_raw = vote.get('time') or vote.get('timestamp')
                        if not vote_time_raw:
                            continue
                        if isinstance(vote_time_raw, datetime):
                            vote_time = vote_time_raw.replace(tzinfo=None)
                        else:
                            vote_time = datetime.strptime(str(vote_time_raw)[:19], "%Y-%m-%dT%H:%M:%S")
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
            if vp is None:
                # Node error — can't determine VP, keep post in pending and retry next cycle
                logger.warning(
                    f"[{self.voter_username}] Could not fetch VP — deferring vote on @{author}"
                )
                return False
            if vp < self.min_voting_power:
                logger.warning(
                    f"[{self.voter_username}] Low VP ({vp:.1f}%) — skipping @{author}"
                )
                self._log_activity("low_vp", author=author, detail=f"VP {vp:.1f}% < {self.min_voting_power}%", level="warn")
                return False

            if self.client.has_already_voted(post, self.voter_username):
                return False

            if self.client.upvote(post, weight=runtime.vote_percentage * 1.0, voter=self.voter_username):
                with self._state_lock:
                    self.votes_made += 1
                runtime.record_vote()
                # Invalidate history cache so the next loop knows we already voted
                self._vote_history_cache.pop(author, None)
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
        # Single RPC call: get_blog(limit=5) provides data for has_voted check,
        # latest_post detection, and competitor timing — avoiding 3 separate calls.
        cached = self._vote_history_cache.get(author)
        if cached and time.time() - cached[0] < self._vote_history_ttl and cached[1]:
            return  # voted recently, skip without any RPC

        blog = self.client.get_blog(author, limit=5)
        if not blog:
            return

        if self._has_voted_in_last_18h_from_blog(author, runtime.daily_vote_limit, blog):
            return

        latest_post = blog[0]
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

        # Competitor timing — reuse already-fetched blog, no extra RPC
        competitor_delay = self._competitor_delay_from_blog(author, blog)
        effective_delay = runtime.post_delay_minutes
        timing_adjusted = False
        if competitor_delay is not None:
            new_delay = min(effective_delay, competitor_delay)
            if new_delay < effective_delay:
                timing_adjusted = True
                original_delay = effective_delay
            effective_delay = new_delay
            logger.info(f"[{self.voter_username}] Adjusted timing for @{author}: {effective_delay:.1f}m")

        post_title = getattr(latest_post, 'title', '')[:60]
        timing_note = f" · ⏱ {original_delay:.1f}m→{effective_delay:.1f}m" if timing_adjusted else ""
        logger.info(f"[{self.voter_username}] New post by @{author} (age {post_age:.1f}m)")
        self._log_activity("new_post", author=author, detail=f"{post_title} (age {post_age:.1f}m){timing_note}")

        if post_age < effective_delay:
            vote_at = post_time.replace(tzinfo=None) + timedelta(minutes=effective_delay)
            with self._state_lock:
                self.pending_posts.append(PendingPost(
                    author=author,
                    post=latest_post,
                    post_time=post_time.replace(tzinfo=None),
                    vote_time=vote_at,
                    runtime=runtime,
                ))
            self._pending_event.set()  # wake pending thread immediately
            wait_min = effective_delay - post_age
            logger.info(
                f"[{self.voter_username}] Queued @{author} — vote in {wait_min:.1f}m"
            )
            self._log_activity("queued", author=author, detail=f"{post_title} — voting in {wait_min:.1f}m{timing_note}")
        else:
            self._upvote_post(latest_post, author, runtime)

    def _check_pending_posts(self):
        current_time = datetime.utcnow()
        with self._state_lock:
            pending_snapshot = list(self.pending_posts)
        for pd in pending_snapshot:
            max_time = pd.post_time + timedelta(minutes=self.max_post_age_minutes)
            if current_time >= max_time:
                if pd.attempts > 0:
                    msg = f"Post scaduto dopo {pd.attempts} tentativo/i senza voto"
                    logger.warning(f"[{self.voter_username}] @{pd.author} {msg}")
                    self._log_activity("expired", author=pd.author, detail=msg, level="warn")
                elif current_time >= pd.vote_time:
                    # vote_time was reached but we never got to vote (e.g. scan was busy)
                    msg = "Post scaduto — voto non eseguito in tempo"
                    logger.warning(f"[{self.voter_username}] @{pd.author} {msg}")
                    self._log_activity("expired", author=pd.author, detail=msg, level="warn")
                with self._state_lock:
                    if pd in self.pending_posts:
                        self.pending_posts.remove(pd)
                continue
            # Skip if it's not yet vote_time and no retry is due
            due = pd.next_retry if pd.next_retry else pd.vote_time
            if current_time < due:
                continue
            logger.info(f"[{self.voter_username}] Processing queued vote for @{pd.author} (attempt {pd.attempts + 1})")
            if self._upvote_post(pd.post, pd.author, pd.runtime):
                with self._state_lock:
                    if pd in self.pending_posts:
                        self.pending_posts.remove(pd)
            else:
                pd.attempts += 1
                if pd.attempts >= pd.MAX_ATTEMPTS:
                    logger.error(
                        f"[{self.voter_username}] @{pd.author} vote failed after "
                        f"{pd.MAX_ATTEMPTS} attempts — giving up"
                    )
                    self._log_activity("error", author=pd.author, detail=f"Vote failed after {pd.MAX_ATTEMPTS} attempts", level="warn")
                    with self._state_lock:
                        if pd in self.pending_posts:
                            self.pending_posts.remove(pd)
                else:
                    pd.next_retry = datetime.utcnow() + timedelta(seconds=pd.RETRY_DELAY_SECS)
                    logger.warning(
                        f"[{self.voter_username}] Vote failed for @{pd.author}, "
                        f"retry in {pd.RETRY_DELAY_SECS}s (attempt {pd.attempts}/{pd.MAX_ATTEMPTS})"
                    )
                    self._pending_event.set()  # wake thread to recalculate next sleep

    def _log_status(self):
        try:
            vp = self.client.get_voting_power(self.voter_username)
            if vp is None:
                logger.info(
                    f"[{self.voter_username}] VP=? | "
                    f"checked={self.posts_checked} voted={self.votes_made} "
                    f"pending={len(self.pending_posts)}"
                )
                return
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
        with self._state_lock:
            pending = list(self.pending_posts)
            activity = list(self._activity)
            votes_made = self.votes_made
        return {
            "voter": self.voter_username,
            "voter_id": self.voter_id,
            "running": self.running,
            "authors_count": len(self.authors),
            "posts_checked": self.posts_checked,
            "votes_made": votes_made,
            "pending_posts": len(pending),
            "last_scan_duration": round(self._last_scan_duration),
            "pending_details": [
                {
                    "author": p.author,
                    "vote_time": p.vote_time.isoformat(),
                    "title": getattr(p.post, 'title', '')[:60],
                }
                for p in pending
            ],
            "activity": activity,
        }
