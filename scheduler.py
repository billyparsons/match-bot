"""
scheduler.py — Scheduled events via APScheduler.

Reads heartbeat definitions from config.yaml and creates cron jobs for each.
Each heartbeat is just a named event with a time and a prompt — they all flow
through the same event callback, which injects them as feed events for Cleo
to process like any other message.
"""

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from auth import refresh_all_groups

log = logging.getLogger("cleo.scheduler")


def start_scheduler(config: dict, event_callback, token_refresh_callback=None) -> AsyncIOScheduler:
    """
    Start the APScheduler with jobs from config.

    event_callback: async function(event_name: str, prompt: str)
    Called when a scheduled event fires. The callback injects the event
    as a feed, triggering a wake cycle.
    """
    scheduler = AsyncIOScheduler(job_defaults={'misfire_grace_time': 3600})

    # Create a job for each heartbeat definition
    for name, hb in config.get("heartbeats", {}).items():
        cron_expr = hb["cron"]
        tz = hb.get("timezone", config.get("timezone", "America/New_York"))
        prompt = hb["prompt"]

        async def job(n=name, p=prompt):
            log.info("Firing scheduled event: %s", n)
            try:
                await event_callback(n, p)
            except Exception as e:
                log.error("Scheduled event %s failed: %s", n, e)

        trigger = CronTrigger.from_crontab(cron_expr, timezone=tz)
        scheduler.add_job(
            job,
            trigger=trigger,
            id=name,
            name=name,
            replace_existing=True,
        )
        log.info("Scheduled: %s [%s] (%s)", name, cron_expr, tz)

    # Refresh group member cache every 5 minutes
    if config.get("authorized_groups"):
        def group_refresh_job():
            refresh_all_groups(
                config["authorized_groups"],
                config["signal_rpc_url"],
                config["bot_number"],
            )

        scheduler.add_job(
            group_refresh_job,
            trigger=IntervalTrigger(minutes=5),
            id="group_member_refresh",
            name="Group member cache refresh",
            replace_existing=True,
        )

    # Proactive OAuth token refresh (prevents overnight expiry)
    if token_refresh_callback:
        async def token_refresh_job():
            try:
                await token_refresh_callback()
            except Exception as e:
                log.warning("Scheduled token refresh failed: %s", e)

        scheduler.add_job(
            token_refresh_job,
            trigger=IntervalTrigger(minutes=30),
            id="token_refresh",
            name="OAuth token refresh",
            replace_existing=True,
        )

    # Periodic vectorstore re-index (incremental, cheap if nothing changed)
    def reindex_job():
        try:
            from vectorstore import init_vectorstore, index_memory_files
            workspace = config["workspace"]
            init_vectorstore(workspace)
            count = index_memory_files(workspace)
            if count > 0:
                log.info("Periodic re-index: %d new chunks", count)
        except Exception as e:
            log.warning("Periodic re-index failed: %s", e)

    scheduler.add_job(
        reindex_job,
        trigger=IntervalTrigger(minutes=30),
        id="vectorstore_reindex",
        name="Vectorstore re-index",
        replace_existing=True,
    )

    scheduler.start()
    heartbeat_names = list(config.get("heartbeats", {}).keys())
    log.info("Scheduler started: %s", ", ".join(heartbeat_names) if heartbeat_names else "no heartbeats")
    return scheduler
