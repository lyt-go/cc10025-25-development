"""回迁容量演示场景：同一个 80GB 冷存对象 + 唯一 80GB 暂存位。

场景时间线（2026-11-22）：

    08:00  A 派发，读取同一份 80GB 冷存对象，09:00 读完，10:00 保留窗口结束
    10:30  临时请求开始，11:30 交付（承诺 13:00 前）
    12:00  B 的目标分区才可保留数据，12:00 开始读取，13:00 交付

A/B 指向同一个冷存对象。派发前容量视图按冷存对象聚合，计划量是 160GB，
但一次物理读取只有 80GB。若把 A 的读取一直留到 12:00 给 B 用，唯一暂存位
会被占住，10:30 的临时请求只能等待；因此调度器不提前预占暂存位，而是让 B
在 12:00 单独读取。这样交付时间全部满足，代价是源端实际读取变成两次 80GB。

这里的常量同时被 seed.py（首屏示例数据）和 PrefetchCapacityService.reset_scenario
（测试/演示重置）使用，避免两处场景时间线漂移。
"""
from __future__ import annotations

SCENARIO_DATE = "2026-11-22"
SCENARIO_COLD_OBJECT = "S3://glacier/obj-80g"
SCENARIO_TEMP_OBJECT = "S3://glacier/tmp-80g"
SCENARIO_STAGING = "staging://prefetch-slot-1"


def _at(hhmm: str) -> str:
    return f"{SCENARIO_DATE}T{hhmm}"


SCENARIO_STAGING_ROWS = [
    {
        "id": 1,
        "status": "可用",
        "pending": False,
        "abnormal": False,
        "暂存位置": SCENARIO_STAGING,
        "容量GB": 80,
        "当前占用GB": 0,
        "说明": "任务队列唯一 80GB 暂存位",
    }
]

SCENARIO_CAPACITY_ROWS = [
    {
        "id": 1,
        "status": "待派发",
        "pending": True,
        "abnormal": False,
        "明细编号": "PCAP-0001",
        "冷存对象": SCENARIO_COLD_OBJECT,
        "回迁任务": "HOT_-A",
        "目标分区": "ods_order_a",
        "计划数据量GB": 80,
        "对象数据量GB": 80,
        # A 在 10:00 前必须可读
        "可开始时间": _at("08:00"),
        "最晚交付": _at("10:00"),
        "保留截止": _at("10:00"),
        "暂存需求GB": 80,
        "读取耗时分钟": 60,
        "优先级": "高",
        "读取编号": "",
        "实际取得GB": 0,
        "数据去向": "",
        "可用副本": "否",
        "暂存占用开始": "",
        "暂存占用结束": "",
        "交付时间": "",
        "派发说明": "A：10:00 前可读，保留到 10:00",
    },
    {
        "id": 2,
        "status": "待派发",
        "pending": True,
        "abnormal": False,
        "明细编号": "PCAP-0002",
        "冷存对象": SCENARIO_COLD_OBJECT,
        "回迁任务": "HOT_-B",
        "目标分区": "dwd_order_b",
        # B 12:00 才有可保留数据的目标分区
        "计划数据量GB": 80,
        "对象数据量GB": 80,
        "可开始时间": _at("12:00"),
        "最晚交付": _at("14:00"),
        "保留截止": _at("18:00"),
        "暂存需求GB": 80,
        "读取耗时分钟": 60,
        "优先级": "高",
        "读取编号": "",
        "实际取得GB": 0,
        "数据去向": "",
        "可用副本": "否",
        "暂存占用开始": "",
        "暂存占用结束": "",
        "交付时间": "",
        "派发说明": "B：12:00 目标分区才可保留数据",
    },
    {
        "id": 3,
        "status": "待派发",
        "pending": True,
        "abnormal": False,
        "明细编号": "PCAP-0003",
        "冷存对象": SCENARIO_TEMP_OBJECT,
        "回迁任务": "HOT_-TEMP",
        "目标分区": "tmp_report_c",
        "计划数据量GB": 80,
        "对象数据量GB": 80,
        "可开始时间": _at("10:30"),
        "最晚交付": _at("13:00"),
        # 临时请求交付后立即消费，不长期占用暂存位
        "保留截止": _at("11:30"),
        "暂存需求GB": 80,
        "读取耗时分钟": 60,
        "优先级": "紧急",
        "读取编号": "",
        "实际取得GB": 0,
        "数据去向": "",
        "可用副本": "否",
        "暂存占用开始": "",
        "暂存占用结束": "",
        "交付时间": "",
        "派发说明": "临时请求：10:30 可开始，最晚 13:00 交付",
    },
]

SCENARIO_HOT_TASKS = [
    {
        "id": 4,
        "status": "排队中",
        "pending": True,
        "abnormal": False,
        "任务编号": "HOT_-A",
        "冷存位置": SCENARIO_COLD_OBJECT,
        "目标分区": "ods_order_a",
        "回迁量": "80GB",
        "预计耗时": "1小时",
        "优先级": "高",
        "过期策略": "2026-11-22T10:00",
        "任务状态": "排队中",
    },
    {
        "id": 5,
        "status": "排队中",
        "pending": True,
        "abnormal": False,
        "任务编号": "HOT_-B",
        "冷存位置": SCENARIO_COLD_OBJECT,
        "目标分区": "dwd_order_b",
        "回迁量": "80GB",
        "预计耗时": "1小时",
        "优先级": "高",
        "过期策略": "2026-11-22T18:00",
        "任务状态": "排队中",
    },
    {
        "id": 6,
        "status": "排队中",
        "pending": True,
        "abnormal": False,
        "任务编号": "HOT_-TEMP",
        "冷存位置": SCENARIO_TEMP_OBJECT,
        "目标分区": "tmp_report_c",
        "回迁量": "80GB",
        "预计耗时": "1小时",
        "优先级": "紧急",
        "过期策略": "2026-11-22T11:30",
        "任务状态": "排队中",
    },
]
