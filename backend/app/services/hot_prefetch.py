"""hot_prefetch 领域服务：预热回迁管理，接收 tier_policy 和 cold_storage 上游事件。

跨模块数据流：
    tier_policy → hot_prefetch: COLD→HOT策略变更触发预热回迁
    cold_storage → hot_prefetch: 冷存完成后检查是否需要预热
    hot_prefetch → cost_tracker: 回迁任务更新成本追踪
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")


class HotPrefetchService(BaseModuleService):
    MODULE = "hot_prefetch"
    ENTITY = "回迁任务"
    OPERATION_TERM = "回填"
    LIST_FIELDS = ["任务编号", "冷存位置", "目标分区", "回迁量", "预计耗时", "优先级", "过期策略", "任务状态"]
    REQUIRED_FIELDS = ["任务编号", "冷存位置", "目标分区"]
    STATUS_ORDER = ["排队中", "回迁中", "已完成", "已取消"]
    ACTION_RULES = {"发起回迁": "回迁中", "确认完成": "已完成", "取消任务": "已取消"}
    EXTRA_ACTIONS = ["回收占用"]
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    def start_prefetch(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        cold_location = str(entry.get("冷存位置", ""))
        target_partition = str(entry.get("目标分区", ""))

        # 有容量明细的任务走容量服务派发，确保产生一次可比对的物理读取记录
        capacity = self._capacity_for_task(str(entry.get("任务编号", "")))
        if capacity is not None and not capacity.get("读取编号"):
            from app.services import service_for  # 延迟导入，避免服务容器循环依赖
            row, message = service_for("prefetch_capacity").dispatch(
                int(capacity["id"]), str(capacity.get("可开始时间")))
            if row is None:
                return None, message

        entry["status"] = "回迁中"
        entry["pending"] = True
        entry["任务状态"] = "回迁中"

        # 写入回迁记录
        self._write_prefetch_record(cold_location, entry)

        # 更新成本追踪
        self._update_cost_for_prefetch(cold_location)

        return entry, f"{self.ENTITY} {entry_id} 回迁中，{cold_location}→{target_partition}"

    def capacity_detail(self) -> dict[str, Any]:
        """按冷存对象聚合的待回迁计划量，供派发前核对实际读量。"""
        from app.services import service_for  # 延迟导入，避免服务容器循环依赖
        return service_for("prefetch_capacity").capacity_summary()

    def capacity_overview(self, as_of: str) -> dict[str, Any]:
        from app.services import service_for  # 延迟导入，避免服务容器循环依赖
        return service_for("prefetch_capacity").overview(as_of)

    def reclaim_capacity(self, as_of: str) -> dict[str, Any]:
        """目标保留窗口结束后回收暂存占用；其他目标仍需的数据继续保留。"""
        from app.services import service_for  # 延迟导入，避免服务容器循环依赖
        return service_for("prefetch_capacity").reclaim(as_of)

    def _capacity_for_task(self, task_no: str) -> dict[str, Any] | None:
        if not task_no:
            return None
        for row in store.rows("prefetch_capacity"):
            if str(row.get("回迁任务")) == task_no:
                return row
        return None

    def complete_prefetch(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        cold_location = str(entry.get("冷存位置", ""))
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["任务状态"] = "已完成"

        capacity = self._capacity_for_task(str(entry.get("任务编号", "")))
        if capacity is not None and capacity.get("读取编号"):
            capacity["status"] = "已交付"
            capacity["可用副本"] = "是"

        self._write_prefetch_record(cold_location, entry)

        return entry, f"{self.ENTITY} {entry_id} 已完成，数据已从冷存回迁到热层"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "发起回迁":
            return self.start_prefetch(entry_id)
        if action == "确认完成":
            return self.complete_prefetch(entry_id)
        if action == "回收占用":
            entry = store.find(self.MODULE, entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在"
            from app.services import service_for  # 延迟导入，避免服务容器循环依赖
            result = service_for("prefetch_capacity").reclaim(
                str(entry.get("过期策略") or "2026-11-22T23:59"))
            return entry, str(result.get("message", ""))
        return super().run_action(entry_id, action)

    def _write_prefetch_record(self, cold_location: str, entry: dict[str, Any]) -> None:
        safe_key = cold_location.replace("/", "_").replace(":", "_")
        filename = f"hot_prefetch_{safe_key}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "cold_location": cold_location,
            "task_id": str(entry.get("任务编号", "")),
            "target_partition": str(entry.get("目标分区", "")),
            "volume": str(entry.get("回迁量", "")),
            "priority": str(entry.get("优先级", "")),
            "expiry_policy": str(entry.get("过期策略", "")),
            "status": str(entry.get("任务状态", "")),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _update_cost_for_prefetch(self, cold_location: str) -> None:
        cost_rows = store.rows("cost_tracker")
        for row in cost_rows:
            if cold_location.find(str(row.get("关联资产", ""))) >= 0:
                row["传输成本"] = "预热回迁中"
                row["status"] = "已核算"
                row["pending"] = False
                break