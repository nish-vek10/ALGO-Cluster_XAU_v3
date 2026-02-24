# src/core/cluster_engine.py
"""
ClusterEngine: rolling-window cluster detection on SiRiX events.

Architecture note:
  One ClusterEngine instance per StrategyState (per MAGIC number).
  Each engine has its own window size (t_seconds) and threshold (k_unique),
  so multiple strategies on the same account are fully independent.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Optional, List

from config.config import CLUSTER_REFRACTORY_SECONDS, VERBOSE_CLUSTERS, VERBOSE_CLUSTER_DEBUG
from src.core.models import SirixPositionEvent
from src.core.logger import log


class ClusterEngine:
    """
    Maintains a rolling deque of SiRiX open-position events and detects
    BUY / SELL clusters based on unique-trader count within a time window.

    KEY DESIGN:
      The rolling window is anchored to the LATEST event's OpenTime, not
      wall-clock time. This matches backtest logic: "T seconds between trade
      open times", so late-delivered batches don't inflate the window.
    """

    def __init__(self, window_seconds: int, k_unique: int):
        self.window_seconds = window_seconds
        self.k_unique       = k_unique

        # Deque of SirixPositionEvent (event-time sorted)
        self.events: deque[SirixPositionEvent] = deque()

        # Anti-spam: track last cluster to enforce refractory period
        self.last_cluster_time: Optional[datetime] = None
        self.last_cluster_side: Optional[str]      = None

    def add_events(self, new_events: List[SirixPositionEvent]) -> Optional[str]:
        """
        Ingest new events and check whether a cluster has formed.

        Returns:
          "buy"  if a BUY  cluster is detected
          "sell" if a SELL cluster is detected
          None   if no cluster (or refractory active)
        """
        # 1) Append all new events
        for ev in new_events:
            self.events.append(ev)

        if not self.events:
            return None

        # 2) Trim window: keep only events within [latest_time - T, latest_time]
        latest_time = self.events[-1].time
        cutoff      = latest_time - timedelta(seconds=self.window_seconds)
        while self.events and self.events[0].time < cutoff:
            self.events.popleft()

        if not self.events:
            return None

        # 3) Count unique traders per side within the window
        buy_users:  set = set()
        sell_users: set = set()
        for ev in self.events:
            if ev.side == "buy":
                buy_users.add(ev.user_id)
            else:
                sell_users.add(ev.user_id)

        if VERBOSE_CLUSTER_DEBUG:
            log(
                f"[CLUSTER_DEBUG] T={self.window_seconds}s | "
                f"events={len(self.events)} | "
                f"buy_unique={len(buy_users)} | sell_unique={len(sell_users)} | "
                f"latest={latest_time.isoformat()}"
            )

        # 4) Determine cluster side (largest side wins; buy takes priority on tie)
        cluster_side: Optional[str] = None
        if len(buy_users) >= self.k_unique:
            cluster_side = "buy"
        elif len(sell_users) >= self.k_unique:
            cluster_side = "sell"

        if cluster_side is None:
            return None

        # 5) Refractory: suppress repeat clusters of the same side within 1 second
        if (
            self.last_cluster_time is not None
            and self.last_cluster_side == cluster_side
            and (latest_time - self.last_cluster_time)
                < timedelta(seconds=CLUSTER_REFRACTORY_SECONDS)
        ):
            return None

        # 6) Cluster confirmed — record and return
        self.last_cluster_time = latest_time
        self.last_cluster_side = cluster_side

        if VERBOSE_CLUSTERS:
            log(
                f"[CLUSTER] {cluster_side.upper()} detected "
                f"(T={self.window_seconds}s, K={self.k_unique}, "
                f"buy_u={len(buy_users)}, sell_u={len(sell_users)})"
            )

        return cluster_side
