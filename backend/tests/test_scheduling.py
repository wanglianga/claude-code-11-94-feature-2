"""检验排程测试：两种排法、结果绑定、复测、停机重排、校准失效、取消、迟到回执。"""
from datetime import datetime, timezone

from tests.conftest import ADMIN, h

T1 = "u_t1"
T2 = "u_t2"
SAFETY = "u_safety"

UTC = timezone.utc


def _dt(hh, mm):
    return datetime(2026, 11, 25, hh, mm, tzinfo=UTC)


def _compare(client):
    r = client.post("/api/scheduling/compare", headers=h(T1))
    assert r.status_code == 200, r.text
    return r.json()


def _plan(data, strategy):
    return next(p for p in data["plans"] if p["strategy"] == strategy)


def _step(plan, pack, kind, attempt=1):
    return next(s for s in plan["steps"]
                if s["pack_serial"] == pack and s["kind"] == kind and s["attempt"] == attempt)


def _concl(plan, pack):
    return next(c for c in plan["conclusions"] if c["pack_serial"] == pack)


def test_two_schedules_windows_and_handoffs(client):
    data = _compare(client)
    pack = _plan(data, "pack_continuous")
    stage = _plan(data, "equipment_stage")

    # 按包连续：T1 全程，10:35 完成，0 次换人
    assert pack["metrics"]["completion_at"] == "10:35"
    assert pack["metrics"]["handoff_count"] == 0
    assert _step(pack, "P-8", "insulation")["planned_start_hhmm"] == "09:00"
    assert _step(pack, "P-8", "insulation")["planned_end_hhmm"] == "09:30"
    assert _step(pack, "P-8", "thermal")["planned_end_hhmm"] == "09:50"
    assert _step(pack, "P-9", "insulation")["planned_start_hhmm"] == "09:50"
    assert _step(pack, "P-9", "thermal")["planned_end_hhmm"] == "10:35"
    assert {s["inspector_code"] for s in pack["steps"]} == {"T1"}

    # 按设备阶段交错：T1 绝缘、T2 热像，10:15 完成，2 次交接
    assert stage["metrics"]["completion_at"] == "10:15"
    assert stage["metrics"]["handoff_count"] == 2
    assert _step(stage, "P-8", "thermal")["inspector_code"] == "T2"
    assert _step(stage, "P-8", "thermal")["planned_start_hhmm"] == "09:30"
    assert _step(stage, "P-9", "insulation")["planned_start_hhmm"] == "09:30"
    assert _step(stage, "P-9", "thermal")["planned_end_hhmm"] == "10:15"
    handoffs = {(x["pack_serial"], x["from_inspector"], x["to_inspector"]) for x in stage["handoffs"]}
    assert ("P-8", "T1", "T2") in handoffs and ("P-9", "T1", "T2") in handoffs

    # 两种排法都在设备可用窗口与人员下班时间内
    for plan in (pack, stage):
        for s in plan["steps"]:
            end = s["planned_end_hhmm"]
            assert end <= "10:40"
            if s["kind"] == "thermal":
                assert end <= "10:40"
            else:
                assert end <= "10:20"

    # 设备空等：按设备阶段交错时绝缘仪在 P-9 检验后空等 30 分钟
    ins_idle = next(x for x in stage["metrics"]["stations"] if x["station_no"] == "INS-1")
    assert ins_idle["idle_min"] == 30
    assert {"from": "09:50", "to": "10:20", "min": 30} in ins_idle["idle_gaps"]


def test_result_binds_pack_step_device_calibration_qualification_prerequisite(client):
    data = _compare(client)
    plan = _plan(data, "equipment_stage")
    thermal_p9 = _step(plan, "P-9", "thermal")
    r = client.post(f"/api/scheduling/plans/{plan['id']}/results", headers=h(T2), json={
        "step_id": thermal_p9["id"], "pack_serial": "P-9", "result": "pass"})
    assert r.status_code == 409  # 前置绝缘结果尚未录入

    ins_p9 = _step(plan, "P-9", "insulation")
    r = client.post(f"/api/scheduling/plans/{plan['id']}/results", headers=h(T1), json={
        "step_id": ins_p9["id"], "pack_serial": "P-9", "result": "pass"})
    assert r.status_code == 200
    r = client.post(f"/api/scheduling/plans/{plan['id']}/results", headers=h(T2), json={
        "step_id": thermal_p9["id"], "pack_serial": "P-9", "result": "pass"})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["pack_serial"] == "P-9"
    assert result["step_id"] == thermal_p9["id"]
    assert result["sequence"] == 2 and result["kind"] == "thermal"
    assert result["station_no"] == "THM-1"
    assert result["station_calibrated_until"].startswith("2027-01-04")
    assert result["inspector_code"] == "T2"
    assert result["inspector_qualified_kinds"] == ["thermal"]
    assert result["prerequisite_result"] == "pass"
    assert result["prerequisite_step_id"] == ins_p9["id"]


def test_cross_pack_and_overwrite_rejected(client):
    data = _compare(client)
    plan = _plan(data, "pack_continuous")
    ins_p8 = _step(plan, "P-8", "insulation")
    # 把 P-8 的读数提交到 P-9 的步骤 -> 拒绝
    r = client.post(f"/api/scheduling/plans/{plan['id']}/results", headers=h(T1), json={
        "step_id": ins_p8["id"], "pack_serial": "P-9", "result": "pass"})
    assert r.status_code == 409 and r.json()["code"] == "cross_pack_reading"
    # 读数里夹带另一包序列号 -> 拒绝
    r = client.post(f"/api/scheduling/plans/{plan['id']}/results", headers=h(T1), json={
        "step_id": ins_p8["id"], "pack_serial": "P-8", "result": "pass",
        "readings": {"serial_no": "P-9"}})
    assert r.status_code == 409 and r.json()["code"] == "cross_pack_reading"
    # 正常录入后再覆盖 -> 拒绝
    ok = client.post(f"/api/scheduling/plans/{plan['id']}/results", headers=h(T1), json={
        "step_id": ins_p8["id"], "pack_serial": "P-8", "result": "pass"})
    assert ok.status_code == 200
    over = client.post(f"/api/scheduling/plans/{plan['id']}/results", headers=h(T1), json={
        "step_id": ins_p8["id"], "pack_serial": "P-8", "result": "fail"})
    assert over.status_code == 409 and over.json()["code"] == "result_immutable"


def test_thermal_failure_generates_retest_not_manual_pass(client):
    data = _compare(client)
    plan = _plan(data, "equipment_stage")
    r = client.post(f"/api/scheduling/plans/{plan['id']}/execute", headers=h(T1), json={
        "fail": [{"pack_serial": "P-9", "kind": "thermal"}]})
    assert r.status_code == 200
    fresh = r.json()["plan"]
    failed = _step(fresh, "P-9", "thermal", attempt=1)
    assert failed["status"] == "failed" and failed["result"] == "fail"
    retest = _step(fresh, "P-9", "thermal", attempt=2)
    assert retest["status"] == "scheduled"
    assert retest["retest_of_step_id"] == failed["id"]
    assert retest["planned_start_hhmm"] >= "10:15"
    # 结论：P-9 待复测，P-8 就绪
    assert _concl(fresh, "P-8")["conclusion_type"] == "released_ready"
    assert _concl(fresh, "P-9")["conclusion_type"] == "retest_required"

    # 签发：P-9 不签发，P-8 签发
    issue = client.post(f"/api/scheduling/plans/{plan['id']}/issue", headers=h(SAFETY))
    assert issue.status_code == 200
    issued = {x["pack"] for x in issue.json()["issued"]}
    assert issued == {"P-8"}
    assert {x["pack"] for x in issue.json()["blocked"]} == {"P-9"}

    # 复测是新增尝试，原失败记录保留
    retest_pass = client.post(f"/api/scheduling/plans/{plan['id']}/results/retest",
                              headers=h(T1), json={
                                  "step_id": retest["id"], "pack_serial": "P-9",
                                  "result": "pass"})
    assert retest_pass.status_code == 200
    final = retest_pass.json()["plan"]
    history = [a for a in _concl(final, "P-9")["summary"]["kinds"]["thermal"]["attempts"]]
    assert history[0]["result"] == "fail" and history[0]["valid"] is True
    assert history[1]["result"] == "pass"
    assert _concl(final, "P-9")["conclusion_type"] == "released_ready"


def test_downtime_reschedules_only_not_started(client):
    data = _compare(client)
    plan = _plan(data, "equipment_stage")
    pid = plan["id"]
    # 先执行绝缘与 P-8 热像，再让热像舱 09:50-10:10 停机
    client.post(f"/api/scheduling/plans/{pid}/execute", headers=h(T1), json={
        "fail": [{"pack_serial": "P-8", "kind": "thermal"}]})
    r = client.post(f"/api/scheduling/plans/{pid}/downtime", headers=h(SAFETY), json={
        "station_no": "THM-1", "start_at": _dt(10, 15).isoformat(),
        "end_at": _dt(10, 30).isoformat(), "reason": "热像舱临时维修"})
    assert r.status_code == 200
    affected = r.json()["affected"]
    kept = [a for a in affected if a["kept"]]
    # 已完成的热像步骤保持不动
    assert all(a["status"] in ("done", "failed", "in_progress") for a in kept)
    fresh = r.json()["plan"]
    p8_thermal = _step(fresh, "P-8", "thermal")
    assert p8_thermal["actual_end_hhmm"] == p8_thermal["planned_end_hhmm"]
    assert p8_thermal["status"] == "failed"


def test_downtime_before_start_reflows_pending(client):
    data = _compare(client)
    plan = _plan(data, "equipment_stage")
    pid = plan["id"]
    # 热像舱 09:30-09:50 停机：P-8 热像应重排到 09:50 之后
    r = client.post(f"/api/scheduling/plans/{pid}/downtime", headers=h(SAFETY), json={
        "station_no": "THM-1", "start_at": _dt(9, 30).isoformat(),
        "end_at": _dt(9, 50).isoformat(), "reason": "热像舱故障"})
    assert r.status_code == 200
    fresh = r.json()["plan"]
    p8_thermal = _step(fresh, "P-8", "thermal")
    assert p8_thermal["planned_start_hhmm"] >= "09:50"
    # 该步骤未开始，所以没有实际时间
    assert p8_thermal["actual_start_hhmm"] is None


def test_calibration_early_expiry_invalidates_and_redoes(client):
    data = _compare(client)
    plan = _plan(data, "equipment_stage")
    pid = plan["id"]
    client.post(f"/api/scheduling/plans/{pid}/execute", headers=h(T1), json={})
    # 绝缘仪校准提前失效到 09:15：09:00-09:30 与 09:30-09:50 的绝缘结果均超出
    r = client.post(f"/api/scheduling/plans/{pid}/calibration", headers=h(SAFETY), json={
        "station_no": "INS-1", "new_until": _dt(9, 15).isoformat(),
        "reason": "校准提前失效"})
    assert r.status_code == 200
    body = r.json()
    assert {x["pack"] for x in body["invalidated_results"]} == {"P-8", "P-9"}
    assert all("超出有效期" in x["invalid_reason"] for x in body["invalidated_results"])
    assert {x["pack"] for x in body["redo_steps"]} == {"P-8", "P-9"}
    fresh = body["plan"]
    assert _step(fresh, "P-8", "insulation")["status"] == "redo_required"
    assert _concl(fresh, "P-8")["conclusion_type"] == "retest_required"
    # 无法签发
    issue = client.post(f"/api/scheduling/plans/{pid}/issue", headers=h(SAFETY))
    assert {x["pack"] for x in issue.json()["issued"]} == set()


def test_cancel_pack_keeps_history_and_released_immutable(client):
    data = _compare(client)
    plan = _plan(data, "pack_continuous")
    pid = plan["id"]
    # 取消 P-9（未开始）
    r = client.post(f"/api/scheduling/plans/{pid}/cancel", headers=h(SAFETY), json={
        "pack_serial": "P-9", "reason": "客户临时取消"})
    assert r.status_code == 200
    assert r.json()["cancelled_steps"] == 2
    fresh = r.json()["plan"]
    assert _concl(fresh, "P-9")["conclusion_type"] == "cancelled"
    assert all(s["status"] == "cancelled"
               for s in fresh["steps"] if s["pack_serial"] == "P-9")
    # P-8 不受影响
    assert _step(fresh, "P-8", "insulation")["status"] == "scheduled"

    # 已签发后取消应被拒绝
    client.post(f"/api/scheduling/plans/{pid}/execute", headers=h(T1), json={})
    client.post(f"/api/scheduling/plans/{pid}/issue", headers=h(SAFETY))
    blocked = client.post(f"/api/scheduling/plans/{pid}/cancel", headers=h(SAFETY), json={
        "pack_serial": "P-8", "reason": "试图取消"})
    assert blocked.status_code == 409 and blocked.json()["code"] == "already_released"


def test_late_receipt_never_overwrites_released_conclusion(client):
    data = _compare(client)
    plan = _plan(data, "equipment_stage")
    pid = plan["id"]
    client.post(f"/api/scheduling/plans/{pid}/execute", headers=h(T1), json={})
    issue = client.post(f"/api/scheduling/plans/{pid}/issue", headers=h(SAFETY)).json()
    issued_at = {x["pack"]: x["issued_at"] for x in issue["issued"]}
    assert set(issued_at) == {"P-8", "P-9"}

    r = client.post(f"/api/scheduling/plans/{pid}/late-receipt", headers=h(T1), json={
        "pack_serial": "P-8", "kind": "thermal", "result": "fail",
        "source_doc": "LATE-P8-001"})
    assert r.status_code == 200
    assert r.json()["applied"] is False
    fresh = r.json()["plan"]
    p8 = _concl(fresh, "P-8")
    assert p8["conclusion_type"] == "released"
    assert p8["issued_at"] == issued_at["P-8"]
    assert p8["review_required"] is True
    assert "LATE-P8-001" in p8["review_note"]
    # 原热像结果仍是合格
    thermal = [x for x in fresh["results"]
               if x["pack_serial"] == "P-8" and x["kind"] == "thermal"]
    assert all(x["result"] == "pass" and x["valid"] for x in thermal)
    assert fresh["late_receipts"][-1]["applied"] is False


def test_late_receipt_before_issue_is_applied(client):
    data = _compare(client)
    plan = _plan(data, "equipment_stage")
    pid = plan["id"]
    client.post(f"/api/scheduling/plans/{pid}/execute", headers=h(T1), json={})
    r = client.post(f"/api/scheduling/plans/{pid}/late-receipt", headers=h(T1), json={
        "pack_serial": "P-9", "kind": "thermal", "result": "fail",
        "source_doc": "LATE-P9-002"})
    assert r.status_code == 200
    assert r.json()["applied"] is True
    fresh = r.json()["plan"]
    assert _concl(fresh, "P-9")["conclusion_type"] == "retest_required"
    # 迟到失败同样生成复测，而不是把原结果改成合格
    assert any(s["pack_serial"] == "P-9" and s["kind"] == "thermal" and s["attempt"] == 2
               for s in fresh["steps"])


def test_unqualified_inspector_rejected(client):
    data = _compare(client)
    plan = _plan(data, "equipment_stage")
    ins_p8 = _step(plan, "P-8", "insulation")
    # T2 只具热像资质，不能做绝缘
    r = client.post(f"/api/scheduling/plans/{plan['id']}/results", headers=h(T2), json={
        "step_id": ins_p8["id"], "pack_serial": "P-8", "result": "pass"})
    assert r.status_code == 409 and r.json()["code"] == "inspector_not_qualified"


def test_replay_endpoint_covers_four_scenarios(client):
    r = client.post("/api/scheduling/replay", headers=h(T1))
    assert r.status_code == 200
    body = r.json()
    assert body["thermal_failure"]["overwrite_blocked"] == "result_immutable"
    assert body["thermal_failure"]["cross_pack_blocked"] == "cross_pack_reading"
    assert body["thermal_failure"]["retest_step"]["attempt"] == 2
    assert len(body["calibration_early_expiry"]["invalidated_results"]) == 2
    assert body["pack_cancellation"]["cancelled_steps"] == 2
    assert body["late_receipt"]["after_issue"]["applied"] is False
    assert body["late_receipt"]["after_issue"]["conclusion_type"] == "released"
    assert body["late_receipt"]["after_issue"]["review_required"] is True
    assert body["late_receipt"]["before_issue"]["applied"] is True
