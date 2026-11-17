<template>
  <section class="page" data-module="audit">
    <header class="page-head">
      <div>
        <h2>数据审计管理</h2>
        <p class="page-desc">
          维护审计记录，围绕记录编号、操作类型、操作人、操作对象做登记、筛选与状态流转；
          旧版自由文本可按批次迁移为字段级差异，查询、导出与回滚共享同一迁移批次。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记审计记录</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出数据审计清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <section class="migration-panel" data-testid="migration-panel">
      <header class="panel-head">
        <div>
          <h3>审计文本迁移批次</h3>
          <p class="page-desc">
            归档输入为旧版审计文本与操作人记录归档明细；重复提交只保留一份有效批次，
            失败整批回滚，回滚后仍保留操作人与对象引用。
          </p>
        </div>
        <div class="page-actions">
          <button class="btn" data-testid="refresh-batches" type="button" @click="loadBatches">刷新批次</button>
          <button class="btn primary" data-testid="open-migrate" type="button" @click="openMigrate">迁移旧版审计文本</button>
        </div>
      </header>

      <p v-if="batchMessage" class="panel-message" data-testid="batch-message">{{ batchMessage }}</p>

      <table class="data-table batch-table">
        <thead>
          <tr>
            <th>批次编号</th>
            <th>批次名称</th>
            <th>批次状态</th>
            <th>目标/已迁移/跳过</th>
            <th>冲突策略</th>
            <th>创建时间</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="batch in batches"
            :key="batch.批次编号"
            :data-testid="`batch-row-${batch.批次编号}`"
            :data-batch-status="batch.批次状态"
          >
            <td>{{ batch.批次编号 }}</td>
            <td>{{ batch.批次名称 }}</td>
            <td>{{ batch.批次状态 }}</td>
            <td>
              {{ batch.迁移统计?.['目标记录数'] ?? 0 }} /
              {{ batch.迁移统计?.['已迁移'] ?? 0 }} /
              {{ batch.迁移统计?.['跳过'] ?? 0 }}
            </td>
            <td>{{ batch.冲突策略 }}</td>
            <td>{{ batch.创建时间 }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`view-batch-${batch.批次编号}`" @click="openBatch(batch)">
                查看旧文本与新差异
              </button>
              <button
                class="link"
                type="button"
                :disabled="batch.批次状态 !== '已迁移'"
                :data-testid="`rollback-batch-${batch.批次编号}`"
                @click="rollbackBatch(batch)"
              >
                回滚批次
              </button>
            </td>
          </tr>
          <tr v-if="!batches.length">
            <td colspan="7" class="empty-state">暂无迁移批次，可先迁移旧版审计文本</td>
          </tr>
        </tbody>
      </table>
    </section>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>迁移批次</span>
        <select v-model="batchFilter" data-testid="filter-batch">
          <option value="">全部批次</option>
          <option v-for="batch in batches" :key="batch.批次编号" :value="batch.批次编号">
            {{ batch.批次编号 }}
          </option>
        </select>
      </label>
      <label class="filter-item">
        <span>内容版本</span>
        <select v-model="versionFilter" data-testid="filter-version">
          <option value="">全部版本</option>
          <option value="旧版文本">旧版文本</option>
          <option value="版本化差异">版本化差异</option>
        </select>
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>内容版本</th>
          <th>差异摘要</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row['内容版本'] ?? '旧版文本' }}</td>
          <td class="diff-summary">{{ row['差异摘要'] || '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" :data-testid="`diff-${row.id}`" @click="openDiff(row)">
              查看差异
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无数据审计数据，可先登记审计记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据审计记录</span>
      <span v-if="errorMessage" class="error-text" data-testid="error-message">{{ errorMessage }}</span>
    </footer>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记审计记录</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <p class="page-desc">「变更内容」支持「字段: 旧值 -&gt; 新值」，登记后直接写入版本化差异。</p>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="showMigrate" class="modal-backdrop" data-testid="migrate-modal">
      <form class="modal" @submit.prevent="submitMigrate">
        <h3>迁移旧版审计文本</h3>
        <p class="page-desc">
          读取既有审计记录与操作人归档明细，把旧版文本拆成字段级差异；失败时整批回滚，
          批次归档输入仍会保留。
        </p>
        <label class="create-field">
          <span>批次名称</span>
          <input v-model="migrateForm.batch_label" data-testid="migrate-label" placeholder="例如：2026-11 首轮迁移" />
        </label>
        <label class="create-field">
          <span>迁移范围</span>
          <select v-model="migrateForm.scope" data-testid="migrate-scope">
            <option value="legacy">仅旧版文本记录</option>
            <option value="all">全部记录（已迁移的按冲突策略处理）</option>
            <option value="selected">指定记录编号</option>
          </select>
        </label>
        <label v-if="migrateForm.scope === 'selected'" class="create-field">
          <span>记录 id（逗号分隔）</span>
          <input v-model="migrateForm.record_ids" data-testid="migrate-record-ids" placeholder="例如：1,2" />
        </label>
        <label class="create-field">
          <span>冲突策略</span>
          <select v-model="migrateForm.conflict_policy" data-testid="migrate-conflict">
            <option value="skip">跳过已迁移记录，保留既有差异</option>
            <option value="fail">存在已迁移记录时整批失败</option>
          </select>
        </label>
        <label class="create-field checkbox-field">
          <input v-model="migrateForm.strict" data-testid="migrate-strict" type="checkbox" />
          <span>严格模式：无法解析为字段级差异时整批回滚</span>
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showMigrate = false">取消</button>
          <button class="btn primary" data-testid="submit-migrate" type="submit" :disabled="migrating">
            {{ migrating ? '迁移中…' : '开始迁移' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="batchDetail" class="modal-backdrop" data-testid="batch-detail-modal">
      <div class="modal wide">
        <header class="panel-head">
          <h3>{{ batchDetail.batch.批次编号 }} · {{ batchDetail.batch.批次名称 }}</h3>
          <div class="page-actions">
            <button class="btn" type="button" @click="batchDetail = null">关闭</button>
            <button
              class="btn primary"
              type="button"
              data-testid="detail-rollback"
              :disabled="batchDetail.batch.批次状态 !== '已迁移'"
              @click="rollbackBatch(batchDetail.batch)"
            >
              回滚到旧记录
            </button>
          </div>
        </header>
        <dl class="batch-meta">
          <div><dt>批次状态</dt><dd>{{ batchDetail.batch.批次状态 }}</dd></div>
          <div><dt>提交人</dt><dd>{{ batchDetail.batch.提交人 }}</dd></div>
          <div><dt>创建时间</dt><dd>{{ batchDetail.batch.创建时间 }}</dd></div>
          <div><dt>冲突策略</dt><dd>{{ batchDetail.batch.冲突策略 }}</dd></div>
          <div><dt>回滚入口</dt><dd>{{ batchDetail.batch.回滚入口 }}</dd></div>
          <div><dt>数据库依赖</dt><dd>{{ (batchDetail.batch.数据库依赖 || []).join('、') }}</dd></div>
        </dl>
        <p v-if="batchDetail.batch.失败原因" class="error-text">失败原因：{{ batchDetail.batch.失败原因 }}</p>
        <p v-if="batchDetail.batch.回滚时间" class="page-desc">
          已于 {{ batchDetail.batch.回滚时间 }} 回滚 {{ batchDetail.batch.回滚记录数 }} 条记录
        </p>

        <h4>归档输入与加工流水线</h4>
        <div class="pipeline-row">
          <span
            v-for="step in batchDetail.batch.构建流水线"
            :key="step.步骤"
            class="pipeline-step"
            :data-step-status="step.状态"
          >
            {{ step.步骤 }} · {{ step.状态 }}
          </span>
        </div>

        <h4>旧文本与新差异对照</h4>
        <table class="data-table">
          <thead>
            <tr>
              <th>记录编号</th>
              <th>旧版文本</th>
              <th>新差异</th>
              <th>操作人</th>
              <th>操作对象</th>
              <th>核对结论</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in batchDetail.items" :key="item.审计记录id" :data-testid="`batch-item-${item.审计记录id}`">
              <td>{{ item.记录编号 }}</td>
              <td class="old-text">{{ item.旧版文本 }}</td>
              <td>
                <span v-for="change in item.变更差异" :key="change.field" class="diff-chip">
                  {{ change.field }}：{{ change.before || '∅' }} → {{ change.after }}
                </span>
                <span v-if="!item.变更差异?.length">—</span>
              </td>
              <td>{{ item.操作人 || '—' }}</td>
              <td>{{ item.操作对象 || '—' }}</td>
              <td :class="item.校验?.ok ? 'check-ok' : 'check-bad'">
                {{ item.校验?.ok ? '通过' : (item.校验?.问题 || []).join('；') }}
              </td>
            </tr>
            <tr v-if="!batchDetail.items.length">
              <td colspan="6" class="empty-state">该批次没有归档输入</td>
            </tr>
          </tbody>
        </table>

        <h4>冲突明细（记录级 / 批次级）</h4>
        <ul class="conflict-list">
          <li v-for="(conflict, index) in batchDetail.batch.冲突明细" :key="index">
            [{{ conflict.范围 }}] {{ conflict.记录编号 || '整体' }} · {{ conflict.类型 }}：{{ conflict.说明 }}
          </li>
          <li v-if="!batchDetail.batch.冲突明细?.length">无冲突</li>
        </ul>
      </div>
    </div>

    <div v-if="diffDetail" class="modal-backdrop" data-testid="diff-modal">
      <div class="modal">
        <header class="panel-head">
          <h3>{{ diffDetail.entry.记录编号 }} · 差异明细</h3>
          <button class="btn" type="button" @click="diffDetail = null">关闭</button>
        </header>
        <p class="page-desc">
          内容版本：{{ diffDetail.内容版本 }} · 迁移批次：{{ diffDetail.迁移批次?.批次编号 || '实时写入' }}
        </p>
        <dl class="batch-meta">
          <div><dt>操作人</dt><dd>{{ diffDetail.entry.操作人 }}</dd></div>
          <div><dt>操作对象</dt><dd>{{ diffDetail.entry.操作对象 }}</dd></div>
          <div><dt>操作时间</dt><dd>{{ diffDetail.entry.操作时间 }}</dd></div>
          <div>
            <dt>核对结论</dt>
            <dd :class="diffDetail.校验?.ok ? 'check-ok' : 'check-bad'">
              {{ diffDetail.校验?.ok ? '通过' : (diffDetail.校验?.问题 || []).join('；') }}
            </dd>
          </div>
        </dl>
        <h4>旧版文本（来源依据）</h4>
        <pre class="old-text">{{ diffDetail.旧版文本 || '—' }}</pre>
        <p v-if="diffDetail.待迁移" class="page-desc" data-testid="diff-pending">
          该记录尚未迁移，下方仅为解析预览；迁移成功后才会写入批次并可被列表与导出消费。
        </p>
        <h4>字段级差异</h4>
        <table class="data-table">
          <thead><tr><th>字段</th><th>变更前</th><th>变更后</th></tr></thead>
          <tbody>
            <tr v-for="change in diffDetail.变更差异" :key="change.field">
              <td>{{ change.field }}</td>
              <td>{{ change.before || '∅' }}</td>
              <td>{{ change.after }}</td>
            </tr>
            <tr v-for="change in diffDetail.待迁移 ? diffDetail.预览差异 : []" :key="`preview-${change.field}`">
              <td>{{ change.field }}（预览）</td>
              <td>{{ change.before || '∅' }}</td>
              <td>{{ change.after }}</td>
            </tr>
          </tbody>
        </table>
        <h4>核对清单</h4>
        <ul class="check-list">
          <li v-for="check in diffDetail.校验?.检查项 || []" :key="check.检查项" :class="check.结果 === '通过' ? 'check-ok' : 'check-bad'">
            {{ check.检查项 }}：{{ check.结果 }}（{{ check.说明 }}）
          </li>
        </ul>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = {
  id: number
  [key: string]: string | number | null | undefined | Record<string, unknown>[] | Record<string, unknown>
}
type Change = { field: string; before: string; after: string }
type Batch = Record<string, any>
type BatchDetail = { batch: Batch; items: Record<string, any>[]; total: number }
type DiffDetail = Record<string, any>

const ENDPOINT = '/api/audit'
const BATCH_ENDPOINT = '/api/audit/migration-batches'
const columns = ["记录编号", "操作类型", "操作人", "操作对象", "操作时间", "变更内容", "审计结果", "记录状态"]
const actions = ["提交审计", "标记异常"]
const statuses = ["已记录", "已审计", "异常"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const batchFilter = ref('')
const versionFilter = ref('')
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const batches = ref<Batch[]>([])
const batchMessage = ref('')
const showMigrate = ref(false)
const migrating = ref(false)
const migrateForm = ref({
  batch_label: '', scope: 'legacy', record_ids: '', conflict_policy: 'skip', strict: false,
})
const batchDetail = ref<BatchDetail | null>(null)
const diffDetail = ref<DiffDetail | null>(null)

const stats = computed(() => [
  { label: '审计记录数', value: total.value },
  { label: '已审计记录数', value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: '异常记录数', value: rows.value.filter((row) => row.status === statuses[2]).length },
  { label: '版本化差异记录', value: rows.value.filter((row) => row['内容版本'] === '版本化差异').length },
])

function resetFilters() {
  filters.value = {}
  batchFilter.value = ''
  versionFilter.value = ''
  void reload()
}

function queryString() {
  const params = new URLSearchParams(filters.value)
  if (batchFilter.value) params.set('batch_id', batchFilter.value)
  if (versionFilter.value) params.set('content_version', versionFilter.value)
  return params.toString()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/export?${queryString()}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'audit.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('数据审计动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据审计操作失败'
  }
}

async function loadBatches() {
  try {
    const response = await request(BATCH_ENDPOINT)
    if (!response.ok) throw new Error('迁移批次读取失败')
    const payload = await response.json()
    batches.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移批次读取失败'
  }
}

function openMigrate() {
  migrateForm.value = { batch_label: '', scope: 'legacy', record_ids: '', conflict_policy: 'skip', strict: false }
  batchMessage.value = ''
  showMigrate.value = true
}

async function submitMigrate() {
  migrating.value = true
  batchMessage.value = ''
  try {
    const recordIds = migrateForm.value.record_ids
      .split(/[,，\s]+/)
      .filter(Boolean)
      .map((item) => Number(item))
      .filter((item) => Number.isInteger(item))
    const response = await request(BATCH_ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({
        batch_label: migrateForm.value.batch_label,
        scope: migrateForm.value.scope,
        record_ids: recordIds,
        conflict_policy: migrateForm.value.conflict_policy,
        strict: migrateForm.value.strict,
      }),
    })
    const payload = await response.json()
    batchMessage.value = payload.message || ''
    if (!response.ok || !payload.ok) throw new Error(payload.message || '迁移失败')
    showMigrate.value = false
    await loadBatches()
    await reload()
  } catch (error) {
    batchMessage.value = error instanceof Error ? error.message : '迁移失败'
  } finally {
    migrating.value = false
  }
}

async function openBatch(batch: Batch) {
  try {
    const response = await request(`${BATCH_ENDPOINT}/${encodeURIComponent(batch.批次编号)}/records`)
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '批次明细读取失败')
    batchDetail.value = payload
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批次明细读取失败'
  }
}

async function rollbackBatch(batch: Batch) {
  batchMessage.value = ''
  try {
    const response = await request(`${BATCH_ENDPOINT}/${encodeURIComponent(batch.批次编号)}/rollback`, {
      method: 'POST',
    })
    const payload = await response.json()
    batchMessage.value = payload.message || ''
    if (!response.ok || !payload.ok) throw new Error(payload.message || '回滚失败')
    batchDetail.value = null
    await loadBatches()
    await reload()
  } catch (error) {
    batchMessage.value = error instanceof Error ? error.message : '回滚失败'
  }
}

async function openDiff(row: Row) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}/diff`)
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '差异读取失败')
    diffDetail.value = payload
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '差异读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}?${queryString()}`)
    if (!response.ok) {
      throw new Error('审计记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据审计列表读取失败'
  }
}

onMounted(async () => {
  await loadBatches()
  await reload()
})
</script>

<style scoped>
.migration-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 14px;
}
.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}
.panel-head h3 { margin: 0; font-size: 15px; }
.panel-message {
  margin: 8px 0;
  font-size: 13px;
  color: var(--brand);
  background: #eef4ff;
  border-radius: 6px;
  padding: 6px 10px;
}
.batch-table { margin-top: 8px; }
.batch-table .link:disabled { color: var(--muted); cursor: not-allowed; }
.filter-item select {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 13px;
  background: #fff;
}
.diff-summary {
  max-width: 320px;
  color: var(--muted);
  font-size: 12px;
}
.modal.wide { width: min(1080px, 100%); }
.batch-meta {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px 16px;
  margin: 12px 0;
}
.batch-meta dt { font-size: 12px; color: var(--muted); }
.batch-meta dd { margin: 2px 0 0; font-size: 13px; word-break: break-all; }
.pipeline-row { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 16px; }
.pipeline-step {
  font-size: 12px;
  padding: 4px 8px;
  border-radius: 999px;
  background: #eef4ff;
  color: var(--brand);
}
.pipeline-step[data-step-status='失败'] { background: #fee4e2; color: #b42318; }
.pipeline-step[data-step-status='已回滚'] { background: #f1f5f9; color: var(--muted); }
.diff-chip {
  display: inline-block;
  margin: 2px 6px 2px 0;
  padding: 2px 6px;
  border-radius: 4px;
  background: #f1f5f9;
  font-size: 12px;
}
.old-text { white-space: pre-wrap; word-break: break-all; color: #475569; font-size: 12px; margin: 4px 0 12px; }
.check-ok { color: #15803d; }
.check-bad { color: #b42318; }
.check-list { font-size: 13px; padding-left: 18px; }
.conflict-list { font-size: 13px; color: var(--muted); padding-left: 18px; }
.checkbox-field { display: flex; align-items: center; gap: 8px; }
.checkbox-field input { width: auto; }
.checkbox-field span { margin: 0; }
</style>
