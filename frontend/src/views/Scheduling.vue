<template>
  <div>
    <div class="panel">
      <h2>🧭 周三检验班排程（P-8 / P-9 放行）</h2>
      <p class="muted small">
        绝缘仪 09:00-10:20、热像舱 09:00-10:40；T1 具绝缘+热像资质（至 10:45），T2 仅热像（至 10:30）。
        P-8 需绝缘 30 分 + 热像 20 分，P-9 需绝缘 20 分 + 热像 25 分（含换包准备）。
        每条结果都绑定包序列号、步骤、设备校准区间、操作者资质与前置结果。
      </p>
      <div class="row">
        <button @click="runCompare" :disabled="loading">{{ loading ? '计算中…' : '运行两种排法对比' }}</button>
        <button class="secondary" @click="runReplay" :disabled="loading">回放异常场景（失败/校准/取消/迟到回执）</button>
      </div>
      <div v-if="error" class="msg err" style="margin-top:12px">{{ error }}</div>
      <div v-if="msg" class="msg ok" style="margin-top:12px">{{ msg }}</div>
    </div>

    <template v-if="compare">
      <div class="grid cols-2">
        <div class="panel" v-for="plan in compare.plans" :key="plan.id">
          <h2>{{ plan.label }}</h2>
          <div class="kv">
            <span class="k">完成时刻</span><span><b>{{ plan.metrics.completion_at }}</b>（班次起 +{{ plan.metrics.completion_minutes_from_shift_start }} 分）</span>
            <span class="k">交接次数</span><span><span class="tag" :class="plan.metrics.handoff_count ? 'amber' : 'green'">{{ plan.metrics.handoff_count }} 次</span></span>
            <span class="k">各包完成</span><span>P-8 {{ plan.metrics.per_pack_completion['P-8'] }} / P-9 {{ plan.metrics.per_pack_completion['P-9'] }}</span>
          </div>
          <h3>步骤排程</h3>
          <table>
            <thead><tr><th>包</th><th>工序</th><th>设备</th><th>检验员</th><th>计划区间</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="s in plan.steps.filter(x => x.attempt === 1)" :key="s.id">
                <td class="mono">{{ s.pack_serial }}</td>
                <td>{{ kindName(s.kind) }}</td>
                <td class="small">{{ s.station_no }}</td>
                <td>{{ s.inspector_code }}</td>
                <td class="mono small">{{ s.planned_start_hhmm }}-{{ s.planned_end_hhmm }}</td>
                <td><span class="tag" :class="stepTag(s.status)">{{ stepName(s.status) }}</span></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="grid cols-2">
        <div class="panel">
          <h3>设备空等（分钟）</h3>
          <table>
            <thead><tr><th>设备</th><th>策略</th><th>可用</th><th>占用</th><th>空等</th><th>空等区间</th></tr></thead>
            <tbody>
              <template v-for="plan in compare.plans" :key="'st' + plan.id">
                <tr v-for="st in plan.metrics.stations" :key="plan.id + st.station_no">
                  <td class="mono">{{ st.station_no }}</td>
                  <td class="small">{{ plan.label }}</td>
                  <td class="small">{{ st.available }}</td>
                  <td>{{ st.busy_min }}</td>
                  <td><b>{{ st.idle_min }}</b></td>
                  <td class="small">{{ st.idle_gaps.map(g => g.from + '-' + g.to).join('，') || '—' }}</td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>
        <div class="panel">
          <h3>交接记录</h3>
          <table>
            <thead><tr><th>策略</th><th>包</th><th>交接</th><th>时刻</th></tr></thead>
            <tbody>
              <template v-for="plan in compare.plans" :key="'ho' + plan.id">
                <tr v-for="hd in plan.handoffs" :key="hd.id">
                  <td class="small">{{ plan.label }}</td>
                  <td class="mono">{{ hd.pack_serial }}</td>
                  <td>{{ hd.from_inspector }} → {{ hd.to_inspector }}（{{ kindName(hd.station_kind) }}）</td>
                  <td class="mono">{{ hd.handoff_at_hhmm }}</td>
                </tr>
                <tr v-if="!plan.handoffs.length"><td colspan="4" class="small muted">{{ plan.label }}：无换人交接</td></tr>
              </template>
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <h2>结果与放行结论</h2>
        <div class="row" style="margin-bottom:10px">
          <button class="secondary" @click="executeAll(compare.plans[0].id)">按「{{ compare.plans[0].label }}」执行全部初检</button>
          <button class="secondary" @click="executeWithFail(compare.plans[1].id)">按「{{ compare.plans[1].label }}」执行并让 P-9 热像失败</button>
          <button @click="issue(compare.plans[1].id)">签发第二个计划的放行结论</button>
        </div>
        <div class="row" style="margin-bottom:10px">
          <div><label>迟到回执：包</label>
            <select v-model="late.pack_serial"><option>P-8</option><option>P-9</option></select>
          </div>
          <div><label>工序</label>
            <select v-model="late.kind"><option value="insulation">绝缘</option><option value="thermal">热像</option></select>
          </div>
          <div><label>结果</label>
            <select v-model="late.result"><option value="fail">失败</option><option value="pass">合格</option></select>
          </div>
          <div><label>回执编号</label><input v-model="late.source_doc" /></div>
          <button class="danger" style="align-self:flex-end" @click="submitLate(compare.plans[1].id)">提交迟到回执</button>
        </div>
        <div v-for="plan in compare.plans" :key="'c' + plan.id" style="margin-top:14px">
          <h3>{{ plan.label }} · 放行结论</h3>
          <table>
            <thead><tr><th>包</th><th>结论</th><th>签发时刻</th><th>复核</th><th>摘要</th></tr></thead>
            <tbody>
              <tr v-for="c in plan.conclusions" :key="c.pack_serial">
                <td class="mono">{{ c.pack_serial }}</td>
                <td><span class="tag" :class="conclusionTag(c.conclusion_type)">{{ conclusionName(c.conclusion_type) }}</span></td>
                <td class="small mono">{{ c.issued_at ? c.issued_at.slice(11, 16) : '—' }}</td>
                <td><span v-if="c.review_required" class="tag red">需复核</span><span v-else class="small muted">—</span></td>
                <td class="small">{{ kindSummary(c) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="plan.late_receipts.length" class="small muted" style="margin-top:6px">
            迟到回执：{{ plan.late_receipts.map(r => `${r.pack}/${kindName(r.kind)} ${r.result}（${r.applied ? '已应用' : '未覆盖'}）`).join('；') }}
          </p>
          <details style="margin-top:8px">
            <summary class="small muted">全部步骤（含复测/复做）与结果 EAR</summary>
            <table>
              <thead><tr><th>包</th><th>工序</th><th>尝试</th><th>检验员</th><th>设备校准至</th><th>结果</th><th>有效</th><th>来源</th><th>前置</th></tr></thead>
              <tbody>
                <tr v-for="r in plan.results" :key="r.id">
                  <td class="mono">{{ r.pack_serial }}</td>
                  <td>{{ kindName(r.kind) }}</td>
                  <td>{{ stepAttempt(plan, r) }}</td>
                  <td>{{ r.inspector_code }}</td>
                  <td class="small mono">{{ r.station_calibrated_until ? r.station_calibrated_until.slice(0, 10) : '—' }}</td>
                  <td><span class="tag" :class="r.result === 'pass' ? 'green' : 'red'">{{ r.result === 'pass' ? '合格' : '失败' }}</span></td>
                  <td><span class="tag" :class="r.valid ? 'green' : 'red'">{{ r.valid ? '有效' : '失效' }}</span></td>
                  <td class="small">{{ r.source }}</td>
                  <td class="small">{{ r.prerequisite_result || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </details>
        </div>
      </div>
    </template>

    <template v-if="replay">
      <div class="panel">
        <h2>🔁 异常回放结果</h2>
        <div class="grid cols-2">
          <div>
            <h3>热像失败 → 自动生成复测</h3>
            <p class="small">失败步骤 {{ replay.thermal_failure.failed_step.pack_serial }}/{{ kindName(replay.thermal_failure.failed_step.kind) }}（第 {{ replay.thermal_failure.failed_step.attempt }} 次）：
              <span class="tag red">失败</span>；复测步骤尝试 {{ replay.thermal_failure.retest_step.attempt }}，原失败记录保留。</p>
            <p class="small">手工改成合格的尝试被拒绝：<span class="tag red">{{ replay.thermal_failure.overwrite_blocked }}</span>；
              跨包读数被拒绝：<span class="tag red">{{ replay.thermal_failure.cross_pack_blocked }}</span>。</p>
          </div>
          <div>
            <h3>校准提前失效 → 结果作废并复做</h3>
            <ul class="small">
              <li v-for="x in replay.calibration_early_expiry.invalidated_results" :key="x.result_id">
                {{ x.pack }}/{{ kindName(x.kind) }} 执行于 {{ x.executed_at.slice(11, 16) }} → 失效
              </li>
            </ul>
            <p class="small muted">复做步骤：{{ replay.calibration_early_expiry.redo_steps.map(x => x.pack + '/' + kindName(x.kind)).join('，') }}</p>
          </div>
          <div>
            <h3>包件取消 → 未开始取消、历史保留</h3>
            <p class="small">P-9 取消步骤数：{{ replay.pack_cancellation.cancelled_steps }}，结论：
              <span class="tag red">{{ conclusionName(replay.pack_cancellation.conclusion.conclusion_type) }}</span>；已完成的步骤记录未被删除。</p>
          </div>
          <div>
            <h3>迟到回执 → 已签发不覆盖</h3>
            <p class="small">签发前迟到回执：{{ replay.late_receipt.before_issue.applied ? '已应用' : '未应用' }}；
              签发后 P-8 结论仍为 <span class="tag green">{{ conclusionName(replay.late_receipt.after_issue.conclusion_type) }}</span>，
              仅标记复核 <span class="tag amber">需复核</span>。</p>
            <p class="small muted">{{ replay.late_receipt.after_issue.review_note }}</p>
          </div>
        </div>
        <details style="margin-top:10px">
          <summary class="small muted">回放原始数据</summary>
          <pre class="json">{{ JSON.stringify(replay, null, 2) }}</pre>
        </details>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { api } from '../api'

const compare = ref(null)
const replay = ref(null)
const loading = ref(false)
const error = ref('')
const msg = ref('')
const late = ref({ pack_serial: 'P-8', kind: 'thermal', result: 'fail', source_doc: 'LATE-001' })

const KIND_NAMES = { insulation: '绝缘', thermal: '热像' }
const STEP_NAMES = {
  planned: '待排', scheduled: '已排', in_progress: '进行中', done: '完成', failed: '失败',
  blocked: '阻塞', redo_required: '待复做', cancelled: '已取消',
}
const CONCLUSION_NAMES = {
  pending: '待检验', released_ready: '可放行', retest_required: '需复测', held: '暂扣',
  released: '已签发放行', cancelled: '已取消',
}
const kindName = (k) => KIND_NAMES[k] || k
const stepName = (s) => STEP_NAMES[s] || s
const conclusionName = (c) => CONCLUSION_NAMES[c] || c

function stepTag(s) {
  if (['failed', 'blocked', 'redo_required', 'cancelled'].includes(s)) return 'red'
  if (['done'].includes(s)) return 'green'
  return 'amber'
}
function conclusionTag(c) {
  if (['released', 'released_ready'].includes(c)) return 'green'
  if (['retest_required', 'cancelled', 'held'].includes(c)) return 'red'
  return 'amber'
}
function stepAttempt(plan, r) {
  const s = plan.steps.find(x => x.id === r.step_id)
  return s ? `第 ${s.attempt} 次` : '—'
}
function kindSummary(c) {
  const k = c.summary?.kinds || {}
  return ['insulation', 'thermal'].map(x => {
    const v = k[x]
    if (!v) return `${kindName(x)}：缺`
    const res = v.result === 'pass' ? '合格' : (v.result === 'fail' ? '失败' : '未检')
    return `${kindName(x)}：${res}${v.valid === false ? '(失效)' : ''}/第${v.attempt}次`
  }).join('；')
}

async function runCompare() {
  loading.value = true; error.value = ''; msg.value = ''
  try {
    compare.value = await api.post('/api/scheduling/compare')
    replay.value = null
  } catch (e) { error.value = e.message } finally { loading.value = false }
}

async function runReplay() {
  loading.value = true; error.value = ''; msg.value = ''
  try {
    replay.value = await api.post('/api/scheduling/replay')
    compare.value = null
  } catch (e) { error.value = e.message } finally { loading.value = false }
}

async function refreshPlan(planId) {
  const plan = await api.get(`/api/scheduling/plans/${planId}`)
  const idx = compare.value.plans.findIndex(p => p.id === planId)
  if (idx >= 0) compare.value.plans[idx] = plan
}

async function executeAll(planId) {
  loading.value = true; error.value = ''; msg.value = ''
  try {
    const r = await api.post(`/api/scheduling/plans/${planId}/execute`, { fail: [] })
    const idx = compare.value.plans.findIndex(p => p.id === planId)
    compare.value.plans[idx] = r.plan
    msg.value = '已执行全部初检'
  } catch (e) { error.value = e.message } finally { loading.value = false }
}

async function executeWithFail(planId) {
  loading.value = true; error.value = ''; msg.value = ''
  try {
    const r = await api.post(`/api/scheduling/plans/${planId}/execute`, {
      fail: [{ pack_serial: 'P-9', kind: 'thermal' }],
    })
    const idx = compare.value.plans.findIndex(p => p.id === planId)
    compare.value.plans[idx] = r.plan
    msg.value = 'P-9 热像失败，已自动生成复测'
  } catch (e) { error.value = e.message } finally { loading.value = false }
}

async function issue(planId) {
  loading.value = true; error.value = ''; msg.value = ''
  try {
    const r = await api.post(`/api/scheduling/plans/${planId}/issue`, {})
    const idx = compare.value.plans.findIndex(p => p.id === planId)
    compare.value.plans[idx] = r.plan
    msg.value = `已签发 ${r.issued.map(x => x.pack).join('、') || '无'}；未签发：${r.blocked.map(x => x.pack).join('、') || '无'}`
  } catch (e) { error.value = e.message } finally { loading.value = false }
}

async function submitLate(planId) {
  loading.value = true; error.value = ''; msg.value = ''
  try {
    const r = await api.post(`/api/scheduling/plans/${planId}/late-receipt`, { ...late.value })
    const idx = compare.value.plans.findIndex(p => p.id === planId)
    compare.value.plans[idx] = r.plan
    msg.value = r.message
  } catch (e) { error.value = e.message } finally { loading.value = false }
}
</script>
