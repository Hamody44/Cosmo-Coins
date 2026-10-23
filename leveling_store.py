"""Per-guild storage and idempotent state transitions for the leveling system."""

import time
from database import get_guild_db

XP_PER_LEVEL = 100
XP_COOLDOWN_SECONDS = 30
SPAM_WINDOW_SECONDS = 10
SPAM_MESSAGE_THRESHOLD = 5
SPAM_QUARANTINE_SECONDS = 10
SPAM_WARNING_COOLDOWN_SECONDS = 60


def ensure_level_config(guild_id: int) -> dict:
    conn, cur = get_guild_db(guild_id)
    cur.execute("INSERT OR IGNORE INTO leveling_config (guild_id) VALUES (?)", (guild_id,))
    conn.commit()
    cur.execute(
        "SELECT is_enabled, milestone_reward, announcement_channel_id, last_daily_report_date, enabled_since "
        "FROM leveling_config WHERE guild_id = ?",
        (guild_id,),
    )
    row = cur.fetchone()
    conn.close()
    return {
        "enabled": bool(row[0]),
        "milestone_reward": int(row[1] or 0),
        "announcement_channel_id": row[2],
        "last_daily_report_date": row[3],
        "enabled_since": float(row[4]) if row[4] is not None else None,
    }


def should_track_channel(guild_id: int, channel_id: int) -> bool:
    conn, cur = get_guild_db(guild_id)
    cur.execute(
        "SELECT c.is_enabled FROM leveling_config c "
        "JOIN leveling_channels ch ON ch.channel_id = ? WHERE c.guild_id = ?",
        (int(channel_id), int(guild_id)),
    )
    row = cur.fetchone()
    conn.close()
    return bool(row and row[0])


def set_leveling_enabled(guild_id: int, enabled: bool, now: float | None = None) -> None:
    now = float(now or time.time())
    conn, cur = get_guild_db(guild_id)
    cur.execute(
        "INSERT INTO leveling_config (guild_id, is_enabled, enabled_since) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET is_enabled = excluded.is_enabled, "
        "enabled_since = CASE WHEN excluded.is_enabled = 0 THEN NULL "
        "WHEN leveling_config.is_enabled = 0 THEN excluded.enabled_since "
        "ELSE leveling_config.enabled_since END",
        (guild_id, int(enabled), now if enabled else None),
    )
    conn.commit()
    conn.close()


def set_milestone_reward(guild_id: int, amount: int) -> None:
    conn, cur = get_guild_db(guild_id)
    cur.execute(
        "INSERT INTO leveling_config (guild_id, milestone_reward) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET milestone_reward = excluded.milestone_reward",
        (guild_id, int(amount)),
    )
    conn.commit()
    conn.close()


def set_announcement_channel(guild_id: int, channel_id: int) -> None:
    conn, cur = get_guild_db(guild_id)
    cur.execute(
        "INSERT INTO leveling_config (guild_id, announcement_channel_id) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET announcement_channel_id = excluded.announcement_channel_id",
        (guild_id, int(channel_id)),
    )
    # Reapplying this setting is also the operator's explicit retry for reports
    # that exhausted their retry budget, and moves pending reports off a stale channel.
    cur.execute(
        "UPDATE leveling_daily_reports SET channel_id = ?, status = 'pending', attempt_count = 0, "
        "next_attempt_at = 0, last_error = NULL, "
        "message_id = CASE WHEN last_send_channel_id IS NOT NULL AND last_send_channel_id != ? THEN NULL ELSE message_id END, "
        "last_send_attempt_at = CASE WHEN last_send_channel_id IS NOT NULL AND last_send_channel_id != ? THEN NULL ELSE last_send_attempt_at END, "
        "last_send_channel_id = CASE WHEN last_send_channel_id IS NOT NULL AND last_send_channel_id != ? THEN NULL ELSE last_send_channel_id END "
        "WHERE status = 'failed' "
        "OR (status = 'pending' AND channel_id != ?)",
        (int(channel_id), int(channel_id), int(channel_id), int(channel_id), int(channel_id)),
    )
    conn.commit()
    conn.close()


def add_level_channel(guild_id: int, channel_id: int) -> bool:
    conn, cur = get_guild_db(guild_id)
    cur.execute("INSERT OR IGNORE INTO leveling_channels (channel_id) VALUES (?)", (int(channel_id),))
    added = cur.rowcount == 1
    conn.commit()
    conn.close()
    return added


def remove_level_channel(guild_id: int, channel_id: int) -> bool:
    conn, cur = get_guild_db(guild_id)
    cur.execute("DELETE FROM leveling_channels WHERE channel_id = ?", (int(channel_id),))
    removed = cur.rowcount == 1
    conn.commit()
    conn.close()
    return removed


def get_level_channels(guild_id: int) -> list[int]:
    conn, cur = get_guild_db(guild_id)
    cur.execute("SELECT channel_id FROM leveling_channels ORDER BY channel_id")
    rows = [int(row[0]) for row in cur.fetchall()]
    conn.close()
    return rows


def record_pending_message(guild_id: int, user_id: int, message_id: int, channel_id: int, created_at: float) -> dict:
    """Stage a message for 10s so a 5-in-10s burst can be excluded as a whole."""
    conn, cur = get_guild_db(guild_id)
    conn.execute("BEGIN IMMEDIATE")
    cur.execute("INSERT OR IGNORE INTO leveling_users (user_id) VALUES (?)", (int(user_id),))
    duplicate = conn.execute(
        "SELECT 1 FROM leveling_pending_messages WHERE message_id = ? "
        "UNION ALL SELECT 1 FROM leveling_message_events WHERE message_id = ? LIMIT 1",
        (int(message_id), int(message_id)),
    ).fetchone()
    if duplicate:
        conn.commit()
        conn.close()
        return {"status": "duplicate", "warn": False}

    row = conn.execute(
        "SELECT spam_until, last_spam_warning_at FROM leveling_users WHERE user_id = ?",
        (int(user_id),),
    ).fetchone()
    spam_until, last_warning_at = (float(row[0] or 0), float(row[1] or 0)) if row else (0.0, 0.0)
    created_at = float(created_at)
    if spam_until > created_at:
        conn.commit()
        conn.close()
        return {"status": "quarantined", "warn": False}

    conn.execute(
        "INSERT INTO leveling_pending_messages (message_id, user_id, channel_id, created_at) VALUES (?, ?, ?, ?)",
        (int(message_id), int(user_id), int(channel_id), created_at),
    )
    cutoff = created_at - SPAM_WINDOW_SECONDS
    burst_count = conn.execute(
        "SELECT COUNT(*) FROM leveling_pending_messages WHERE user_id = ? AND created_at > ? AND created_at <= ?",
        (int(user_id), cutoff, created_at),
    ).fetchone()[0]
    if burst_count >= SPAM_MESSAGE_THRESHOLD:
        warn = last_warning_at == 0 or created_at - last_warning_at >= SPAM_WARNING_COOLDOWN_SECONDS
        conn.execute(
            "DELETE FROM leveling_pending_messages WHERE user_id = ? AND created_at > ? AND created_at <= ?",
            (int(user_id), cutoff, created_at),
        )
        conn.execute(
            "UPDATE leveling_users SET spam_until = ?, last_spam_warning_at = ? WHERE user_id = ?",
            (
                created_at + SPAM_QUARANTINE_SECONDS,
                created_at if warn else last_warning_at,
                int(user_id),
            ),
        )
        conn.commit()
        conn.close()
        return {"status": "spam", "warn": warn, "burst_count": int(burst_count)}

    conn.commit()
    conn.close()
    return {"status": "pending", "warn": False}


def mature_pending_messages(guild_id: int, now: float | None = None, limit: int = 1000) -> list[dict]:
    """Accept messages older than the spam window and award capped XP transactionally."""
    now = float(now or time.time())
    limit = max(1, min(int(limit), 5000))
    conn, cur = get_guild_db(guild_id)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT OR IGNORE INTO leveling_config (guild_id) VALUES (?)", (guild_id,))
    reward_row = conn.execute(
        "SELECT milestone_reward FROM leveling_config WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    milestone_reward = int(reward_row[0] or 0) if reward_row else 0
    pending = conn.execute(
        "SELECT message_id, user_id, channel_id, created_at FROM leveling_pending_messages "
        "WHERE created_at <= ? ORDER BY created_at, message_id LIMIT ?",
        (now - SPAM_WINDOW_SECONDS, limit),
    ).fetchall()
    milestone_payouts = []

    for message_id, user_id, channel_id, created_at in pending:
        user_id = int(user_id)
        created_at = float(created_at)
        already_processed = conn.execute(
            "SELECT 1 FROM leveling_message_events WHERE message_id = ?", (int(message_id),)
        ).fetchone()
        if already_processed:
            conn.execute("DELETE FROM leveling_pending_messages WHERE message_id = ?", (int(message_id),))
            continue
        conn.execute("INSERT OR IGNORE INTO leveling_users (user_id) VALUES (?)", (user_id,))
        profile = conn.execute(
            "SELECT total_points, last_point_at FROM leveling_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        old_points, last_point_at = int(profile[0] or 0), float(profile[1] or 0)
        points_awarded = int(last_point_at == 0 or created_at - last_point_at >= XP_COOLDOWN_SECONDS)
        new_points = old_points + points_awarded

        conn.execute(
            "INSERT OR IGNORE INTO leveling_message_events (message_id, user_id, channel_id, created_at, points_awarded) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(message_id), user_id, int(channel_id), created_at, points_awarded),
        )
        if points_awarded:
            conn.execute(
                "UPDATE leveling_users SET total_points = ?, last_point_at = ? WHERE user_id = ?",
                (new_points, created_at, user_id),
            )
            old_level = old_points // XP_PER_LEVEL + 1
            new_level = new_points // XP_PER_LEVEL + 1
            if milestone_reward > 0 and new_level > old_level:
                first_milestone = (old_level // 10 + 1) * 10
                for milestone in range(first_milestone, new_level + 1, 10):
                    payout_key = f"milestone:{guild_id}:{user_id}:{milestone}"
                    cur.execute(
                        "INSERT OR IGNORE INTO leveling_payouts "
                        "(payout_key, user_id, payout_type, amount, status, created_at, updated_at) "
                        "VALUES (?, ?, 'milestone', ?, 'pending', ?, ?)",
                        (payout_key, user_id, milestone_reward, created_at, now),
                    )
                    if cur.rowcount:
                        milestone_payouts.append({
                            "payout_key": payout_key,
                            "user_id": user_id,
                            "amount": milestone_reward,
                            "milestone": milestone,
                        })
        conn.execute("DELETE FROM leveling_pending_messages WHERE message_id = ?", (int(message_id),))

    conn.commit()
    conn.close()
    return milestone_payouts


def get_level_profile(guild_id: int, user_id: int) -> dict:
    conn, cur = get_guild_db(guild_id)
    cur.execute("INSERT OR IGNORE INTO leveling_users (user_id) VALUES (?)", (int(user_id),))
    conn.commit()
    row = conn.execute(
        "SELECT total_points FROM leveling_users WHERE user_id = ?", (int(user_id),)
    ).fetchone()
    points = int(row[0] or 0) if row else 0
    messages = int(conn.execute(
        "SELECT COUNT(*) FROM leveling_message_events WHERE user_id = ?", (int(user_id),)
    ).fetchone()[0])
    rank = int(conn.execute(
        "SELECT COUNT(*) + 1 FROM leveling_users u "
        "WHERE (u.total_points > ? OR (u.total_points = ? AND u.user_id < ?)) "
        "AND EXISTS (SELECT 1 FROM leveling_message_events e WHERE e.user_id = u.user_id)",
        (points, points, int(user_id)),
    ).fetchone()[0])
    conn.close()
    level = points // XP_PER_LEVEL + 1
    progress = points % XP_PER_LEVEL
    return {
        "user_id": int(user_id),
        "points": points,
        "level": level,
        "rank": rank,
        "messages": messages,
        "progress": progress,
        "next_level_points": XP_PER_LEVEL - progress,
    }


def get_level_leaderboard(guild_id: int, start_at: int | None, end_at: int | None, limit: int = 5) -> list[dict]:
    conn, cur = get_guild_db(guild_id)
    if start_at is None:
        query = (
            "SELECT u.user_id, u.total_points, COUNT(e.message_id) "
            "FROM leveling_users u JOIN leveling_message_events e ON e.user_id = u.user_id "
            "GROUP BY u.user_id ORDER BY u.total_points DESC, COUNT(e.message_id) DESC, u.user_id ASC LIMIT ?"
        )
        rows = cur.execute(query, (int(limit),)).fetchall()
    else:
        query = (
            "SELECT user_id, SUM(points_awarded), COUNT(*) FROM leveling_message_events "
            "WHERE created_at >= ? AND created_at < ? GROUP BY user_id "
            "ORDER BY SUM(points_awarded) DESC, COUNT(*) DESC, user_id ASC LIMIT ?"
        )
        rows = cur.execute(query, (int(start_at), int(end_at), int(limit))).fetchall()
    conn.close()
    return [
        {"user_id": int(row[0]), "points": int(row[1] or 0), "messages": int(row[2] or 0)}
        for row in rows
    ]


def set_salary_tier(guild_id: int, threshold: int, amount: int) -> None:
    conn, cur = get_guild_db(guild_id)
    cur.execute(
        "INSERT INTO leveling_salary_tiers (message_threshold, amount) VALUES (?, ?) "
        "ON CONFLICT(message_threshold) DO UPDATE SET amount = excluded.amount",
        (int(threshold), int(amount)),
    )
    conn.commit()
    conn.close()


def get_salary_tiers(guild_id: int) -> list[tuple[int, int]]:
    conn, cur = get_guild_db(guild_id)
    cur.execute("SELECT message_threshold, amount FROM leveling_salary_tiers ORDER BY message_threshold")
    rows = [(int(row[0]), int(row[1])) for row in cur.fetchall()]
    conn.close()
    return rows


def queue_payout(
    guild_id: int,
    payout_key: str,
    user_id: int,
    payout_type: str,
    amount: int,
    now: int | None = None,
    *,
    local_date: str | None = None,
    message_count: int | None = None,
    tier_threshold: int | None = None,
) -> dict:
    if int(amount) <= 0:
        return {"status": "disabled", "amount": 0}
    now = int(now or time.time())
    conn, cur = get_guild_db(guild_id)
    cur.execute(
        "INSERT OR IGNORE INTO leveling_payouts "
        "(payout_key, user_id, payout_type, amount, status, created_at, updated_at, local_date, message_count, tier_threshold) "
        "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
        (payout_key, int(user_id), payout_type, int(amount), now, now, local_date,
         int(message_count) if message_count is not None else None,
         int(tier_threshold) if tier_threshold is not None else None),
    )
    row = cur.execute(
        "SELECT user_id, payout_type, amount, status, local_date, message_count, tier_threshold "
        "FROM leveling_payouts WHERE payout_key = ?",
        (payout_key,),
    ).fetchone()
    if not row or int(row[0]) != int(user_id) or row[1] != payout_type:
        conn.close()
        raise ValueError("مفتاح الصرف مرتبط بطلب مختلف.")
    cur.execute(
        "UPDATE leveling_payouts SET local_date = COALESCE(local_date, ?), "
        "message_count = COALESCE(message_count, ?), tier_threshold = COALESCE(tier_threshold, ?) "
        "WHERE payout_key = ?",
        (local_date, int(message_count) if message_count is not None else None,
         int(tier_threshold) if tier_threshold is not None else None, payout_key),
    )
    row = cur.execute(
        "SELECT amount, status, local_date, message_count, tier_threshold "
        "FROM leveling_payouts WHERE payout_key = ?",
        (payout_key,),
    ).fetchone()
    conn.commit()
    conn.close()
    return {
        "amount": int(row[0]),
        "status": row[1],
        "local_date": row[2],
        "message_count": int(row[3]) if row[3] is not None else None,
        "tier_threshold": int(row[4]) if row[4] is not None else None,
    }


def get_salary_history(guild_id: int, user_id: int | None = None, limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    conn, cur = get_guild_db(guild_id)
    if user_id is None:
        rows = cur.execute(
            "SELECT payout_key, user_id, amount, status, local_date, message_count, tier_threshold "
            "FROM leveling_payouts WHERE payout_type = 'salary' "
            "ORDER BY COALESCE(local_date, '') DESC, created_at DESC, user_id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = cur.execute(
            "SELECT payout_key, user_id, amount, status, local_date, message_count, tier_threshold "
            "FROM leveling_payouts WHERE payout_type = 'salary' AND user_id = ? "
            "ORDER BY COALESCE(local_date, '') DESC, created_at DESC LIMIT ?",
            (int(user_id), limit),
        ).fetchall()
    conn.close()
    history = []
    for row in rows:
        local_date = row[4]
        if not local_date:
            parts = row[0].split(":")
            local_date = parts[2] if len(parts) >= 4 and parts[0] == "salary" else "غير معروف"
        history.append({
            "payout_key": row[0],
            "user_id": int(row[1]),
            "amount": int(row[2]),
            "status": row[3],
            "local_date": local_date,
            "message_count": int(row[5]) if row[5] is not None else None,
            "tier_threshold": int(row[6]) if row[6] is not None else None,
        })
    return history


def get_retryable_payouts(
    guild_id: int, now: int | None = None, retry_interval: int = 300, limit: int = 100
) -> list[dict]:
    now = int(now or time.time())
    limit = max(1, min(int(limit), 500))
    conn, cur = get_guild_db(guild_id)
    cur.execute(
        "SELECT payout_key, user_id, payout_type, amount, status FROM leveling_payouts "
        "WHERE status = 'pending' OR (status IN ('insufficient', 'blocked_wal') AND updated_at <= ?) "
        "ORDER BY created_at, payout_key LIMIT ?",
        (now - int(retry_interval), limit),
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {"payout_key": row[0], "user_id": int(row[1]), "payout_type": row[2],
         "amount": int(row[3]), "status": row[4]}
        for row in rows
    ]


def get_report_status(guild_id: int, local_date: str):
    conn, cur = get_guild_db(guild_id)
    cur.execute(
        "SELECT channel_id, message_id, status, created_at, attempt_count, next_attempt_at, last_error, "
        "last_send_attempt_at, last_send_channel_id "
        "FROM leveling_daily_reports WHERE local_date = ?",
        (local_date,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def start_daily_report(guild_id: int, local_date: str, channel_id: int, now: int | None = None) -> None:
    now = int(now or time.time())
    conn, cur = get_guild_db(guild_id)
    cur.execute(
        "INSERT INTO leveling_daily_reports (local_date, channel_id, status, created_at) "
        "VALUES (?, ?, 'pending', ?) ON CONFLICT(local_date) DO UPDATE SET "
        "channel_id = excluded.channel_id, created_at = excluded.created_at, status = 'pending', "
        "attempt_count = 0, next_attempt_at = 0, last_error = NULL, "
        "message_id = CASE WHEN leveling_daily_reports.last_send_channel_id IS NOT NULL "
        "AND leveling_daily_reports.last_send_channel_id != excluded.channel_id THEN NULL ELSE leveling_daily_reports.message_id END, "
        "last_send_attempt_at = CASE WHEN leveling_daily_reports.last_send_channel_id IS NOT NULL "
        "AND leveling_daily_reports.last_send_channel_id != excluded.channel_id THEN NULL ELSE leveling_daily_reports.last_send_attempt_at END, "
        "last_send_channel_id = CASE WHEN leveling_daily_reports.last_send_channel_id IS NOT NULL "
        "AND leveling_daily_reports.last_send_channel_id != excluded.channel_id THEN NULL ELSE leveling_daily_reports.last_send_channel_id END "
        "WHERE leveling_daily_reports.status != 'sent' "
        "AND leveling_daily_reports.channel_id != excluded.channel_id",
        (local_date, int(channel_id), now),
    )
    conn.commit()
    conn.close()


def mark_daily_report_send_attempt(
    guild_id: int, local_date: str, channel_id: int, now: float | None = None
) -> None:
    now = float(now or time.time())
    conn, cur = get_guild_db(guild_id)
    cur.execute(
        "UPDATE leveling_daily_reports SET last_send_attempt_at = ?, last_send_channel_id = ? "
        "WHERE local_date = ? AND status != 'sent'",
        (now, int(channel_id), local_date),
    )
    conn.commit()
    conn.close()


def save_daily_report_message(guild_id: int, local_date: str, message_id: int) -> None:
    conn, cur = get_guild_db(guild_id)
    cur.execute(
        "UPDATE leveling_daily_reports SET message_id = ? WHERE local_date = ? AND status != 'sent'",
        (int(message_id), local_date),
    )
    conn.commit()
    conn.close()


def finish_daily_report(guild_id: int, local_date: str, message_id: int) -> None:
    conn, cur = get_guild_db(guild_id)
    cur.execute(
        "UPDATE leveling_daily_reports SET message_id = ?, status = 'sent' WHERE local_date = ?",
        (int(message_id), local_date),
    )
    cur.execute(
        "UPDATE leveling_config SET last_daily_report_date = CASE "
        "WHEN last_daily_report_date IS NULL OR last_daily_report_date < ? THEN ? "
        "ELSE last_daily_report_date END WHERE guild_id = ?",
        (local_date, local_date, guild_id),
    )
    conn.commit()
    conn.close()


def record_report_failure(
    guild_id: int,
    local_date: str,
    error: str,
    now: int | None = None,
    max_attempts: int = 8,
) -> dict:
    now = int(now or time.time())
    conn, cur = get_guild_db(guild_id)
    row = cur.execute(
        "SELECT status, attempt_count FROM leveling_daily_reports WHERE local_date = ?",
        (local_date,),
    ).fetchone()
    if not row or row[0] == "sent":
        conn.close()
        return {"status": row[0] if row else "missing", "attempt_count": int(row[1]) if row else 0}
    attempts = int(row[1] or 0) + 1
    failed = attempts >= int(max_attempts)
    delay = min(15 * (2 ** min(attempts - 1, 6)), 900)
    status = "failed" if failed else "pending"
    next_attempt_at = 0 if failed else now + delay
    cur.execute(
        "UPDATE leveling_daily_reports SET status = ?, attempt_count = ?, next_attempt_at = ?, last_error = ? "
        "WHERE local_date = ? AND status != 'sent'",
        (status, attempts, next_attempt_at, str(error)[:300], local_date),
    )
    conn.commit()
    conn.close()
    return {"status": status, "attempt_count": attempts, "next_attempt_at": next_attempt_at}


def pending_reports(guild_id: int, now: int | None = None) -> list[tuple[str, int]]:
    now = int(now or time.time())
    conn, cur = get_guild_db(guild_id)
    cur.execute(
        "SELECT local_date, channel_id FROM leveling_daily_reports "
        "WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY local_date",
        (now,),
    )
    rows = [(str(row[0]), int(row[1])) for row in cur.fetchall()]
    conn.close()
    return rows
