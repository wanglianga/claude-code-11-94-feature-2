"""周三检验班排程命令行演示（P-8 / P-9）。

    DATABASE_URL=postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/battery_demo \
      .venv/bin/python demo_scheduling.py

输出两种排法的完成时刻、设备空等、交接记录，并回放热像失败、校准提前失效、
包件取消、迟到回执四类异常。脚本会重建库表，演示数据不进入生产配置。
"""
from sqlalchemy import select

from app import models
from app.database import Base, SessionLocal, engine
from app.services import scheduling


def _print_plan(plan: dict) -> None:
    m = plan["metrics"]
    print(f"  策略：{plan['label']}  完成时刻：{m['completion_at']}"
          f"（班次起 +{m['completion_minutes_from_shift_start']} 分），"
          f"交接 {m['handoff_count']} 次")
    for s in plan["steps"]:
        if s["attempt"] != 1:
            continue
        print(f"    {s['pack_serial']:4} {s['kind']:10} "
              f"{s['planned_start_hhmm']}-{s['planned_end_hhmm']} "
              f"{s['station_no']:5} {s['inspector_code']}")
    for st in m["stations"]:
        gaps = "，".join(f"{g['from']}-{g['to']}" for g in st["idle_gaps"]) or "无"
        print(f"    设备 {st['station_no']}: 可用 {st['available']}，占用 {st['busy_min']} 分，"
              f"空等 {st['idle_min']} 分（{gaps}）")
    for h in plan["handoffs"]:
        print(f"    交接：{h['pack_serial']} {h['from_inspector']}→{h['to_inspector']} "
              f"于 {h['handoff_at_hhmm']}")


def main():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    scheduling.ensure_shift_setup(db)
    db.commit()

    print("=" * 74)
    print("一、两种排法对比（完成时刻 / 设备空等 / 交接记录）")
    result = scheduling.compare(db, user=None)
    db.commit()
    for plan in result["plans"]:
        _print_plan(plan)
    print("  结论：", result["comparison"]["note"])

    print("=" * 74)
    print("二、异常回放（热像失败 / 校准提前失效 / 包件取消 / 迟到回执）")
    replay = scheduling.replay_scenarios(db, user=None)
    db.commit()

    tf = replay["thermal_failure"]
    print(f"  1) 热像失败：{tf['failed_step']['pack_serial']} {tf['failed_step']['kind']} "
          f"→ {tf['failed_step']['status']}；自动生成复测（第 {tf['retest_step']['attempt']} 次，"
          f"{tf['retest_step']['status']}）。")
    print(f"     手工改合格被拒：{tf['overwrite_blocked']}；跨包读数被拒：{tf['cross_pack_blocked']}。")
    print(f"     失败后结论：{tf['conclusions_after_failure']}")

    cal = replay["calibration_early_expiry"]
    print(f"  2) 校准提前失效（新有效期 {cal['new_until']}）：")
    for x in cal["invalidated_results"]:
        print(f"     {x['pack']} {x['kind']} 执行于 {x['executed_at'][11:16]} → 失效并复做")
    print(f"     复做步骤状态：{[x['status'] for x in cal['redo_steps']]}")

    cancel = replay["pack_cancellation"]
    print(f"  3) 包件取消：P-9 取消 {cancel['cancelled_steps']} 个未开始步骤，"
          f"结论 {cancel['conclusion']['conclusion_type']}，历史步骤保留。")

    late = replay["late_receipt"]
    print(f"  4) 迟到回执：签发前 applied={late['before_issue']['applied']}；"
          f"签发后 P-8 结论仍为 {late['after_issue']['conclusion_type']}，"
          f"review_required={late['after_issue']['review_required']}")
    print(f"     {late['after_issue']['review_note']}")
    print("=" * 74)
    db.close()


if __name__ == "__main__":
    main()
