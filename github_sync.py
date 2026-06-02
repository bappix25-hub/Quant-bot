import subprocess
import os
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

def sync_to_github(message=None):
    try:
        home = os.path.expanduser("~")
        if not message:
            message = f"auto sync {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
        files = ["meme_bot.py", "learner.py", "github_sync.py", "bot_data.json"]
        for f in files:
            path = os.path.join(home, f)
            if os.path.exists(path):
                subprocess.run(["git", "-C", home, "add", f], capture_output=True)
        result = subprocess.run(
            ["git", "-C", home, "commit", "-m", message],
            capture_output=True, text=True
        )
        if "nothing to commit" in result.stdout:
            logger.info("GitHub: কোনো পরিবর্তন নেই")
            return True
        push = subprocess.run(
            ["git", "-C", home, "push", "origin", "main"],
            capture_output=True, text=True
        )
        if push.returncode == 0:
            logger.info(f"GitHub sync সফল: {message}")
            return True
        else:
            logger.error(f"GitHub push এরর: {push.stderr}")
            return False
    except Exception as e:
        logger.error(f"GitHub sync এরর: {e}")
        return False

def restore_from_github():
    try:
        home = os.path.expanduser("~")
        result = subprocess.run(
            ["git", "-C", home, "pull", "origin", "main"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            logger.info("GitHub থেকে ডেটা রিস্টোর হয়েছে")
            return True
    except Exception as e:
        logger.error(f"Restore এরর: {e}")
    return False
