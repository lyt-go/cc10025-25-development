"""回迁容量明细场景测试：80GB 冷存对象 + 唯一 80GB 暂存位。

覆盖：
  1. 派发前按冷存对象聚合计划量，区分目标侧计划量与源端物理量
  2. 10:00 / 10:30 / 12:00 / 13:00 四个时点的数据取得与暂存占用
  3. 临时请求是否在 13:00 承诺内交付
  4. 重放 B 的派发不重复累加实际读量
  5. A 到期回收后，B 的可用副本在 12:00 交付后仍可读
  6. 若把 A 的暂存留给 B，唯一暂存位冲突会让临时请求错过交付承诺
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from app.services import service_for

RED = "\033[91m"
GREEN = "\033[92m"
RESET = "\033[0m"

passed = 0
failed = 0


def check(desc, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  {GREEN}PASS{RESET} {desc}")
        passed += 1
        return True
    print(f"  {RED}FAIL{RESET} {desc} {detail}")
    failed += 1
    return False


def by_task(snapshot, task):
    for item in snapshot["targets"]:
        if item["回迁任务"] == task:
            return item
    return None


def test_scenario():
    capacity = service_for("prefetch_capacity")
    capacity.reset_scenario()

    print("\n=== 派发前：容量明细按冷存对象聚合 ===")
    summary = capacity.capacity_summary()
    shared = next(item for item in summary["objects"]
                  if item["冷存对象"] == "S3://glacier/obj-80g")
    check("同一对象聚合到两个目标分区", shared["目标分区数"] == 2)
    check("计划量按目标分区求和为 160GB", shared["计划数据量GB"] == 160)
    check("对象物理量为 80GB，不随目标数翻倍", shared["对象数据量GB"] == 80)
    check("全部对象去重物理量 160GB",
          summary["planned_physical_gb"] == 160, summary["planned_physical_gb"])

    print("\n=== 按窗口派发 ===")
    row, message = capacity.dispatch(1, "2026-11-22T08:00")
    check("A 在 08:00 派发成功", row is not None, message)
    row, message = capacity.dispatch(3, "2026-11-22T10:30")
    check("临时请求 10:30 派发成功", row is not None, message)
    row, message = capacity.dispatch(2, "2026-11-22T12:00")
    check("B 在 12:00 派发成功", row is not None, message)

    print("\n=== 10:00 时点 ===")
    snap = capacity.snapshot("2026-11-22T10:00")
    a = by_task(snap, "HOT_-A")
    b = by_task(snap, "HOT_-B")
    temp = by_task(snap, "HOT_-TEMP")
    check("A 实际取得 80GB", a["实际取得GB"] == 80, a)
    check("A 数据去向是 ods_order_a", a["数据去向"] == "ods_order_a")
    check("A 可用副本为是", a["可用副本"] == "是")
    check("A 在 10:00 承诺前交付", a["是否按期"] == "是")
    check("B 尚未取得数据", b["实际取得GB"] == 0)
    check("临时任务尚未开始", temp["实际取得GB"] == 0)
    check("10:00 暂存仍被 A 占满 80GB", snap["staging_occupied_gb"] == 80,
          snap["staging_occupied_gb"])
    check("10:00 实际读取为 80GB", snap["actual_read_gb"] == 80, snap["actual_read_gb"])

    print("\n=== 10:30 时点 ===")
    capacity.reclaim("2026-11-22T10:01")
    snap = capacity.snapshot("2026-11-22T10:30")
    a = by_task(snap, "HOT_-A")
    temp = by_task(snap, "HOT_-TEMP")
    check("A 到期回收后不可用", a["可用副本"] == "否")
    check("A 已取得的数据仍可追溯", a["实际取得GB"] == 80)
    check("临时任务 10:30 读取中", temp["读取编号"] and temp["实际取得GB"] == 0)
    check("暂存被临时请求占用 80GB", snap["staging_occupied_gb"] == 80)
    check("源端实际读取累计 160GB", snap["actual_read_gb"] == 160, snap["actual_read_gb"])

    print("\n=== 12:00 时点 ===")
    capacity.reclaim("2026-11-22T11:31")
    snap = capacity.snapshot("2026-11-22T12:00")
    b = by_task(snap, "HOT_-B")
    temp = by_task(snap, "HOT_-TEMP")
    check("B 在 12:00 读取中尚未交付", b["实际取得GB"] == 0)
    check("B 的暂存占用从 12:00 开始",
          b["暂存占用开始"] == "2026-11-22T12:00", b["暂存占用开始"])
    check("临时任务已取得 80GB", temp["实际取得GB"] == 80)
    check("临时任务在 11:30 交付，早于 13:00 承诺",
          temp["交付时间"] == "2026-11-22T11:30" and temp["是否按期"] == "是")
    check("12:00 暂存被 B 占用 80GB", snap["staging_occupied_gb"] == 80)
    check("源端实际读取累计 240GB", snap["actual_read_gb"] == 240, snap["actual_read_gb"])
    check("目标侧取得量与源端读数分开统计",
          snap["target_received_gb"] == 160, snap["target_received_gb"])

    print("\n=== 13:00 时点 ===")
    snap = capacity.snapshot("2026-11-22T13:00")
    b = by_task(snap, "HOT_-B")
    check("B 实际取得 80GB", b["实际取得GB"] == 80)
    check("B 数据去向是 dwd_order_b", b["数据去向"] == "dwd_order_b")
    check("B 在 14:00 前交付", b["交付时间"] == "2026-11-22T13:00" and b["是否按期"] == "是")
    check("B 可用副本为是", b["可用副本"] == "是")
    check("13:00 暂存仍为 B 保留 80GB", snap["staging_occupied_gb"] == 80)

    print("\n=== 重放 B 的派发 ===")
    reads_before = len(capacity.read_rows())
    row, message = capacity.dispatch(2, "2026-11-22T12:00")
    check("重放命中已有读取", "复用" in message or "不重复" in message, message)
    check("重放未新增物理读取", len(capacity.read_rows()) == reads_before)
    snap = capacity.snapshot("2026-11-22T13:00")
    check("重放后实际读量仍为 240GB", snap["actual_read_gb"] == 240)
    check("重放后 B 的读取编号不变", by_task(snap, "HOT_-B")["读取编号"] == "READ-0003")

    print("\n=== A 到期后检查 B 的可用副本 ===")
    capacity.reclaim("2026-11-22T10:01")
    snap = capacity.snapshot("2026-11-22T13:00")
    check("回收 A 不影响 B 的可用副本", by_task(snap, "HOT_-B")["可用副本"] == "是")
    check("A 的可用副本保持回收状态", by_task(snap, "HOT_-A")["可用副本"] == "否")

    print("\n=== 容量概览：各时间段实际读取与占用 ===")
    overview = capacity.overview("2026-11-22T13:00")
    buckets = {item["时间段"].split("~")[0][-5:]: item for item in overview["buckets"]}
    check("08:00 段读取 80GB", buckets["08:00"]["实际读取GB"] == 80)
    check("10:30 段读取 80GB", buckets["10:30"]["实际读取GB"] == 80)
    check("12:00 段读取 80GB", buckets["12:00"]["实际读取GB"] == 80)
    check("10:00~10:30 段暂存峰值为 0",
          buckets["10:00"]["暂存峰值占用GB"] == 0, buckets.get("10:00"))
    shared_object = next(item for item in overview["objects"]
                         if item["冷存对象"] == "S3://glacier/obj-80g")
    check("共享对象实际读取累计 160GB，未按目标重复叠加",
          shared_object["实际读取GB"] == 160, shared_object["实际读取GB"])
    check("共享对象两次读取分别有编号",
          shared_object["读取编号"] == ["READ-0001", "READ-0003"],
          shared_object["读取编号"])

    print("\n=== 冲突对照：把 A 的暂存留给 B ===")
    capacity.reset_scenario()
    capacity.dispatch(1, "2026-11-22T08:00", hold_until="2026-11-22T18:00")
    row, message = capacity.dispatch(3, "2026-11-22T10:30")
    check("保留到 18:00 时临时请求无法派发", row is None, message)
    check("冲突提示指向唯一暂存位", "暂存位" in message, message)
    tradeoff = capacity.tradeoff()
    check("对照视图给出两种策略读量",
          tradeoff["chosen"]["物理读取GB"] == 160
          and tradeoff["hold_for_late"]["物理读取GB"] == 80)
    check("对照视图说明交付承诺冲突", "13:00" in tradeoff["hold_for_late"]["冲突"])

    capacity.reset_scenario()


if __name__ == "__main__":
    print("=" * 60)
    print("回迁容量明细场景测试")
    print("=" * 60)
    test_scenario()
    print(f"\n{'=' * 60}")
    print(f"结果: {GREEN}{passed} 通过{RESET} / {RED}{failed} 失败{RESET} / "
          f"{passed + failed} 总计")
    print("=" * 60)
    if failed:
        sys.exit(1)
