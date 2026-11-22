"""prefetch_capacity 领域服务：回迁容量明细、冷存物理读取与暂存位占用核算。

冷存储管理在派发预热回迁前先读这份明细：按冷存对象聚合待回迁分区的计划数据量，
确认同一对象是否被多个目标分区共用；派发时真正发起一次冷存读取，并记录
    读取编号 / 物理读量 / 暂存占用区间 / 收到数据的目标分区。
一次物理读取只计一次源端读量，目标分区各自的时限承诺和实际数据去向单独保留，
容量概览不会把源端读量和目标侧取得量重复相加。

时间一律使用 `2026-11-22T10:00` 这种分钟精度字符串；也接受 `10:00`，
缺省日期取演示场景日。业务规则全部留在服务层，路由只做参数搬运。
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any

from app.scenario import (
    SCENARIO_CAPACITY_ROWS,
    SCENARIO_COLD_OBJECT,
    SCENARIO_DATE,
    SCENARIO_HOT_TASKS,
    SCENARIO_STAGING,
    SCENARIO_STAGING_ROWS,
)
from app.services.base import BaseModuleService
from app.store import store

CAPACITY_FIELDS = [
    "明细编号", "冷存对象", "回迁任务", "目标分区", "计划数据量GB", "对象数据量GB",
    "可开始时间", "最晚交付", "保留截止", "暂存需求GB", "优先级",
    "读取编号", "实际取得GB", "数据去向", "可用副本",
    "暂存占用开始", "暂存占用结束", "交付时间", "回收时间", "派发说明",
]
READ_FIELDS = [
    "读取编号", "冷存对象", "物理读取GB", "读取开始", "读取结束", "暂存位置",
    "暂存占用GB", "暂存占用开始", "暂存占用结束", "收到数据目标", "交付时间",
    "回收时间", "状态",
]
STAGING_FIELDS = ["暂存位置", "容量GB", "当前占用GB", "说明"]


def parse_moment(value: str | None, default_hhmm: str = "00:00") -> datetime:
    """解析场景时间：空值取场景日 00:00，`10:30` 补场景日期，带秒 ISO 原样解析。"""
    text = str(value or "").strip() or f"{SCENARIO_DATE}T{default_hhmm}"
    text = text.replace(" ", "T")
    if "T" not in text:
        text = f"{text}T00:00" if ":" not in text else f"{SCENARIO_DATE}T{text}"
    if text.endswith("Z"):
        text = text[:-1]
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"时间格式无法识别：{value}") from exc


def format_moment(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M")


class PrefetchCapacityService(BaseModuleService):
    MODULE = "prefetch_capacity"
    ENTITY = "回迁容量明细"
    LIST_FIELDS = CAPACITY_FIELDS
    REQUIRED_FIELDS = ["明细编号", "冷存对象", "目标分区"]
    STATUS_ORDER = ["待派发", "已派发", "已交付", "已回收"]
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS = []

    # ---------- 查询 ----------
    def capacity_rows(self) -> list[dict[str, Any]]:
        return store.rows("prefetch_capacity")

    def read_rows(self) -> list[dict[str, Any]]:
        return store.rows("prefetch_read")

    def staging_rows(self) -> list[dict[str, Any]]:
        return store.rows("prefetch_staging")

    def _capacity(self, capacity_id: int) -> dict[str, Any] | None:
        return store.find("prefetch_capacity", capacity_id)

    def _read(self, read_id: str) -> dict[str, Any] | None:
        for row in self.read_rows():
            if str(row.get("读取编号")) == read_id:
                return row
        return None

    def staging_capacity_gb(self) -> int:
        return sum(int(row.get("容量GB", 0)) for row in self.staging_rows())

    # ---------- 容量明细：按冷存对象聚合 ----------
    def capacity_summary(self) -> dict[str, Any]:
        """派发前核对用：计划数据量按冷存对象聚合，并给出对象去重后的物理量。"""
        grouped: dict[str, dict[str, Any]] = {}
        for row in self.capacity_rows():
            key = str(row.get("冷存对象", ""))
            item = grouped.setdefault(key, {
                "冷存对象": key,
                "目标分区数": 0,
                "计划数据量GB": 0,
                "对象数据量GB": int(row.get("对象数据量GB", 0) or 0),
                "目标分区": [],
                "明细": [],
            })
            item["目标分区数"] += 1
            item["计划数据量GB"] += int(row.get("计划数据量GB", 0) or 0)
            item["目标分区"].append(str(row.get("目标分区", "")))
            item["明细"].append(str(row.get("明细编号", "")))
            item["对象数据量GB"] = max(
                item["对象数据量GB"], int(row.get("对象数据量GB", 0) or 0))
        objects = list(grouped.values())
        return {
            "objects": objects,
            "planned_target_gb": sum(item["计划数据量GB"] for item in objects),
            "planned_physical_gb": sum(item["对象数据量GB"] for item in objects),
            "note": "计划数据量按目标分区求和；同一冷存对象只按对象大小计一次物理读取量。",
        }

    # ---------- 派发 ----------
    def dispatch(
        self,
        capacity_id: int,
        as_of: str,
        *,
        hold_until: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """按 `as_of` 派发一次冷存读取；可选的 `hold_until` 用于保留暂存副本。"""
        row = self._capacity(capacity_id)
        if row is None:
            return None, f"{self.ENTITY} {capacity_id} 不存在"
        try:
            moment = parse_moment(as_of)
            ready_at = parse_moment(row.get("可开始时间"))
            deadline = parse_moment(row.get("最晚交付"))
            retention_end = parse_moment(row.get("保留截止"))
            duration = timedelta(minutes=int(row.get("读取耗时分钟", 60) or 60))
        except ValueError as exc:
            return None, str(exc)

        if row.get("读取编号"):
            message = self._dispatch_replay(row, moment)
            if message is not None:
                return row, message

        if moment < ready_at:
            return None, (
                f"{row.get('目标分区')} 在 {row.get('可开始时间')} 之前不能保留数据，"
                f"本次派发时间 {format_moment(moment)} 未到窗口")
        if moment > retention_end:
            return None, (
                f"{row.get('目标分区')} 的目标保留窗口 "
                f"{row.get('保留截止')} 已结束，不能再派发回迁")

        read_end = moment + duration
        if read_end > deadline:
            return None, (
                f"按 {format_moment(moment)} 开始读取需到 {format_moment(read_end)} 才能交付，"
                f"晚于最晚交付 {row.get('最晚交付')}")

        occupancy_end = max(read_end, retention_end)
        if hold_until:
            try:
                occupancy_end = max(occupancy_end, parse_moment(hold_until))
            except ValueError as exc:
                return None, str(exc)

        demand = int(row.get("暂存需求GB", 0) or 0)

        # 同一对象的暂存副本仍可用时直接复用，不再产生一次物理读取。
        reusable = self._find_reusable_read(row, moment)
        if reusable is not None:
            extended_end = max(parse_moment(reusable.get("暂存占用结束")), occupancy_end)
            if self._staging_peak(reusable["暂存占用开始"], format_moment(extended_end),
                                  exclude_read_id=str(reusable.get("读取编号"))) + demand <= self.staging_capacity_gb():
                self._attach_target(reusable, row, format_moment(extended_end))
                self._write_audit(
                    "复用冷存读取", str(row.get("明细编号", "")),
                    f"{row.get('目标分区')} 复用 {reusable.get('读取编号')}，不新增物理读取")
                return row, (
                    f"{row.get('目标分区')} 复用已有读取 {reusable.get('读取编号')}，"
                    f"物理读取量不重复累加")

        # 新建一次物理读取前先核对唯一暂存位。
        if not self._staging_available(moment, format_moment(occupancy_end), demand):
            holder = self._staging_holder(moment, format_moment(occupancy_end))
            return None, (
                f"唯一暂存位 {SCENARIO_STAGING} 在 "
                f"{format_moment(moment)}~{format_moment(occupancy_end)} 不可用"
                + (f"，正被 {holder} 占用" if holder else "")
                + "，请调整派发窗口")

        read = self._create_read(row, moment, read_end, occupancy_end)
        self._apply_read_to_capacity(row, read, format_moment(read_end), format_moment(occupancy_end))
        self._mark_hot_task(row, "回迁中")
        self._write_audit(
            "派发冷存读取", str(row.get("明细编号", "")),
            f"{read['读取编号']} {read['冷存对象']} 读取{read['物理读取GB']}GB → "
            f"{row.get('目标分区')}，暂存占用 {read['暂存占用开始']}~{read['暂存占用结束']}")
        return row, (
            f"{row.get('目标分区')} 已派发 {read['读取编号']}，读取 "
            f"{read['物理读取GB']}GB，暂存占用 "
            f"{read['暂存占用开始']}~{read['暂存占用结束']}")

    def _dispatch_replay(self, row: dict[str, Any], moment: datetime) -> str | None:
        """已派发过的明细：返回复用说明；副本已回收则返回 None 允许重新读取。"""
        read = self._read(str(row.get("读取编号", "")))
        if read is None or read.get("回收时间"):
            return None
        read_end = parse_moment(read.get("读取结束"))
        occupancy_end = parse_moment(
            read.get("回收时间") or read.get("暂存占用结束"))
        if moment < read_end:
            return f"{row.get('目标分区')} 的读取仍在进行中，不重复派发"
        if moment <= occupancy_end:
            return (
                f"重放命中：{row.get('目标分区')} 复用 {read.get('读取编号')}，"
                f"实际读取仍为 {read.get('物理读取GB')}GB，不重复计入")
        return None

    def _find_reusable_read(self, row: dict[str, Any],
                            moment: datetime) -> dict[str, Any] | None:
        for read in self.read_rows():
            if read.get("回收时间") or str(read.get("冷存对象")) != str(row.get("冷存对象")):
                continue
            if moment < parse_moment(read.get("读取结束")):
                continue
            if moment > parse_moment(read.get("暂存占用结束")):
                continue
            return read
        return None

    def _attach_target(self, read: dict[str, Any], row: dict[str, Any],
                       occupancy_end: str) -> None:
        target = {
            "明细编号": str(row.get("明细编号", "")),
            "回迁任务": str(row.get("回迁任务", "")),
            "目标分区": str(row.get("目标分区", "")),
            "数据量GB": int(row.get("计划数据量GB", 0) or 0),
            "保留截止": str(row.get("保留截止", "")),
            "状态": "可用",
        }
        targets = list(read.get("目标明细") or [])
        targets.append(target)
        read["目标明细"] = targets
        read["收到数据目标"] = "、".join(str(item["目标分区"]) for item in targets)
        read["暂存占用结束"] = occupancy_end
        self._apply_read_to_capacity(
            row, read, str(row.get("交付时间") or read.get("读取结束")), occupancy_end)
        self._mark_hot_task(row, "回迁中")

    def _create_read(self, row: dict[str, Any], moment: datetime,
                     read_end: datetime, occupancy_end: datetime) -> dict[str, Any]:
        reads = self.read_rows()
        next_id = max((int(item.get("id", 0)) for item in reads), default=0) + 1
        read = {
            "id": next_id,
            "status": "已派发",
            "pending": False,
            "abnormal": False,
            "读取编号": f"READ-{next_id:04d}",
            "冷存对象": str(row.get("冷存对象", "")),
            "物理读取GB": int(row.get("对象数据量GB", 0) or 0),
            "读取开始": format_moment(moment),
            "读取结束": format_moment(read_end),
            "暂存位置": SCENARIO_STAGING,
            "暂存占用GB": int(row.get("暂存需求GB", 0) or 0),
            "暂存占用开始": format_moment(moment),
            "暂存占用结束": format_moment(occupancy_end),
            "收到数据目标": str(row.get("目标分区", "")),
            "目标明细": [{
                "明细编号": str(row.get("明细编号", "")),
                "回迁任务": str(row.get("回迁任务", "")),
                "目标分区": str(row.get("目标分区", "")),
                "数据量GB": int(row.get("计划数据量GB", 0) or 0),
                "保留截止": str(row.get("保留截止", "")),
                "状态": "可用",
            }],
            "交付时间": format_moment(read_end),
            "回收时间": "",
            "状态": "已派发",
        }
        reads.append(read)
        return read

    def _apply_read_to_capacity(self, row: dict[str, Any], read: dict[str, Any],
                                delivered_at: str, occupancy_end: str) -> None:
        row["读取编号"] = str(read.get("读取编号", ""))
        row["实际取得GB"] = int(row.get("计划数据量GB", 0) or 0)
        row["数据去向"] = str(row.get("目标分区", ""))
        row["可用副本"] = "是"
        row["暂存占用开始"] = str(read.get("暂存占用开始", ""))
        row["暂存占用结束"] = occupancy_end
        row["交付时间"] = delivered_at
        row["回收时间"] = ""
        row["status"] = "已派发"
        row["任务状态"] = "已派发"
        row["pending"] = True

    def _mark_hot_task(self, row: dict[str, Any], status: str) -> None:
        for task in store.rows("hot_prefetch"):
            if str(task.get("任务编号")) == str(row.get("回迁任务")):
                task["status"] = status
                task["任务状态"] = status
                task["pending"] = status not in ("已完成", "已取消")
                break

    # ---------- 暂存位占用 ----------
    def _effective_end(self, read: dict[str, Any]) -> str:
        planned = str(read.get("暂存占用结束", ""))
        released = str(read.get("回收时间", ""))
        if released and parse_moment(released) < parse_moment(planned):
            return released
        return planned

    def _active_read_intervals(self, exclude_read_id: str | None = None
                               ) -> list[tuple[datetime, datetime, int, str]]:
        intervals = []
        for read in self.read_rows():
            if read.get("回收时间"):
                continue
            if exclude_read_id and str(read.get("读取编号")) == exclude_read_id:
                continue
            start = parse_moment(read.get("暂存占用开始"))
            end = parse_moment(self._effective_end(read))
            if end <= start:
                continue
            intervals.append((start, end, int(read.get("暂存占用GB", 0) or 0),
                              str(read.get("读取编号", ""))))
        return intervals

    def _staging_peak(self, start: str | None, end: str | None,
                      exclude_read_id: str | None = None) -> int:
        interval_start = parse_moment(start)
        interval_end = parse_moment(end)
        if interval_end <= interval_start:
            return 0
        events: list[tuple[datetime, int]] = []
        for other_start, other_end, demand, _ in self._active_read_intervals(exclude_read_id):
            overlap_start = max(interval_start, other_start)
            overlap_end = min(interval_end, other_end)
            if overlap_end <= overlap_start:
                continue
            events.append((overlap_start, demand))
            events.append((overlap_end, -demand))
        used = 0
        peak = 0
        for _, delta in sorted(events):
            used += delta
            peak = max(peak, used)
        return peak

    def _staging_available(self, start: datetime, end: str, demand: int) -> bool:
        return (self._staging_peak(format_moment(start), end) + demand
                <= self.staging_capacity_gb())

    def _staging_holder(self, start: datetime, end: str) -> str:
        holders = []
        for other_start, other_end, _, read_id in self._active_read_intervals():
            if other_start < parse_moment(end) and start < other_end:
                holders.append(read_id)
        return "、".join(holders)

    def occupied_gb(self, moment: datetime) -> int:
        """占用区间按分钟闭区间计算：`保留到 10:00` 在 10:00 这一分钟仍占着。"""
        used = 0
        for start, end, demand, _ in self._active_read_intervals():
            if start <= moment <= end:
                used += demand
        return used

    def staging_state(self, as_of: str | None = None) -> dict[str, Any]:
        moment = parse_moment(as_of)
        rows = []
        for staging in self.staging_rows():
            holders = [
                read_id for start, end, _, read_id in self._active_read_intervals()
                if start <= moment <= end
            ]
            used = self.occupied_gb(moment)
            rows.append({
                **staging,
                "当前占用GB": used,
                "当前占用读取": holders,
                "剩余GB": int(staging.get("容量GB", 0) or 0) - used,
            })
        return {"as_of": format_moment(moment), "items": rows}

    # ---------- 回收 ----------
    def reclaim(self, as_of: str) -> dict[str, Any]:
        """目标保留窗口结束后释放对应占用；仍有目标在窗口内的读取继续保留。"""
        try:
            moment = parse_moment(as_of)
        except ValueError as exc:
            return {"ok": False, "message": str(exc), "released": [], "retained": []}

        released: list[str] = []
        retained: list[str] = []
        freed_gb = 0
        for read in self.read_rows():
            if read.get("回收时间"):
                continue
            delivered_at = parse_moment(read.get("读取结束"))
            if moment < delivered_at:
                continue
            targets = list(read.get("目标明细") or [])
            still_needed = [item for item in targets
                            if moment <= parse_moment(item.get("保留截止"))]
            for item in targets:
                if item in still_needed or item.get("状态") == "已回收":
                    continue
                item["状态"] = "已回收"
                row = self._find_capacity_row(str(item.get("明细编号", "")))
                if row is not None and row.get("可用副本") == "是":
                    row["可用副本"] = "否"
                    row["回收时间"] = format_moment(moment)
                    row["status"] = "已回收"
            if still_needed:
                read["状态"] = "保留中"
                retained.append(
                    f"{read.get('读取编号')} 保留到 "
                    f"{max((item.get('保留截止') for item in still_needed), default='')}，"
                    f"仍有目标 {', '.join(str(item.get('目标分区')) for item in still_needed)} 需要")
                continue
            read["回收时间"] = format_moment(moment)
            read["状态"] = "已回收"
            freed_gb += int(read.get("暂存占用GB", 0) or 0)
            released.append(str(read.get("读取编号", "")))
            self._write_audit(
                "回收回迁占用", str(read.get("读取编号", "")),
                f"{read.get('读取编号')} 目标保留窗口结束，释放暂存 "
                f"{read.get('暂存占用GB')}GB")

        return {
            "ok": True,
            "as_of": format_moment(moment),
            "released": released,
            "retained": retained,
            "freed_gb": freed_gb,
            "message": (
                f"回收 {len(released)} 次读取、释放 {freed_gb}GB 暂存"
                + (f"；{len(retained)} 次读取因其他目标仍需而保留" if retained else "")),
        }

    def _find_capacity_row(self, detail_id: str) -> dict[str, Any] | None:
        for row in self.capacity_rows():
            if str(row.get("明细编号")) == detail_id:
                return row
        return None

    # ---------- 按时间点核对 ----------
    def snapshot(self, as_of: str) -> dict[str, Any]:
        moment = parse_moment(as_of)
        targets: list[dict[str, Any]] = []
        for row in self.capacity_rows():
            read = self._read(str(row.get("读取编号", ""))) if row.get("读取编号") else None
            delivered_at = str(row.get("交付时间", ""))
            received = 0
            if read is not None and delivered_at and moment >= parse_moment(delivered_at):
                received = int(row.get("实际取得GB", 0) or 0)
            available = bool(
                received
                and row.get("可用副本") == "是"
                and moment <= parse_moment(row.get("保留截止"))
                and not read_has_released(read))
            on_time = bool(
                delivered_at and parse_moment(delivered_at) <= parse_moment(row.get("最晚交付")))
            targets.append({
                "明细编号": row.get("明细编号"),
                "回迁任务": row.get("回迁任务"),
                "冷存对象": row.get("冷存对象"),
                "目标分区": row.get("目标分区"),
                "计划数据量GB": row.get("计划数据量GB"),
                "实际取得GB": received,
                "数据去向": row.get("数据去向") or "",
                "读取编号": row.get("读取编号") or "",
                "暂存占用开始": row.get("暂存占用开始") or "",
                "暂存占用结束": row.get("暂存占用结束") or "",
                "交付时间": delivered_at,
                "最晚交付": row.get("最晚交付"),
                "保留截止": row.get("保留截止"),
                "可用副本": "是" if available else "否",
                "是否按期": "是" if on_time else ("否" if delivered_at else "未交付"),
                "时限承诺": f"{row.get('最晚交付')} 前可读，保留到 {row.get('保留截止')}",
                "派发说明": row.get("派发说明") or "",
            })

        reads = [self._read_view(read, moment) for read in self.read_rows()]
        actual_read_gb = sum(
            int(read.get("物理读取GB", 0) or 0) for read in self.read_rows()
            if parse_moment(read.get("读取开始")) <= moment)
        return {
            "as_of": format_moment(moment),
            "targets": targets,
            "reads": reads,
            "staging": self.staging_state(as_of),
            "staging_occupied_gb": self.occupied_gb(moment),
            "actual_read_gb": actual_read_gb,
            "target_received_gb": sum(item["实际取得GB"] for item in targets),
            "delivered_on_time": {
                item["回迁任务"]: item["是否按期"] for item in targets if item["交付时间"]
            },
            "note": "实际读取按物理读取记录汇总；目标侧取得量单独列出，不与源端读数相加。",
        }

    def _read_view(self, read: dict[str, Any], moment: datetime) -> dict[str, Any]:
        start = parse_moment(read.get("读取开始"))
        end = parse_moment(read.get("读取结束"))
        if moment < start:
            progress = "未开始"
        elif moment < end:
            progress = "读取中"
        else:
            progress = "已交付"
        if read.get("回收时间") and moment >= parse_moment(read.get("回收时间")):
            progress = "已回收"
        return {**read, "时点状态": progress}

    # ---------- 容量概览 ----------
    def overview(self, as_of: str | None = None) -> dict[str, Any]:
        moment = parse_moment(as_of)
        summary = self.capacity_summary()
        reads = self.read_rows()

        objects = []
        for item in summary["objects"]:
            object_reads = [
                read for read in reads
                if str(read.get("冷存对象")) == item["冷存对象"]
                and parse_moment(read.get("读取开始")) <= moment
            ]
            targets = []
            for row in self.capacity_rows():
                if str(row.get("冷存对象")) != item["冷存对象"]:
                    continue
                if row.get("读取编号") and parse_moment(row.get("交付时间")) <= moment:
                    targets.append({
                        "目标分区": row.get("目标分区"),
                        "回迁任务": row.get("回迁任务"),
                        "数据量GB": int(row.get("计划数据量GB", 0) or 0),
                        "数据去向": row.get("数据去向"),
                        "交付时间": row.get("交付时间"),
                        "最晚交付": row.get("最晚交付"),
                        "可用副本": row.get("可用副本"),
                    })
            objects.append({
                **item,
                "实际读取GB": sum(int(read.get("物理读取GB", 0) or 0) for read in object_reads),
                "读取次数": len(object_reads),
                "读取编号": [read.get("读取编号") for read in object_reads],
                "收到数据目标": targets,
            })

        return {
            "as_of": format_moment(moment),
            "objects": objects,
            "actual_read_gb": sum(item["实际读取GB"] for item in objects),
            "target_received_gb": sum(item["计划数据量GB"] for item in summary["objects"]),
            "buckets": self._buckets(moment),
            "staging": self.staging_state(as_of)["items"],
            "staging_occupied_gb": self.occupied_gb(moment),
            "note": summary["note"],
        }

    def _buckets(self, until: datetime) -> list[dict[str, Any]]:
        boundaries: set[datetime] = {
            parse_moment(f"{SCENARIO_DATE}T08:00"),
            parse_moment(f"{SCENARIO_DATE}T13:00"),
        }
        for read in self.read_rows():
            boundaries.add(parse_moment(read.get("读取开始")))
            boundaries.add(parse_moment(read.get("读取结束")))
            boundaries.add(parse_moment(self._effective_end(read)))
        for row in self.capacity_rows():
            boundaries.add(parse_moment(row.get("可开始时间")))
            boundaries.add(parse_moment(row.get("保留截止")))
            if row.get("交付时间"):
                boundaries.add(parse_moment(row.get("交付时间")))
        points = sorted(point for point in boundaries if point <= until)
        buckets = []
        for index in range(len(points) - 1):
            start, end = points[index], points[index + 1]
            started = [read for read in self.read_rows()
                       if start <= parse_moment(read.get("读取开始")) < end]
            delivered = [read for read in self.read_rows()
                         if start <= parse_moment(read.get("读取结束")) < end]
            checkpoints = {start}
            for read in self.read_rows():
                for key in ("暂存占用开始", "暂存占用结束", "回收时间"):
                    if read.get(key):
                        point = parse_moment(read.get(key))
                        if start <= point <= end:
                            checkpoints.add(point)
            peak = max((self.occupied_gb(point) for point in checkpoints), default=0)
            buckets.append({
                "时间段": f"{format_moment(start)}~{format_moment(end)}",
                "实际读取GB": sum(int(read.get("物理读取GB", 0) or 0) for read in started),
                "读取编号": [read.get("读取编号") for read in started],
                "交付GB": sum(int(read.get("物理读取GB", 0) or 0) for read in delivered),
                "暂存峰值占用GB": peak,
            })
        return buckets

    # ---------- 读量-时限权衡 ----------
    def tradeoff(self) -> dict[str, Any]:
        shared = str(SCENARIO_COLD_OBJECT)
        rows = [row for row in self.capacity_rows() if str(row.get("冷存对象")) == shared]
        if len(rows) < 2:
            return {"available": False, "message": "当前没有同一冷存对象上的多个回迁目标"}
        object_gb = max(int(row.get("对象数据量GB", 0) or 0) for row in rows)
        reads = [read for read in self.read_rows()
                 if str(read.get("冷存对象")) == shared]
        late_row = max(rows, key=lambda row: parse_moment(row.get("可开始时间")))
        hold_end = str(late_row.get("保留截止"))
        temp_rows = [row for row in self.capacity_rows()
                     if str(row.get("冷存对象")) != shared
                     and str(row.get("优先级")) == "紧急"]
        temp_deadline = temp_rows[0].get("最晚交付") if temp_rows else ""
        chosen_gb = max(object_gb * len(rows),
                        sum(int(read.get("物理读取GB", 0) or 0) for read in reads))
        chosen_reads = max(len(rows), len(reads))
        return {
            "available": True,
            "chosen": {
                "策略": "各目标按自身窗口分别读取",
                "物理读取GB": chosen_gb,
                "读取次数": chosen_reads,
                "说明": "A 到期即释放暂存，B 在 12:00 单独读取；临时请求 10:30 可用暂存。",
            },
            "hold_for_late": {
                "策略": "保留 A 的暂存副本给 B 复用",
                "物理读取GB": object_gb,
                "读取次数": 1,
                "暂存占用": f"{rows[0].get('可开始时间')}~{hold_end}",
                "冲突": (
                    f"暂存位被占到 {hold_end}，临时请求最早只能在 {hold_end} 后开始，"
                    f"晚于 {temp_deadline} 的交付承诺" if temp_rows else
                    "暂存位会被长期占用"),
            },
            "conclusion": (
                "复用省一次 80GB 读取，但会把唯一暂存位占到 B 的保留截止，"
                "使 10:30 的临时请求错过 13:00 交付；按窗口分别读取则所有目标按期交付。"),
        }

    # ---------- 状态流转 / 登记 ----------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is None:
            return entry, missing
        defaults = {
            "对象数据量GB": entry.get("计划数据量GB") or 0,
            "暂存需求GB": entry.get("计划数据量GB") or 0,
            "可开始时间": f"{SCENARIO_DATE}T00:00",
            "最晚交付": f"{SCENARIO_DATE}T23:59",
            "保留截止": f"{SCENARIO_DATE}T23:59",
            "读取耗时分钟": 60,
            "读取编号": "",
            "实际取得GB": 0,
            "数据去向": "",
            "可用副本": "否",
            "暂存占用开始": "",
            "暂存占用结束": "",
            "交付时间": "",
            "回收时间": "",
            "派发说明": "",
        }
        for key, value in defaults.items():
            entry.setdefault(key, value)
        entry["任务状态"] = entry.get("status", "待派发")
        return entry, []

    def reset_scenario(self) -> dict[str, Any]:
        """把容量明细、读取记录、暂存位和演示回迁任务恢复到初始窗口。"""
        store.replace_rows("prefetch_capacity", copy.deepcopy(SCENARIO_CAPACITY_ROWS))
        store.replace_rows("prefetch_staging", copy.deepcopy(SCENARIO_STAGING_ROWS))
        store.replace_rows("prefetch_read", [])
        hot_rows = store.rows("hot_prefetch")
        scenario_ids = {str(row.get("任务编号")) for row in SCENARIO_HOT_TASKS}
        hot_rows[:] = [row for row in hot_rows
                       if str(row.get("任务编号")) not in scenario_ids]
        hot_rows.extend(copy.deepcopy(SCENARIO_HOT_TASKS))
        return {"ok": True, "message": "回迁容量演示场景已重置"}

    def _write_audit(self, action: str, target: str, detail: str) -> None:
        rows = store.rows("audit_log")
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        rows.append({
            "id": next_id,
            "status": "已记录",
            "pending": False,
            "abnormal": False,
            "记录编号": f"AUDI-{next_id:04d}",
            "操作主体": "冷存储管理",
            "操作类型": action,
            "操作对象": target,
            "操作时间": SCENARIO_DATE,
            "影响范围": detail,
            "审计摘要": detail,
            "归档状态": "已记录",
        })


def read_has_released(read: dict[str, Any] | None) -> bool:
    return bool(read and read.get("回收时间"))


class PrefetchStagingService(BaseModuleService):
    MODULE = "prefetch_staging"
    ENTITY = "回迁暂存位"
    LIST_FIELDS = STAGING_FIELDS
    REQUIRED_FIELDS = ["暂存位置", "容量GB"]
    STATUS_ORDER = ["可用", "占用中", "维护中"]
    ACTION_RULES: dict[str, str] = {}


class PrefetchReadService(BaseModuleService):
    MODULE = "prefetch_read"
    ENTITY = "冷存读取记录"
    LIST_FIELDS = READ_FIELDS
    REQUIRED_FIELDS = ["读取编号", "冷存对象"]
    STATUS_ORDER = ["已派发", "已交付", "已回收"]
    ACTION_RULES: dict[str, str] = {}
