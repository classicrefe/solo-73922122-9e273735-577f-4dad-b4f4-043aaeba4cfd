"""负荷上下界核算（纯函数，不依赖 Web/数据库）。

输入
----
* 流量表：每段给出 UTC 日内左闭右开时段、段总水量 V（m³）与瞬时流量上限 q（m³/s）；
* 浓度表：每段给出 UTC 日内左闭右开时段与总磷浓度上下界 c_lo/c_hi（mg/L）。
两表表内时段互不重叠，数值均须有限且非负。

核算模型
--------
段内水量在时间上的分布未知，只受两个约束：
  1. 每段总水量固定为 V；
  2. 任一瞬时流量不超过 q，故时长 d 秒的子区间最多容纳 q*d 立方米。
负荷单位换算恒等式：1 mg/L × 1 m³ = 1 g（1 m³ = 1000 L），因此
任意子区间负荷（克）= 浓度(mg/L) × 水量(m³)。

对任意窗口 W（整点小时或全日），流量段 i 在 W 内可取水量满足
  w_min_i(W) ≤ w_i(W) ≤ w_max_i(W)
其中
  w_max_i = min(V_i, q_i·|F_i∩W|)          （子区间容量上限）
  w_min_i = max(0, V_i − q_i·|F_i\\W|)      （段外容量放不下的水量必在窗内）
把 F_i∩W 按浓度段切成原子片 k（容量 cap_k、浓度区间 [a_k, b_k]）后：
  * 上界：把可取水量按浓度上界 b_k 从高到低贪心灌满；
  * 下界：把必取水量按浓度下界 a_k 从低到高贪心灌起。
线性规划极值在贪心极点取得，上下界均可由满足全部约束的分段流量分配实际达到。

内部全部使用 Fraction 有理精确运算，避免浮点/Decimal 倒数往返产生的残差
（如 (1/14400)*14400 ≠ 1）；仅在 JSON 输出时转为 int/float。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Sequence

DAY_SECONDS = 86_400
# 1 mg/L × 1 m³ = 1 g（mg 与 L 的 1000 倍因子恰好相消）。
ZERO = Fraction(0)

_FLOW_HEADER = ["start", "end", "volume_m3", "max_flow_m3s"]
_CONC_HEADER = ["start", "end", "conc_low_mg_l", "conc_high_mg_l"]

# 接受 HH:MM、HH:MM:SS、带 Z 的 UTC 时间戳；一律按 UTC 解释为日内秒。
# 右端点允许 24:00[:00] 表示日末（左闭右开约定下唯一合法的 24 点用法）。
_TIME_RE = re.compile(
    r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$"
    r"|^24:00(?::00)?$"
    r"|^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z$"
)


class LoadCalcError(ValueError):
    """原件校验/核算失败；problems 逐条指出问题时段与原因。"""

    def __init__(self, message: str, problems: Sequence[str] | None = None):
        super().__init__(message)
        self.message = message
        self.problems = list(problems or [message])


@dataclass(frozen=True)
class FlowSegment:
    start: int            # 日内秒，左闭
    end: int              # 日内秒，右开
    volume: Fraction      # 段总水量 m³
    max_flow: Fraction    # 瞬时流量上限 m³/s

    @property
    def duration(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class ConcSegment:
    start: int
    end: int
    conc_low: Fraction
    conc_high: Fraction


@dataclass
class LoadBounds:
    low: Fraction
    high: Fraction

    def as_dict(self) -> dict:
        return {"load_low_g": json_number(self.low), "load_high_g": json_number(self.high)}


@dataclass
class LoadReport:
    station_id: str
    sample_date: str
    flow_order_no: str
    conc_order_no: str
    flow_blob_sha256: str
    conc_blob_sha256: str
    hours: list[dict] = field(default_factory=list)
    day: dict = field(default_factory=dict)


def fmt(value: Fraction) -> str:
    """诊断信息用的可读分数格式。"""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def json_number(value: Fraction):
    """Fraction → JSON 数值：整值输出 int，其余输出 float。"""
    if value.denominator == 1:
        return value.numerator
    return float(value)


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def _parse_clock(value: str) -> int:
    """把 UTC 时间表达式解析为日内秒（0..86400），零点歧义由调用方消歧。

    24:00 形式恒为日末 86400；带日期的零点时间戳（如次日 00:00:00Z）同时可表示
    “当日起点 0”或“上一日日末 86400”，故返回候选元组交由行级解析按 start/end
    角色选取；其余时间只有唯一解释。
    """
    text = value.strip()
    if not _TIME_RE.match(text):
        raise ValueError(f"时间 {value!r} 不是合法 UTC 时间（HH:MM[:SS] 或 ...Z）")
    if "T" in text or " " in text:
        normalized = text.replace(" ", "T")
        body = normalized[:-1] if normalized.endswith("Z") else normalized
        fmt_str = (
            "%Y-%m-%dT%H:%M:%S.%f" if "." in normalized else "%Y-%m-%dT%H:%M:%S"
        )
        dt = datetime.strptime(body, fmt_str).replace(tzinfo=timezone.utc)
        seconds = dt.hour * 3600 + dt.minute * 60 + dt.second
        if seconds == 0 and dt.microsecond == 0:
            return (0, DAY_SECONDS)
        return seconds
    parts = text.split(":")
    hour, minute = int(parts[0]), int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    if hour == 24:
        if minute != 0 or second != 0:
            raise ValueError(f"时间 {value!r} 非法：24 点仅允许 24:00 表示日末")
        return DAY_SECONDS
    return hour * 3600 + minute * 60 + second


def _resolve_endpoint(raw: str, role: str) -> int:
    """role 为 'start' 时零点取 0；为 'end' 时优先取日末 86400。"""
    parsed = _parse_clock(raw)
    if isinstance(parsed, tuple):
        return 0 if role == "start" else DAY_SECONDS
    return parsed


def _parse_number(raw: str, field_name: str) -> Fraction:
    text = raw.strip()
    if text == "":
        raise ValueError(f"{field_name} 为空")
    try:
        # Decimal 先做严格词法校验（拒绝 nan/inf/科学计数法拼写异常等），
        # 再无损转为 Fraction。
        decimal = Decimal(text)
    except Exception as exc:  # InvalidOperation 等
        raise ValueError(f"{field_name}={raw!r} 不是合法数值") from exc
    if not decimal.is_finite():
        raise ValueError(f"{field_name}={raw!r} 必须有限")
    if decimal < 0:
        raise ValueError(f"{field_name}={raw!r} 不能为负")
    return Fraction(decimal)


def _read_rows(data: bytes, expected_header: Sequence[str], table_name: str):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LoadCalcError(f"{table_name}不是 UTF-8 编码文本: {exc}") from exc
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise LoadCalcError(f"{table_name}为空文件，无表头与数据行")
    header = [cell.strip() for cell in rows[0]]
    if header != list(expected_header):
        raise LoadCalcError(
            f"{table_name}表头必须为 {','.join(expected_header)}，"
            f"实际为 {','.join(header) or '空'}"
        )
    return rows[1:]


def parse_flow_csv(data: bytes) -> list[FlowSegment]:
    table = "流量表"
    rows = _read_rows(data, _FLOW_HEADER, table)
    segments: list[FlowSegment] = []
    problems: list[str] = []
    for idx, row in enumerate(rows, start=2):  # 含表头的 CSV 行号
        if len(row) != len(_FLOW_HEADER):
            problems.append(f"流量表第 {idx} 行列数={len(row)}，应为 {len(_FLOW_HEADER)}")
            continue
        try:
            start = _resolve_endpoint(row[0], "start")
            end = _resolve_endpoint(row[1], "end")
            volume = _parse_number(row[2], "volume_m3")
            max_flow = _parse_number(row[3], "max_flow_m3s")
        except ValueError as exc:
            problems.append(f"流量表第 {idx} 行：{exc}")
            continue
        if end <= start:
            problems.append(
                f"流量表第 {idx} 行时段 {row[0].strip()}~{row[1].strip()} "
                f"非左闭右开（end 须晚于 start）"
            )
            continue
        if start < 0 or end > DAY_SECONDS:
            problems.append(f"流量表第 {idx} 行时段超出 UTC 当日 [00:00, 24:00) 范围")
            continue
        segments.append(FlowSegment(start, end, volume, max_flow))
    if problems:
        raise LoadCalcError("流量表校验失败", problems)
    if not segments:
        raise LoadCalcError("流量表无任何数据行")
    return segments


def parse_conc_csv(data: bytes) -> list[ConcSegment]:
    table = "浓度表"
    rows = _read_rows(data, _CONC_HEADER, table)
    segments: list[ConcSegment] = []
    problems: list[str] = []
    for idx, row in enumerate(rows, start=2):
        if len(row) != len(_CONC_HEADER):
            problems.append(f"浓度表第 {idx} 行列数={len(row)}，应为 {len(_CONC_HEADER)}")
            continue
        try:
            start = _resolve_endpoint(row[0], "start")
            end = _resolve_endpoint(row[1], "end")
            low = _parse_number(row[2], "conc_low_mg_l")
            high = _parse_number(row[3], "conc_high_mg_l")
        except ValueError as exc:
            problems.append(f"浓度表第 {idx} 行：{exc}")
            continue
        if end <= start:
            problems.append(
                f"浓度表第 {idx} 行时段 {row[0].strip()}~{row[1].strip()} "
                f"非左闭右开（end 须晚于 start）"
            )
            continue
        if start < 0 or end > DAY_SECONDS:
            problems.append(f"浓度表第 {idx} 行时段超出 UTC 当日 [00:00, 24:00) 范围")
            continue
        if high < low:
            problems.append(
                f"浓度表第 {idx} 行浓度上界 {fmt(high)} 小于下界 {fmt(low)}"
            )
            continue
        segments.append(ConcSegment(start, end, low, high))
    if problems:
        raise LoadCalcError("浓度表校验失败", problems)
    if not segments:
        raise LoadCalcError("浓度表无任何数据行")
    return segments


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def _check_non_overlap(segments, table_name: str) -> list[str]:
    problems: list[str] = []
    ordered = sorted(segments, key=lambda s: (s.start, s.end))
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.start < prev.end:
            problems.append(
                f"{table_name}时段重叠：[{prev.start},{prev.end}) 与 "
                f"[{cur.start},{cur.end})"
            )
    return problems


def _interval_label(start: int, end: int) -> str:
    def hms(sec: int) -> str:
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    return f"{hms(start)}~{hms(end)}"


def validate_segments(
    flow: Sequence[FlowSegment], conc: Sequence[ConcSegment]
) -> list[str]:
    """跨段校验：不重叠、流量覆盖全天、容量不矛盾、有水时段不缺浓度。"""
    problems = _check_non_overlap(flow, "流量表")
    problems += _check_non_overlap(conc, "浓度表")
    if problems:
        return problems

    # 流量须连续覆盖全天（左闭右开拼接后恰为 [0, 86400)）。
    ordered = sorted(flow, key=lambda s: s.start)
    cursor = 0
    for seg in ordered:
        if seg.start > cursor:
            problems.append(
                f"流量未覆盖全天：缺口时段 {_interval_label(cursor, seg.start)} 无流量段"
            )
        elif seg.start < cursor:  # 重叠已在上面拦截，这里防御性处理
            problems.append(
                f"流量表时段衔接异常：{_interval_label(seg.start, seg.end)}"
            )
        cursor = max(cursor, seg.end)
    if cursor < DAY_SECONDS:
        problems.append(
            f"流量未覆盖全天：缺口时段 {_interval_label(cursor, DAY_SECONDS)} 无流量段"
        )

    # 段总水量与瞬时上限矛盾：V > q·d 在任何分配下都不可能成立。
    for seg in ordered:
        capacity = seg.max_flow * seg.duration
        if seg.volume > capacity:
            problems.append(
                f"流量段 {_interval_label(seg.start, seg.end)} 总水量 {fmt(seg.volume)} "
                f"m³ 超过上限容量 q·d = {fmt(seg.max_flow)}×{seg.duration}s = "
                f"{fmt(capacity)} m³"
            )

    # 有水量的时段必须能在浓度表中找到完整覆盖（零水量段无需浓度）。
    conc_sorted = sorted(conc, key=lambda s: s.start)
    for seg in ordered:
        if seg.volume == 0:
            continue
        covered_until = seg.start
        for cseg in conc_sorted:
            if cseg.end <= covered_until:
                continue
            if cseg.start > covered_until:
                break
            covered_until = max(covered_until, min(cseg.end, seg.end))
            if covered_until >= seg.end:
                break
        if covered_until < seg.end:
            problems.append(
                f"有水时段缺浓度：流量段 {_interval_label(seg.start, seg.end)} 的 "
                f"{_interval_label(covered_until, seg.end)} 部分无浓度段覆盖"
            )
    return problems


# --------------------------------------------------------------------------- #
# 核算
# --------------------------------------------------------------------------- #
def _concentration_pieces(
    seg: FlowSegment, conc: Sequence[ConcSegment]
) -> list[tuple[int, int, Fraction, Fraction]]:
    """把流量段与浓度表求交，返回 (片起点, 片终点, 浓度下界, 浓度上界) 原子片。"""
    pieces: list[tuple[int, int, Fraction, Fraction]] = []
    for cseg in sorted(conc, key=lambda s: s.start):
        lo = max(seg.start, cseg.start)
        hi = min(seg.end, cseg.end)
        if hi > lo:
            pieces.append((lo, hi, cseg.conc_low, cseg.conc_high))
    return pieces


def _window_bounds(
    flow: Sequence[FlowSegment],
    conc: Sequence[ConcSegment],
    win_start: int,
    win_end: int,
) -> LoadBounds:
    """计算窗口 [win_start, win_end) 内可达到的负荷上下界（克）。"""
    low_total = ZERO
    high_total = ZERO
    for seg in flow:
        if seg.end <= win_start or seg.start >= win_end:
            continue
        inside_lo = max(seg.start, win_start)
        inside_hi = min(seg.end, win_end)
        inside_seconds = inside_hi - inside_lo
        outside_seconds = seg.duration - inside_seconds

        w_max = min(seg.volume, seg.max_flow * inside_seconds)
        w_min = max(ZERO, seg.volume - seg.max_flow * outside_seconds)
        # 容量校验保证 w_min ≤ w_max；再夹一次防止边界口径分歧。
        w_min = min(w_min, w_max)

        pieces = _concentration_pieces(
            FlowSegment(inside_lo, inside_hi, seg.volume, seg.max_flow), conc
        )
        # 每片容量 = q × 片长（秒）。
        caps = [
            (seg.max_flow * (hi - lo), c_lo, c_hi)
            for lo, hi, c_lo, c_hi in pieces
        ]
        # 上界：按浓度上界降序灌满可取水量。
        remaining = w_max
        for cap, _c_lo, c_hi in sorted(caps, key=lambda item: item[2], reverse=True):
            take = min(remaining, cap)
            if take > 0:
                high_total += take * c_hi
                remaining -= take
        # 下界：按浓度下界升序灌起必取水量。
        remaining = w_min
        for cap, c_lo, _c_hi in sorted(caps, key=lambda item: item[1]):
            take = min(remaining, cap)
            if take > 0:
                low_total += take * c_lo
                remaining -= take
    return LoadBounds(low=low_total, high=high_total)


def compute_report(
    station_id: str,
    sample_date: str,
    flow_order_no: str,
    conc_order_no: str,
    flow_blob_sha256: str,
    conc_blob_sha256: str,
    flow: Sequence[FlowSegment],
    conc: Sequence[ConcSegment],
) -> LoadReport:
    """核算 24 个整点小时与全日的负荷上下界。"""
    hours: list[dict] = []
    for hour in range(24):
        start = hour * 3600
        end = start + 3600
        bounds = _window_bounds(flow, conc, start, end)
        hours.append(
            {
                "hour": hour,
                "window_start": f"{hour:02d}:00:00",
                "window_end": f"{(hour + 1) % 24:02d}:00:00",
                **bounds.as_dict(),
            }
        )
    day = _window_bounds(flow, conc, 0, DAY_SECONDS)
    return LoadReport(
        station_id=station_id,
        sample_date=sample_date,
        flow_order_no=flow_order_no,
        conc_order_no=conc_order_no,
        flow_blob_sha256=flow_blob_sha256,
        conc_blob_sha256=conc_blob_sha256,
        hours=hours,
        day={"load_low_g": json_number(day.low), "load_high_g": json_number(day.high)},
    )


def parse_and_validate(
    flow_csv: bytes, conc_csv: bytes
) -> tuple[list[FlowSegment], list[ConcSegment]]:
    """解析两份原件并执行全部原件级校验，失败时聚合问题后一次性抛出。"""
    flow = parse_flow_csv(flow_csv)
    conc = parse_conc_csv(conc_csv)
    problems = validate_segments(flow, conc)
    if problems:
        raise LoadCalcError("原件时段校验失败", problems)
    return flow, conc
