<template>
  <section class="page" data-module="prefetch_capacity">
    <header class="page-head">
      <div>
        <h2>回迁容量明细</h2>
        <p class="page-desc">
          派发前把待回迁分区的计划数据量按冷存对象聚合，核对同一次物理读取会被哪些目标分区共用；
          派发后记录真实读量、暂存占用区间和收到数据的目标。
        </p>
      </div>
      <div class="page-actions">
        <label class="inline-field">
          <span>核对时点</span>
          <input v-model="asOf" data-testid="capacity-as-of" placeholder="2026-11-22T13:00" />
        </label>
        <button class="btn" data-testid="capacity-refresh" type="button" @click="reload">
          刷新
        </button>
        <button class="btn" data-testid="capacity-reclaim" type="button" @click="reclaim">
          回收到期占用
        </button>
        <button class="btn ghost" data-testid="capacity-reset" type="button" @click="resetScenario">
          重置演示场景
        </button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
    <p v-if="message" class="ok-text" data-testid="capacity-message">{{ message }}</p>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">计划数据量（按目标分区求和）</span>
        <strong class="stat-value" data-testid="planned-target-gb">
          {{ summary?.planned_target_gb ?? 0 }}GB
        </strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">对象去重物理读取量</span>
        <strong class="stat-value" data-testid="planned-physical-gb">
          {{ summary?.planned_physical_gb ?? 0 }}GB
        </strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">截至时点实际读取</span>
        <strong class="stat-value" data-testid="actual-read-gb">
          {{ snapshot?.actual_read_gb ?? 0 }}GB
        </strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">暂存占用</span>
        <strong class="stat-value" data-testid="staging-occupied-gb">
          {{ snapshot?.staging_occupied_gb ?? 0 }}GB / {{ stagingCapacityGB }}GB
        </strong>
      </article>
    </div>

    <h3 class="section-title">按冷存对象聚合</h3>
    <table class="data-table" data-testid="capacity-summary">
      <thead>
        <tr>
          <th>冷存对象</th>
          <th>目标分区数</th>
          <th>目标分区</th>
          <th>计划数据量GB</th>
          <th>对象数据量GB</th>
          <th>实际读取GB</th>
          <th>读取次数</th>
          <th>收到数据目标</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in overview?.objects ?? []" :key="item.冷存对象" :data-testid="`object-${item.冷存对象}`">
          <td>{{ item.冷存对象 }}</td>
          <td>{{ item.目标分区数 }}</td>
          <td>{{ item.目标分区.join('、') }}</td>
          <td>{{ item.计划数据量GB }}</td>
          <td>{{ item.对象数据量GB }}</td>
          <td>{{ item.实际读取GB ?? 0 }}</td>
          <td>{{ item.读取次数 ?? 0 }}</td>
          <td>
            <span v-for="target in item.收到数据目标 ?? []" :key="target.目标分区" class="pill">
              {{ target.目标分区 }}（{{ target.数据量GB }}GB，{{ target.交付时间 || '未交付' }}）
            </span>
            <span v-if="!(item.收到数据目标 ?? []).length">—</span>
          </td>
        </tr>
        <tr v-if="!(overview?.objects ?? []).length">
          <td colspan="8" class="empty-state">暂无回迁容量明细</td>
        </tr>
      </tbody>
    </table>
    <p class="hint">{{ summary?.note }}</p>

    <h3 class="section-title">读量与交付时限</h3>
    <div v-if="tradeoff?.available" class="tradeoff-grid" data-testid="capacity-tradeoff">
      <article class="tradeoff-card chosen">
        <h4>{{ tradeoff.chosen?.策略 }}</h4>
        <p>物理读取 {{ tradeoff.chosen?.物理读取GB }}GB / {{ tradeoff.chosen?.读取次数 }} 次</p>
        <p class="hint">{{ tradeoff.chosen?.说明 }}</p>
      </article>
      <article class="tradeoff-card rejected">
        <h4>{{ tradeoff.hold_for_late?.策略 }}</h4>
        <p>物理读取 {{ tradeoff.hold_for_late?.物理读取GB }}GB / {{ tradeoff.hold_for_late?.读取次数 }} 次</p>
        <p class="hint">暂存占用 {{ tradeoff.hold_for_late?.暂存占用 }}</p>
        <p class="error-text">{{ tradeoff.hold_for_late?.冲突 }}</p>
      </article>
    </div>
    <p v-if="tradeoff?.conclusion" class="hint">{{ tradeoff.conclusion }}</p>

    <h3 class="section-title">容量明细与派发</h3>
    <table class="data-table" data-testid="capacity-rows">
      <thead>
        <tr>
          <th>明细编号</th>
          <th>回迁任务</th>
          <th>目标分区</th>
          <th>计划GB</th>
          <th>可开始</th>
          <th>最晚交付</th>
          <th>保留截止</th>
          <th>读取编号</th>
          <th>实际取得GB</th>
          <th>数据去向</th>
          <th>可用副本</th>
          <th>派发</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`capacity-row-${row.明细编号}`">
          <td>{{ row.明细编号 }}</td>
          <td>{{ row.回迁任务 }}</td>
          <td>{{ row.目标分区 }}</td>
          <td>{{ row.计划数据量GB }}</td>
          <td>{{ row.可开始时间 }}</td>
          <td>{{ row.最晚交付 }}</td>
          <td>{{ row.保留截止 }}</td>
          <td>{{ row.读取编号 || '—' }}</td>
          <td>{{ row.实际取得GB ?? 0 }}</td>
          <td>{{ row.数据去向 || '—' }}</td>
          <td>{{ row.可用副本 }}</td>
          <td class="row-actions">
            <input
              v-model="dispatchAt[String(row.id)]"
              class="mini-input"
              :data-testid="`dispatch-at-${row.明细编号}`"
              :placeholder="String(row.可开始时间 ?? '')"
            />
            <button
              class="link"
              type="button"
              :data-testid="`dispatch-${row.明细编号}`"
              @click="dispatch(row)"
            >
              派发读取
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td colspan="12" class="empty-state">暂无回迁容量明细</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">时点核对：{{ snapshot?.as_of ?? asOf }}</h3>
    <table class="data-table" data-testid="capacity-snapshot">
      <thead>
        <tr>
          <th>回迁任务</th>
          <th>目标分区</th>
          <th>实际取得GB</th>
          <th>数据去向</th>
          <th>读取编号</th>
          <th>暂存占用区间</th>
          <th>交付时间</th>
          <th>是否按期</th>
          <th>可用副本</th>
          <th>时限承诺</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="target in snapshot?.targets ?? []" :key="target.明细编号" :data-testid="`snapshot-${target.回迁任务}`">
          <td>{{ target.回迁任务 }}</td>
          <td>{{ target.目标分区 }}</td>
          <td>{{ target.实际取得GB }}</td>
          <td>{{ target.数据去向 || '—' }}</td>
          <td>{{ target.读取编号 || '—' }}</td>
          <td>
            {{ target.暂存占用开始 ? `${short(target.暂存占用开始)}~${short(target.暂存占用结束)}` : '—' }}
          </td>
          <td>{{ target.交付时间 || '—' }}</td>
          <td>{{ target.是否按期 }}</td>
          <td>{{ target.可用副本 }}</td>
          <td class="hint">{{ target.时限承诺 }}</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">物理读取记录</h3>
    <table class="data-table" data-testid="capacity-reads">
      <thead>
        <tr>
          <th>读取编号</th>
          <th>冷存对象</th>
          <th>物理读取GB</th>
          <th>读取区间</th>
          <th>暂存占用区间</th>
          <th>收到数据目标</th>
          <th>时点状态</th>
          <th>回收时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="read in snapshot?.reads ?? []" :key="read.读取编号">
          <td>{{ read.读取编号 }}</td>
          <td>{{ read.冷存对象 }}</td>
          <td>{{ read.物理读取GB }}</td>
          <td>{{ short(read.读取开始) }}~{{ short(read.读取结束) }}</td>
          <td>{{ short(read.暂存占用开始) }}~{{ short(read.暂存占用结束) }}</td>
          <td>{{ read.收到数据目标 }}</td>
          <td>{{ read.时点状态 }}</td>
          <td>{{ read.回收时间 || '—' }}</td>
        </tr>
        <tr v-if="!(snapshot?.reads ?? []).length">
          <td colspan="8" class="empty-state">尚未派发任何冷存读取</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">容量概览：各时间段实际读取与占用</h3>
    <table class="data-table" data-testid="capacity-overview">
      <thead>
        <tr>
          <th>时间段</th>
          <th>实际读取GB</th>
          <th>读取编号</th>
          <th>交付GB</th>
          <th>暂存峰值占用GB</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="bucket in overview?.buckets ?? []" :key="bucket.时间段">
          <td>{{ bucket.时间段 }}</td>
          <td>{{ bucket.实际读取GB }}</td>
          <td>{{ bucket.读取编号.join('、') || '—' }}</td>
          <td>{{ bucket.交付GB }}</td>
          <td>{{ bucket.暂存峰值占用GB }}</td>
        </tr>
      </tbody>
    </table>
    <p class="hint">
      暂存位：
      <span v-for="item in overview?.staging ?? []" :key="item.暂存位置">
        {{ item.暂存位置 }} 容量 {{ item.容量GB }}GB，占用 {{ item.当前占用GB }}GB，剩余 {{ item.剩余GB }}GB。
      </span>
    </p>
    <p class="hint">{{ overview?.note }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  dispatchCapacity,
  listEntries,
  loadCapacityOverview,
  loadCapacitySnapshot,
  loadCapacitySummary,
  loadCapacityTradeoff,
  reclaimCapacity,
  resetCapacityScenario,
} from '@/api/backend-service'
import type {
  CapacityOverview,
  CapacitySnapshot,
  CapacitySummary,
  CapacityTradeoff,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const asOf = ref('2026-11-22T13:00')
const rows = ref<EntryRow[]>([])
const summary = ref<CapacitySummary | null>(null)
const overview = ref<CapacityOverview | null>(null)
const snapshot = ref<CapacitySnapshot | null>(null)
const tradeoff = ref<CapacityTradeoff | null>(null)
const dispatchAt = ref<Record<string, string>>({})
const errorMessage = ref('')
const message = ref('')

const stagingCapacityGB = computed(() => {
  const items = overview.value?.staging ?? []
  return items.reduce((total, item) => total + item.容量GB, 0)
})

function short(value: string | undefined): string {
  return value ? value.replace('2026-11-22T', '') : ''
}

async function reload() {
  errorMessage.value = ''
  try {
    const [capacityPayload, summaryPayload, overviewPayload, snapshotPayload, tradeoffPayload] =
      await Promise.all([
        listEntries('prefetch_capacity', {}, 1, 50),
        loadCapacitySummary(),
        loadCapacityOverview(asOf.value),
        loadCapacitySnapshot(asOf.value),
        loadCapacityTradeoff(),
      ])
    rows.value = capacityPayload.items
    summary.value = summaryPayload
    overview.value = overviewPayload
    snapshot.value = snapshotPayload
    tradeoff.value = tradeoffPayload
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回迁容量明细读取失败'
  }
}

async function dispatch(row: EntryRow) {
  errorMessage.value = ''
  message.value = ''
  const at = dispatchAt.value[String(row.id)] || String(row.可开始时间)
  try {
    const result = await dispatchCapacity(Number(row.id), at)
    message.value = result.message
    if (!result.ok) errorMessage.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '派发冷存读取失败'
  }
}

async function reclaim() {
  errorMessage.value = ''
  message.value = ''
  try {
    const result = await reclaimCapacity(asOf.value)
    message.value = result.message
    if (!result.ok) errorMessage.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回收回迁占用失败'
  }
}

async function resetScenario() {
  errorMessage.value = ''
  message.value = ''
  try {
    const result = await resetCapacityScenario()
    message.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重置演示场景失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.section-title {
  margin: 20px 0 8px;
  font-size: 15px;
}
.inline-field {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--muted);
}
.inline-field input,
.mini-input {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 12px;
}
.mini-input {
  width: 132px;
}
.hint {
  color: var(--muted);
  font-size: 12px;
  margin: 6px 0;
}
.ok-text {
  color: #067647;
  font-size: 13px;
}
.pill {
  display: inline-block;
  margin-right: 6px;
  padding: 1px 8px;
  border-radius: 999px;
  background: #eef4ff;
  color: #1f4dbf;
  font-size: 12px;
}
.tradeoff-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
}
.tradeoff-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #fff;
  padding: 12px 14px;
}
.tradeoff-card h4 {
  margin: 0 0 8px;
  font-size: 14px;
}
.tradeoff-card.chosen {
  border-left: 4px solid #12b76a;
}
.tradeoff-card.rejected {
  border-left: 4px solid #f04438;
}
</style>
