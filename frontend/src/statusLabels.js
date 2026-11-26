export const PACK_STATUS = {
  received: '接收',
  quarantined: '隔离',
  dismantling: '拆解',
  pending_inspection: '待检',
  pending_approval: '待审批',
  reusable: '可复用',
  pending_recycle: '待回收',
  outbound: '已出库',
}

export const APPROVAL_STATUS = { active: '有效', voided: '已失效', superseded: '已替代' }
export const DECISION = { echelon: '梯次利用', recycle: '回收', reinspect: '退回补检' }
export const PICKING_STATUS = { waiting: '待拣货', corrected: '已更正', picked: '已拣货', cancelled: '已取消' }
export const OUTBOUND_STATUS = {
  created: '已建单', notified: '已通知买家', shipped: '已装车',
  rejected: '买家拒收', recalled: '召回', returned: '退运', signed: '已签收',
}
export const MODULE_STATUS = {
  linked: '已建立父子关系', quarantine_review: '隔离待核', reusable: '可复用',
  pending_recycle: '待回收', outbound: '已出库', rejected_returned: '拒收退回待复核',
}
export const EVENT_TYPE = { collision: '碰撞', water: '浸水', thermal: '热事件' }
export const WO_STATUS = {
  created: '已创建', in_progress: '进行中', pending_verification: '待核（挂起）', completed: '已完成',
}

export function tagClass(status) {
  if (['quarantined', 'voided', 'corrected', 'rejected', 'fail', 'pending_verification'].includes(status)) return 'red'
  if (['reusable', 'picked', 'signed', 'pass', 'completed', 'linked'].includes(status)) return 'green'
  if (['pending_approval', 'pending_inspection', 'waiting', 'notified', 'created', 'dismantling', 'received', 'in_progress'].includes(status)) return 'amber'
  if (['outbound', 'shipped', 'recalled', 'returned', 'closed'].includes(status)) return 'gray'
  return ''
}
