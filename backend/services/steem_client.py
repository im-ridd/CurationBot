import logging
import time
from beem import Steem
from beem.account import Account
from beem.exceptions import AccountDoesNotExistsException
from beem.comment import Comment
from beem.imageuploader import ImageUploader

from backend.config import STEEM_NODES

logger = logging.getLogger(__name__)


def verify_posting_key(username: str, wif_key: str) -> tuple[bool, str]:
    """Check that wif_key is the posting key for username.
    Returns (True, "") on success or (False, error_message).
    """
    try:
        from beemgraphenebase.account import PrivateKey as _PrivKey
        from backend.config import STEEM_NODES as _NODES
        steem = Steem(node=_NODES, timeout=15)
        account = Account(username, blockchain_instance=steem)
        posting_keys = [auth[0] for auth in account["posting"]["key_auths"]]
        pub = str(_PrivKey(wif_key).get_public_key())
        if pub in posting_keys:
            return True, ""
        return False, f"La posting key non corrisponde all'account @{username}"
    except AccountDoesNotExistsException:
        return False, f"Account @{username} non trovato sulla blockchain"
    except Exception as e:
        return False, f"Errore verifica posting key: {e}"


class SteemClient:
    """Shared wrapper around beem. One instance per posting key."""

    def __init__(self, posting_key: str, nodes: list[str] | None = None):
        self._nodes = nodes or STEEM_NODES
        self._posting_key = posting_key
        self.steem: Steem | None = None

    # Errors that mean the current node doesn't support a required API method.
    # Beem doesn't auto-rotate on these, so we do it manually.
    _BAD_NODE_SIGNALS = (
        "Could not find method",
        "method_itr != api_itr",
        "Bad Cast",
        "Invalid cast",
        "NoneType is not iterable",
        "argument of type 'NoneType'",
    )

    def _is_bad_node_error(self, err: str) -> bool:
        return any(s in err for s in self._BAD_NODE_SIGNALS)

    def _rotate_node(self):
        """Ask beem to switch to the next node in its list."""
        try:
            if self.steem and hasattr(self.steem, 'rpc'):
                self.steem.rpc.next()
                logger.warning("Rotated to next Steem node (API incompatibility)")
        except Exception:
            pass

    def connect(self) -> bool:
        try:
            self.steem = Steem(
                node=self._nodes,
                keys=[self._posting_key],
                timeout=30,
                storekeys=False,  # avoid "table keys already exists" SQLite error
            )
            logger.info("Connected to Steem nodes")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Steem node: {e}")
            return False

    def get_account(self, username: str) -> Account | None:
        for attempt in range(len(self._nodes)):
            try:
                return Account(username, blockchain_instance=self.steem)
            except AccountDoesNotExistsException:
                logger.error(f"Account @{username} does not exist")
                return None
            except Exception as e:
                err = str(e)
                if self._is_bad_node_error(err) and attempt < len(self._nodes) - 1:
                    self._rotate_node()
                    continue
                # Fallback: try condenser_api.get_accounts (more widely supported)
                try:
                    raw = self.steem.rpc.get_accounts([username])
                    if raw:
                        return Account(raw[0], blockchain_instance=self.steem)
                    return None
                except Exception:
                    pass
                logger.error(f"Error fetching account @{username}: {e}")
                return None
        logger.warning(f"All nodes failed for @{username} (get_account) — skipping this cycle")
        return None

    def _raw_blog(self, author: str, limit: int):
        """Fetch blog posts via condenser_api.get_discussions_by_blog (no find_accounts)."""
        raw = self.steem.rpc.get_discussions_by_blog({"tag": author, "limit": limit})
        if not raw:
            return []
        # Filter to only the author's own posts (not resteems)
        return [Comment(entry, blockchain_instance=self.steem)
                for entry in raw if entry.get("author") == author]

    def get_latest_post(self, author: str):
        for attempt in range(len(self._nodes)):
            try:
                posts = self._raw_blog(author, 1)
                return posts[0] if posts else None
            except Exception as e:
                err = str(e)
                if self._is_bad_node_error(err) and attempt < len(self._nodes) - 1:
                    self._rotate_node()
                    continue
                logger.error(f"Error retrieving latest post for @{author}: {e}")
                return None
        logger.warning(f"All nodes failed for @{author} (get_latest_post) — skipping this cycle")
        return None

    def get_blog(self, author: str, limit: int = 5):
        for attempt in range(len(self._nodes)):
            try:
                return self._raw_blog(author, limit)
            except Exception as e:
                err = str(e)
                if self._is_bad_node_error(err) and attempt < len(self._nodes) - 1:
                    self._rotate_node()
                    continue
                logger.error(f"Error retrieving blog for @{author}: {e}")
                return []
        logger.warning(f"All nodes failed for @{author} (get_blog) — skipping this cycle")
        return []

    def get_active_votes(self, author: str, permlink: str) -> list:
        """Returns active_votes with timestamp via condenser_api.get_active_votes."""
        for attempt in range(len(self._nodes)):
            try:
                raw = self.steem.rpc.get_active_votes(author, permlink)
                return raw or []
            except Exception as e:
                err = str(e)
                if self._is_bad_node_error(err) and attempt < len(self._nodes) - 1:
                    self._rotate_node()
                    continue
                logger.error(f"Error retrieving active_votes for @{author}/{permlink}: {e}")
                return []
        logger.warning(f"All nodes failed for @{author} (get_active_votes)")
        return []

    def has_already_voted(self, post, voter: str) -> bool:
        votes = post.get_votes()
        return any(v['voter'] == voter for v in votes)

    def get_voting_power(self, username: str) -> float | None:
        """Returns None if the VP could not be fetched (node error), 0.0..100.0 otherwise."""
        account = self.get_account(username)
        if account:
            return account.get_voting_power()
        return None

    def upvote(self, post, weight: float, voter: str) -> bool:
        for attempt in range(2):
            try:
                post.upvote(weight=weight, voter=voter)
                return True
            except Exception as e:
                err = str(e)
                if "Duplicate transaction" in err:
                    logger.warning(f"Duplicate-tx error for @{voter} on {post.authorperm} — vote likely went through")
                    return True
                if "already voted" in err.lower() or "You have already voted" in err:
                    logger.info(f"@{voter} already voted on {post.authorperm} — skipping")
                    return False  # not an error, just no-op
                if "STEEM_MIN_VOTE_INTERVAL" in err and attempt == 0:
                    logger.warning(f"Vote rate limit hit for @{voter}, retrying in 4s")
                    time.sleep(4)
                    continue
                logger.error(f"Error upvoting as @{voter}: {e}")
                return False
        return False

    def comment_on_post(self, post, voter: str, body: str) -> bool:
        try:
            comment = Comment(post, blockchain_instance=self.steem)
            comment.reply(body=body, author=voter)
            return True
        except Exception as e:
            logger.error(f"Error commenting as @{voter}: {e}")
            return False

    def upload_image(self, image_path: str, voter: str) -> str | None:
        try:
            uploader = ImageUploader(blockchain_instance=self.steem)
            account = Account(voter, blockchain_instance=self.steem)
            result = uploader.upload(image_path, account.name)
            if isinstance(result, dict) and 'url' in result:
                return result['url']
            logger.error(f"Image upload failed: {result}")
            return None
        except Exception as e:
            logger.error(f"Error uploading image: {e}")
            return None
