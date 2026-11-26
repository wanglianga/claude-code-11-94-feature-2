"""检验排程与结果绑定服务（周三检验班 P-8 / P-9 示例）。

核心约束
--------
1. 每条结果都绑定：包序列号、步骤、设备（含当时校准有效期）、操作者资质、前置结果。
   任何一步的读数只能落到它自己的包上，跨包提交（含回执里的序列号不符）直接拒绝。
2. 失败只生成复测任务，不存在把失败结果手工改成合格的接口；复测是新的尝试记录。
3. 设备中途停机：只重排尚未开始的步骤，已完成/进行中的步骤保持实际时间不动。
4. 校准提前失效：已完成结果若落在新的校准有效期之外，标记失效并生成复做任务。
5. 迟到回执：包已签发（released）时绝不覆盖结论，只登记回执并标记复核。
6. 两种排法：按包连续（pack_continuous）与按设备阶段交错（equipment_stage），
   输出完成时刻、设备空等与交接记录供验收。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from . import audit
from .errors import BusinessRuleError, NotFoundError

# ----------------------------------------------------------------------
# 周三检验班默认资源（时间点为班次内本地时刻，统一按 UTC 存储与比较）
# ----------------------------------------------------------------------
SHIFT_DAY = datetime(2026, 11, 25, tzinfo=timezone.utc)

DEFAULT_SPEC = {
    "shift_start": (9, 0),
    "shift_end": (10, 45),
    "stations": [
        {"station_no": "INS-1", "name": "绝缘仪", "kind": "insulation",
         "from": (9, 0), "to": (10, 20), "calibrated_days": 40},
        {"station_no": "THM-1", "name": "热像舱", "kind": "thermal",
         "from": (9, 0), "to": (10, 40), "calibrated_days": 40},
    ],
    "inspectors": [
        {"code": "T1", "display_name": "T1 检验员", "username": "u_t1",
         "qualified": ["insulation", "thermal"], "from": (9, 0), "to": (10, 45)},
        {"code": "T2", "display_name": "T2 检验员", "username": "u_t2",
         "qualified": ["thermal"], "from": (9, 0), "to": (10, 30)},
    ],
    # 每个包的工序顺序；时长已包含设备换包准备工时
    "packs": [
        {"serial_no": "P-8", "steps": [("insulation", 30), ("thermal", 20)]},
        {"serial_no": "P-9", "steps": [("insulation", 20), ("thermal", 25)]},
    ],
}

STRATEGY_LABELS = {
    "pack_continuous": "按包连续检验",
    "equipment_stage": "按设备阶段交错",
}

_STARTED = ("in_progress", "done", "failed", "redo_required")
_NOT_STARTED = ("planned", "scheduled", "blocked")


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _dt(hh: int, mm: int) -> datetime:
    return SHIFT_DAY.replace(hour=hh, minute=mm)


def _t(hm: tuple[int, int]) -> datetime:
    return _dt(hm[0], hm[1])


def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for s, e in sorted(intervals):
        if e <= s:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _overlap(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _is_free(busy: list[tuple[datetime, datetime]], s: datetime, e: datetime) -> bool:
    return not any(_overlap((s, e), iv) for iv in busy)


def _minutes(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds() // 60)


def _complement(window: tuple[datetime, datetime],
                busy: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """窗口内未被占用的区间（用于设备空等统计）。"""
    ws, we = window
    gaps: list[tuple[datetime, datetime]] = []
    cursor = ws
    for s, e in _merge([(max(s, ws), min(e, we)) for s, e in busy if _overlap((s, e), window)]):
        if s > cursor:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < we:
        gaps.append((cursor, we))
    return gaps


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _hhmm(v: datetime | None) -> str | None:
    return v.strftime("%H:%M") if v else None


# ----------------------------------------------------------------------
# 资源初始化
# ----------------------------------------------------------------------
def ensure_shift_setup(db: Session, spec: dict | None = None) -> dict:
    """幂等创建检验设备与检验员资质档案。"""
    spec = spec or DEFAULT_SPEC
    stations: dict[str, models.InspectionStation] = {}
    for st in spec["stations"]:
        row = db.scalar(select(models.InspectionStation).where(
            models.InspectionStation.station_no == st["station_no"]))
        calibrated = SHIFT_DAY + timedelta(days=st["calibrated_days"])
        if not row:
            row = models.InspectionStation(
                station_no=st["station_no"], name=st["name"], kind=st["kind"],
                calibrated_until=calibrated,
            )
            db.add(row)
        stations[st["station_no"]] = row
    db.flush()

    inspectors: dict[str, models.InspectorProfile] = {}
    for ins in spec["inspectors"]:
        user = db.scalar(select(models.User).where(models.User.username == ins["username"]))
        if not user:
            user = models.User(username=ins["username"], display_name=ins["display_name"],
                               role="inspector", team="检验班")
            db.add(user)
            db.flush()
        prof = db.scalar(select(models.InspectorProfile).where(
            models.InspectorProfile.code == ins["code"]))
        if not prof:
            prof = models.InspectorProfile(
                user_id=user.id, code=ins["code"], display_name=ins["display_name"],
                qualified_kinds=list(ins["qualified"]),
            )
            db.add(prof)
        inspectors[ins["code"]] = prof
    db.flush()
    return {"stations": stations, "inspectors": inspectors}


# ----------------------------------------------------------------------
# 排程
# ----------------------------------------------------------------------
def _load_resources(db: Session, plan: models.ShiftPlan) -> dict:
    """读取一个计划的设备/人员窗口与实时校准状态。"""
    stations = []
    for w in db.scalars(select(models.PlanStationWindow).where(
            models.PlanStationWindow.plan_id == plan.id)).all():
        row = db.get(models.InspectionStation, w.station_id)
        stations.append({
            "id": row.id, "station_no": row.station_no, "kind": row.kind,
            "available_from": w.available_from, "available_to": w.available_to,
            "calibrated_until": row.calibrated_until,
        })
    inspectors = []
    for w in db.scalars(select(models.PlanInspectorWindow).where(
            models.PlanInspectorWindow.plan_id == plan.id)).all():
        row = db.get(models.InspectorProfile, w.inspector_id)
        inspectors.append({
            "id": row.id, "code": row.code, "qualified": set(w.qualified_kinds or []),
            "available_from": w.available_from, "available_to": w.available_to,
        })
    downtime: dict[int, list[tuple[datetime, datetime]]] = {s["id"]: [] for s in stations}
    for d in db.scalars(select(models.StationDowntime)).all():
        if d.station_id in downtime and _overlap((d.start_at, d.end_at),
                                                 (plan.shift_start, plan.shift_end)):
            downtime[d.station_id].append((d.start_at, d.end_at))
    return {"stations": stations, "inspectors": inspectors, "downtime": downtime}


def _try_slot(
    kind: str,
    duration: int,
    min_start: datetime,
    resources: dict,
    *,
    station_busy: dict[int, list[tuple[datetime, datetime]]],
    inspector_busy: dict[int, list[tuple[datetime, datetime]]],
    prefer_inspector: int | None = None,
    prefer_station: int | None = None,
):
    """找到最早可行的时间槽：满足设备窗口+校准、人员窗口+资质、双方不冲突。"""
    stations = [s for s in resources["stations"] if s["kind"] == kind]
    stations.sort(key=lambda s: (0 if s["id"] == prefer_station else 1, s["station_no"]))
    if not stations:
        return None
    limit = max(min(s["available_to"], s["calibrated_until"]) for s in stations)
    step = timedelta(minutes=1)
    t = min_start
    while t + timedelta(minutes=duration) <= limit:
        end = t + timedelta(minutes=duration)
        for st in stations:
            if t < st["available_from"]:
                continue
            if end > min(st["available_to"], st["calibrated_until"]):
                continue
            if not _is_free(station_busy.get(st["id"], []), t, end):
                continue
            eligible = [
                i for i in resources["inspectors"]
                if kind in i["qualified"] and t >= i["available_from"]
                and end <= i["available_to"]
                and _is_free(inspector_busy.get(i["id"], []), t, end)
            ]
            if eligible:
                eligible.sort(key=lambda i: (0 if i["id"] == prefer_inspector else 1, i["code"]))
                return t, end, st, eligible[0]
        t += step
    return None


def _schedule_steps(db: Session, plan: models.ShiftPlan, *, strategy: str, spec: dict) -> list[models.InspectionStep]:
    resources = _load_resources(db, plan)
    station_busy: dict[int, list[tuple[datetime, datetime]]] = {
        s["id"]: list(resources["downtime"].get(s["id"], [])) for s in resources["stations"]
    }
    inspector_busy: dict[int, list[tuple[datetime, datetime]]] = {
        i["id"]: [] for i in resources["inspectors"]
    }
    created: list[models.InspectionStep] = []
    shift_start = plan.shift_start

    if strategy == "pack_continuous":
        # 按包连续：逐包按工序列，尽量由同一检验员完成整包
        for order, pack in enumerate(spec["packs"], start=1):
            prev_end = shift_start
            prev_inspector = None
            prev_step = None
            for seq, (kind, duration) in enumerate(pack["steps"], start=1):
                slot = _try_slot(kind, duration, max(prev_end, shift_start), resources,
                                 station_busy=station_busy, inspector_busy=inspector_busy,
                                 prefer_inspector=prev_inspector)
                step = _make_step(db, plan, pack, order, seq, kind, duration, slot, prev_step)
                created.append(step)
                if slot:
                    s, e, st, ins = slot
                    station_busy[st["id"]].append((s, e))
                    inspector_busy[ins["id"]].append((s, e))
                    prev_end, prev_inspector = e, ins["id"]
                else:
                    prev_inspector = None
                prev_step = step
    else:
        # 按设备阶段交错：先把所有包的绝缘阶段排完，再排热像阶段；
        # 同一设备阶段尽量由固定检验员操作（形成换人交接）。
        stage_operator: dict[str, int] = {}
        pack_steps: dict[str, list[models.InspectionStep]] = {p["serial_no"]: [] for p in spec["packs"]}
        for seq_index in range(max(len(p["steps"]) for p in spec["packs"])):
            for order, pack in enumerate(spec["packs"], start=1):
                if seq_index >= len(pack["steps"]):
                    continue
                kind, duration = pack["steps"][seq_index]
                prev_step = pack_steps[pack["serial_no"]][-1] if pack_steps[pack["serial_no"]] else None
                if prev_step and prev_step.planned_end:
                    min_start = prev_step.planned_end
                else:
                    min_start = shift_start
                slot = _try_slot(kind, duration, min_start, resources,
                                 station_busy=station_busy, inspector_busy=inspector_busy,
                                 prefer_inspector=stage_operator.get(kind))
                step = _make_step(db, plan, pack, order, seq_index + 1, kind, duration, slot, prev_step)
                created.append(step)
                pack_steps[pack["serial_no"]].append(step)
                if slot:
                    s, e, st, ins = slot
                    station_busy[st["id"]].append((s, e))
                    inspector_busy[ins["id"]].append((s, e))
                    stage_operator[kind] = ins["id"]
    db.flush()
    _rebuild_handoffs(db, plan)
    return created


def _make_step(db, plan, pack, order, seq, kind, duration, slot, prev_step) -> models.InspectionStep:
    step = models.InspectionStep(
        plan_id=plan.id, pack_serial=pack["serial_no"], pack_order=order,
        sequence=seq, attempt=1, kind=kind, duration_min=duration,
        depends_on_step_id=prev_step.id if prev_step else None,
    )
    if slot:
        s, e, st, ins = slot
        step.planned_start, step.planned_end = s, e
        step.station_id, step.inspector_id = st["id"], ins["id"]
        step.status = "scheduled"
    else:
        step.status = "blocked"
        step.note = "资源窗口内无可用时间槽"
    db.add(step)
    db.flush()
    return step


def _rebuild_handoffs(db: Session, plan: models.ShiftPlan) -> None:
    """按实际排定的检验员重建包内换人交接记录。"""
    db.query(models.HandoffRecord).filter(
        models.HandoffRecord.plan_id == plan.id).delete(synchronize_session=False)
    packs = {s.pack_serial for s in db.scalars(select(models.InspectionStep).where(
        models.InspectionStep.plan_id == plan.id)).all()}
    profiles = {p.id: p for p in db.scalars(select(models.InspectorProfile)).all()}
    for serial in sorted(packs):
        steps = [s for s in db.scalars(
            select(models.InspectionStep).where(
                models.InspectionStep.plan_id == plan.id,
                models.InspectionStep.pack_serial == serial,
                models.InspectionStep.status != "cancelled",
            ).order_by(models.InspectionStep.planned_start, models.InspectionStep.sequence)
        ).all() if s.inspector_id]
        for prev, nxt in zip(steps, steps[1:]):
            if prev.inspector_id != nxt.inspector_id:
                db.add(models.HandoffRecord(
                    plan_id=plan.id, pack_serial=serial,
                    from_step_id=prev.id, to_step_id=nxt.id,
                    from_inspector_code=profiles[prev.inspector_id].code,
                    to_inspector_code=profiles[nxt.inspector_id].code,
                    station_kind=nxt.kind,
                    handoff_at=nxt.planned_start,
                    note=f"{serial} 的 {prev.kind} → {nxt.kind} 换人交接",
                ))
    db.flush()


# ----------------------------------------------------------------------
# 计划构建 / 重置
# ----------------------------------------------------------------------
def _purge_all_scheduling(db: Session) -> None:
    for model in (
        models.LateReceipt, models.PackCancellation, models.CalibrationChange,
        models.StationDowntime, models.StepResult, models.HandoffRecord,
        models.PackConclusion,
    ):
        db.query(model).delete(synchronize_session=False)
    db.query(models.InspectionStep).filter(
        models.InspectionStep.depends_on_step_id.is_not(None)).update(
        {models.InspectionStep.depends_on_step_id: None}, synchronize_session=False)
    db.query(models.InspectionStep).update(
        {models.InspectionStep.retest_of_step_id: None}, synchronize_session=False)
    db.query(models.InspectionStep).delete(synchronize_session=False)
    db.query(models.PlanStationWindow).delete(synchronize_session=False)
    db.query(models.PlanInspectorWindow).delete(synchronize_session=False)
    db.query(models.ShiftPlan).delete(synchronize_session=False)
    # 恢复设备默认校准有效期，避免上一轮回放残留
    for st in DEFAULT_SPEC["stations"]:
        row = db.scalar(select(models.InspectionStation).where(
            models.InspectionStation.station_no == st["station_no"]))
        if row:
            row.calibrated_until = SHIFT_DAY + timedelta(days=st["calibrated_days"])
    db.flush()


def build_plan(db: Session, *, user: models.User | None, strategy: str,
               spec: dict | None = None) -> models.ShiftPlan:
    spec = spec or DEFAULT_SPEC
    if strategy not in STRATEGY_LABELS:
        raise BusinessRuleError(f"未知排程策略 {strategy}")
    setup = ensure_shift_setup(db, spec)
    plan = models.ShiftPlan(
        plan_no=f"WED-{strategy.upper()}", label=STRATEGY_LABELS[strategy],
        strategy=strategy, shift_start=_t(spec["shift_start"]),
        shift_end=_t(spec["shift_end"]), status="planned",
        created_by=user.id if user else None,
    )
    db.add(plan)
    db.flush()
    for st in spec["stations"]:
        db.add(models.PlanStationWindow(
            plan_id=plan.id, station_id=setup["stations"][st["station_no"]].id,
            available_from=_t(st["from"]), available_to=_t(st["to"])))
    for ins in spec["inspectors"]:
        db.add(models.PlanInspectorWindow(
            plan_id=plan.id, inspector_id=setup["inspectors"][ins["code"]].id,
            available_from=_t(ins["from"]), available_to=_t(ins["to"]),
            qualified_kinds=list(ins["qualified"])))
    db.flush()
    _schedule_steps(db, plan, strategy=strategy, spec=spec)
    for pack in spec["packs"]:
        db.add(models.PackConclusion(plan_id=plan.id, pack_serial=pack["serial_no"],
                                     conclusion_type="pending", summary={}))
    audit.log(db, user.id if user else None, "build_shift_plan", "shift_plan", plan.id,
              {"strategy": strategy})
    db.flush()
    return plan


def build_both_plans(db: Session, *, user: models.User | None) -> list[models.ShiftPlan]:
    _purge_all_scheduling(db)
    return [build_plan(db, user=user, strategy=s) for s in STRATEGY_LABELS]


# ----------------------------------------------------------------------
# 结果录入（含绑定与跨包保护）
# ----------------------------------------------------------------------
def _get_step(db: Session, plan: models.ShiftPlan, step_id: int) -> models.InspectionStep:
    step = db.get(models.InspectionStep, step_id)
    if not step or step.plan_id != plan.id:
        raise NotFoundError("步骤不存在或不属于该计划")
    return step


def _latest_result(db: Session, step_id: int, *, valid_only: bool = False):
    q = select(models.StepResult).where(models.StepResult.step_id == step_id)
    if valid_only:
        q = q.where(models.StepResult.valid.is_(True))
    return db.scalar(q.order_by(models.StepResult.id.desc()))


def _effective_step_for_kind(db: Session, plan, pack_serial, kind) -> models.InspectionStep | None:
    steps = db.scalars(select(models.InspectionStep).where(
        models.InspectionStep.plan_id == plan.id,
        models.InspectionStep.pack_serial == pack_serial,
        models.InspectionStep.kind == kind,
        models.InspectionStep.status != "cancelled",
    ).order_by(models.InspectionStep.attempt.desc(), models.InspectionStep.id.desc())).all()
    return steps[0] if steps else None


def _guard_cross_pack(pack_serial: str, readings: dict) -> None:
    for key in ("serial_no", "pack_serial"):
        if key in (readings or {}) and readings[key] != pack_serial:
            raise BusinessRuleError(
                "读数绑定了其他包的序列号，已拒绝："
                f"步骤属于 {pack_serial}，读数声称 {readings[key]}",
                code="cross_pack_reading")


def _apply_result(
    db: Session, *, plan: models.ShiftPlan, step: models.InspectionStep, pack_serial: str,
    result: str, readings: dict | None, note: str, user: models.User | None,
    source: str = "field", executed_at: datetime | None = None,
    operator: models.InspectorProfile | None = None,
) -> models.StepResult:
    if result not in ("pass", "fail"):
        raise BusinessRuleError("result 只能是 pass 或 fail")
    if step.pack_serial != pack_serial:
        raise BusinessRuleError(
            f"包序列号不匹配：步骤属于 {step.pack_serial}，提交的是 {pack_serial}",
            code="cross_pack_reading")
    _guard_cross_pack(pack_serial, readings or {})
    if step.status in ("cancelled",):
        raise BusinessRuleError("该步骤已取消，不能录入结果")
    if step.status in ("done", "failed") and source == "field":
        raise BusinessRuleError("该步骤已有结果；若需重检请走复测，不能覆盖既有结果",
                                code="result_immutable")

    resources = _load_resources(db, plan)
    station = next((s for s in resources["stations"] if s["id"] == step.station_id), None)
    if not station:
        raise BusinessRuleError("步骤未分配设备，无法录入结果")
    profile = operator or (db.get(models.InspectorProfile, step.inspector_id)
                           if step.inspector_id else None)
    if not profile:
        raise BusinessRuleError("步骤未分配检验员，无法录入结果")

    actual_start = executed_at or step.actual_start or step.planned_start or _dt(9, 0)
    actual_end = executed_at or step.actual_end or step.planned_end or (
        actual_start + timedelta(minutes=step.duration_min))

    if step.kind not in (profile.qualified_kinds or []):
        raise BusinessRuleError(
            f"检验员 {profile.code} 不具备 {step.kind} 资质，不能录入该步结果",
            code="inspector_not_qualified")
    if actual_end > station["calibrated_until"]:
        raise BusinessRuleError(
            f"设备 {station['station_no']} 校准有效期至 "
            f"{station['calibrated_until']:%Y-%m-%d %H:%M}，"
            f"执行时刻 {actual_end:%H:%M} 已超出，结果不可用",
            code="calibration_expired")

    prerequisite_step_id = None
    prerequisite_result = None
    if step.depends_on_step_id:
        pre = _get_step(db, plan, step.depends_on_step_id)
        pre_res = _latest_result(db, pre.id, valid_only=True)
        if not pre_res or pre_res.result != "pass":
            raise BusinessRuleError(
                f"{step.pack_serial} 的 {step.kind} 前序 {pre.kind} 尚未合格，"
                "不能录入后续结果",
                code="prerequisite_not_passed")
        prerequisite_step_id = pre.id
        prerequisite_result = pre_res.result

    step.actual_start, step.actual_end = actual_start, actual_end
    step.status = "done" if result == "pass" else "failed"
    record = models.StepResult(
        step_id=step.id, plan_id=plan.id, pack_serial=pack_serial,
        sequence=step.sequence, kind=step.kind,
        station_no=station["station_no"], station_kind=station["kind"],
        station_calibrated_until=station["calibrated_until"],
        inspector_code=profile.code,
        inspector_qualified_kinds=list(profile.qualified_kinds or []),
        prerequisite_step_id=prerequisite_step_id,
        prerequisite_result=prerequisite_result,
        result=result, readings=readings or {}, source=source, valid=True, note=note,
    )
    db.add(record)
    db.flush()
    audit.log(db, user.id if user else None, "record_step_result", "inspection_step",
              step.id, {"pack": pack_serial, "kind": step.kind, "result": result,
                        "station": station["station_no"], "inspector": profile.code})
    if result == "fail":
        _create_retest(db, plan, step, reason=f"{step.kind} 检验失败，自动生成复测")
    db.flush()
    _refresh_conclusion(db, plan, pack_serial, user)
    return record


def _current_busy(db: Session, plan: models.ShiftPlan) -> tuple[dict, dict]:
    resources = _load_resources(db, plan)
    station_busy = {s["id"]: list(resources["downtime"].get(s["id"], [])) for s in resources["stations"]}
    inspector_busy = {i["id"]: [] for i in resources["inspectors"]}
    for s in db.scalars(select(models.InspectionStep).where(
            models.InspectionStep.plan_id == plan.id,
            models.InspectionStep.status != "cancelled")).all():
        if not s.station_id:
            continue
        start = s.actual_start or s.planned_start
        end = s.actual_end or s.planned_end
        if not start or not end:
            continue
        if s.station_id in station_busy:
            station_busy[s.station_id].append((start, end))
        if s.inspector_id in inspector_busy:
            inspector_busy[s.inspector_id].append((start, end))
    return station_busy, inspector_busy


def _create_retest(db: Session, plan: models.ShiftPlan, failed: models.InspectionStep,
                   *, reason: str) -> models.InspectionStep:
    resources = _load_resources(db, plan)
    station_busy, inspector_busy = _current_busy(db, plan)
    # 本步自身已结束，其占用不参与后续排程（同一包复测在其之后）
    if failed.station_id in station_busy:
        station_busy[failed.station_id] = [
            iv for iv in station_busy[failed.station_id] if iv != (failed.actual_start, failed.actual_end)]
    if failed.inspector_id in inspector_busy:
        inspector_busy[failed.inspector_id] = [
            iv for iv in inspector_busy[failed.inspector_id]
            if iv != (failed.actual_start, failed.actual_end)]

    depends_on = None
    if failed.kind != "insulation":
        pre = _effective_step_for_kind(db, plan, failed.pack_serial, "insulation")
        if pre:
            pre_res = _latest_result(db, pre.id, valid_only=True)
            if pre_res and pre_res.result == "pass":
                depends_on = pre.id

    min_start = failed.actual_end or failed.planned_end or plan.shift_start
    slot = _try_slot(failed.kind, failed.duration_min, min_start, resources,
                     station_busy=station_busy, inspector_busy=inspector_busy,
                     prefer_inspector=failed.inspector_id, prefer_station=failed.station_id)
    retest = models.InspectionStep(
        plan_id=plan.id, pack_serial=failed.pack_serial, pack_order=failed.pack_order,
        sequence=failed.sequence, attempt=failed.attempt + 1, kind=failed.kind,
        duration_min=failed.duration_min, depends_on_step_id=depends_on,
        retest_of_step_id=failed.id, note=reason,
    )
    if slot:
        s, e, st, ins = slot
        retest.planned_start, retest.planned_end = s, e
        retest.station_id, retest.inspector_id = st["id"], ins["id"]
        retest.status = "scheduled"
    else:
        retest.status = "blocked"
        retest.note = reason + "；现有资源窗口内无可用时间槽，需人工协调"
    db.add(retest)
    db.flush()

    # 下游未开始步骤改依赖复测结果，并顺延到复测结束之后
    for dep in db.scalars(select(models.InspectionStep).where(
            models.InspectionStep.plan_id == plan.id,
            models.InspectionStep.pack_serial == failed.pack_serial,
            models.InspectionStep.depends_on_step_id == failed.id,
            models.InspectionStep.status.in_(_NOT_STARTED),
    )).all():
        dep.depends_on_step_id = retest.id
        if retest.planned_end and dep.planned_start and dep.planned_start < retest.planned_end:
            dur = timedelta(minutes=dep.duration_min)
            dep.planned_start = retest.planned_end
            dep.planned_end = retest.planned_end + dur
    db.flush()
    _rebuild_handoffs(db, plan)
    return retest


def record_result(
    db: Session, *, plan_id: int, step_id: int, pack_serial: str, result: str,
    readings: dict | None = None, note: str = "", user: models.User | None = None,
    executed_at: datetime | None = None, operator: models.InspectorProfile | None = None,
) -> models.StepResult:
    plan = db.get(models.ShiftPlan, plan_id)
    if not plan:
        raise NotFoundError("排程计划不存在")
    step = _get_step(db, plan, step_id)
    return _apply_result(db, plan=plan, step=step, pack_serial=pack_serial, result=result,
                         readings=readings, note=note, user=user, source="field",
                         executed_at=executed_at, operator=operator)


def execute_plan(db: Session, *, plan_id: int, user: models.User | None = None,
                 fail: list[dict] | None = None) -> dict:
    """按排定顺序自动跑完尚未开始的步骤；可注入指定 (包, 工序) 检验失败。"""
    plan = db.get(models.ShiftPlan, plan_id)
    if not plan:
        raise NotFoundError("排程计划不存在")
    fail = fail or []
    fail_pairs = {(f["pack_serial"], f["kind"]) for f in fail}
    ran: list[dict] = []
    # 只执行初检（attempt==1）；失败产生的复测保留为待执行任务，供人工/复测流程处理
    initial = db.scalars(select(models.InspectionStep).where(
        models.InspectionStep.plan_id == plan.id,
        models.InspectionStep.attempt == 1,
        models.InspectionStep.status.in_(_NOT_STARTED),
    ).order_by(models.InspectionStep.planned_start, models.InspectionStep.id)).all()
    for step in initial:
        if step.depends_on_step_id:
            pre = _get_step(db, plan, step.depends_on_step_id)
            pre_res = _latest_result(db, pre.id, valid_only=True)
            if not pre_res or pre_res.result != "pass":
                step.status = "blocked"
                step.note = f"前序 {pre.kind} 未合格，后续步骤阻塞"
                continue
        outcome = "fail" if (step.pack_serial, step.kind) in fail_pairs else "pass"
        try:
            rec = _apply_result(
                db, plan=plan, step=step, pack_serial=step.pack_serial, result=outcome,
                readings={"note": "自动执行"}, note="计划执行", user=user, source="field")
            ran.append({"pack": step.pack_serial, "kind": step.kind, "result": rec.result,
                        "attempt": step.attempt, "step_id": step.id})
        except BusinessRuleError as exc:
            step.status = "blocked"
            step.note = exc.message
            ran.append({"pack": step.pack_serial, "kind": step.kind, "result": "blocked",
                        "attempt": step.attempt, "reason": exc.message})
    plan.status = "running"
    db.flush()
    return {"ran": ran, "plan": detail(db, plan)}


# ----------------------------------------------------------------------
# 结论
# ----------------------------------------------------------------------
def _pack_evaluation(db: Session, plan: models.ShiftPlan, pack_serial: str) -> dict:
    cancelled = db.scalar(select(models.PackCancellation).where(
        models.PackCancellation.plan_id == plan.id,
        models.PackCancellation.pack_serial == pack_serial))
    steps_by_kind: dict[str, models.InspectionStep] = {}
    for kind in ("insulation", "thermal"):
        eff = _effective_step_for_kind(db, plan, pack_serial, kind)
        if eff:
            steps_by_kind[kind] = eff

    kinds: dict[str, dict] = {}
    eligible = not cancelled
    needs_retest = False
    incomplete = False
    for kind in ("insulation", "thermal"):
        step = steps_by_kind.get(kind)
        if not step:
            eligible = False
            incomplete = True
            continue
        res = _latest_result(db, step.id)
        ok = bool(res and res.result == "pass" and res.valid and step.status == "done")
        if not ok:
            eligible = False
        # 该工序的全部尝试（含历史失败/失效）：
        attempts = db.scalars(select(models.InspectionStep).where(
            models.InspectionStep.plan_id == plan.id,
            models.InspectionStep.pack_serial == pack_serial,
            models.InspectionStep.kind == kind,
            models.InspectionStep.status != "cancelled",
        ).order_by(models.InspectionStep.attempt)).all()
        attempt_log = []
        for att in attempts:
            r = _latest_result(db, att.id)
            attempt_log.append({"attempt": att.attempt, "status": att.status,
                                "result": r.result if r else None,
                                "valid": r.valid if r else None})
            if r is not None and (r.result != "pass" or not r.valid):
                needs_retest = True
            if att.status in ("failed", "redo_required", "blocked"):
                needs_retest = True
        if res is None:
            incomplete = True
        kinds[kind] = {
            "step_id": step.id, "status": step.status, "attempt": step.attempt,
            "result": res.result if res else None,
            "valid": res.valid if res else None,
            "inspector": res.inspector_code if res else None,
            "station": res.station_no if res else None,
            "executed_at": _iso(step.actual_end or step.planned_end),
            "attempts": attempt_log,
            "prerequisite_step_id": res.prerequisite_step_id if res else step.depends_on_step_id,
            "prerequisite_result": res.prerequisite_result if res else None,
        }

    if cancelled:
        conclusion = "cancelled"
    elif eligible:
        conclusion = "released_ready"
    elif needs_retest:
        conclusion = "retest_required"
    elif incomplete:
        conclusion = "pending"
    else:
        conclusion = "retest_required"
    return {"eligible": eligible, "conclusion": conclusion, "kinds": kinds,
            "cancelled": bool(cancelled)}


_CONCLUSION_STORE = {
    "released_ready": "released_ready",
    "retest_required": "retest_required",
    "pending": "pending",
    "cancelled": "cancelled",
}


def _refresh_conclusion(db: Session, plan: models.ShiftPlan, pack_serial: str,
                        user: models.User | None) -> models.PackConclusion:
    evaluation = _pack_evaluation(db, plan, pack_serial)
    concl = db.scalar(select(models.PackConclusion).where(
        models.PackConclusion.plan_id == plan.id,
        models.PackConclusion.pack_serial == pack_serial))
    if not concl:
        concl = models.PackConclusion(plan_id=plan.id, pack_serial=pack_serial)
        db.add(concl)
    # 已签发的放行结论不因后续自动刷新而改变类型（迟到回执只做复核标记）
    if concl.conclusion_type in ("released", "cancelled"):
        concl.updated_at = datetime.now(timezone.utc)
        return concl
    concl.conclusion_type = _CONCLUSION_STORE[evaluation["conclusion"]]
    concl.summary = evaluation
    concl.updated_at = datetime.now(timezone.utc)
    db.flush()
    return concl


def issue_conclusions(db: Session, *, plan_id: int, user: models.User | None = None) -> dict:
    """签发放行结论：全部工序合格且结果有效、绑定完整者才签发 released。"""
    plan = db.get(models.ShiftPlan, plan_id)
    if not plan:
        raise NotFoundError("排程计划不存在")
    issued, blocked = [], []
    for concl in db.scalars(select(models.PackConclusion).where(
            models.PackConclusion.plan_id == plan.id)).all():
        evaluation = _pack_evaluation(db, plan, concl.pack_serial)
        concl.summary = evaluation
        if concl.conclusion_type == "released":
            issued.append({"pack": concl.pack_serial, "issued_at": _iso(concl.issued_at),
                           "already": True})
            continue
        if concl.conclusion_type == "cancelled":
            blocked.append({"pack": concl.pack_serial, "reason": "包件已取消"})
            continue
        if not evaluation["eligible"]:
            concl.conclusion_type = "retest_required"
            blocked.append({"pack": concl.pack_serial,
                            "reason": "存在失败/失效/阻塞工序，需复测或复做"})
            continue
        issued_at = max(
            (datetime.fromisoformat(k["executed_at"]) for k in evaluation["kinds"].values()
             if k["executed_at"]), default=None)
        concl.conclusion_type = "released"
        concl.issued_at = issued_at
        concl.issued_by = user.id if user else None
        issued.append({"pack": concl.pack_serial, "issued_at": _iso(issued_at)})
        audit.log(db, user.id if user else None, "issue_release_conclusion", "pack_conclusion",
                  concl.id, {"pack": concl.pack_serial})
    plan.status = "completed"
    db.flush()
    return {"issued": issued, "blocked": blocked, "plan": detail(db, plan)}


# ----------------------------------------------------------------------
# 停机 / 校准 / 取消 / 迟到回执
# ----------------------------------------------------------------------
def apply_downtime(db: Session, *, plan_id: int, station_no: str, start: datetime,
                   end: datetime, reason: str, user: models.User | None = None) -> dict:
    plan = db.get(models.ShiftPlan, plan_id)
    if not plan:
        raise NotFoundError("排程计划不存在")
    station = db.scalar(select(models.InspectionStation).where(
        models.InspectionStation.station_no == station_no))
    if not station:
        raise NotFoundError(f"设备 {station_no} 不存在")
    if end <= start:
        raise BusinessRuleError("停机结束时间必须晚于开始时间")
    db.add(models.StationDowntime(station_id=station.id, start_at=start, end_at=end,
                                  reason=reason, created_by=user.id if user else None))
    db.flush()

    kept, reflow_ids = [], set()
    steps = db.scalars(select(models.InspectionStep).where(
        models.InspectionStep.plan_id == plan.id,
        models.InspectionStep.station_id == station.id,
        models.InspectionStep.status != "cancelled",
    ).order_by(models.InspectionStep.planned_start)).all()
    for step in steps:
        if step.status in ("done", "failed", "in_progress", "redo_required"):
            kept.append({"step_id": step.id, "pack": step.pack_serial, "kind": step.kind,
                         "status": step.status, "kept": True,
                         "reason": "已完成/进行中，保持实际时间不动"})
            continue
        interval = _step_interval(step)
        if interval and (_overlap(interval, (start, end)) or step.planned_start < end):
            reflow_ids.add(step.id)
            step.status = "planned"
            # 已完成步骤推不动，未开始步骤统一重排
    # 下游依赖步骤（同一包、被影响步骤之后）也需要顺延
    if reflow_ids:
        for dep in db.scalars(select(models.InspectionStep).where(
                models.InspectionStep.plan_id == plan.id,
                models.InspectionStep.status.in_(_NOT_STARTED))).all():
            if dep.depends_on_step_id in reflow_ids:
                reflow_ids.add(dep.id)
                dep.status = "planned"

    _reflow(db, plan, reflow_ids=reflow_ids)
    db.flush()
    _rebuild_handoffs(db, plan)
    affected = list(kept)
    for step in steps:
        if step.id in reflow_ids:
            affected.append({"step_id": step.id, "pack": step.pack_serial, "kind": step.kind,
                             "status": step.status, "kept": False,
                             "new_start": _iso(step.planned_start),
                             "new_end": _iso(step.planned_end)})
    for serial in sorted({s.pack_serial for s in steps}):
        _refresh_conclusion(db, plan, serial, user)
    audit.log(db, user.id if user else None, "station_downtime", "inspection_station",
              station.id, {"start": _iso(start), "end": _iso(end), "reason": reason})
    db.flush()
    return {"station": station_no, "affected": affected, "plan": detail(db, plan)}


def _reflow(db: Session, plan: models.ShiftPlan, *, reflow_ids: set[int] | None = None) -> None:
    """只重排 reflow_ids 指定的未开始步骤；其余步骤（含完成/未受影响）作为固定占用。"""
    resources = _load_resources(db, plan)
    reflow_ids = reflow_ids or set()
    fixed_station: dict[int, list[tuple[datetime, datetime]]] = {
        s["id"]: list(resources["downtime"].get(s["id"], [])) for s in resources["stations"]}
    fixed_inspector: dict[int, list[tuple[datetime, datetime]]] = {
        i["id"]: [] for i in resources["inspectors"]}
    pending: list[models.InspectionStep] = []
    for step in db.scalars(select(models.InspectionStep).where(
            models.InspectionStep.plan_id == plan.id,
            models.InspectionStep.status != "cancelled")).all():
        if step.id in reflow_ids or step.status in ("planned", "blocked"):
            pending.append(step)
            continue
        interval = _step_interval(step)
        if interval and step.station_id in fixed_station:
            fixed_station[step.station_id].append(interval)
            if step.inspector_id in fixed_inspector:
                fixed_inspector[step.inspector_id].append(interval)
    pending.sort(key=lambda s: (s.planned_start or plan.shift_end, s.pack_order, s.sequence))

    for step in pending:
        min_start = step.planned_start or plan.shift_start
        if step.depends_on_step_id:
            pre = db.get(models.InspectionStep, step.depends_on_step_id)
            if pre:
                pre_end = pre.actual_end or pre.planned_end
                if pre_end and pre_end > min_start:
                    min_start = pre_end
        slot = _try_slot(step.kind, step.duration_min, min_start, resources,
                         station_busy=fixed_station, inspector_busy=fixed_inspector,
                         prefer_inspector=step.inspector_id, prefer_station=step.station_id)
        if slot:
            s, e, st, ins = slot
            step.planned_start, step.planned_end = s, e
            step.station_id, step.inspector_id = st["id"], ins["id"]
            step.status = "scheduled"
            fixed_station[st["id"]].append((s, e))
            fixed_inspector[ins["id"]].append((s, e))
        else:
            step.status = "blocked"
            step.note = (step.note + "；" if step.note else "") + "资源窗口内无可用时间槽"
    db.flush()


def apply_calibration_change(db: Session, *, plan_id: int, station_no: str,
                             new_until: datetime, reason: str,
                             user: models.User | None = None) -> dict:
    plan = db.get(models.ShiftPlan, plan_id)
    if not plan:
        raise NotFoundError("排程计划不存在")
    station = db.scalar(select(models.InspectionStation).where(
        models.InspectionStation.station_no == station_no))
    if not station:
        raise NotFoundError(f"设备 {station_no} 不存在")
    previous = station.calibrated_until
    db.add(models.CalibrationChange(station_id=station.id, previous_until=previous,
                                    new_until=new_until, reason=reason,
                                    changed_by=user.id if user else None))
    station.calibrated_until = new_until
    db.flush()

    invalidated, redone = [], []
    for res in db.scalars(select(models.StepResult).where(
            models.StepResult.plan_id == plan.id,
            models.StepResult.station_no == station_no,
            models.StepResult.valid.is_(True)).order_by(models.StepResult.id)).all():
        step = db.get(models.InspectionStep, res.step_id)
        executed_at = (step.actual_end if step and step.actual_end else res.recorded_at)
        if executed_at > new_until:
            res.valid = False
            res.invalid_reason = (
                f"设备 {station_no} 校准提前失效于 {new_until:%Y-%m-%d %H:%M}，"
                f"该结果执行于 {executed_at:%H:%M}，超出有效期须复做")
            if step:
                step.status = "redo_required"
                step.note = "校准提前失效，原结果作废，须复做"
            invalidated.append({"result_id": res.id, "pack": res.pack_serial,
                                "kind": res.kind, "executed_at": _iso(executed_at),
                                "invalid_reason": res.invalid_reason})
            redo = _make_redo_step(db, plan, res)
            redone.append({"redo_step_id": redo.id, "pack": res.pack_serial,
                           "kind": res.kind, "status": redo.status,
                           "note": redo.note})
    # 尚未开始但计划时间已越过校准有效期的步骤一并阻塞
    for step in db.scalars(select(models.InspectionStep).where(
            models.InspectionStep.plan_id == plan.id,
            models.InspectionStep.station_id == station.id,
            models.InspectionStep.status.in_(_NOT_STARTED))).all():
        if step.planned_end and step.planned_end > new_until:
            step.status = "blocked"
            step.note = "设备校准提前失效，需重新校准后执行"
    db.flush()
    for serial in sorted({r["pack"] for r in invalidated}):
        _refresh_conclusion(db, plan, serial, user)
    audit.log(db, user.id if user else None, "calibration_change", "inspection_station",
              station.id, {"new_until": _iso(new_until), "reason": reason})
    db.flush()
    return {"station": station_no, "new_until": _iso(new_until),
            "invalidated_results": invalidated, "redo_steps": redone,
            "plan": detail(db, plan)}


def _make_redo_step(db: Session, plan: models.ShiftPlan, res: models.StepResult) -> models.InspectionStep:
    original = db.get(models.InspectionStep, res.step_id)
    attempt = (original.attempt if original else 1) + 1
    redo = models.InspectionStep(
        plan_id=plan.id, pack_serial=res.pack_serial,
        pack_order=original.pack_order if original else 0,
        sequence=res.sequence, attempt=attempt, kind=res.kind,
        duration_min=original.duration_min if original else 10,
        retest_of_step_id=original.id if original else None,
        status="blocked",
        note="校准提前失效须复做；设备重新校准后方可排程",
    )
    db.add(redo)
    db.flush()
    return redo


def cancel_pack(db: Session, *, plan_id: int, pack_serial: str, reason: str,
                user: models.User | None = None) -> dict:
    plan = db.get(models.ShiftPlan, plan_id)
    if not plan:
        raise NotFoundError("排程计划不存在")
    concl = db.scalar(select(models.PackConclusion).where(
        models.PackConclusion.plan_id == plan.id,
        models.PackConclusion.pack_serial == pack_serial))
    if not concl:
        raise NotFoundError(f"计划中不存在包 {pack_serial}")
    if concl.conclusion_type == "released":
        raise BusinessRuleError(
            f"包 {pack_serial} 已签发放行结论，不能直接取消；如需撤回请走召回/复核流程",
            code="already_released")
    cancelled = 0
    for step in db.scalars(select(models.InspectionStep).where(
            models.InspectionStep.plan_id == plan.id,
            models.InspectionStep.pack_serial == pack_serial)).all():
        if step.status in _NOT_STARTED:
            step.status = "cancelled"
            cancelled += 1
    db.add(models.PackCancellation(plan_id=plan.id, pack_serial=pack_serial, reason=reason,
                                   cancelled_by=user.id if user else None))
    concl.conclusion_type = "cancelled"
    concl.summary = {"reason": reason, "cancelled_steps": cancelled}
    concl.updated_at = datetime.now(timezone.utc)
    db.flush()
    _rebuild_handoffs(db, plan)
    audit.log(db, user.id if user else None, "cancel_pack", "pack_conclusion", concl.id,
              {"pack": pack_serial, "reason": reason})
    return {"pack": pack_serial, "cancelled_steps": cancelled, "plan": detail(db, plan)}


def record_late_receipt(db: Session, *, plan_id: int, pack_serial: str, kind: str,
                        result: str, readings: dict | None = None, source_doc: str = "",
                        note: str = "", user: models.User | None = None) -> dict:
    """迟到回执：已签发结论绝不覆盖；否则登记并据此更新（失败会生成复测）。"""
    plan = db.get(models.ShiftPlan, plan_id)
    if not plan:
        raise NotFoundError("排程计划不存在")
    concl = db.scalar(select(models.PackConclusion).where(
        models.PackConclusion.plan_id == plan.id,
        models.PackConclusion.pack_serial == pack_serial))
    if not concl:
        raise NotFoundError(f"计划中不存在包 {pack_serial}")
    if kind not in ("insulation", "thermal"):
        raise BusinessRuleError("kind 只能是 insulation 或 thermal")
    _guard_cross_pack(pack_serial, readings or {})
    step = _effective_step_for_kind(db, plan, pack_serial, kind)
    if not step:
        raise NotFoundError(f"包 {pack_serial} 无 {kind} 检验步骤，迟到回执无法归属")
    if step.status == "cancelled":
        raise BusinessRuleError("该步骤已取消，迟到回执不能重新激活")

    if concl.conclusion_type == "released":
        receipt = models.LateReceipt(
            plan_id=plan.id, pack_serial=pack_serial, step_kind=kind, step_id=step.id,
            result=result, readings=readings or {}, source_doc=source_doc, applied=False,
            note="包已签发放行结论；迟到回执不覆盖既有结论，仅标记复核。原结论不变。",
            received_by=user.id if user else None,
        )
        db.add(receipt)
        concl.review_required = True
        concl.review_note = (
            (concl.review_note + "；" if concl.review_note else "")
            + f"迟到回执 {source_doc or '(无编号)'}：{pack_serial} {kind}={result}，"
              "已签发结论保持不变，请人工复核"
        )
        concl.updated_at = datetime.now(timezone.utc)
        db.flush()
        audit.log(db, user.id if user else None, "late_receipt_flagged", "pack_conclusion",
                  concl.id, {"pack": pack_serial, "kind": kind, "result": result})
        return {"applied": False, "pack": pack_serial, "kind": kind,
                "message": "已签发结论未被覆盖，已标记复核",
                "receipt_id": receipt.id, "plan": detail(db, plan)}

    # 尚未签发：登记并可据此更新后续步骤
    receipt = models.LateReceipt(
        plan_id=plan.id, pack_serial=pack_serial, step_kind=kind, step_id=step.id,
        result=result, readings=readings or {}, source_doc=source_doc, applied=True,
        note=note, received_by=user.id if user else None,
    )
    db.add(receipt)
    db.flush()
    applied_result = None
    try:
        applied_result = _apply_result(
            db, plan=plan, step=step, pack_serial=pack_serial, result=result,
            readings=readings, note=f"迟到回执 {source_doc}", user=user, source="late",
            executed_at=step.planned_end or step.actual_end)
    except BusinessRuleError as exc:
        receipt.applied = False
        receipt.note = f"迟到回执未能应用：{exc.message}"
        concl.review_required = True
        concl.review_note = (concl.review_note + "；" if concl.review_note else "") + receipt.note
    db.flush()
    return {"applied": receipt.applied, "pack": pack_serial, "kind": kind,
            "receipt_id": receipt.id,
            "message": "迟到回执已登记并应用" if receipt.applied else "迟到回执已登记但未应用，需复核",
            "plan": detail(db, plan)}


# ----------------------------------------------------------------------
# 指标与序列化
# ----------------------------------------------------------------------
def _step_interval(step: models.InspectionStep):
    s = step.actual_start or step.planned_start
    e = step.actual_end or step.planned_end
    return (s, e) if s and e else None


def compute_metrics(db: Session, plan: models.ShiftPlan) -> dict:
    resources = _load_resources(db, plan)
    steps = db.scalars(select(models.InspectionStep).where(
        models.InspectionStep.plan_id == plan.id,
        models.InspectionStep.status != "cancelled")).all()

    stations = []
    for st in resources["stations"]:
        window = (st["available_from"], st["available_to"])
        busy = [_step_interval(s) for s in steps if s.station_id == st["id"] and _step_interval(s)]
        busy = _merge([(max(a, window[0]), min(b, window[1])) for a, b in busy
                       if _overlap((a, b), window)])
        gaps = _complement(window, busy)
        stations.append({
            "station_no": st["station_no"], "kind": st["kind"],
            "available": f"{_hhmm(window[0])}-{_hhmm(window[1])}",
            "available_min": _minutes(*window),
            "busy_min": sum(_minutes(a, b) for a, b in busy),
            "idle_min": _minutes(*window) - sum(_minutes(a, b) for a, b in busy),
            "idle_gaps": [{"from": _hhmm(a), "to": _hhmm(b), "min": _minutes(a, b)}
                          for a, b in gaps],
        })

    inspectors = []
    for ins in resources["inspectors"]:
        window = (ins["available_from"], ins["available_to"])
        busy = [_step_interval(s) for s in steps if s.inspector_id == ins["id"] and _step_interval(s)]
        busy = _merge([(max(a, window[0]), min(b, window[1])) for a, b in busy
                       if _overlap((a, b), window)])
        inspectors.append({
            "code": ins["code"], "qualified": sorted(ins["qualified"]),
            "available": f"{_hhmm(window[0])}-{_hhmm(window[1])}",
            "busy_min": sum(_minutes(a, b) for a, b in busy),
            "idle_min": _minutes(*window) - sum(_minutes(a, b) for a, b in busy),
        })

    completion = None
    completed = [iv[1] for s in steps if (iv := _step_interval(s)) and s.status in ("done", "failed", "redo_required")]
    all_ends = [iv[1] for s in steps if (iv := _step_interval(s))]
    if all_ends:
        completion = max(all_ends)

    per_pack = {}
    for serial in sorted({s.pack_serial for s in steps}):
        pack_steps = [s for s in steps if s.pack_serial == serial]
        ends = [iv[1] for s in pack_steps if (iv := _step_interval(s))]
        per_pack[serial] = _hhmm(max(ends)) if ends else None

    handoffs = db.scalars(select(models.HandoffRecord).where(
        models.HandoffRecord.plan_id == plan.id)).all()
    return {
        "completion_at": _hhmm(completion),
        "completion_minutes_from_shift_start": (
            _minutes(plan.shift_start, completion) if completion else None),
        "per_pack_completion": per_pack,
        "stations": stations,
        "inspectors": inspectors,
        "handoff_count": len(handoffs),
        "executed_steps": sum(1 for s in steps if s.status in ("done", "failed")),
        "total_steps": len([s for s in steps if s.attempt == 1]),
    }


def _step_out(db: Session, step: models.InspectionStep) -> dict:
    station = db.get(models.InspectionStation, step.station_id) if step.station_id else None
    prof = db.get(models.InspectorProfile, step.inspector_id) if step.inspector_id else None
    result = db.scalar(select(models.StepResult).where(
        models.StepResult.step_id == step.id).order_by(models.StepResult.id.desc()))
    return {
        "id": step.id, "pack_serial": step.pack_serial, "sequence": step.sequence,
        "attempt": step.attempt, "kind": step.kind, "duration_min": step.duration_min,
        "station_no": station.station_no if station else None,
        "inspector_code": prof.code if prof else None,
        "planned_start": _iso(step.planned_start), "planned_end": _iso(step.planned_end),
        "planned_start_hhmm": _hhmm(step.planned_start), "planned_end_hhmm": _hhmm(step.planned_end),
        "actual_start_hhmm": _hhmm(step.actual_start), "actual_end_hhmm": _hhmm(step.actual_end),
        "status": step.status, "depends_on_step_id": step.depends_on_step_id,
        "retest_of_step_id": step.retest_of_step_id, "note": step.note,
        "result": result.result if result else None,
        "result_valid": result.valid if result else None,
        "result_source": result.source if result else None,
    }


def detail(db: Session, plan: models.ShiftPlan) -> dict:
    steps = db.scalars(select(models.InspectionStep).where(
        models.InspectionStep.plan_id == plan.id
    ).order_by(models.InspectionStep.pack_order, models.InspectionStep.sequence,
               models.InspectionStep.attempt)).all()
    results = db.scalars(select(models.StepResult).where(
        models.StepResult.plan_id == plan.id
    ).order_by(models.StepResult.id)).all()
    conclusions = db.scalars(select(models.PackConclusion).where(
        models.PackConclusion.plan_id == plan.id).order_by(models.PackConclusion.pack_serial)).all()
    handoffs = db.scalars(select(models.HandoffRecord).where(
        models.HandoffRecord.plan_id == plan.id).order_by(models.HandoffRecord.id)).all()
    late = db.scalars(select(models.LateReceipt).where(
        models.LateReceipt.plan_id == plan.id).order_by(models.LateReceipt.id)).all()
    cancellations = db.scalars(select(models.PackCancellation).where(
        models.PackCancellation.plan_id == plan.id)).all()
    return {
        "id": plan.id, "plan_no": plan.plan_no, "label": plan.label,
        "strategy": plan.strategy, "status": plan.status,
        "shift": f"{_hhmm(plan.shift_start)}-{_hhmm(plan.shift_end)}",
        "steps": [_step_out(db, s) for s in steps],
        "results": [_result_out(r) for r in results],
        "conclusions": [_conclusion_out(c) for c in conclusions],
        "handoffs": [_handoff_out(h) for h in handoffs],
        "late_receipts": [{"id": r.id, "pack": r.pack_serial, "kind": r.step_kind,
                           "result": r.result, "applied": r.applied,
                           "source_doc": r.source_doc, "note": r.note,
                           "received_at": _iso(r.created_at)} for r in late],
        "cancellations": [{"pack": c.pack_serial, "reason": c.reason,
                           "at": _iso(c.created_at)} for c in cancellations],
        "metrics": compute_metrics(db, plan),
    }


def _result_out(r: models.StepResult) -> dict:
    return {
        "id": r.id, "step_id": r.step_id, "pack_serial": r.pack_serial,
        "sequence": r.sequence, "kind": r.kind,
        "station_no": r.station_no, "station_kind": r.station_kind,
        "station_calibrated_until": _iso(r.station_calibrated_until),
        "inspector_code": r.inspector_code,
        "inspector_qualified_kinds": r.inspector_qualified_kinds,
        "prerequisite_step_id": r.prerequisite_step_id,
        "prerequisite_result": r.prerequisite_result,
        "result": r.result, "readings": r.readings, "source": r.source,
        "valid": r.valid, "invalid_reason": r.invalid_reason, "note": r.note,
        "recorded_at": _iso(r.recorded_at),
    }


def _conclusion_out(c: models.PackConclusion) -> dict:
    return {
        "pack_serial": c.pack_serial, "conclusion_type": c.conclusion_type,
        "issued_at": _iso(c.issued_at), "summary": c.summary,
        "review_required": c.review_required, "review_note": c.review_note,
        "updated_at": _iso(c.updated_at),
    }


def _handoff_out(h: models.HandoffRecord) -> dict:
    return {
        "id": h.id, "pack_serial": h.pack_serial,
        "from_inspector": h.from_inspector_code, "to_inspector": h.to_inspector_code,
        "station_kind": h.station_kind, "handoff_at": _iso(h.handoff_at),
        "handoff_at_hhmm": _hhmm(h.handoff_at), "note": h.note,
    }


def compare(db: Session, *, user: models.User | None = None) -> dict:
    plans = build_both_plans(db, user=user)
    summaries = [detail(db, p) for p in plans]
    pack = summaries[0]
    stage = summaries[1]
    return {
        "plans": summaries,
        "comparison": {
            "pack_continuous": {
                "completion_at": pack["metrics"]["completion_at"],
                "station_idle": {s["station_no"]: s["idle_min"] for s in pack["metrics"]["stations"]},
                "handoff_count": pack["metrics"]["handoff_count"],
                "per_pack_completion": pack["metrics"]["per_pack_completion"],
            },
            "equipment_stage": {
                "completion_at": stage["metrics"]["completion_at"],
                "station_idle": {s["station_no"]: s["idle_min"] for s in stage["metrics"]["stations"]},
                "handoff_count": stage["metrics"]["handoff_count"],
                "per_pack_completion": stage["metrics"]["per_pack_completion"],
            },
            "note": "两种排法均在设备校准与人员窗口内完成；按设备阶段交错更早交付"
                    "（10:15 vs 10:35），但产生换人交接（2 次 vs 0 次）。",
        },
    }


# ----------------------------------------------------------------------
# 回放：热像失败、校准提前失效、包件取消、迟到回执
# ----------------------------------------------------------------------
def _hh(dt: datetime) -> datetime:
    return dt


def replay_scenarios(db: Session, *, user: models.User | None = None) -> dict:
    """在同一份种子资源上回放四类异常，逐条输出可验收的证据。

    使用独立计划副本（two-strategy 中的 equipment_stage 计划）演示：
      1) 热像失败 → 自动生成复测，失败结果不可手改为合格；
      2) 校准提前失效 → 超出有效期的已完成结果作废并生成复做；
      3) 包件取消 → 未开始步骤取消、历史保留；
      4) 迟到回执 → 已签发结论不覆盖，仅标记复核。
    回放结束回滚，避免污染其它计划（异常均演示在一次性事务里）。
    """
    if user is None:
        user = db.scalar(select(models.User).where(models.User.username == "u_t1"))

    # 每次回放前清空旧排程并重建一份设备阶段计划
    _purge_all_scheduling(db)
    plan = build_plan(db, user=user, strategy="equipment_stage")
    plan_id = plan.id

    # --- 1) 热像失败 -> 复测 ---
    fail_run = execute_plan(db, plan_id=plan_id, user=user,
                            fail=[{"pack_serial": "P-9", "kind": "thermal"}])
    detail_after_fail = detail(db, plan)
    failed_step = next(s for s in detail_after_fail["steps"]
                       if s["pack_serial"] == "P-9" and s["kind"] == "thermal"
                       and s["attempt"] == 1)
    overwrite_blocked = None
    try:
        record_result(db, plan_id=plan_id, step_id=failed_step["id"],
                      pack_serial="P-9", result="pass", user=user)
    except BusinessRuleError as exc:
        overwrite_blocked = exc.code
    cross_pack_blocked = None
    ins_p9 = next(s for s in detail_after_fail["steps"]
                  if s["pack_serial"] == "P-9" and s["kind"] == "insulation")
    try:
        record_result(db, plan_id=plan_id, step_id=ins_p9["id"], pack_serial="P-9",
                      result="pass", readings={"serial_no": "P-8"}, user=user)
    except BusinessRuleError as exc:
        cross_pack_blocked = exc.code
    retest = next((s for s in detail_after_fail["steps"]
                   if s["pack_serial"] == "P-9" and s["kind"] == "thermal"
                   and s["attempt"] == 2), None)

    # --- 4a) 迟到回执（未签发前登记）应被应用 ---
    late_before = record_late_receipt(db, plan_id=plan_id, pack_serial="P-9", kind="thermal",
                                      result="pass", source_doc="LATE-009", user=user)

    # --- 签发放行结论 ---
    issue = issue_conclusions(db, plan_id=plan_id, user=user)
    detail_after_issue = detail(db, plan)
    p8_concl = next(c for c in detail_after_issue["conclusions"] if c["pack_serial"] == "P-8")

    # --- 4b) 已签发的 P-8 迟到热像失败回执：不得覆盖 ---
    late_after = record_late_receipt(db, plan_id=plan_id, pack_serial="P-8", kind="thermal",
                                     result="fail", source_doc="LATE-P8-FAIL", user=user)
    p8_after_late = next(c for c in detail(db, plan)["conclusions"]
                         if c["pack_serial"] == "P-8")

    # --- 2) 校准提前失效（绝缘仪 09:15 失效）---
    cal = apply_calibration_change(
        db, plan_id=plan_id, station_no="INS-1",
        new_until=_dt(9, 15), reason="校准提前失效（回放）", user=user)

    # --- 3) 包件取消（新计划中取消 P-9，历史步骤保留）---
    _purge_all_scheduling(db)
    plan2 = build_plan(db, user=user, strategy="pack_continuous")
    cancel = cancel_pack(db, plan_id=plan2.id, pack_serial="P-9",
                         reason="客户临时取消（回放）", user=user)
    detail_cancel = detail(db, plan2)
    cancelled_concl = next(c for c in detail_cancel["conclusions"]
                           if c["pack_serial"] == "P-9")

    # 清空回放数据，避免影响后续对比
    _purge_all_scheduling(db)
    db.flush()

    return {
        "thermal_failure": {
            "ran": fail_run["ran"],
            "failed_step": failed_step,
            "retest_step": retest,
            "overwrite_blocked": overwrite_blocked,
            "cross_pack_blocked": cross_pack_blocked,
            "conclusions_after_failure": [
                {"pack": c["pack_serial"], "type": c["conclusion_type"]}
                for c in detail_after_fail["conclusions"]],
        },
        "calibration_early_expiry": {
            "new_until": cal["new_until"],
            "invalidated_results": cal["invalidated_results"],
            "redo_steps": cal["redo_steps"],
        },
        "pack_cancellation": {
            "cancelled_steps": cancel["cancelled_steps"],
            "conclusion": cancelled_concl,
            "kept_steps": [s for s in detail_cancel["steps"]
                           if s["pack_serial"] == "P-9"],
        },
        "late_receipt": {
            "before_issue": {"applied": late_before["applied"], "message": late_before["message"]},
            "issued_conclusion": {"pack": p8_concl["pack_serial"],
                                  "type": p8_concl["conclusion_type"],
                                  "issued_at": p8_concl["issued_at"]},
            "after_issue": {"applied": late_after["applied"], "message": late_after["message"],
                            "conclusion_type": p8_after_late["conclusion_type"],
                            "review_required": p8_after_late["review_required"],
                            "review_note": p8_after_late["review_note"]},
            "issue_summary": issue,
        },
    }
