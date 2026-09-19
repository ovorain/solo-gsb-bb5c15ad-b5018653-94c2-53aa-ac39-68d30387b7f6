"""FastAPI 入口：API + 原生静态页面。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .core import (
    Config,
    duration_audit,
    fare_path_audit,
    parse_fares_text,
    parse_records_text,
    parse_stations_text,
    repair_preview,
    station_peak_audit,
    trial_compare,
    validate_config,
    verify,
)
from .schemas import ConfigIn, PeakAuditIn, TextIn, TrialConfigIn

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="公交刷卡票价核验台",
    description="录入站点分区、票价矩阵、换乘抵扣、每日封顶与刷卡记录，"
                "按卡号配对进出站并核验计价。",
    version="1.4.0",
    lifespan=lifespan,
)


def _parse_error_response(errors: list[dict], status: int = 422) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"ok": False, "errors": errors})


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@app.get("/api/config")
def get_config():
    with db.connect() as conn:
        cfg = db.get_config(conn)
    return {
        "transfer_minutes": cfg.transfer_minutes,
        "transfer_discount": cfg.transfer_discount,
        "daily_cap": cfg.daily_cap,
        "day_split_hour": cfg.day_split_hour,
    }


@app.put("/api/config")
def put_config(body: ConfigIn):
    cfg = Config(
        transfer_minutes=body.transfer_minutes,
        transfer_discount=body.transfer_discount,
        daily_cap=body.daily_cap,
        day_split_hour=body.day_split_hour,
    )
    with db.connect() as conn:
        db.update_config(conn, cfg)
        # 规则参数变化后，旧账单可能已不再成立，标记为失效
        db.clear_bills(conn)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 站点分区
# ---------------------------------------------------------------------------

@app.get("/api/stations")
def get_stations():
    with db.connect() as conn:
        return {"items": db.list_stations(conn)}


@app.put("/api/stations")
def put_stations(body: TextIn):
    stations, errors = parse_stations_text(body.text)
    if errors:
        return _parse_error_response(errors)
    with db.connect() as conn:
        db.replace_stations(conn, stations)
        db.clear_bills(conn)
    return {"ok": True, "count": len(stations)}


# ---------------------------------------------------------------------------
# 票价矩阵
# ---------------------------------------------------------------------------

@app.get("/api/fares")
def get_fares():
    with db.connect() as conn:
        return {"items": db.list_fares(conn)}


@app.put("/api/fares")
def put_fares(body: TextIn):
    fares, errors = parse_fares_text(body.text)
    if errors:
        return _parse_error_response(errors)
    with db.connect() as conn:
        db.replace_fares(conn, fares)
        db.clear_bills(conn)
    return {"ok": True, "count": len(fares)}


# ---------------------------------------------------------------------------
# 刷卡记录
# ---------------------------------------------------------------------------

@app.get("/api/records")
def get_records():
    with db.connect() as conn:
        return {"items": db.list_records(conn)}


@app.put("/api/records")
def put_records(body: TextIn):
    default_year = datetime.now().year
    records, errors = parse_records_text(body.text, default_year=default_year)
    if errors:
        return _parse_error_response(errors)
    with db.connect() as conn:
        db.replace_records(conn, records)
        db.clear_bills(conn)
    return {"ok": True, "count": len(records)}


# ---------------------------------------------------------------------------
# 核验
# ---------------------------------------------------------------------------

@app.post("/api/verify")
def run_verify():
    """执行全量核验。

    成功：清空旧账单并写入新账单（同一事务）。
    失败：返回 422 与全部定位错误，不写入/保留任何账单（无部分账单）。
    """
    with db.connect() as conn:
        cfg = db.get_config(conn)
        stations, fares, records = db.load_domain(conn)
        result = verify(stations, fares, records, cfg)
        if not result.ok:
            db.clear_bills(conn)
            return JSONResponse(
                status_code=422,
                content={"ok": False,
                         "errors": result.errors,
                         "bill_count": 0})
        db.save_bills(conn, result.bills)
        return {"ok": True,
                "bill_count": len(result.bills),
                "bills": result.bills}


@app.get("/api/bills")
def get_bills():
    with db.connect() as conn:
        bills = db.list_bills(conn)
        last = db.get_meta(conn, "last_verified_at")
    return {"items": bills, "count": len(bills), "last_verified_at": last}


# ---------------------------------------------------------------------------
# 规则试算对比（只读：不改配置、不写账单）
# ---------------------------------------------------------------------------

@app.post("/api/trial-compare")
def run_trial_compare(body: TrialConfigIn):
    """用一组未保存的草案参数，对同一批已保存刷卡记录做双边试算。

    - 当前配置（已保存）与草案配置分别复用 core.verify 的配对 / 计价规则；
    - 任一方案存在错误（CONFIG_BAD 或数据错误）时返回 422，
      current / draft 各自携带该方案的原有定位错误，不修改任何数据；
    - 两边均成功时返回按 (卡号, 卡内序号) 对齐的行程行、
      每张卡汇总与全批次汇总。
    """
    draft_cfg = Config(
        transfer_minutes=body.transfer_minutes,
        transfer_discount=body.transfer_discount,
        daily_cap=body.daily_cap,
        day_split_hour=body.day_split_hour,
    )
    with db.connect() as conn:
        current_cfg = db.get_config(conn)
        stations, fares, records = db.load_domain(conn)
        # 草案参数先按核心规则校验（错误返回原有 CONFIG_BAD）；
        # 合法时把整数型字段规整为 int，使运营日/分钟相关文案与正式配置口径一致。
        draft_errors = validate_config(draft_cfg)
        if not draft_errors:
            draft_cfg = Config(
                transfer_minutes=int(draft_cfg.transfer_minutes),
                transfer_discount=draft_cfg.transfer_discount,
                daily_cap=draft_cfg.daily_cap,
                day_split_hour=int(draft_cfg.day_split_hour),
            )
        result = trial_compare(stations, fares, records, current_cfg, draft_cfg)

    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={"ok": False,
                     "current": result.current,
                     "draft": result.draft})

    return {
        "ok": True,
        "current": result.current,
        "draft": result.draft,
        "rows": result.rows,
        "card_summaries": result.card_summaries,
        "batch_summary": result.batch_summary,
    }


# ---------------------------------------------------------------------------
# 配对修复预览（只读：不改记录、不写/清空账单）
# ---------------------------------------------------------------------------

@app.post("/api/repair-preview")
def run_repair_preview():
    """对已保存刷卡记录生成「可完整配对」的最小编辑修复预览。

    - 仅针对缺失进站 / 缺失出站 / 连续同向刷卡（MISSING_ENTRY /
      MISSING_EXIT）：按卡号 + 时间排序后做全局最小编辑搜索，
      编辑仅限「翻转进/出方向」与「删除记录」，时刻与站点不改写；
    - 同成本方案按稳定顺序最多返回 5 个，并标明最小编辑数与
      未展示的方案数量，不替用户选定任何方案；
    - 存在非配对错误（未知站点、缺票价、时间逆序、重复刷卡等）时
      返回 422 与 verify() 的原有定位错误，不生成修复方案；
    - 全程只读：不保存记录、不写入也不清空账单。
    """
    with db.connect() as conn:
        cfg = db.get_config(conn)
        stations, fares, records = db.load_domain(conn)
        result = repair_preview(stations, fares, records, cfg)

    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "errors": result.errors})

    return {
        "ok": True,
        "min_edits": result.min_edits,
        "total_plans": result.total_plans,
        "shown_plans": result.shown_plans,
        "hidden_plans": result.hidden_plans,
        "plans": result.plans,
    }


# ---------------------------------------------------------------------------
# 票价矩阵路径审计（只读：不改矩阵、不写/清空账单）
# ---------------------------------------------------------------------------

@app.post("/api/fare-path-audit")
def run_fare_path_audit():
    """对已保存数据做票价矩阵路径审计。

    - 以分区为节点、已配置的有向票价为边，对已保存刷卡记录配对出的
      实际行程分区对计算最低成本路径（整数分 Dijkstra，相同总价按
      分区序列稳定选路，零价边与非对称方向按图语义处理）；
    - 缺直达票价但可达：给出建议票价、完整路径、建议补录行及
      受影响卡号与原始行号；不可达：保留原 MISSING_FARE 定位错误；
    - 已有直达票价高于间接路径：标出差额（直达 − 间接最低）；
    - 除 MISSING_FARE 外的数据错误：返回 422 与 verify() 的原有定位错误；
    - 全程只读：不修改票价矩阵、不写入也不清空账单。
    """
    with db.connect() as conn:
        cfg = db.get_config(conn)
        stations, fares, records = db.load_domain(conn)
        result = fare_path_audit(stations, fares, records, cfg)

    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "errors": result.errors})

    return {
        "ok": True,
        "trip_count": result.trip_count,
        "pair_count": result.pair_count,
        "missing": result.missing,
        "overpriced": result.overpriced,
    }


# ---------------------------------------------------------------------------
# 行程耗时异常审计（只读：不改记录、配置或账单）
# ---------------------------------------------------------------------------

@app.post("/api/duration-audit")
def run_duration_audit():
    """对已保存刷卡记录做行程耗时异常审计。

    - 按（进站站点 -> 出站站点）的有向站点对分组，耗时 = 出站时刻 - 进站时刻；
      仅样本数 >= 6 的站点对参与判定，样本不足的站点对单独列出；
    - 每条行程从基线中排除自身，以其余样本的中位数与 MAD 计算阈值
      max(10 分钟, 3 × 1.4826 × MAD)，绝对偏差严格大于阈值时标记为
      过长 / 过短，并给出基线样本数、中位数、MAD、阈值、偏差与原始进出行号；
    - 配对 / 时间类错误（缺失进出站、时间逆序、重复刷卡）返回 422 与
      verify() 的原有定位错误；未知站点与票价缺项不阻断审计；
    - 全程只读：不修改记录、配置，也不写入或清空账单。
    """
    with db.connect() as conn:
        cfg = db.get_config(conn)
        stations, fares, records = db.load_domain(conn)
        result = duration_audit(stations, fares, records, cfg)

    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "errors": result.errors})

    return {
        "ok": True,
        "trip_count": result.trip_count,
        "pair_count": result.pair_count,
        "audited_pairs": result.audited_pairs,
        "min_samples": result.min_samples,
        "anomaly_count": len(result.anomalies),
        "anomalies": result.anomalies,
        "insufficient": result.insufficient,
    }


# ---------------------------------------------------------------------------
# 站点闸机峰值审计（只读：不改记录、配置或账单）
# ---------------------------------------------------------------------------

@app.post("/api/station-peak-audit")
def run_station_peak_audit(body: PeakAuditIn):
    """对已保存刷卡记录做站点闸机峰值审计。

    - 按（站点, 进/出方向）分组，在 1～120 分钟的给定窗口内查找
      刷卡数最多的半开区间 [开始, 结束)，候选窗口只从实际刷卡时刻起算；
    - 每组返回峰值刷卡数、起止时刻、涉及卡号数与原始文本行号；
      峰值数量并列时依次按更早开始时刻、站点名排序；
    - 峰值刷卡数 >= min_alert 的组标记 alert=true（最低告警数）；
    - 直接统计已保存刷卡记录，不依赖进出站配对、站点分区与票价；
    - 全程只读：不修改记录、配置，也不写入或清空账单。
    """
    with db.connect() as conn:
        _stations, _fares, records = db.load_domain(conn)
        result = station_peak_audit(
            records, body.window_minutes, body.min_alert)

    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "errors": result.errors})

    return {
        "ok": True,
        "window_minutes": result.window_minutes,
        "min_alert": result.min_alert,
        "record_count": result.record_count,
        "group_count": result.group_count,
        "alert_count": result.alert_count,
        "groups": result.groups,
    }


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------------------
# 原生前端（静态资源）
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
