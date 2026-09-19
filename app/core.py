"""公交刷卡票价核验核心逻辑。

纯标准库实现：文本解析、记录校验、进出站配对、
换乘抵扣与运营日封顶计算。不依赖 FastAPI / SQLite，便于单独测试。

金额在内部统一以「元」的浮点数表示并保留两位小数；
存储层写入 SQLite 时转换为整数分，避免二进制浮点误差。
"""
from __future__ import annotations

import heapq
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

# 允许的文本分隔符：逗号、制表符、分号、空白(多空格)、全角逗号
_SPLIT_RE = re.compile(r"[,\t;，]|\s+")

# 日期与时刻之间可能用空格连接（如 "2026-09-18 08:00:00"），
# 而空格本身又是字段分隔符，切分前先把时间戳内部空白替换为 T。
# 形如：YYYY[-/]MM[-/]DD HH:MM[:SS] 或 MM[-/]DD HH:MM
_DATETIME_RE = re.compile(
    r"\d{2,4}[-/]\d{1,2}(?:[-/]\d{1,2})?[ \t]+\d{1,2}:\d{2}(?::\d{2})?"
)

# 支持的进站时刻格式
_TIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%dT%H:%M:%S",
    "%Y/%m/%dT%H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%m-%dT%H:%M",       # 缺省年份，补当前年
    "%m-%d %H:%M",
    "%m/%dT%H:%M",
    "%m/%d %H:%M",
)

ACTION_IN = "IN"
ACTION_OUT = "OUT"
_ACTION_ALIASES = {
    "IN": "IN", "进": "IN", "进站": "IN", "入": "IN", "上": "IN",
    "OUT": "OUT", "出": "OUT", "出站": "OUT", "下": "OUT",
}


def _err(code: str, message: str, line: Optional[int] = None,
         card: Optional[str] = None, time: Optional[str] = None,
         station: Optional[str] = None) -> dict:
    """构造定位错误对象。行号 / 卡号 / 时刻 / 站点尽量补齐，便于定位。"""
    loc: dict = {}
    if line is not None:
        loc["line"] = line
    if card:
        loc["card"] = card
    if time:
        loc["time"] = time
    if station:
        loc["station"] = station
    e: dict = {"code": code, "message": message}
    if loc:
        e["location"] = loc
    return e


def _split(line: str) -> list[str]:
    """按分隔符切分一行文本，去掉空白段。"""
    return [p for p in _SPLIT_RE.split(line.strip()) if p != ""]


def _round2(x: float) -> float:
    return round(float(x) + 1e-9, 2)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Station:
    name: str
    zone: int
    line: int = 1  # 保存时的原始行号，用于错误定位


@dataclass
class Fare:
    from_zone: int
    to_zone: int
    price: float
    line: int = 1


@dataclass
class Swipe:
    card: str
    t: datetime
    station: str
    action: str          # IN / OUT
    line: int = 1        # 文本行号（按卡号排序校验时用于定位）

    @property
    def time_text(self) -> str:
        return self.t.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Config:
    transfer_minutes: int = 90          # 换乘有效间隔（分钟）
    transfer_discount: float = 1.0      # 换乘抵扣额（元）
    daily_cap: float = 10.0             # 每卡每运营日封顶额（元）
    day_split_hour: int = 4             # 运营日分界时刻


@dataclass
class VerifyResult:
    ok: bool
    bills: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    # 配对成功的行程 (卡号, 卡内序号, 进站记录, 出站记录)，内部使用：
    # 即使核验失败（如 MISSING_FARE）也会带出，供路径审计复用。
    trips: list = field(default_factory=list)


@dataclass
class TrialResult:
    """规则试算对比结果。

    ok 为 False 时 current / draft 两个分块各自携带错误数组（原有定位错误）；
    ok 为 True 时携带对齐后的行程行 rows、按卡汇总 card_summaries 与
    全批次汇总 batch_summary。
    """
    ok: bool
    current: dict = field(default_factory=dict)
    draft: dict = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    card_summaries: list[dict] = field(default_factory=list)
    batch_summary: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 文本解析（录入）
# ---------------------------------------------------------------------------

def parse_time(text: str, default_year: Optional[int] = None) -> datetime:
    """解析刷卡时刻，支持多种常见格式；失败抛 ValueError。"""
    text = text.strip()
    for fmt in _TIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            dt = dt.replace(year=default_year or datetime.now().year)
        return dt
    raise ValueError(f"无法识别的时刻格式：{text}（示例 2026-09-18 08:00:00）")


def _parse_int(text: str, label: str) -> int:
    try:
        v = int(str(text).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是整数：{text!r}")
    return v


def _parse_amount(text: str, label: str = "金额") -> float:
    try:
        v = float(str(text).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是数字：{text!r}")
    if math.isnan(v) or math.isinf(v) or v < 0:
        raise ValueError(f"{label}必须是不小于 0 的数字：{text!r}")
    return _round2(v)


def parse_stations_text(text: str) -> tuple[list[Station], list[dict]]:
    """站点分区文本，每行：站点名, 分区号。"""
    stations: list[Station] = []
    errors: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _split(line)
        if len(parts) != 2:
            errors.append(_err("STATION_BAD_FIELDS",
                               f"站点行应为「站点名, 分区号」，实际 {len(parts)} 段：{line}",
                               line=i))
            continue
        name, zone_text = parts
        if name in seen:
            errors.append(_err("STATION_DUPLICATE",
                               f"站点 {name} 重复定义", line=i, station=name))
            continue
        try:
            zone = _parse_int(zone_text, "分区号")
        except ValueError as ex:
            errors.append(_err("STATION_BAD_ZONE", str(ex), line=i, station=name))
            continue
        stations.append(Station(name=name, zone=zone, line=i))
        seen.add(name)
    return stations, errors


def parse_fares_text(text: str) -> tuple[list[Fare], list[dict]]:
    """票价矩阵文本，每行：起始分区, 到达分区, 票价。"""
    fares: list[Fare] = []
    errors: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _split(line)
        if len(parts) != 3:
            errors.append(_err("FARE_BAD_FIELDS",
                               f"票价行应为「起始分区, 到达分区, 票价」，实际 {len(parts)} 段：{line}",
                               line=i))
            continue
        fz_text, tz_text, price_text = parts
        try:
            fz = _parse_int(fz_text, "起始分区")
            tz = _parse_int(tz_text, "到达分区")
            price = _parse_amount(price_text, "票价")
        except ValueError as ex:
            errors.append(_err("FARE_BAD_VALUE", str(ex), line=i))
            continue
        key = (fz, tz)
        if key in seen:
            errors.append(_err("FARE_DUPLICATE",
                               f"分区 {fz}->{tz} 的票价重复定义", line=i))
            continue
        fares.append(Fare(from_zone=fz, to_zone=tz, price=price, line=i))
        seen.add(key)
    return fares, errors


def parse_records_text(text: str, default_year: Optional[int] = None
                       ) -> tuple[list[Swipe], list[dict]]:
    """刷卡记录文本，每行：卡号, 时刻, 站点, 进/出。

    保存阶段只做行格式解析；同卡配对、时间逆序、重复刷卡等
    跨记录的业务校验统一在 verify() 中完成，保证错误一次性报全。
    """
    records: list[Swipe] = []
    errors: list[dict] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 粘合时间戳内部的空白，避免被当作字段分隔符
        line = _DATETIME_RE.sub(lambda m: re.sub(r"[ \t]+", "T", m.group(0)), line)
        parts = _split(line)
        if len(parts) != 4:
            errors.append(_err("RECORD_BAD_FIELDS",
                               f"记录行应为「卡号, 时刻, 站点, 进/出」，实际 {len(parts)} 段：{line}",
                               line=i))
            continue
        card, time_text, station, action_text = parts
        card = card.strip()
        if not card:
            errors.append(_err("RECORD_BAD_CARD", "卡号不能为空", line=i))
            continue
        try:
            t = parse_time(time_text, default_year=default_year)
        except ValueError as ex:
            errors.append(_err("RECORD_BAD_TIME", str(ex),
                               line=i, card=card, station=station))
            continue
        action = _ACTION_ALIASES.get(action_text.upper())
        if action is None:
            errors.append(_err("RECORD_BAD_ACTION",
                               f"进出方向必须是 IN/进 或 OUT/出，实际：{action_text!r}",
                               line=i, card=card, time=time_text, station=station))
            continue
        records.append(Swipe(card=card, t=t, station=station,
                             action=action, line=i))
    return records, errors


# ---------------------------------------------------------------------------
# 运营日
# ---------------------------------------------------------------------------

def operating_day(t: datetime, split_hour: int = 4) -> str:
    """以某日 split_hour 点为界归属运营日。

    04:00 之前（含 00:00-03:59:59）归属前一自然日；
    04:00 及之后归属当日。例如 2026-09-18 03:30 属于 2026-09-17 运营日。
    """
    if t.hour < split_hour:
        t = t - timedelta(days=1)
    return t.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 配置校验
# ---------------------------------------------------------------------------

def _is_finite_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def _is_int_value(x) -> bool:
    return _is_finite_number(x) and float(x).is_integer()


def validate_config(cfg: Config) -> list[dict]:
    errors: list[dict] = []
    if not _is_int_value(cfg.transfer_minutes) or cfg.transfer_minutes <= 0:
        errors.append(_err("CONFIG_BAD", "换乘有效间隔必须为正整数（分钟）"))
    if not _is_finite_number(cfg.transfer_discount) or cfg.transfer_discount < 0:
        errors.append(_err("CONFIG_BAD", "换乘抵扣额不能为负且必须是有限数字"))
    if not _is_finite_number(cfg.daily_cap) or cfg.daily_cap < 0:
        errors.append(_err("CONFIG_BAD", "每日封顶额不能为负且必须是有限数字"))
    if not _is_int_value(cfg.day_split_hour) or not (0 <= cfg.day_split_hour <= 23):
        errors.append(_err("CONFIG_BAD", "运营日分界时刻必须是 0-23 之间的整数小时"))
    return errors


# ---------------------------------------------------------------------------
# 核验 + 计价
# ---------------------------------------------------------------------------

def _missing_fare_error(card: str, sw_in: "Swipe", sw_out: "Swipe",
                        z1: int, z2: int) -> dict:
    """票价矩阵缺项的定位错误（verify 与路径审计共用同一文案与定位）。"""
    return _err(
        "MISSING_FARE",
        f"票价矩阵缺少分区 {z1}->{z2}（{sw_in.station} -> "
        f"{sw_out.station}）的票价",
        line=sw_in.line, card=card, time=sw_in.time_text,
        station=sw_in.station)


def verify(stations: list[Station],
           fares: list[Fare],
           records: list[Swipe],
           cfg: Config) -> VerifyResult:
    """全量核验并计价。

    原子语义：任何记录级错误（缺出站、缺进站、时间逆序、重复刷卡、
    未知站点、矩阵缺票价）都会使整批核验失败，不生成任何账单。
    只有全部通过时才一次性产出全部行程账单。
    """
    errors: list[dict] = []
    errors.extend(validate_config(cfg))

    zone_of = {s.name: s.zone for s in stations}
    fare_of: dict[tuple[int, int], float] = {
        (f.from_zone, f.to_zone): f.price for f in fares
    }

    # 1) 站点引用检查（未知站点）
    for r in records:
        if r.station not in zone_of:
            errors.append(_err(
                "UNKNOWN_STATION",
                f"刷卡站点 {r.station} 未在站点分区中定义",
                line=r.line, card=r.card,
                time=r.time_text, station=r.station))

    # 2) 按卡号分组，时间升序，逐卡做重复刷卡 / 时间逆序 / 进出站结构检查
    by_card: dict[str, list[Swipe]] = {}
    for r in records:
        by_card.setdefault(r.card, []).append(r)

    trips: list[tuple[str, int, Swipe, Swipe]] = []
    # trips: (卡号, 卡内序号(从1), 进站记录, 出站记录)

    for card, rs in by_card.items():
        # 录入顺序（数据库按 id 返回，即文本录入先后）必须构成
        # 单调不减的时间流：下降=时间逆序；同卡同时刻=重复刷卡。
        ordered = True
        seen_times: dict[datetime, Swipe] = {}
        for r in rs:
            first = seen_times.get(r.t)
            if first is not None:
                errors.append(_err(
                    "DUPLICATE_SWIPE",
                    f"卡号 {card} 在 {r.time_text} 重复刷卡"
                    f"（第 {r.line} 行与第 {first.line} 行时刻完全相同）",
                    line=r.line, card=card, time=r.time_text,
                    station=r.station))
                ordered = False
            else:
                seen_times[r.t] = r
        for a, b in zip(rs, rs[1:]):
            if b.t < a.t:
                errors.append(_err(
                    "TIME_REVERSED",
                    f"卡号 {card} 刷卡时间逆序：第 {b.line} 行 {b.time_text} "
                    f"早于第 {a.line} 行 {a.time_text}",
                    line=b.line, card=card, time=b.time_text,
                    station=b.station))
                ordered = False

        # 存在逆序/重复时，进出站配对不可靠，跳过该卡结构检查
        # （结构异常很可能是乱序的假象，避免误报叠加）。
        if not ordered:
            continue

        # 时间流有序时录入顺序即时间顺序；逐对做进出站结构检查。
        rs_sorted = rs

        # 3) 进出站交替结构检查并配对。
        #    期望从进站开始，进出成对；起始为出站=缺进站，末尾落单=缺出站。
        i, seq = 0, 0
        while i < len(rs_sorted):
            cur = rs_sorted[i]
            if cur.action != ACTION_IN:
                errors.append(_err(
                    "MISSING_ENTRY",
                    f"卡号 {card} 的出站刷卡缺少配对进站"
                    f"（{cur.time_text} @ {cur.station}）",
                    line=cur.line, card=card,
                    time=cur.time_text, station=cur.station))
                i += 1
                continue
            if i + 1 >= len(rs_sorted):
                errors.append(_err(
                    "MISSING_EXIT",
                    f"卡号 {card} 进站后缺失出站"
                    f"（{cur.time_text} @ {cur.station}）",
                    line=cur.line, card=card,
                    time=cur.time_text, station=cur.station))
                i += 1
                continue
            nxt = rs_sorted[i + 1]
            if nxt.action != ACTION_OUT:
                errors.append(_err(
                    "MISSING_EXIT",
                    f"卡号 {card} 在 {cur.time_text} @ {cur.station} 进站后，"
                    f"下一条不是出站（第 {nxt.line} 行又是进站）",
                    line=cur.line, card=card,
                    time=cur.time_text, station=cur.station))
                i += 1
                continue
            seq += 1
            trips.append((card, seq, cur, nxt))
            i += 2

    # 4) 票价矩阵覆盖检查（仅对结构完整、站点已知的行程）
    for card, seq, sw_in, sw_out in trips:
        z1 = zone_of.get(sw_in.station)
        z2 = zone_of.get(sw_out.station)
        if z1 is None or z2 is None:
            continue
        if (z1, z2) not in fare_of:
            errors.append(_missing_fare_error(card, sw_in, sw_out, z1, z2))

    if errors:
        # 原子失败：不生成任何账单（无部分账单）
        return VerifyResult(ok=False, bills=[], errors=errors, trips=trips)

    # 5) 计价：按 (卡号, 进站时刻) 顺序处理。
    #    - 换乘抵扣看「同卡前次出站 -> 本次进站」的真实间隔（跨运营日也算）；
    #    - 封顶按行程进站时刻所属运营日、分卡累计。
    per_card_prev_out: dict[str, Swipe] = {}
    # 每卡每运营日累计实扣：card -> {day: amount}
    cumulative: dict[str, dict[str, float]] = {}

    bills: list[dict] = []
    ordered_trips = sorted(trips, key=lambda x: (x[0], x[2].t))

    for card, _seq, sw_in, sw_out in ordered_trips:
        z1 = zone_of[sw_in.station]
        z2 = zone_of[sw_out.station]
        base = _round2(fare_of[(z1, z2)])
        day = operating_day(sw_in.t, cfg.day_split_hour)

        discount = 0.0
        source = "无"
        prev_out = per_card_prev_out.get(card)
        if prev_out is not None:
            gap_min = (sw_in.t - prev_out.t).total_seconds() / 60.0
            if 0 <= gap_min <= cfg.transfer_minutes:
                discount = _round2(min(cfg.transfer_discount, base))
                source = (f"换乘优惠（距上次出站 {int(round(gap_min))} 分钟，"
                          f"≤{cfg.transfer_minutes} 分钟，抵扣 {discount:.2f} 元）")

        after_discount = _round2(base - discount)

        card_days = cumulative.setdefault(card, {})
        already = _round2(card_days.get(day, 0.0))
        cap_room = _round2(cfg.daily_cap - already)
        charge = _round2(max(0.0, min(after_discount, cap_room)))
        if cap_room <= 0:
            source = (f"{source}；当日已封顶" if discount > 0
                      else f"当日封顶（已累计 {already:.2f} 元，达到 "
                           f"{cfg.daily_cap:.2f} 元）")
        elif charge < after_discount:
            source = (f"{source}；封顶保护" if discount > 0
                      else f"封顶保护（当日封顶 {cfg.daily_cap:.2f} 元）")

        card_days[day] = _round2(already + charge)
        per_card_prev_out[card] = sw_out

        bills.append({
            "card": card,
            "seq": None,  # 下方按全局展示顺序统一编号
            "operating_day": day,
            "entry_time": sw_in.time_text,
            "exit_time": sw_out.time_text,
            "entry_station": sw_in.station,
            "exit_station": sw_out.station,
            "from_zone": z1,
            "to_zone": z2,
            "base_fare": base,
            "discount": discount,
            "discount_source": source,
            "charged_fare": charge,
            "daily_cumulative": card_days[day],
            "daily_cap": _round2(cfg.daily_cap),
            "entry_line": sw_in.line,
            "exit_line": sw_out.line,
        })

    # 同一卡号内按进站顺序给出展示序号
    seq_by_card: dict[str, int] = {}
    for b in bills:
        seq_by_card[b["card"]] = seq_by_card.get(b["card"], 0) + 1
        b["seq"] = seq_by_card[b["card"]]

    return VerifyResult(ok=True, bills=bills, errors=[], trips=trips)


# ---------------------------------------------------------------------------
# 配对修复预览（只读：不改记录、不写账单）
# ---------------------------------------------------------------------------

# 修复预览只处理进出站配对结构问题；其余错误（未知站点、缺票价、
# 时间逆序、重复刷卡、参数非法）一经出现即按原样返回，不生成修复方案。
_REPAIRABLE_CODES = {"MISSING_ENTRY", "MISSING_EXIT"}

# 允许的编辑操作（各计 1 次编辑）
OP_KEEP = "keep"      # 原样保留
OP_FLIP = "flip"      # 翻转进/出方向
OP_DELETE = "delete"  # 删除该条记录

# 同成本方案最多返回条数
REPAIR_MAX_PLANS = 5


@dataclass
class RepairPreview:
    """配对修复预览结果。

    ok=False 时 errors 为 verify() 的原有定位错误（未知站点、缺票价、
    时间逆序、重复刷卡等非配对错误原样返回，不生成修复方案）。
    ok=True 时 plans 为最小编辑方案（按稳定顺序，最多 REPAIR_MAX_PLANS 个），
    min_edits 为全局最小编辑数，total_plans 为同成本方案总数，
    hidden_plans 为未展示的条数；min_edits=0 时 plans 为空（无需修复）。
    """
    ok: bool
    min_edits: int = 0
    total_plans: int = 0
    shown_plans: int = 0
    hidden_plans: int = 0
    plans: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def _flip_action(action: str) -> str:
    return ACTION_OUT if action == ACTION_IN else ACTION_IN


def _min_edit_solutions(swipes: list[Swipe]
                        ) -> tuple[int, int, list[list[str]]]:
    """单卡（已按时间升序）最小编辑修复：动态规划。

    每条记录可 keep（0 编辑，方向须与期待一致）、flip（1 编辑，
    翻转后须与期待一致）或 delete（1 编辑）；保留记录的方向序列
    必须从进站开始严格交替（即可完整配对）。

    返回 (最小编辑数, 最小编辑方案总数, 按稳定顺序的前
    REPAIR_MAX_PLANS 个操作序列)。稳定顺序为 DFS 字典序
    （keep < flip < delete，靠前的记录优先被编辑）。
    """
    n = len(swipes)
    INF = n + 1
    # f[i][p]：处理第 i..n-1 条、期待方向 p（0=期待进站，1=期待出站）
    # 所需的最小编辑数；g[i][p] 为达到该最小值的方案数。
    f = [[INF, INF] for _ in range(n + 1)]
    f[n][0] = 0
    for i in range(n - 1, -1, -1):
        a = swipes[i].action
        for p in (0, 1):
            best = INF
            if (p == 0) == (a == ACTION_IN):              # keep
                best = min(best, f[i + 1][1 - p])
            if (p == 0) == (_flip_action(a) == ACTION_IN):  # flip
                best = min(best, 1 + f[i + 1][1 - p])
            best = min(best, 1 + f[i + 1][p])             # delete
            f[i][p] = best
    g = [[0, 0] for _ in range(n + 1)]
    g[n][0] = 1
    for i in range(n - 1, -1, -1):
        a = swipes[i].action
        for p in (0, 1):
            total = 0
            if (p == 0) == (a == ACTION_IN) \
                    and f[i + 1][1 - p] == f[i][p]:
                total += g[i + 1][1 - p]
            if (p == 0) == (_flip_action(a) == ACTION_IN) \
                    and 1 + f[i + 1][1 - p] == f[i][p]:
                total += g[i + 1][1 - p]
            if 1 + f[i + 1][p] == f[i][p]:
                total += g[i + 1][p]
            g[i][p] = total

    # 沿最优边按稳定顺序物化前 REPAIR_MAX_PLANS 个操作序列
    plans: list[list[str]] = []

    def walk(i: int, p: int, ops: list[str]) -> None:
        if len(plans) >= REPAIR_MAX_PLANS:
            return
        if i == n:
            plans.append(list(ops))
            return
        a = swipes[i].action
        if (p == 0) == (a == ACTION_IN) and f[i + 1][1 - p] == f[i][p]:
            ops.append(OP_KEEP)
            walk(i + 1, 1 - p, ops)
            ops.pop()
        if (p == 0) == (_flip_action(a) == ACTION_IN) \
                and 1 + f[i + 1][1 - p] == f[i][p]:
            ops.append(OP_FLIP)
            walk(i + 1, 1 - p, ops)
            ops.pop()
        if 1 + f[i + 1][p] == f[i][p]:
            ops.append(OP_DELETE)
            walk(i + 1, p, ops)
            ops.pop()

    walk(0, 0, [])
    return f[0][0], g[0][0], plans


def _build_repair_plan(by_card: dict[str, list[Swipe]],
                       card_plans: dict[str, list[list[str]]],
                       choices: dict[str, int],
                       min_edits: int) -> dict:
    """按各卡选定的操作序列，组装一个完整修复方案。

    方案按卡号排序、卡内按时间顺序逐条给出变更（删除 / 翻转方向），
    并生成修复后的完整记录文本（按卡号 + 时刻排序，方向写作 进/出）。
    """
    changes: list[dict] = []
    fixed: list[Swipe] = []
    for card in sorted(by_card):
        swipes = by_card[card]
        ops = card_plans[card][choices[card]]
        for r, op in zip(swipes, ops):
            if op == OP_DELETE:
                changes.append({
                    "op": OP_DELETE,
                    "card": r.card, "time": r.time_text,
                    "station": r.station, "line": r.line,
                    "from_action": r.action, "to_action": None,
                    "detail": f"删除记录：{r.card} {r.time_text} "
                              f"@ {r.station}（原方向 {r.action}）",
                })
            elif op == OP_FLIP:
                na = _flip_action(r.action)
                changes.append({
                    "op": OP_FLIP,
                    "card": r.card, "time": r.time_text,
                    "station": r.station, "line": r.line,
                    "from_action": r.action, "to_action": na,
                    "detail": f"翻转方向：{r.card} {r.time_text} @ {r.station}"
                              f"（{r.action} → {na}）",
                })
                fixed.append(Swipe(card=r.card, t=r.t, station=r.station,
                                   action=na, line=r.line))
            else:
                fixed.append(r)

    fixed.sort(key=lambda r: (r.card, r.t))
    lines = [f"{r.card}, {r.time_text}, {r.station}, "
             f"{'进' if r.action == ACTION_IN else '出'}" for r in fixed]
    return {
        "edit_count": min_edits,
        "changes": changes,
        "change_count": len(changes),
        "kept_count": len(fixed),
        "trip_count": len(fixed) // 2,
        "fixed_text": "\n".join(lines),
    }


def repair_preview(stations: list[Station],
                   fares: list[Fare],
                   records: list[Swipe],
                   cfg: Config) -> RepairPreview:
    """对已保存刷卡记录生成「可完整配对」的最小编辑修复预览。

    - 先用 verify() 全量校验：通过则无需修复；存在非配对错误
      （未知站点、缺票价、时间逆序、重复刷卡、参数非法）时按原错误返回；
    - 仅针对缺失进站 / 缺失出站 / 连续同向刷卡（verify 的 MISSING_ENTRY /
      MISSING_EXIT），按卡号分组、卡内按时间升序，做全局最小编辑搜索；
    - 允许的编辑只有「翻转进/出方向」与「删除记录」，各计 1 次；
      原始时刻、站点、卡号一律不改写；
    - 同成本方案按稳定顺序最多返回 REPAIR_MAX_PLANS 个，并标明
      最小编辑数与未展示的方案数量，不替用户选定任何方案；
    - 全程只读：不保存记录、不写/清空账单。
    """
    result = verify(stations, fares, records, cfg)
    if result.ok:
        return RepairPreview(ok=True)  # 记录已可完整配对，无需修复

    blocking = [e for e in result.errors
                if e.get("code") not in _REPAIRABLE_CODES]
    if blocking:
        # 非配对错误原样返回，不生成修复方案
        return RepairPreview(ok=False, errors=result.errors)

    # 按卡号分组，卡内按时间升序（verify 已确认有序，即录入顺序；
    # 此处显式排序使预览不依赖录入顺序）
    by_card: dict[str, list[Swipe]] = {}
    for r in records:
        by_card.setdefault(r.card, []).append(r)
    for rs in by_card.values():
        rs.sort(key=lambda r: r.t)

    # 各卡独立求最小编辑；全局最小编辑数 = 各卡之和，
    # 同成本方案总数 = 各卡方案数之积（卡间相互独立）。
    card_plans: dict[str, list[list[str]]] = {}
    min_edits = 0
    total = 1
    for card in sorted(by_card):
        cost, count, first = _min_edit_solutions(by_card[card])
        card_plans[card] = first
        min_edits += cost
        total *= count

    shown = min(total, REPAIR_MAX_PLANS)
    plans: list[dict] = []
    # 稳定顺序枚举方案组合：混合进制计数，卡号大的方案索引变化最快；
    # 全局前 REPAIR_MAX_PLANS 个组合只依赖各卡物化的前 REPAIR_MAX_PLANS 个序列。
    cards = sorted(by_card)
    for combo in range(shown):
        choices: dict[str, int] = {}
        rest = combo
        for card in reversed(cards):
            choices[card] = rest % len(card_plans[card])
            rest //= len(card_plans[card])
        plans.append(_build_repair_plan(by_card, card_plans,
                                        choices, min_edits))

    return RepairPreview(
        ok=True,
        min_edits=min_edits,
        total_plans=total,
        shown_plans=len(plans),
        hidden_plans=total - len(plans),
        plans=plans,
    )


# ---------------------------------------------------------------------------
# 规则试算对比（不落库、不改配置）
# ---------------------------------------------------------------------------

def _snap_int(x) -> int | float:
    """整数型数字快照为 int（如 120.0 -> 120），非整数保留原值。"""
    return int(x) if _is_finite_number(x) and float(x).is_integer() else x


def _config_snapshot(cfg: Config) -> dict:
    return {
        "transfer_minutes": _snap_int(cfg.transfer_minutes),
        "transfer_discount": _round2(cfg.transfer_discount),
        "daily_cap": _round2(cfg.daily_cap),
        "day_split_hour": _snap_int(cfg.day_split_hour),
    }


def _index_bills(bills: list[dict]) -> dict[tuple[str, int], dict]:
    return {(b["card"], b["seq"]): b for b in bills}


def trial_compare(stations: list[Station],
                  fares: list[Fare],
                  records: list[Swipe],
                  current_cfg: Config,
                  draft_cfg: Config) -> TrialResult:
    """用同一批已保存的刷卡记录，分别按当前配置与草案配置试算计费。

    - 完全复用 verify() 的解析后配对 / 校验 / 计价规则，本函数不做任何写操作；
    - 任一方案失败（配置参数非法或数据错误）时，返回该方案 verify() 产出的
      原有定位错误，不产出对比账单；
    - 两方案都成功时，按 (卡号, 卡内序号) 对齐行程，逐程给出运营日、
      优惠、实扣及差额，并汇总每张卡与全批次的金额变化。
    """
    current = verify(stations, fares, records, current_cfg)
    draft = verify(stations, fares, records, draft_cfg)

    if not current.ok or not draft.ok:
        return TrialResult(
            ok=False,
            current={"config": _config_snapshot(current_cfg),
                     "ok": current.ok,
                     "errors": current.errors},
            draft={"config": _config_snapshot(draft_cfg),
                   "ok": draft.ok,
                   "errors": draft.errors},
        )

    cur_map = _index_bills(current.bills)
    draft_map = _index_bills(draft.bills)
    # 配对与卡内序号仅取决于记录数据本身，两边应当一致；用并集以防万一。
    keys = sorted(set(cur_map) | set(draft_map))

    rows: list[dict] = []
    for card, seq in keys:
        cb = cur_map.get((card, seq))
        db = draft_map.get((card, seq))

        def pick(key: str):
            return (cb[key] if cb is not None else None,
                    db[key] if db is not None else None)

        cur_day, draft_day = pick("operating_day")
        cur_disc, draft_disc = pick("discount")
        cur_charge, draft_charge = pick("charged_fare")
        rows.append({
            "card": card,
            "seq": seq,
            "entry_time": (cb or db)["entry_time"],
            "exit_time": (cb or db)["exit_time"],
            "entry_station": (cb or db)["entry_station"],
            "exit_station": (cb or db)["exit_station"],
            "from_zone": (cb or db)["from_zone"],
            "to_zone": (cb or db)["to_zone"],
            "base_fare": (cb or db)["base_fare"],
            "current_operating_day": cur_day,
            "draft_operating_day": draft_day,
            "day_changed": cur_day != draft_day,
            "current_discount": cur_disc,
            "draft_discount": draft_disc,
            "discount_diff": _round2((draft_disc or 0) - (cur_disc or 0)),
            "current_charge": cur_charge,
            "draft_charge": draft_charge,
            "charge_diff": _round2((draft_charge or 0) - (cur_charge or 0)),
            "draft_discount_source": db["discount_source"] if db else None,
            "current_discount_source": cb["discount_source"] if cb else None,
        })

    card_summaries: list[dict] = []
    for card in sorted({k[0] for k in keys}):
        cb_sum = sum(b["charged_fare"] for b in current.bills if b["card"] == card)
        db_sum = sum(b["charged_fare"] for b in draft.bills if b["card"] == card)
        cd_sum = sum(b["discount"] for b in current.bills if b["card"] == card)
        dd_sum = sum(b["discount"] for b in draft.bills if b["card"] == card)
        card_summaries.append({
            "card": card,
            "trip_count": sum(1 for k in keys if k[0] == card),
            "current_charge_total": _round2(cb_sum),
            "draft_charge_total": _round2(db_sum),
            "charge_diff": _round2(db_sum - cb_sum),
            "current_discount_total": _round2(cd_sum),
            "draft_discount_total": _round2(dd_sum),
            "discount_diff": _round2(dd_sum - cd_sum),
        })

    cur_total = sum(b["charged_fare"] for b in current.bills)
    draft_total = sum(b["charged_fare"] for b in draft.bills)
    cur_disc_total = sum(b["discount"] for b in current.bills)
    draft_disc_total = sum(b["discount"] for b in draft.bills)
    batch_summary = {
        "trip_count": len(keys),
        "current_charge_total": _round2(cur_total),
        "draft_charge_total": _round2(draft_total),
        "charge_diff": _round2(draft_total - cur_total),
        "current_discount_total": _round2(cur_disc_total),
        "draft_discount_total": _round2(draft_disc_total),
        "discount_diff": _round2(draft_disc_total - cur_disc_total),
    }

    return TrialResult(
        ok=True,
        current={"config": _config_snapshot(current_cfg),
                 "ok": True,
                 "errors": [],
                 "bill_count": len(current.bills)},
        draft={"config": _config_snapshot(draft_cfg),
               "ok": True,
               "errors": [],
               "bill_count": len(draft.bills)},
        rows=rows,
        card_summaries=card_summaries,
        batch_summary=batch_summary,
    )


# ---------------------------------------------------------------------------
# 票价矩阵路径审计（只读：不改矩阵、不写账单）
# ---------------------------------------------------------------------------

@dataclass
class FarePathAudit:
    """票价矩阵路径审计结果。

    ok=False 时 errors 为 verify() 的原有定位错误（除 MISSING_FARE 外的
    数据错误原样返回，不生成审计结果）。
    ok=True 时 missing / overpriced 为按 (起始分区, 到达分区) 稳定排序的
    审计发现；不可达的缺失分区对 reachable=False，并在该项 errors 内
    保留原 MISSING_FARE 定位错误。
    """
    ok: bool
    trip_count: int = 0
    pair_count: int = 0
    missing: list[dict] = field(default_factory=list)
    overpriced: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def _cents_of(yuan: float) -> int:
    """元 -> 整数分。路径成本全程以整数分累加，避免浮点误差影响等值比较。"""
    return int(round(float(yuan) * 100))


def _yuan_of(cents: int) -> float:
    return round(cents / 100.0, 2)


def _min_cost_paths(fare_cents: dict[tuple[int, int], int], source: int
                    ) -> dict[int, tuple[int, tuple[int, ...]]]:
    """从 source 出发到各分区的 (最低成本分, 分区序列)，Dijkstra。

    - 边为已配置的有向票价（非对称方向按图语义处理，零价边合法）；
    - 优先队列键为 (成本, 分区序列)：相同总价按分区序列字典序稳定选路；
    - 回到 source 的边不参与中途松弛（简单路径不会重返起点）；
      source 自身的同区闭合成本由 _min_cycle 另行计算。
    """
    adj: dict[int, list[tuple[int, int]]] = {}
    for (fz, tz), c in fare_cents.items():
        if tz == source and fz != source:
            continue  # 重返起点的边只在闭合同区路径时考虑
        adj.setdefault(fz, []).append((tz, c))
    for lst in adj.values():
        lst.sort()

    dist: dict[int, tuple[int, tuple[int, ...]]] = {source: (0, (source,))}
    pq: list[tuple[int, tuple[int, ...], int]] = [(0, (source,), source)]
    while pq:
        cost, path, u = heapq.heappop(pq)
        if dist.get(u) != (cost, path):
            continue  # 过期堆项
        for v, w in adj.get(u, ()):
            cand = (cost + w, path + (v,))
            cur = dist.get(v)
            if cur is None or cand < cur:
                dist[v] = cand
                heapq.heappush(pq, (cost + w, path + (v,), v))
    return dist


def _min_cycle(fare_cents: dict[tuple[int, int], int],
               dist: dict[int, tuple[int, tuple[int, ...]]],
               source: int) -> Optional[tuple[int, tuple[int, ...]]]:
    """同区对 (source, source) 的最低成本闭合路径（至少经过一条边）。

    dist 为不重返 source 的最短路径；枚举最后一条回到 source 的边闭合。
    直达自环边（source->source 本身）不算间接路径，不参与闭合。
    """
    best: Optional[tuple[int, tuple[int, ...]]] = None
    for (fz, tz), c in fare_cents.items():
        if tz != source or fz == source:
            continue
        d = dist.get(fz)
        if d is None:
            continue
        cand = (d[0] + c, d[1] + (source,))
        if best is None or cand < best:
            best = cand
    return best


def _path_legs(path: tuple[int, ...],
               fare_yuan: dict[tuple[int, int], float]) -> list[dict]:
    """完整路径的逐段明细（每段的起讫分区与已配置单价）。"""
    return [{"from_zone": a, "to_zone": b, "price": fare_yuan[(a, b)]}
            for a, b in zip(path, path[1:])]


def fare_path_audit(stations: list[Station],
                    fares: list[Fare],
                    records: list[Swipe],
                    cfg: Config) -> FarePathAudit:
    """票价矩阵路径审计（全程只读：不修改矩阵、不写/清空账单）。

    以分区为节点、已配置的有向票价为边，对已保存刷卡记录配对出的
    实际行程分区对计算最低成本路径：

    - 缺直达票价但存在可达路径：给出建议票价（= 最低成本路径总价）、
      完整路径、建议补录行，以及受影响卡号与原始行号；
    - 缺直达票价且不可达：保留 verify() 的原 MISSING_FARE 定位错误；
    - 已有直达票价高于间接路径：标出差额（直达 − 间接最低）与最低成本路径；
    - 除 MISSING_FARE 外的数据错误：按 verify() 原有定位错误返回，不生成审计。

    相同总价按分区序列字典序稳定选路；零价边与非对称方向按有向图语义处理；
    同区行程按「至少经过一条边」的闭合路径计算。
    """
    result = verify(stations, fares, records, cfg)
    if not result.ok:
        blocking = [e for e in result.errors if e.get("code") != "MISSING_FARE"]
        if blocking:
            # 非缺票价错误原样返回（含原有定位信息），不生成审计
            return FarePathAudit(ok=False, errors=result.errors)

    # 此时核验要么全部通过，要么仅剩 MISSING_FARE：站点必然全部已知
    zone_of = {s.name: s.zone for s in stations}
    fare_cents = {(f.from_zone, f.to_zone): _cents_of(f.price) for f in fares}
    fare_yuan = {(f.from_zone, f.to_zone): _round2(f.price) for f in fares}

    # 实际行程分区对 -> 该对上的原始行程（卡号 / 序号 / 进出站记录）
    pair_trips: dict[tuple[int, int], list[tuple[str, int, Swipe, Swipe]]] = {}
    for card, seq, sw_in, sw_out in result.trips:
        key = (zone_of[sw_in.station], zone_of[sw_out.station])
        pair_trips.setdefault(key, []).append((card, seq, sw_in, sw_out))

    # 每个涉及的起始分区跑一次 Dijkstra（整数分、非负边、稳定选路）
    dist_by_src = {z: _min_cost_paths(fare_cents, z)
                   for z in {p[0] for p in pair_trips}}

    def best_path(z1: int, z2: int):
        if z1 == z2:
            return _min_cycle(fare_cents, dist_by_src[z1], z1)
        return dist_by_src[z1].get(z2)

    missing: list[dict] = []
    overpriced: list[dict] = []
    for z1, z2 in sorted(pair_trips):
        raw = sorted(pair_trips[(z1, z2)], key=lambda t: (t[0], t[1]))
        affected = [{
            "card": c, "seq": s,
            "entry_time": i.time_text,
            "entry_station": i.station, "exit_station": o.station,
            "entry_line": i.line, "exit_line": o.line,
        } for c, s, i, o in raw]
        got = best_path(z1, z2)
        direct = fare_cents.get((z1, z2))

        if direct is None:
            item: dict = {
                "from_zone": z1, "to_zone": z2,
                "reachable": got is not None,
                "suggested_price": None,
                "path": None, "path_text": None, "legs": [],
                "suggest_line": None,
                "affected": affected, "trip_count": len(affected),
                "errors": [],
            }
            if got is not None:
                cents, path = got
                price = _yuan_of(cents)
                item.update({
                    "suggested_price": price,
                    "path": list(path),
                    "path_text": " → ".join(str(z) for z in path),
                    "legs": _path_legs(path, fare_yuan),
                    "suggest_line": f"{z1}, {z2}, {price:.2f}",
                })
            else:
                # 不可达：保留原 MISSING_FARE 定位错误（逐受影响行程一条）
                item["errors"] = [
                    _missing_fare_error(c, i, o, z1, z2) for c, s, i, o in raw
                ]
            missing.append(item)
        elif got is not None and got[0] < direct:
            cents, path = got
            best = _yuan_of(cents)
            overpriced.append({
                "from_zone": z1, "to_zone": z2,
                "direct_price": fare_yuan[(z1, z2)],
                "best_price": best,
                "diff": _round2(fare_yuan[(z1, z2)] - best),
                "path": list(path),
                "path_text": " → ".join(str(z) for z in path),
                "legs": _path_legs(path, fare_yuan),
                "affected": affected, "trip_count": len(affected),
            })

    return FarePathAudit(
        ok=True,
        trip_count=len(result.trips),
        pair_count=len(pair_trips),
        missing=missing,
        overpriced=overpriced,
    )


# ---------------------------------------------------------------------------
# 站点闸机峰值审计（只读：不改记录、配置或账单）
# ---------------------------------------------------------------------------

# 统计窗口允许的分钟数范围
PEAK_WINDOW_MIN_MINUTES = 1
PEAK_WINDOW_MAX_MINUTES = 120
# 默认最低告警数：峰值刷卡数 >= 该值的组才标记 alert
PEAK_DEFAULT_MIN_ALERT = 1

ACTION_LABELS = {"IN": "进", "OUT": "出"}

# 审计结果中时刻的展示格式（与 Swipe.time_text 一致，精确到秒）
_PEAK_TIME_FMT = "%Y-%m-%d %H:%M:%S"


@dataclass
class PeakAudit:
    """站点闸机峰值审计结果。

    参数非法（窗口分钟数不是 1-120 的整数、最低告警数不是正整数）时
    ok=False，errors 携带 CONFIG_BAD；ok=True 时 groups 按
    峰值数量降序、开始时刻升序、站点名、方向稳定排序，每个组给出
    半开区间 [start, end) 内的峰值刷卡数、涉及卡号数与原始行号。
    """
    ok: bool
    window_minutes: int = 0
    min_alert: int = 0
    record_count: int = 0
    group_count: int = 0
    alert_count: int = 0
    groups: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def _validate_peak_params(window_minutes, min_alert) -> list[dict]:
    """峰值审计参数校验，非法时给出与其他接口一致的 CONFIG_BAD 错误。"""
    errors: list[dict] = []
    if not _is_int_value(window_minutes) \
            or not (PEAK_WINDOW_MIN_MINUTES <= int(window_minutes)
                    <= PEAK_WINDOW_MAX_MINUTES):
        errors.append(_err(
            "CONFIG_BAD",
            f"统计窗口必须为 {PEAK_WINDOW_MIN_MINUTES}～"
            f"{PEAK_WINDOW_MAX_MINUTES} 之间的整数（分钟）"))
    if not _is_int_value(min_alert) or int(min_alert) < 1:
        errors.append(_err(
            "CONFIG_BAD", "最低告警数必须为不小于 1 的整数"))
    return errors


def _peak_of_group(events: list[Swipe], window: timedelta
                   ) -> tuple[int, datetime, datetime, int, list[int]]:
    """单组（同站点同方向）事件上求刷卡数最多的半开区间。

    events 已按时刻升序（同时刻按原始行号稳定排序）。候选起点只取
    实际刷卡时刻；窗口为 [开始, 开始 + window)，半开区间保证边界
    时刻的刷卡不会被重复计入。滑动窗口以事件为起点逐个右移，
    出现并列时先遇到的起点更早，故只在严格更大时更新。

    返回 (峰值刷卡数, 开始时刻, 结束时刻, 涉及卡号数, 原始行号列表)。
    """
    best_count = 0
    best_start_idx = 0
    best_end_idx = 0
    right = 0
    for left in range(len(events)):
        start = events[left].t
        limit = start + window
        if right < left:
            right = left
        # 半开区间：时刻严格小于 start + window 才计入
        while right < len(events) and events[right].t < limit:
            right += 1
        count = right - left
        if count > best_count:
            best_count = count
            best_start_idx = left
            best_end_idx = right
    start_t = events[best_start_idx].t
    end_t = start_t + window
    window_events = events[best_start_idx:best_end_idx]
    card_count = len({e.card for e in window_events})
    lines = sorted(e.line for e in window_events)
    return best_count, start_t, end_t, card_count, lines


def station_peak_audit(records: list[Swipe],
                       window_minutes,
                       min_alert) -> PeakAudit:
    """站点闸机峰值审计（全程只读：不改记录、配置或账单）。

    直接统计已保存的刷卡记录（不依赖进出站配对、站点分区与票价）：
    按（站点, 进/出方向）分组，在给定的 1～120 分钟窗口内查找刷卡数
    最多的半开区间 [开始, 结束)。候选窗口只从实际刷卡时刻起算，避免
    固定整点分桶漏掉跨边界高峰；峰值数量并列时取更早的开始时刻
    （事件按时间顺序扫描，首个最大值即最早起点），组间按峰值数量
    降序、开始时刻升序、站点名、方向排序。

    - 每组返回峰值刷卡数、窗口起止时刻、涉及卡号数（去重）与
      窗口内记录的原始文本行号；
    - 峰值刷卡数 >= min_alert 的组标记 alert=true，其余组照常返回
      但不标记；
    - 参数非法（窗口非 1～120 整数、最低告警数非正整数）时返回
      CONFIG_BAD，不生成审计结果。
    """
    errors = _validate_peak_params(window_minutes, min_alert)
    if errors:
        return PeakAudit(ok=False, errors=errors)
    window_minutes = int(window_minutes)
    min_alert = int(min_alert)
    window = timedelta(minutes=window_minutes)

    # （站点, 方向）-> 该组全部刷卡事件
    groups: dict[tuple[str, str], list[Swipe]] = {}
    for r in records:
        groups.setdefault((r.station, r.action), []).append(r)

    result_rows: list[dict] = []
    for (station, action), events in groups.items():
        events.sort(key=lambda e: (e.t, e.line, e.card))
        count, start_t, end_t, card_count, lines = _peak_of_group(
            events, window)
        result_rows.append({
            "_peak": count,
            "_start": start_t,
            "station": station,
            "action": action,
            "direction": ACTION_LABELS.get(action, action),
            "peak_count": count,
            "start_time": start_t.strftime(_PEAK_TIME_FMT),
            "end_time": end_t.strftime(_PEAK_TIME_FMT),
            "card_count": card_count,
            "total_count": len(events),
            "lines": lines,
            "alert": count >= min_alert,
        })

    # 组间排序：峰值数量降序，并列时开始时刻更早优先，再按站点名、方向
    result_rows.sort(key=lambda g: (-g["_peak"], g["_start"],
                                    g["station"], g["action"]))
    for g in result_rows:
        del g["_peak"]
        del g["_start"]

    return PeakAudit(
        ok=True,
        window_minutes=window_minutes,
        min_alert=min_alert,
        record_count=len(records),
        group_count=len(result_rows),
        alert_count=sum(1 for g in result_rows if g["alert"]),
        groups=result_rows,
    )


# ---------------------------------------------------------------------------
# 行程耗时异常审计（只读：不改记录、配置或账单）
# ---------------------------------------------------------------------------

# 有向站点对参与判定的最小样本数（行程数），不足的站点对单独列出
DURATION_MIN_SAMPLES = 6
# 阈值下限（分钟）：即使 MAD 为 0，也至少允许 10 分钟的波动
DURATION_FLOOR_MINUTES = 10.0
# 阈值 = max(下限, DURATION_MAD_K × MAD_SCALE × MAD)
DURATION_MAD_K = 3.0
MAD_SCALE = 1.4826  # 正态分布一致性常数

KIND_LONG = "long"    # 耗时过长（高于基线中位数）
KIND_SHORT = "short"  # 耗时过短（低于基线中位数）

# 阻断审计的错误类型（配对 / 时间类）；未知站点、票价缺项、参数类不阻断
_DURATION_BLOCKING_CODES = {
    "MISSING_ENTRY", "MISSING_EXIT", "TIME_REVERSED", "DUPLICATE_SWIPE",
}


@dataclass
class DurationAudit:
    """行程耗时异常审计结果。

    ok=False 时 errors 为 verify() 的原有定位错误（仅配对 / 时间类错误
    阻断审计；未知站点、票价缺项不阻断）。
    ok=True 时 anomalies 为被标记的行程（含基线样本数、中位数、MAD、
    阈值、偏差与原始进出行号），insufficient 为样本不足
    （< DURATION_MIN_SAMPLES 程）而未参与判定的有向站点对。
    """
    ok: bool
    trip_count: int = 0
    pair_count: int = 0
    audited_pairs: int = 0
    min_samples: int = DURATION_MIN_SAMPLES
    anomalies: list[dict] = field(default_factory=list)
    insufficient: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def _median(values: list[float]) -> float:
    """中位数；偶数个取中间两值的平均。"""
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def duration_audit(stations: list[Station],
                   fares: list[Fare],
                   records: list[Swipe],
                   cfg: Config) -> DurationAudit:
    """行程耗时异常审计（全程只读：不改记录、配置或账单）。

    对已保存且可完整配对的刷卡记录，按（进站站点 -> 出站站点）的有向
    站点对分组，行程耗时 = 出站时刻 - 进站时刻：

    - 仅样本数 >= DURATION_MIN_SAMPLES 的站点对参与判定；样本不足的
      站点对列入 insufficient，不产生任何标记；
    - 每条行程从基线中排除自身，以其余样本的中位数与 MAD（基线样本
      相对中位数的绝对偏差的中位数）计算阈值
      max(10 分钟, 3 × 1.4826 × MAD)；绝对偏差严格大于阈值时标记为
      过长（long）或过短（short）；
    - 判定在秒级精度上进行，展示值换算为分钟并保留两位小数；
    - 配对 / 时间类错误（MISSING_ENTRY / MISSING_EXIT / TIME_REVERSED /
      DUPLICATE_SWIPE）按 verify() 的原有定位错误返回，不生成审计结果；
      未知站点与票价缺项不阻断审计（耗时与分区、票价无关）。
    """
    result = verify(stations, fares, records, cfg)
    if not result.ok:
        blocking = [e for e in result.errors
                    if e.get("code") in _DURATION_BLOCKING_CODES]
        if blocking:
            # 配对 / 时间类错误：原有定位错误原样返回，不生成审计
            return DurationAudit(ok=False, errors=result.errors)

    # 有向站点对 -> 该对上的行程（卡号, 卡内序号, 进站记录, 出站记录）
    pair_trips: dict[tuple[str, str], list[tuple[str, int, Swipe, Swipe]]] = {}
    for card, seq, sw_in, sw_out in result.trips:
        key = (sw_in.station, sw_out.station)
        pair_trips.setdefault(key, []).append((card, seq, sw_in, sw_out))

    anomalies: list[dict] = []
    insufficient: list[dict] = []
    audited = 0
    for pair in sorted(pair_trips):
        trips = sorted(pair_trips[pair], key=lambda t: (t[0], t[1]))
        n = len(trips)
        if n < DURATION_MIN_SAMPLES:
            insufficient.append({
                "entry_station": pair[0],
                "exit_station": pair[1],
                "trip_count": n,
                "required": DURATION_MIN_SAMPLES,
            })
            continue
        audited += 1
        # 秒级耗时（出站 - 进站）；verify 已保证同卡时间有序，耗时非负
        durations = [(t[3].t - t[2].t).total_seconds() for t in trips]
        for idx, (card, seq, sw_in, sw_out) in enumerate(trips):
            baseline = durations[:idx] + durations[idx + 1:]
            median_s = _median(baseline)
            mad_s = _median([abs(x - median_s) for x in baseline])
            threshold_s = max(DURATION_FLOOR_MINUTES * 60.0,
                              DURATION_MAD_K * MAD_SCALE * mad_s)
            dev_s = durations[idx] - median_s
            if abs(dev_s) <= threshold_s:
                continue  # 严格大于阈值才标记
            anomalies.append({
                "kind": KIND_LONG if dev_s > 0 else KIND_SHORT,
                "card": card,
                "seq": seq,
                "entry_station": sw_in.station,
                "exit_station": sw_out.station,
                "entry_time": sw_in.time_text,
                "exit_time": sw_out.time_text,
                "duration_min": _round2(durations[idx] / 60.0),
                "baseline_count": len(baseline),
                "median_min": _round2(median_s / 60.0),
                "mad_min": _round2(mad_s / 60.0),
                "threshold_min": _round2(threshold_s / 60.0),
                "deviation_min": _round2(dev_s / 60.0),
                "entry_line": sw_in.line,
                "exit_line": sw_out.line,
            })

    return DurationAudit(
        ok=True,
        trip_count=len(result.trips),
        pair_count=len(pair_trips),
        audited_pairs=audited,
        anomalies=anomalies,
        insufficient=insufficient,
    )


# ---------------------------------------------------------------------------
# 外部刷卡批次合并预览（只读：不改记录、账单）
# ---------------------------------------------------------------------------

# 配对容差允许的秒数范围（闭区间）
MERGE_TOLERANCE_MIN_SECONDS = 0
MERGE_TOLERANCE_MAX_SECONDS = 120


@dataclass
class MergePreview:
    """外部刷卡批次合并预览结果。

    参数非法（容差不是 0～120 的整数）时 ok=False，errors 携带 CONFIG_BAD；
    待导入文本的解析错误由调用方（parse_records_text）先行拦截。
    ok=True 时 pairs 为重复配对（基准 ↔ 导入 一对一），base_only /
    import_only 为两侧各自独有的记录，merged_text 为去重后按时间排序的
    合并文本（配对取基准行）。
    """
    ok: bool
    tolerance_seconds: int = 0
    base_count: int = 0
    import_count: int = 0
    pair_count: int = 0
    total_abs_diff_seconds: int = 0
    pairs: list[dict] = field(default_factory=list)
    base_only: list[dict] = field(default_factory=list)
    import_only: list[dict] = field(default_factory=list)
    merged_text: str = ""
    errors: list[dict] = field(default_factory=list)


def _lex_bases(base_line: list[int], import_line: list[int], n: int
               ) -> tuple[int, int]:
    """计算费用编码的两个进位基，保证行号分量绝不向上进位。

    一条匹配至多 n 条边：导入行号总和 ≤ n·maxIL（取 B2 严格大于它）；
    单条边中间分量为 基准行号·B2 + 导入行号，n 条之和
    ≤ n·maxBL·B2 + n·maxIL（取 B1 严格大于它）。行号取参与匹配各点的
    真实最大行号，分组很小时基数仍然紧凑。
    """
    max_bl = max(base_line, default=1) or 1
    max_il = max(import_line, default=1) or 1
    b2 = n * max_il + 1
    b1 = n * max_bl * b2 + n * max_il + 1
    return b1, b2


def _hungarian_dense(edge_cost: dict[tuple[int, int], int],
                     n: int, k: int, b1: int, tol: int
                     ) -> list[tuple[int, int]]:
    """稠密 / 完整图上的最小权匹配（e-maxx 匈牙利，整数标量费用）。

    列为 k 条真实导入列 + n 条虚拟列；候选边取已编码整数费用，非候选边
    取足够大的禁止值，虚拟列（未配对）费用 = n·(tol+1)·B1：
    它严格高于任何 n 条候选边的时间差分量总和（每条 ≤ tol，
    加权行号后仍可由 B1 主导），故匹配先最大化配对数、再最小化总费用
    （总时间差 → 基准行号和 → 导入行号和）。
    """
    forbidden = 10 ** 30
    dummy = n * (tol + 1) * b1
    m = k + n
    cost = [[forbidden] * m for _ in range(n)]
    for (bi, ii), c in edge_cost.items():
        cost[bi][ii] = c
    for bi in range(n):
        for j in range(k, m):
            cost[bi][j] = dummy

    INF = 10 ** 36
    u = [0] * (n + 1)
    v = [0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)
        while p[j0] != 0:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = 0
            r = cost[i0 - 1]
            ui = u[i0]
            for j in range(1, m + 1):
                if not used[j]:
                    cur = r[j - 1] - ui - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    pairs = []
    for j in range(1, m + 1):
        if p[j] != 0 and j - 1 < k:
            pairs.append((p[j] - 1, j - 1))
    return pairs


def _min_cost_flow(edge_cost: dict[tuple[int, int], int],
                   n: int, k: int) -> list[tuple[int, int]]:
    """稀疏图上的最小费用最大流：源→基准→候选导入→汇，容量均为 1。

    沿最短路逐次增广 1 单位，直到汇不可达为止；流量即最大配对数，
    且在最大配对数下费用最小（Johnson 势 + Dijkstra，初始费用非负）。
    相比稠密匈牙利 O(n³)，只遍历容差窗口内的候选边。
    """
    S = n + k
    T = n + k + 1
    V = T + 1
    graph: list[list[list]] = [[] for _ in range(V)]

    def add_edge(u, v, cap, w):
        graph[u].append([v, cap, w, len(graph[v])])
        graph[v].append([u, 0, -w, len(graph[u]) - 1])

    for bi in range(n):
        add_edge(S, bi, 1, 0)
    for ii in range(k):
        add_edge(n + ii, T, 1, 0)
    cand_ref: dict[tuple[int, int], list] = {}
    for (bi, ii), w in edge_cost.items():
        add_edge(bi, n + ii, 1, w)
        cand_ref[(bi, ii)] = graph[bi][-1]

    pot = [0] * V
    for _ in range(n):
        dist: list[Optional[int]] = [None] * V
        prev_v = [-1] * V
        prev_e = [-1] * V
        dist[S] = 0
        pq = [(0, S)]
        while pq:
            d, u = heapq.heappop(pq)
            if d != dist[u]:
                continue
            for ei, e in enumerate(graph[u]):
                v2, cap, w, _rev = e
                if cap <= 0:
                    continue
                nd = d + w + pot[u] - pot[v2]
                if dist[v2] is None or nd < dist[v2]:
                    dist[v2] = nd
                    prev_v[v2] = u
                    prev_e[v2] = ei
                    heapq.heappush(pq, (nd, v2))
        if dist[T] is None:
            break  # 已达最大流
        for v2 in range(V):
            if dist[v2] is not None:
                pot[v2] += dist[v2]
        v2 = T
        while v2 != S:
            u = prev_v[v2]
            e = graph[u][prev_e[v2]]
            e[1] -= 1
            graph[e[0]][e[3]][1] += 1
            v2 = u

    return [(bi, ii) for (bi, ii), e in cand_ref.items() if e[1] == 0]


def _match_group(base: list[Swipe], imports: list[Swipe], tolerance: int
                 ) -> list[tuple[int, int, int]]:
    """单个（卡号, 站点, 方向）分组内的全局一对一匹配。

    仅当 |基准时刻 − 导入时刻| ≤ tolerance（秒）时建立候选边。
    匹配目标依次为：
      1) 配对数最多（最大基数匹配）；
      2) 配对的总绝对时间差最小；
      3) 时间差总和相同时，依次最小化基准行号总和、导入行号总和
         （行号为录入文本原始行，结果与处理顺序无关）。

    候选边费用编码为整数  diff·B1 + 基准行号·B2 + 导入行号，
    B1/B2 取大于任意行号总和的进位基，使整数费用比较等价于上述字典序。
    先剔除不可能配对的孤立点；稠密 / 完整图用匈牙利算法，
    稀疏图（容差窗口内候选很少）用最小费用最大流。

    返回 [(基准下标, 导入下标, 绝对时间差秒), ...]。
    """
    n_all, k_all = len(base), len(imports)
    if n_all == 0 or k_all == 0:
        return []

    # 候选边（原始下标, 秒级绝对时间差）：两侧按时刻排序后滑动窗口生成，
    # 只枚举 |Δt| ≤ tolerance 的对，避免对容差窗口外的记录对做无谓比较。
    bo = sorted(range(n_all), key=lambda i: (base[i].t, base[i].line))
    xo = sorted(range(k_all), key=lambda j: (imports[j].t, imports[j].line))
    bsec = [int(base[i].t.timestamp()) for i in bo]
    xsec = [int(imports[j].t.timestamp()) for j in xo]
    cand: dict[tuple[int, int], int] = {}
    lo = hi = 0
    for pos, bi in enumerate(bo):
        t = bsec[pos]
        while lo < k_all and xsec[lo] < t - tolerance:
            lo += 1
        if hi < lo:
            hi = lo
        while hi < k_all and xsec[hi] <= t + tolerance:
            hi += 1
        for q in range(lo, hi):
            cand[(bi, xo[q])] = abs(t - xsec[q])
    if not cand:
        return []

    # 剔除孤立点：只保留至少有一条候选边的基准 / 导入，压缩成连续子图
    used_b = sorted({bi for bi, _ in cand})
    used_i = sorted({ii for _ , ii in cand})
    bidx = {bi: r for r, bi in enumerate(used_b)}
    iidx = {ii: c for c, ii in enumerate(used_i)}
    n, k = len(used_b), len(used_i)
    bl = [base[bi].line for bi in used_b]
    il = [imports[ii].line for ii in used_i]
    b1, b2 = _lex_bases(bl, il, n)
    edge_cost = {
        (bidx[bi], iidx[ii]): diff * b1 + base[bi].line * b2 + imports[ii].line
        for (bi, ii), diff in cand.items()
    }

    # 稠密（候选占比高）走匈牙利；稀疏走费用流。完整图也归入稠密分支。
    density = len(edge_cost) / (n * k)
    if density >= 0.5:
        rows = _hungarian_dense(edge_cost, n, k, b1, tolerance)
    else:
        rows = _min_cost_flow(edge_cost, n, k)

    return [(used_b[r], used_i[c], cand[(used_b[r], used_i[c])])
            for r, c in rows]


def _merge_item(sw: Swipe, source: str) -> dict:
    return {
        "card": sw.card,
        "time": sw.time_text,
        "station": sw.station,
        "action": sw.action,
        "direction": ACTION_LABELS.get(sw.action, sw.action),
        "line": sw.line,
        "source": source,  # base = 已保存基准记录；import = 待导入记录
    }


def _merge_record_line(card: str, t: datetime, station: str, action: str) -> str:
    return (f"{card}, {t.strftime('%Y-%m-%d %H:%M:%S')}, {station}, "
            f"{'进' if action == ACTION_IN else '出'}")


def external_merge_preview(base_records: list[Swipe],
                           import_records: list[Swipe],
                           tolerance_seconds) -> MergePreview:
    """外部刷卡批次合并预览（全程只读：不改记录、不写/清空账单）。

    - 以**已保存记录为基准**，待导入记录由 parse_records_text() 完成行格式
      解析（解析错误在调用方按原有定位错误返回）；
    - 仅当两条记录的**卡号、站点、进/出方向相同**且时间差不超过
      tolerance 秒（0～120）时建立候选；
    - 按（卡号, 站点, 方向）分组各自做**全局一对一匹配**：先最大化配对数，
      再最小化配对的总绝对时间差，完全同分时以基准行号、导入行号稳定破同分；
    - 返回重复配对、仅基准记录、仅导入记录，以及去重后按时间排序的合并文本
      （配对取基准行，格式与录入文本兼容，可直接复制核对）。
    """
    if not _is_int_value(tolerance_seconds) \
            or not (MERGE_TOLERANCE_MIN_SECONDS
                    <= int(tolerance_seconds) <= MERGE_TOLERANCE_MAX_SECONDS):
        return MergePreview(
            ok=False,
            errors=[_err(
                "CONFIG_BAD",
                f"合并容差必须为 {MERGE_TOLERANCE_MIN_SECONDS}～"
                f"{MERGE_TOLERANCE_MAX_SECONDS} 之间的整数（秒）")])
    tolerance = int(tolerance_seconds)

    groups: dict[tuple[str, str, str],
                 dict[str, list[Swipe]]] = {}
    for r in base_records:
        groups.setdefault((r.card, r.station, r.action),
                          {"base": [], "import": []})["base"].append(r)
    for r in import_records:
        groups.setdefault((r.card, r.station, r.action),
                          {"base": [], "import": []})["import"].append(r)

    pair_objs: list[dict] = []
    matched_base: set[int] = set()   # 以 id() 标记被配对的基准记录
    matched_import: set[int] = set()
    total_diff = 0
    # 合并文本中的条目：(排序键, 文本行)
    merged_entries: list[tuple[tuple, str]] = []

    for key in sorted(groups):
        g = groups[key]
        bs, xs = g["base"], g["import"]
        pairs = _match_group(bs, xs, tolerance)
        for bi, ii, diff in pairs:
            b, x = bs[bi], xs[ii]
            matched_base.add(id(b))
            matched_import.add(id(x))
            total_diff += diff
            pair_objs.append({
                "card": b.card,
                "station": b.station,
                "action": b.action,
                "direction": ACTION_LABELS.get(b.action, b.action),
                "base": _merge_item(b, "base"),
                "import": _merge_item(x, "import"),
                "base_time": b.time_text,
                "import_time": x.time_text,
                "abs_diff_seconds": diff,
                "delta_seconds": int((x.t - b.t).total_seconds()),
            })
            # 配对以基准行计入合并文本
            merged_entries.append((
                (b.t, b.card, b.station, b.action, 0, b.line),
                _merge_record_line(b.card, b.t, b.station, b.action)))

    base_only = [r for r in base_records if id(r) not in matched_base]
    import_only = [r for r in import_records if id(r) not in matched_import]
    for r in base_only:
        merged_entries.append((
            (r.t, r.card, r.station, r.action, 0, r.line),
            _merge_record_line(r.card, r.t, r.station, r.action)))
    for r in import_only:
        merged_entries.append((
            (r.t, r.card, r.station, r.action, 1, r.line),
            _merge_record_line(r.card, r.t, r.station, r.action)))
    merged_entries.sort(key=lambda e: e[0])

    pair_objs.sort(key=lambda p: (p["base"]["time"], p["base"]["line"],
                                  p["import"]["line"]))
    base_only_items = sorted(
        (_merge_item(r, "base") for r in base_only),
        key=lambda it: (it["time"], it["line"]))
    import_only_items = sorted(
        (_merge_item(r, "import") for r in import_only),
        key=lambda it: (it["time"], it["line"]))

    return MergePreview(
        ok=True,
        tolerance_seconds=tolerance,
        base_count=len(base_records),
        import_count=len(import_records),
        pair_count=len(pair_objs),
        total_abs_diff_seconds=total_diff,
        pairs=pair_objs,
        base_only=base_only_items,
        import_only=import_only_items,
        merged_text="\n".join(line for _key, line in merged_entries),
    )
