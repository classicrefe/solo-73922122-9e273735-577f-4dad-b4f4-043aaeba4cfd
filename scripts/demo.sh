#!/usr/bin/env bash
# 端到端演示：发布 -> 幂等重试 -> 冲突拒绝 -> 取件 -> 两类证明 -> 离线核验 -> 篡改拒绝
#           -> 负荷核算 -> 缺测拒绝 -> 乐观并发重算 -> 查询。
# 用法：BASE=http://127.0.0.1:8001 bash scripts/demo.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8001}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/day1.csv" <<'CSV'
station,time,ph,do
ST01,2026-11-16T08:00,7.2,8.1
CSV
cat > "$WORK/day2.csv" <<'CSV'
station,time,ph,do
ST02,2026-11-16T08:00,7.0,8.4
CSV

echo "== 1. 首次发布 =="
curl -s -X POST "$BASE/releases" \
  -F station_id=ST01 -F sample_date=2026-11-16 -F order_no=ORD-001 \
  -F file=@"$WORK/day1.csv" | python3 -m json.tool

echo "== 2. 同单同输入重试（期望 reused=true）=="
curl -s -X POST "$BASE/releases" \
  -F station_id=ST01 -F sample_date=2026-11-16 -F order_no=ORD-001 \
  -F file=@"$WORK/day1.csv" | python3 -m json.tool

echo "== 3. 同单异输入（期望 HTTP 409）=="
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/releases" \
  -F station_id=ST01 -F sample_date=2026-11-16 -F order_no=ORD-001 \
  -F file=@"$WORK/day2.csv"

echo "== 4. 第二件发布 =="
curl -s -X POST "$BASE/releases" \
  -F station_id=ST02 -F sample_date=2026-11-16 -F order_no=ORD-002 \
  -F file=@"$WORK/day2.csv" | python3 -m json.tool

echo "== 5. 下载并比对原件 =="
curl -s "$BASE/releases/ST01/2026-11-16/ORD-001/download" -o "$WORK/back.csv"
cmp "$WORK/day1.csv" "$WORK/back.csv" && echo "字节一致"

echo "== 6. 纳入证明与离线核验 =="
curl -s "$BASE/releases/ST01/2026-11-16/ORD-001/inclusion-proof" \
  > "$WORK/inclusion.json"
python3 -m app.verify inclusion \
  --proof "$WORK/inclusion.json" --file "$WORK/day1.csv"

echo "== 7. 追加证明（1 号 -> 最新）与离线核验 =="
curl -s "$BASE/checkpoints/1" > "$WORK/cp1.json"
curl -s "$BASE/checkpoints/latest" > "$WORK/cp-latest.json"
curl -s "$BASE/checkpoints/1/append-proof" > "$WORK/append.json"
python3 -m app.verify append \
  --proof "$WORK/append.json" \
  --old-checkpoint "$WORK/cp1.json" \
  --new-checkpoint "$WORK/cp-latest.json"

echo "== 8. 篡改原件（期望离线拒绝，退出码 1）=="
cp "$WORK/day1.csv" "$WORK/tampered.csv"
printf 'X' >> "$WORK/tampered.csv"
python3 -m app.verify inclusion \
  --proof "$WORK/inclusion.json" --file "$WORK/tampered.csv" || true

# --------------------------------------------------------------------------- #
# 负荷核算：同站同日的流量与总磷浓度原件
# --------------------------------------------------------------------------- #
ST="ST-LOAD"; DAY="2026-11-20"
cat > "$WORK/flow.csv" <<'CSV'
start,end,volume_m3,max_flow_m3s
00:00,12:00,1800,1
12:00,24:00,1800,1
CSV
cat > "$WORK/conc.csv" <<'CSV'
start,end,conc_low_mg_l,conc_high_mg_l
00:00,24:00,2,4
CSV

echo "== 9. 发布流量与浓度两份原件 =="
curl -s -X POST "$BASE/releases" \
  -F station_id="$ST" -F sample_date="$DAY" -F order_no=FLOW-001 \
  -F file=@"$WORK/flow.csv" | python3 -m json.tool
curl -s -X POST "$BASE/releases" \
  -F station_id="$ST" -F sample_date="$DAY" -F order_no=CONC-001 \
  -F file=@"$WORK/conc.csv" | python3 -m json.tool

echo "== 10. 首次负荷核算（expected_id 留空，期望 201 + 24 个整点小时区间）=="
curl -s -X POST "$BASE/load-results" -H 'Content-Type: application/json' -d "{
  \"station_id\": \"$ST\", \"sample_date\": \"$DAY\",
  \"flow_order_no\": \"FLOW-001\", \"conc_order_no\": \"CONC-001\"
}" | tee "$WORK/load1.json" | python3 -m json.tool
RID="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["id"])' "$WORK/load1.json")"
echo "现行结果编号：$RID"

echo "== 11. 缺测/坏原件拒绝（流量只覆盖半天，期望 422 且指出缺口时段，旧结果保留）=="
cat > "$WORK/flow_gap.csv" <<'CSV'
start,end,volume_m3,max_flow_m3s
00:00,12:00,9,1
CSV
curl -s -X POST "$BASE/releases" \
  -F station_id="$ST" -F sample_date="$DAY" -F order_no=FLOW-GAP \
  -F file=@"$WORK/flow_gap.csv" > /dev/null
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/load-results" \
  -H 'Content-Type: application/json' -d "{
  \"station_id\": \"$ST\", \"sample_date\": \"$DAY\",
  \"flow_order_no\": \"FLOW-GAP\", \"conc_order_no\": \"CONC-001\",
  \"expected_id\": $RID
}"

echo "== 12. 携带现行编号重算（期望 201，编号 +1）与过期编号冲突（期望 409）=="
curl -s -X POST "$BASE/load-results" -H 'Content-Type: application/json' -d "{
  \"station_id\": \"$ST\", \"sample_date\": \"$DAY\",
  \"flow_order_no\": \"FLOW-001\", \"conc_order_no\": \"CONC-001\",
  \"expected_id\": $RID
}" | tee "$WORK/load2.json" | python3 -m json.tool
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/load-results" \
  -H 'Content-Type: application/json' -d "{
  \"station_id\": \"$ST\", \"sample_date\": \"$DAY\",
  \"flow_order_no\": \"FLOW-001\", \"conc_order_no\": \"CONC-001\",
  \"expected_id\": $RID
}"

echo "== 13. 查询现行结果、历史版本与全部站日目录 =="
curl -s "$BASE/load-results/$ST/$DAY" | python3 -m json.tool
curl -s "$BASE/load-results/$ST/$DAY/revisions" | python3 -m json.tool
curl -s "$BASE/load-results" | python3 -m json.tool
