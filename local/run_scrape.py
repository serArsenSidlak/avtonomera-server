"""Run one local scrape cycle: scrape (this Mac) → persist → notify.

Usage:
    python -m local.run_scrape
For the server architecture the Mac instead PUSHES results via local.push_scrape.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from local.persist import apply_scan, notify_new
from local.scraper import scrape


async def run_once(regions=None, progress=None, only_scopes=None) -> Dict[str, Any]:
    """Scrape, persist (scope-safe), notify. Returns a detailed summary.

    Args:
        regions: optional set/list of region NAMES (None = all).
        progress: optional async callback(region_name, running_total).
        only_scopes: optional set of (region, type) to re-scan ONLY those (retry failed).
    """
    async def _default_progress(region_name: str, running_total: int) -> None:
        print(f"[scan] region done: {region_name} · running total {running_total}", flush=True)

    res = await scrape(progress=progress or _default_progress, regions=regions, only_scopes=only_scopes)
    applied = await apply_scan(res["rows"], res["ok_scopes"], source="mac")
    notified = await notify_new(applied["new_ids"])
    summary = {
        "scraped": applied["scraped"], "new": len(applied["new_ids"]), "removed": applied["removed"],
        "notified": notified, "ok_scopes": len(res["ok_scopes"]), "fail_scopes": res["fail_scopes"],
        "by_region": res["by_region"], "by_type": res["by_type"],
    }
    print(f"[scan] scraped={summary['scraped']} new={summary['new']} removed={summary['removed']} "
          f"ok={summary['ok_scopes']} failed={len(res['fail_scopes'])}", flush=True)
    return summary


if __name__ == "__main__":
    asyncio.run(run_once())
