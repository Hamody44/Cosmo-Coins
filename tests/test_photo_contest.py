import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cogs import photo_contest


SCHEMA = """
CREATE TABLE photo_contests (contest_id INTEGER PRIMARY KEY, guild_id INTEGER, participation_channel_id INTEGER, voting_channel_id INTEGER, results_channel_id INTEGER, status TEXT, prizes_json TEXT, conditions TEXT, update_interval INTEGER, results_message_id INTEGER, created_at INTEGER, ended_at INTEGER);
CREATE TABLE contest_entries (contest_id INTEGER, entry_no INTEGER, author_id INTEGER, source_message_id INTEGER, voting_message_id INTEGER, image_url TEXT, created_at INTEGER, PRIMARY KEY(contest_id,entry_no));
CREATE TABLE contest_votes (contest_id INTEGER, entry_no INTEGER, voter_id INTEGER, voted_at INTEGER, excluded INTEGER DEFAULT 0, PRIMARY KEY(contest_id,voter_id));
CREATE TABLE contest_voter_reviews (contest_id INTEGER, voter_id INTEGER, username TEXT, avatar_url TEXT, banner_url TEXT, public_flags INTEGER, suspicion_score INTEGER DEFAULT 0, ai_review_status TEXT, excluded INTEGER DEFAULT 0, created_at INTEGER, PRIMARY KEY(contest_id,voter_id));
CREATE TABLE invite_uses (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, invite_code TEXT, inviter_id INTEGER, member_id INTEGER, member_username TEXT, joined_at INTEGER);
"""


class PhotoContestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "guild.db")
        with sqlite3.connect(self.path) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO photo_contests VALUES (1,1,10,20,30,'active','{}',NULL,300,NULL,1,NULL)")
            db.executemany("INSERT INTO contest_entries VALUES (1,?,?,?,?,?,1)", [(1,11,None,101,"a"), (2,22,None,102,"b")])
        self.patch = patch("cogs.photo_contest.get_guild_db", side_effect=lambda _: (sqlite3.connect(self.path), sqlite3.connect(self.path).cursor()))
        # use one connection/cursor pair, not two connections
        self.patch.stop()
        def db(_):
            conn = sqlite3.connect(self.path)
            return conn, conn.cursor()
        self.patch = patch("cogs.photo_contest.get_guild_db", side_effect=db)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def user(self, uid):
        return SimpleNamespace(id=uid, name=f"user{uid}", bot=False, public_flags=SimpleNamespace(value=0), display_avatar=SimpleNamespace(url=""), banner=None, created_at=None)

    def test_vote_order_and_duplicate_rejection_is_one_vote_per_user(self):
        self.assertEqual(photo_contest.record_vote(1, 1, 1, self.user(90))[0], True)
        # A second reaction changes the vote, rather than creating a second vote.
        self.assertEqual(photo_contest.record_vote(1, 1, 2, self.user(90))[1], "changed")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM contest_votes").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT entry_no FROM contest_votes").fetchone()[0], 2)

    def test_self_vote_is_rejected(self):
        self.assertEqual(photo_contest.record_vote(1, 1, 1, self.user(11)), (False, "self_vote"))

    def test_exclusion_removes_vote_from_ranking(self):
        with sqlite3.connect(self.path) as db:
            db.executemany("INSERT INTO contest_votes VALUES (1,?,?,1,?)", [(1, 90, 0), (2, 91, 0), (1, 92, 1)])
            rows = [{"entry_no": 1, "votes": 1}, {"entry_no": 2, "votes": 1}]
        self.assertEqual([r["entry_no"] for r in photo_contest.rank_entries(rows)], [1, 2])
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE contest_votes SET excluded=1 WHERE voter_id=90")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM contest_votes WHERE excluded=0").fetchone()[0], 1)

    def test_contest_lifecycle_status(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE photo_contests SET status='ended' WHERE contest_id=1")
            self.assertEqual(db.execute("SELECT status FROM photo_contests WHERE contest_id=1").fetchone()[0], "ended")
            db.execute("UPDATE photo_contests SET status='cancelled' WHERE contest_id=1")
            self.assertEqual(db.execute("SELECT status FROM photo_contests WHERE contest_id=1").fetchone()[0], "cancelled")

    def test_invite_history_can_be_counted_and_cleared(self):
        with sqlite3.connect(self.path) as db:
            db.executemany("INSERT INTO invite_uses(guild_id,invite_code,inviter_id,member_id,member_username,joined_at) VALUES (1,?,?,?,?,1)", [("abc", 7, 101, "a"), ("abc", 7, 102, "b")])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM invite_uses WHERE inviter_id=7").fetchone()[0], 2)
            db.execute("DELETE FROM invite_uses WHERE member_id=101")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM invite_uses").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
