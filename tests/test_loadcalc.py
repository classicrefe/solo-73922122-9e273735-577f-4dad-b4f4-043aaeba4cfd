"""负荷核算纯函数测试：解析、校验拒绝路径、上下界数值与紧界性质。"""
from __future__ import annotations

import itertools
from fractions import Fraction

import pytest

from app.loadcalc import (
    DAY_SECONDS,
    ConcSegment,
    FlowSegment,
    LoadCalcError,
    compute_report,
    parse_and_validate,
    parse_conc_csv,
    parse_flow_csv,
    validate_segments,
    _window_bounds,
)

F = Fraction


def _flow(rows: str) -> bytes:
    return (
        "start,end,volume_m3,max_flow_m3s\n" + rows
    ).encode()


def _conc(rows: str) -> bytes:
    return (
        "start,end,conc_low_mg_l,conc_high_mg_l\n" + rows
    ).encode()


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def test_parse_accepts_hhmm_seconds_and_z_timestamp():
    # 次日 00:00:00Z 即当日 24:00（左闭右开日末）。
    segs = parse_flow_csv(
        b"start,end,volume_m3,max_flow_m3s\n"
        b"00:00,01:00:10,10,0.5\n"
        b"2026-11-20T01:00:00Z,2026-11-21T00:00:00Z,0,0\n"
    )
    assert segs[0].start == 0 and segs[0].end == 3610  # 01:00:10 = 3610 秒
    assert segs[0].volume == F(10) and segs[0].max_flow == F(1, 2)
    assert segs[1].start == 3600 and segs[1].end == DAY_SECONDS
    # 24:00 右端点
    segs2 = parse_flow_csv(_flow("00:00,24:00,1,1\n"))
    assert segs2[0].end == DAY_SECONDS


def test_parse_rejects_bad_header_empty_and_non_numeric():
    with pytest.raises(LoadCalcError) as ei:
        parse_flow_csv(b"a,b,c,d\n1,2,3,4\n")
    assert "表头" in ei.value.problems[0]
    with pytest.raises(LoadCalcError):
        parse_flow_csv(b"start,end,volume_m3,max_flow_m3s\n")
    with pytest.raises(LoadCalcError) as ei:
        parse_flow_csv(_flow("00:00,01:00,-5,1\n01:00,24:00,0,0\n"))
    assert any("不能为负" in p for p in ei.value.problems)
    with pytest.raises(LoadCalcError) as ei:
        parse_flow_csv(_flow("00:00,01:00,nan,1\n01:00,24:00,0,0\n"))
    assert any("合法数值" in p or "有限" in p for p in ei.value.problems)
    with pytest.raises(LoadCalcError) as ei:
        parse_flow_csv(_flow("01:00,00:00,5,1\n"))
    assert any("左闭右开" in p for p in ei.value.problems)
    with pytest.raises(LoadCalcError) as ei:
        parse_conc_csv(_conc("00:00,01:00,4,2\n"))
    assert any("上界" in p and "下界" in p for p in ei.value.problems)


# --------------------------------------------------------------------------- #
# 校验拒绝路径（任务要求的三类问题时段）
# --------------------------------------------------------------------------- #
def test_reject_flow_not_covering_full_day():
    flow = parse_flow_csv(_flow("00:00,12:00,10,1\n"))
    conc = parse_conc_csv(_conc("00:00,12:00,1,2\n"))
    problems = validate_segments(flow, conc)
    assert any("未覆盖全天" in p and "12:00:00~24:00:00" in p for p in problems)


def test_reject_overlapping_segments():
    flow = parse_flow_csv(
        _flow("00:00,02:00,10,1\n01:00,24:00,0,0\n")
    )
    conc = parse_conc_csv(_conc("00:00,24:00,1,2\n"))
    problems = validate_segments(flow, conc)
    assert any("重叠" in p for p in problems)


def test_reject_volume_exceeding_capacity():
    # q=1 m³/s、3600s 容量 3600 m³，段总水量 3601 矛盾。
    flow = parse_flow_csv(_flow("00:00,01:00,3601,1\n01:00,24:00,0,0\n"))
    conc = parse_conc_csv(_conc("00:00,24:00,1,2\n"))
    problems = validate_segments(flow, conc)
    assert any("超过上限容量" in p for p in problems)


def test_reject_missing_concentration_on_wet_interval():
    # 有水段 00:00~01:00，但浓度只覆盖前半小时。
    flow = parse_flow_csv(_flow("00:00,01:00,100,1\n01:00,24:00,0,0\n"))
    conc = parse_conc_csv(_conc("00:00,00:30,1,2\n"))
    problems = validate_segments(flow, conc)
    assert any("缺浓度" in p and "00:30:00~01:00:00" in p for p in problems)
    # 零水量段不需要浓度：同形状但段水量为 0 时通过。
    flow0 = parse_flow_csv(_flow("00:00,01:00,0,1\n01:00,24:00,0,0\n"))
    assert validate_segments(flow0, conc) == []


def test_parse_and_validate_aggregates_problems():
    with pytest.raises(LoadCalcError) as ei:
        parse_and_validate(
            _flow("00:00,12:00,999999,1\n"),
            _conc("00:00,12:00,1,1\n"),
        )
    joined = " ".join(ei.value.problems)
    assert "未覆盖全天" in joined and "超过上限容量" in joined


# --------------------------------------------------------------------------- #
# 核算数值
# --------------------------------------------------------------------------- #
def test_constant_concentration_full_day():
    flow, conc = parse_and_validate(
        _flow("00:00,24:00,3600,1\n"),
        _conc("00:00,24:00,2,4\n"),
    )
    report = compute_report("S", "2026-11-20", "F", "C", "h1", "h2", flow, conc)
    assert report.day == {"load_low_g": 7200.0, "load_high_g": 14400.0}
    assert len(report.hours) == 24
    # 每小时最多 3600 m³（q×3600），最少 0（段外容量足以容纳全部水量）。
    assert all(h["load_low_g"] == 0 for h in report.hours)
    assert all(h["load_high_g"] == 14400.0 for h in report.hours)
    assert report.hours[0]["window_start"] == "00:00:00"
    assert report.hours[23]["window_end"] == "00:00:00"  # 24:00 归一展示


def test_two_concentration_pieces_greedy_order():
    flow, conc = parse_and_validate(
        _flow("00:00,01:00,1800,1\n01:00,24:00,0,0\n"),
        _conc("00:00,00:30,1,2\n00:30,01:00,3,4\n"),
    )
    report = compute_report("S", "d", "F", "C", "a", "b", flow, conc)
    # w_max=1800：上界把水全部灌到 c_hi=4 的片 -> 7200 g。
    # w_min=1800（段外容量 0）：下界从 c_lo=1 的片灌起 -> 1800 g。
    assert report.hours[0]["load_low_g"] == 1800
    assert report.hours[0]["load_high_g"] == 7200
    assert report.day == {"load_low_g": 1800, "load_high_g": 7200}
    # 其余 23 小时为干枯小时，上下界均为 0。
    assert all(
        h["load_low_g"] == 0 and h["load_high_g"] == 0
        for h in report.hours[1:]
    )


def test_water_forced_into_hour_by_outside_capacity():
    # 段跨 2 小时、总量 5400、q=1：每小时容量 3600，段外容量 3600，
    # 故任一小时 w_min = 5400-3600 = 1800、w_max = 3600。
    flow, conc = parse_and_validate(
        _flow("00:00,02:00,5400,1\n02:00,24:00,0,0\n"),
        _conc("00:00,24:00,2,2\n"),
    )
    report = compute_report("S", "d", "F", "C", "a", "b", flow, conc)
    assert report.hours[0] == {
        "hour": 0,
        "window_start": "00:00:00",
        "window_end": "01:00:00",
        "load_low_g": 3600,
        "load_high_g": 7200,
    }
    assert report.hours[1]["load_low_g"] == 3600
    # 全日：全部 5400 m³ × 2 mg/L = 10800 g，上下界收敛。
    assert report.day == {"load_low_g": 10800, "load_high_g": 10800}


def test_zero_volume_day_yields_zero_load():
    flow, conc = parse_and_validate(
        _flow("00:00,12:00,0,1\n12:00,24:00,0,0\n"),
        _conc("00:00,12:00,1,1\n"),
    )
    report = compute_report("S", "d", "F", "C", "a", "b", flow, conc)
    assert report.day == {"load_low_g": 0, "load_high_g": 0}
    assert all(h["load_low_g"] == 0 and h["load_high_g"] == 0 for h in report.hours)


def test_fractional_values_exact():
    # q = 1/3600 m³/s，小时容量恰为 1 m³；V=1，浓度 1/3 mg/L。
    flow, conc = parse_and_validate(
        _flow("00:00,01:00,1,0.00027777777777777777777777777778\n"
              "01:00,24:00,0,0\n"),
        _conc("00:00,24:00,0.33333333333333333333,1\n"),
    )
    report = compute_report("S", "d", "F", "C", "a", "b", flow, conc)
    # 全日上界 1 m³ × 1 mg/L = 1 g；下界 1 × 1/3 g。
    assert report.day["load_high_g"] == 1
    assert report.day["load_low_g"] == pytest.approx(F(1, 3), rel=1e-12)


# --------------------------------------------------------------------------- #
# 紧界：离散暴力枚举（全天 6 槽，枚举所有可行水量分配）
# --------------------------------------------------------------------------- #
SLOT = DAY_SECONDS // 6


def _feasible(n_slots, q, v):
    q, v = int(q), int(v)

    def rec(k, left):
        if k == 1:
            if 0 <= left <= q:
                yield (left,)
            return
        for x in range(min(q, left) + 1):
            for tail in rec(k - 1, left - x):
                yield (x,) + tail

    yield from rec(n_slots, v)


def test_greedy_bounds_match_brute_force_enumeration():
    import random

    random.seed(7)
    for trial in range(25):
        nseg = random.randint(1, 3)
        bounds = sorted(random.sample(range(1, 6), nseg - 1)) if nseg > 1 else []
        edges = [0] + bounds + [6]
        flow, seg_q = [], []
        for i in range(nseg):
            s, e = edges[i] * SLOT, edges[i + 1] * SLOT
            d = (e - s) // SLOT
            q = random.randint(0, 3)
            v = random.randint(0, q * d)
            seg_q.append(q)
            flow.append(FlowSegment(s, e, F(v), F(q) / F(SLOT)))
        nc = random.randint(1, 3)
        cb = sorted(random.sample(range(1, 6), nc - 1)) if nc > 1 else []
        cedges = [0] + cb + [6]
        conc = []
        for i in range(nc):
            lo = F(random.randint(0, 4))
            conc.append(
                ConcSegment(cedges[i] * SLOT, cedges[i + 1] * SLOT, lo, lo + F(random.randint(0, 3)))
            )
        assert validate_segments(flow, conc) == []

        per_seg = [
            list(_feasible(seg.duration // SLOT, q, seg.volume))
            for seg, q in zip(flow, seg_q)
        ]
        assert all(per_seg)

        def slot_conc(t):
            for c in conc:
                if c.start <= t < c.end:
                    return c.conc_low, c.conc_high
            raise AssertionError("no conc")

        windows = [(h * SLOT, (h + 1) * SLOT) for h in range(6)] + [
            (0, 3 * SLOT),
            (2 * SLOT, 5 * SLOT),
            (0, DAY_SECONDS),
        ]
        true_lo = {w: None for w in windows}
        true_hi = {w: None for w in windows}
        for combo in itertools.product(*per_seg):
            slots = []
            for seg, alloc in zip(flow, combo):
                for j, w in enumerate(alloc):
                    t = seg.start + j * SLOT
                    slots.append((t, F(w), *slot_conc(t)))
            for ws, we in windows:
                inside = [s for s in slots if ws <= s[0] < we]
                lo = sum(w * c_lo for _, w, c_lo, _ in inside)
                hi = sum(w * c_hi for _, w, _, c_hi in inside)
                k = (ws, we)
                true_lo[k] = lo if true_lo[k] is None else min(true_lo[k], lo)
                true_hi[k] = hi if true_hi[k] is None else max(true_hi[k], hi)
        for ws, we in windows:
            got = _window_bounds(flow, conc, ws, we)
            assert got.low == true_lo[(ws, we)], (trial, (ws, we), got.low, true_lo[(ws, we)])
            assert got.high == true_hi[(ws, we)], (trial, (ws, we), got.high, true_hi[(ws, we)])
