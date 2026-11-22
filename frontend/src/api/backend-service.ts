import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
  page = 1,
  size = 20,
): Promise<PageResult> {
  const query = new URLSearchParams({ ...filters, page: String(page), size: String(size) }).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---------- 回迁容量明细：聚合视图 / 派发 / 回收 ----------
export type CapacityObject = {
  冷存对象: string
  目标分区数: number
  计划数据量GB: number
  对象数据量GB: number
  目标分区: string[]
  实际读取GB?: number
  读取次数?: number
  读取编号?: string[]
  收到数据目标?: {
    目标分区: string
    回迁任务: string
    数据量GB: number
    数据去向: string
    交付时间: string
    最晚交付: string
    可用副本: string
  }[]
}

export type CapacitySummary = {
  objects: CapacityObject[]
  planned_target_gb: number
  planned_physical_gb: number
  note: string
}

export type CapacitySnapshot = {
  as_of: string
  targets: {
    明细编号: string
    回迁任务: string
    冷存对象: string
    目标分区: string
    计划数据量GB: number
    实际取得GB: number
    数据去向: string
    读取编号: string
    暂存占用开始: string
    暂存占用结束: string
    交付时间: string
    最晚交付: string
    保留截止: string
    可用副本: string
    是否按期: string
    时限承诺: string
    派发说明: string
  }[]
  reads: {
    id: number
    读取编号: string
    冷存对象: string
    物理读取GB: number
    读取开始: string
    读取结束: string
    暂存占用GB: number
    暂存占用开始: string
    暂存占用结束: string
    收到数据目标: string
    回收时间: string
    时点状态: string
  }[]
  staging_occupied_gb: number
  actual_read_gb: number
  target_received_gb: number
  delivered_on_time: Record<string, string>
  note: string
}

export type CapacityOverview = {
  as_of: string
  objects: CapacityObject[]
  actual_read_gb: number
  target_received_gb: number
  buckets: {
    时间段: string
    实际读取GB: number
    读取编号: string[]
    交付GB: number
    暂存峰值占用GB: number
  }[]
  staging: {
    暂存位置: string
    容量GB: number
    当前占用GB: number
    剩余GB: number
    当前占用读取: string[]
  }[]
  staging_occupied_gb: number
  note: string
}

export type CapacityTradeoff = {
  available: boolean
  message?: string
  chosen?: { 策略: string; 物理读取GB: number; 读取次数: number; 说明: string }
  hold_for_late?: {
    策略: string
    物理读取GB: number
    读取次数: number
    暂存占用: string
    冲突: string
  }
  conclusion?: string
}

export async function loadCapacitySummary(): Promise<CapacitySummary> {
  const response = await request('/api/prefetch_capacity/summary')
  if (!response.ok) throw new Error('回迁容量明细读取失败')
  return await response.json()
}

export async function loadCapacitySnapshot(asOf: string): Promise<CapacitySnapshot> {
  const response = await request(
    `/api/prefetch_capacity/snapshot?as_of=${encodeURIComponent(asOf)}`,
  )
  if (!response.ok) throw new Error('回迁容量核对失败')
  return await response.json()
}

export async function loadCapacityOverview(asOf: string): Promise<CapacityOverview> {
  const response = await request(
    `/api/prefetch_capacity/overview?as_of=${encodeURIComponent(asOf)}`,
  )
  if (!response.ok) throw new Error('回迁容量概览读取失败')
  return await response.json()
}

export async function loadCapacityTradeoff(): Promise<CapacityTradeoff> {
  const response = await request('/api/prefetch_capacity/tradeoff')
  if (!response.ok) throw new Error('读量与时限冲突说明读取失败')
  return await response.json()
}

export async function dispatchCapacity(
  capacityId: number,
  asOf: string,
  holdUntil?: string,
): Promise<ActionResult> {
  const response = await request('/api/prefetch_capacity/dispatch', {
    method: 'POST',
    body: JSON.stringify({ capacity_id: capacityId, as_of: asOf, hold_until: holdUntil || null }),
  })
  return await response.json()
}

export async function reclaimCapacity(asOf: string): Promise<ActionResult> {
  const response = await request('/api/prefetch_capacity/1/actions', {
    method: 'POST',
    body: JSON.stringify({ action: '回收占用', effective_at: asOf }),
  })
  return await response.json()
}

export async function resetCapacityScenario(): Promise<ActionResult> {
  const response = await request('/api/prefetch_capacity/1/actions', {
    method: 'POST',
    body: JSON.stringify({ action: '重置演示场景' }),
  })
  return await response.json()
}
