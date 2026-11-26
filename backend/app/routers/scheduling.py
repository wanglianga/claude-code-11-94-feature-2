"""检验排程 API：两种排法对比、结果绑定录入、复测、停机重排、校准失效、取消与迟到回执。"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_current_user, require_roles
from ..schemas import (
    CalibrationChangeIn,
    CancelPackIn,
    DowntimeIn,
    ExecutePlanIn,
    IssueConclusionsIn,
    LateReceiptIn,
    ScheduleBuildIn,
    StepResultIn,
)
from ..services import scheduling
from ..services.errors import NotFoundError

router = APIRouter(prefix="/api/scheduling", tags=["scheduling"])

INSPECTOR_ROLES = ("inspector", "safety", "admin")


def _profile(db: Session, user: models.User | None) -> models.InspectorProfile | None:
    if not user:
        return None
    return db.scalar(select(models.InspectorProfile).where(
        models.InspectorProfile.user_id == user.id))


def _plan(db: Session, plan_id: int) -> models.ShiftPlan:
    plan = db.get(models.ShiftPlan, plan_id)
    if not plan:
        raise NotFoundError("排程计划不存在")
    return plan


@router.get("/setup")
def get_setup(db: Session = Depends(get_db), _: models.User = Depends(get_current_user)):
    """设备与检验员资质参考数据（含校准有效期与资质）。"""
    stations = db.scalars(select(models.InspectionStation).order_by(
        models.InspectionStation.id)).all()
    inspectors = db.scalars(select(models.InspectorProfile).order_by(
        models.InspectorProfile.code)).all()
    return {
        "stations": [{"station_no": s.station_no, "name": s.name, "kind": s.kind,
                      "calibrated_until": s.calibrated_until.isoformat()} for s in stations],
        "inspectors": [{"code": i.code, "display_name": i.display_name,
                        "qualified_kinds": i.qualified_kinds} for i in inspectors],
        "spec": {
            "shift": "09:00-10:45",
            "stations": scheduling.DEFAULT_SPEC["stations"],
            "inspectors": scheduling.DEFAULT_SPEC["inspectors"],
            "packs": scheduling.DEFAULT_SPEC["packs"],
        },
    }


@router.post("/plans")
def build_plan(body: ScheduleBuildIn, db: Session = Depends(get_db),
               user: models.User = Depends(require_roles(*INSPECTOR_ROLES))):
    """按指定策略构建排程（重建会清空上一轮排程与回放记录）。"""
    if body.strategy == "both":
        plans = scheduling.build_both_plans(db, user=user)
    else:
        scheduling._purge_all_scheduling(db)
        plans = [scheduling.build_plan(db, user=user, strategy=body.strategy)]
    db.commit()
    return {"ok": True, "plans": [scheduling.detail(db, p) for p in plans]}


@router.get("/plans")
def list_plans(db: Session = Depends(get_db), _: models.User = Depends(get_current_user)):
    plans = db.scalars(select(models.ShiftPlan).order_by(models.ShiftPlan.id)).all()
    return [{"id": p.id, "plan_no": p.plan_no, "label": p.label, "strategy": p.strategy,
             "status": p.status} for p in plans]


@router.post("/compare")
def compare(db: Session = Depends(get_db),
            user: models.User = Depends(require_roles(*INSPECTOR_ROLES))):
    """构建两种排法并输出完成时刻、设备空等与交接记录对比。"""
    result = scheduling.compare(db, user=user)
    db.commit()
    return result


@router.post("/replay")
def replay(db: Session = Depends(get_db),
           user: models.User = Depends(require_roles(*INSPECTOR_ROLES))):
    """回放热像失败、校准提前失效、包件取消、迟到回执四类异常。"""
    result = scheduling.replay_scenarios(db, user=user)
    db.commit()
    return result


@router.get("/plans/{plan_id}")
def get_plan(plan_id: int, db: Session = Depends(get_db),
             _: models.User = Depends(get_current_user)):
    return scheduling.detail(db, _plan(db, plan_id))


@router.post("/plans/{plan_id}/execute")
def execute_plan(plan_id: int, body: ExecutePlanIn, db: Session = Depends(get_db),
                 user: models.User = Depends(require_roles(*INSPECTOR_ROLES))):
    """按排定顺序自动执行尚未开始的步骤；可注入指定失败项以触发生成复测。"""
    result = scheduling.execute_plan(db, plan_id=plan_id, user=user, fail=body.fail)
    db.commit()
    return result


@router.post("/plans/{plan_id}/results")
def submit_result(plan_id: int, body: StepResultIn, db: Session = Depends(get_db),
                  user: models.User = Depends(require_roles(*INSPECTOR_ROLES))):
    """录入单步结果：绑定包序列号、步骤、设备校准、操作者资质与前置结果。"""
    plan = _plan(db, plan_id)
    profile = _profile(db, user)
    record = scheduling.record_result(
        db, plan_id=plan.id, step_id=body.step_id, pack_serial=body.pack_serial,
        result=body.result, readings=body.readings, note=body.note,
        user=user, operator=profile,
    )
    db.commit()
    return {"ok": True, "result": scheduling._result_out(record),
            "plan": scheduling.detail(db, plan)}


@router.post("/plans/{plan_id}/results/retest")
def retest_result(plan_id: int, body: StepResultIn, db: Session = Depends(get_db),
                  user: models.User = Depends(require_roles(*INSPECTOR_ROLES))):
    """对失败步骤提交复测：写入新尝试，不修改原失败结果。"""
    plan = _plan(db, plan_id)
    profile = _profile(db, user)
    record = scheduling.record_result(
        db, plan_id=plan.id, step_id=body.step_id, pack_serial=body.pack_serial,
        result=body.result, readings=body.readings, note=body.note or "复测",
        user=user, operator=profile,
    )
    db.commit()
    return {"ok": True, "result": scheduling._result_out(record),
            "plan": scheduling.detail(db, plan)}


@router.post("/plans/{plan_id}/issue")
def issue(plan_id: int, db: Session = Depends(get_db),
          user: models.User = Depends(require_roles("safety", "admin", "inspector"))):
    """签发放行结论：失败/失效/阻塞项不会签发，只保留复测要求。"""
    result = scheduling.issue_conclusions(db, plan_id=plan_id, user=user)
    db.commit()
    return result


@router.post("/plans/{plan_id}/downtime")
def downtime(plan_id: int, body: DowntimeIn, db: Session = Depends(get_db),
             user: models.User = Depends(require_roles("safety", "admin", "inspector"))):
    """设备中途停机：只重排未开始的步骤，已完成步骤保持不动。"""
    plan = _plan(db, plan_id)
    result = scheduling.apply_downtime(
        db, plan_id=plan.id, station_no=body.station_no, start=body.start_at,
        end=body.end_at, reason=body.reason, user=user)
    db.commit()
    return result


@router.post("/plans/{plan_id}/calibration")
def calibration(plan_id: int, body: CalibrationChangeIn, db: Session = Depends(get_db),
                user: models.User = Depends(require_roles("safety", "admin"))):
    """校准提前失效：超出新有效期已完成结果标记失效并生成复做。"""
    plan = _plan(db, plan_id)
    result = scheduling.apply_calibration_change(
        db, plan_id=plan.id, station_no=body.station_no, new_until=body.new_until,
        reason=body.reason, user=user)
    db.commit()
    return result


@router.post("/plans/{plan_id}/cancel")
def cancel(plan_id: int, body: CancelPackIn, db: Session = Depends(get_db),
           user: models.User = Depends(require_roles("safety", "admin", "inspector"))):
    """包件取消：未开始步骤取消、历史结果保留；已签发结论不能直接取消。"""
    plan = _plan(db, plan_id)
    result = scheduling.cancel_pack(db, plan_id=plan.id, pack_serial=body.pack_serial,
                                    reason=body.reason, user=user)
    db.commit()
    return result


@router.post("/plans/{plan_id}/late-receipt")
def late_receipt(plan_id: int, body: LateReceiptIn, db: Session = Depends(get_db),
                 user: models.User = Depends(require_roles(*INSPECTOR_ROLES))):
    """迟到回执：已签发结论不覆盖，只登记并标记复核。"""
    plan = _plan(db, plan_id)
    result = scheduling.record_late_receipt(
        db, plan_id=plan.id, pack_serial=body.pack_serial, kind=body.kind,
        result=body.result, readings=body.readings, source_doc=body.source_doc,
        note=body.note, user=user)
    db.commit()
    return result
