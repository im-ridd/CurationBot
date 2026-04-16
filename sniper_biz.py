import time
import logging
from datetime import datetime, timedelta
import threading
from beem import Steem
from beem.account import Account
from beem.exceptions import AccountDoesNotExistsException
from beem.comment import Comment
from beem.imageuploader import ImageUploader
import queue
import json

nodes = [
	'https://api.steemit.com',
#	'https://api.campingclub.me',
	'https://api.moecki.online',
#	'https://api.pennsif.net',
	'https://steemapi.boylikegirl.club',
	'https://cn.steems.top',
	'https://api.worldofxpilar.com',
	'https://api.upvu.org'
    # Aggiungi qui altri nodi Hive
]


# Modifica la configurazione del logger per avere un output più dettagliato
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Configurazione del logger
logger = logging.getLogger(__name__)

class AuthorConfig:
    def __init__(self, vote_percentage=50, post_delay_minutes=1, daily_vote_limit=5,
                 add_comment=True, add_image=True, comment_text="", image_path=""):
        self.vote_percentage = vote_percentage
        self.post_delay_minutes = post_delay_minutes
        self.daily_vote_limit = daily_vote_limit
        self.add_comment = add_comment
        self.add_image = add_image
        self.comment_text = comment_text
        self.image_path = image_path
        self.votes_today = 0
        self.last_vote_time = None
        self.insert_at_now = datetime.now()
        self.log_queue = queue.Queue()

    def can_vote(self):
        now = datetime.now()
        if self.last_vote_time is None or now.date() > self.last_vote_time.date():
            self.votes_today = 0
        return self.votes_today < self.daily_vote_limit and self.insert_at_now <= now

    def record_vote(self):
        self.votes_today += 1
        self.last_vote_time = datetime.now()


class SteemSniperBackend:
    def __init__(self):
        self.steem = None
        self.running = False
        self.config = {
            'posting_key': '',
            'voter': '',
            'interval': 1,  # intervallo di polling in secondi
        }
        self.author_configs = {}
        self.lock = threading.Lock()
        self.pending_posts = []  # Lista di post da ricontrollare
        self.log_queue = queue.Queue()
        self.posts_checked = 0
        self.votes_made = 0

    def get_logs(self):
        logs = []
        while not self.log_queue.empty():
            logs.append(self.log_queue.get())
        return logs

    def log(self, message, level='INFO'):
        log_entry = {
            'timestamp': datetime.now().isoformat(),
            'level': level,
            'message': message
        }
        self.log_queue.put(json.dumps(log_entry))

    def configure(self, **kwargs):
        """Update global configuration with provided values."""
        self.config.update(kwargs)

    def configure_author(self, author, **kwargs):
        """Configure or update settings for a specific author."""
        if author not in self.author_configs:
            self.author_configs[author] = AuthorConfig()
        for key, value in kwargs.items():
            setattr(self.author_configs[author], key, value)

    def setup_steem_client(self):
        """Set up and return a Steem client."""
        try:
            self.steem = Steem(node=nodes, keys=[self.config['posting_key']])
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Steem node: {str(e)}")
            self.log("Failed to connect to Steem node", "ERROR")
            return False

    def validate_author(self, author):
        """Validate if an author exists on Steem."""
        try:
            Account(author, blockchain_instance=self.steem)
            return True
        except AccountDoesNotExistsException:
            logger.error(f"Invalid target: {author}")
            self.log("Invalid target: {author}", "ERROR")

            return False

    def get_latest_post(self, author):
        """Get the latest post of an author."""
        try:
            account = Account(author, blockchain_instance=self.steem)
            posts = account.get_blog(limit=1)
            return posts[0] if posts else None
        except Exception as e:
            self.log(f"Error retrieving latest post for {author}: {str(e)}", "ERROR")
            return None

    def has_already_voted(self, post, voter):
        """Check if the voter has already voted on the post."""
        votes = post.get_votes()
        return any(vote['voter'] == voter for vote in votes)


    def upload_image(self, image_path, voter):
        """Upload an image and return the URL."""
        try:
            uploader = ImageUploader(blockchain_instance=self.steem)
            account = Account(voter, blockchain_instance=self.steem)
            image_url = uploader.upload(image_path, account.name)
            if isinstance(image_url, dict) and 'url' in image_url:
                return image_url['url']
            else:
                self.log(f"Image upload failed: {image_url}", "ERROR")
                return None
        except Exception as e:
            self.log(f"Error uploading image: {str(e)}", "ERROR")
            return None

    def comment_post(self, post, author_config):
        """Add a comment to a post based on author configuration."""
        try:
            voter_account = Account(self.config['voter'], blockchain_instance=self.steem)
            comment = Comment(post, blockchain_instance=self.steem)

            comment_body = author_config.comment_text
            if author_config.add_image and author_config.image_path:
                image_url = self.upload_image(author_config.image_path, self.config['voter'])
                if image_url:
                    comment_body += f"\n\n![image]({image_url})"
                else:
                    self.log("Failed to upload image for comment", "ERROR")

            comment.reply(body=comment_body, author=voter_account.name)
            return True
        except Exception as e:
            self.log(f"Error commenting on post: {str(e)}", "ERROR")
            return False

    def has_voted_in_last_24h(self, author):
        """Check votes given to author in last 18 hours and compare with daily limit"""
        try:
            # Get author config
            author_config = self.author_configs.get(author)
            if not author_config:
                return True  # If no config exists, prevent voting

            account = Account(author, blockchain_instance=self.steem)
            current_time = datetime.utcnow()
            cutoff_time = current_time - timedelta(hours=18)

            # Get author's recent posts with votes
            blog = account.get_blog(limit=5)
            votes_in_period = 0

            # Count votes in last 18 hours
            for post in blog:
                post_time = post['created'].replace(tzinfo=None)
                if post_time > cutoff_time:
                    votes = post.get_votes()
                    if any(vote['voter'] == self.config['voter'] for vote in votes):
                        votes_in_period += 1

            # Check if we've hit the daily limit
            if votes_in_period >= author_config.daily_vote_limit:
             #   logger.info(f"Daily vote limit reached for @{author} ({votes_in_period}/{author_config.daily_vote_limit})")
                return True

            return False

        except Exception as e:
            logger.error(f"❌ Error checking vote history for @{author}: {str(e)}")
            return False

    def upvote_post(self, post, author):
        """Upvote a post with enhanced logging"""
        author_config = self.author_configs[author]
        try:
            voter_account = Account(self.config['voter'], blockchain_instance=self.steem)

            # Check voting power
            current_voting_power = voter_account.get_voting_power()
            if current_voting_power < self.config['min_voting_power']:
                logger.warning(f"⚡ Low voting power ({current_voting_power}%) for {author}")
                return False

            if self.has_already_voted(post, voter_account.name):
                logger.info(f"✓ Already voted on post: {post.title[:30]}...")
                return False

            # Perform the vote
            post.upvote(weight=author_config.vote_percentage*1.0, voter=voter_account.name)
            self.votes_made += 1
            logger.info(f"✅ Voted {author_config.vote_percentage}% on @{author}'s post: {post.title[:30]}...")
            return True

        except Exception as e:
            logger.error(f"❌ Error voting on @{author}'s post: {str(e)}")
            return False

    def get_post_creation_time(self, post):
        """Get the creation time of a post."""
        return post['created']



    def add_pending_post(self, author, post, config, post_time):
        """Aggiungi un post da votare con il tempo di attesa, se non già presente nei pending."""
        post_delay = post_time + timedelta(minutes=config.post_delay_minutes)

        # Controlla se il post è già presente nei pending
        for pending_post in self.pending_posts:
            if pending_post['post'].identifier == post.identifier:  # confronta gli identificatori del post
            #    logger.info(f"Il post '{post.title}' di '{author}' è già presente nei pending.")
                return  # Esci dalla funzione senza aggiungere il post

        # Aggiungi il post ai pending se non esiste già
        self.pending_posts.append({
            'author': author,
            'post': post,
            'post_time': post_time,
            'vote_time': post_delay,
            'config': config,
            'attempts': 0
        })
    #  logger.info(f"Aggiunto post '{post.title}' di '{author}' ai pending con tempo di voto {post_delay}.")


    def analyze_competitor_timing(self, author, competitor="karja"):
        """Analyze competitor's last vote timing for this author"""
        try:
            account = Account(author, blockchain_instance=self.steem)
            # Get last post (excluding the current one)
            blog = account.get_blog(limit=2)

            if len(blog) > 1:  # Make sure we have a previous post
                last_post = blog[1]  # Get the previous post
                post_time = last_post['created'].replace(tzinfo=None)
                votes = last_post.get_votes()

                for vote in votes:
                    if vote['voter'] == competitor:
                        vote_time = vote['time'].replace(tzinfo=None)
                        delay = (vote_time - post_time).total_seconds() / 60
                        logger.info(f"Last time {competitor} voted after {delay:.1f} minutes on @{author}")
                        if delay > 4:
                            # Anticipa di 15 secondi
                            return delay - 0.25
                        else:
                            # Non seguire il competitor se ha votato troppo presto
                            return None

            return None

        except Exception as e:
            logger.error(f"Error analyzing competitor timing: {str(e)}")
            return None

    def run_upvote_for_author(self, author, config):
        """Check and vote on author's posts with enhanced logging"""
        if not self.validate_author(author):
            return

        if self.has_voted_in_last_24h(author):
            return False

        latest_post = self.get_latest_post(author)

        if latest_post:
            self.posts_checked += 1
            current_time = datetime.utcnow()
            post_time = self.get_post_creation_time(latest_post)
            post_age = (current_time - post_time.replace(tzinfo=None)).total_seconds() / 60

            if post_age <= self.config['max_post_age_minutes']:
                already_pending = any(
                    p['post'].identifier == latest_post.identifier
                    for p in self.pending_posts
                )

                if not already_pending:
                    # Check competitor's timing and adjust our delay
                    competitor_delay = self.analyze_competitor_timing(author)
                    if competitor_delay is not None:
                        original_delay = config.post_delay_minutes
                        config.post_delay_minutes = min(original_delay, competitor_delay)
                        logger.info(f"Adjusting vote timing to beat competitor ({config.post_delay_minutes:.1f}m)")

                    logger.info(f"\n=== New Voteable Post Detected ===")
                    logger.info(f"Author: @{author}")
                    logger.info(f"Title: {latest_post.title[:50]}...")
                    logger.info(f"Age: {post_age:.1f} minutes")

                    if post_age < config.post_delay_minutes:
                        self.add_pending_post(author, latest_post, config, post_time)
                        logger.info(f"Status: Added to queue (will vote in {config.post_delay_minutes-post_age:.1f} minutes)")
                    else:
                        logger.info(f"Status: Processing immediate vote")
                        self.upvote_post(latest_post, author)
                    logger.info("=====================================")

    def check_pending_posts(self):
        """Check pending posts with minimal logging"""
        current_time = datetime.utcnow()

        for post_data in list(self.pending_posts):
            post_time = post_data['post_time'].replace(tzinfo=None)
            vote_time = post_data['vote_time'].replace(tzinfo=None)
            max_time = post_time + timedelta(minutes=self.config['max_post_age_minutes'])

            if current_time >= max_time:
                self.pending_posts.remove(post_data)
                continue

            if current_time >= vote_time:
                logger.info(f"\n=== Processing Queued Vote ===")
                logger.info(f"Author: @{post_data['author']}")
                logger.info(f"Title: {post_data['post'].title[:50]}...")
                if self.upvote_post(post_data['post'], post_data['author']):
                    self.pending_posts.remove(post_data)
                logger.info("============================")

    def log_status(self):
        """Log periodic status update with enhanced statistics"""
        try:
            voter_account = Account(self.config['voter'], blockchain_instance=self.steem)
            current_vp = voter_account.get_voting_power()
           # voted_authors = [author for author in self.author_configs.keys()
            #                if self.has_voted_in_last_24h(author)]

            # Calculate voting power recovery
            vp_to_full = 100 - current_vp
            hours_to_full = (vp_to_full * 432000) / (100 * 3600)  # 432000 seconds = 5 days for full recovery

            # Calculate success rate
#            success_rate = (self.votes_made / max(1, self.posts_checked)) * 100

            logger.info("\n=== Status Update ===")
            logger.info(f"Posts Checked: {self.posts_checked}")
            logger.info(f"Votes Made: {self.votes_made}")
          #  logger.info(f"Success Rate: {success_rate:.1f}%")
          #  logger.info(f"Authors Voted Today: {len(voted_authors)}/{len(self.author_configs)}")
            logger.info(f"Voting Power: {current_vp:.2f}%")
            logger.info(f"Full Power in: {hours_to_full:.1f}h")
            logger.info(f"Pending Posts: {len(self.pending_posts)}")
            if self.pending_posts:
                next_vote = min(p['vote_time'] for p in self.pending_posts)
                time_to_next = (next_vote.replace(tzinfo=None) - datetime.utcnow()).total_seconds() / 60
                if time_to_next > 0:
                    logger.info(f"Next Vote in: {time_to_next:.1f}m")
            logger.info("===================")

        except Exception as e:
            logger.error(f"Error getting stats: {str(e)}")
            # Fallback to basic status
            logger.info("\n=== Status Update (Basic) ===")
            logger.info(f"Posts Checked: {self.posts_checked}")
            logger.info(f"Votes Made: {self.votes_made}")
            logger.info(f"Pending Posts: {len(self.pending_posts)}")
            logger.info("===================")

    def run_upvote(self):
        """Main voting loop with minimal logging"""
        if not self.setup_steem_client():
            return

        logger.info("🚀 Bot Started - Monitoring {} authors".format(len(self.author_configs)))

        while self.running:
            try:
                for author, config in self.author_configs.items():
                    if not self.running:
                        break

                    self.check_pending_posts()
                    if config.can_vote():
                        self.run_upvote_for_author(author, config)

                # Log status every cycle
                self.log_status()
                time.sleep(self.config['interval'])

            except Exception as e:
                logger.error(f"Error in main loop: {str(e)}")
                time.sleep(self.config['interval'])

        logger.info("🛑 Stopping voting bot...")

    def start(self):
        """Start the sniping process."""
        if self.running:
            logger.warning("Sniper is already running!")
            self.log("Sniper is already running!", "WARNING")
            return

        self.running = True
        main_thread = threading.Thread(target=self.run_upvote)
        main_thread.start()

    def stop(self):
        """Stop the sniping process."""
        self.running = False
        logger.info("Stopping sniper...")
        self.log("Stopping sniper...", "INFO")