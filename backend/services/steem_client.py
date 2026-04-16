import logging
from beem import Steem
from beem.account import Account
from beem.exceptions import AccountDoesNotExistsException
from beem.comment import Comment
from beem.imageuploader import ImageUploader

from backend.config import STEEM_NODES

logger = logging.getLogger(__name__)


class SteemClient:
    """Shared wrapper around beem. One instance per posting key."""

    def __init__(self, posting_key: str, nodes: list[str] | None = None):
        self._nodes = nodes or STEEM_NODES
        self._posting_key = posting_key
        self.steem: Steem | None = None

    def connect(self) -> bool:
        try:
            self.steem = Steem(node=self._nodes, keys=[self._posting_key])
            logger.info("Connected to Steem nodes")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Steem node: {e}")
            return False

    def get_account(self, username: str) -> Account | None:
        try:
            return Account(username, blockchain_instance=self.steem)
        except AccountDoesNotExistsException:
            logger.error(f"Account @{username} does not exist")
            return None
        except Exception as e:
            logger.error(f"Error fetching account @{username}: {e}")
            return None

    def get_latest_post(self, author: str):
        try:
            account = Account(author, blockchain_instance=self.steem)
            posts = account.get_blog(limit=1)
            return posts[0] if posts else None
        except Exception as e:
            logger.error(f"Error retrieving latest post for @{author}: {e}")
            return None

    def get_blog(self, author: str, limit: int = 5):
        try:
            account = Account(author, blockchain_instance=self.steem)
            return account.get_blog(limit=limit)
        except Exception as e:
            logger.error(f"Error retrieving blog for @{author}: {e}")
            return []

    def has_already_voted(self, post, voter: str) -> bool:
        votes = post.get_votes()
        return any(v['voter'] == voter for v in votes)

    def get_voting_power(self, username: str) -> float:
        account = self.get_account(username)
        if account:
            return account.get_voting_power()
        return 0.0

    def upvote(self, post, weight: float, voter: str) -> bool:
        try:
            post.upvote(weight=weight, voter=voter)
            return True
        except Exception as e:
            err = str(e)
            if "Duplicate transaction" in err:
                logger.warning(f"Duplicate-tx error for @{voter} on {post.authorperm} — vote likely went through")
                return True
            logger.error(f"Error upvoting as @{voter}: {e}")
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
