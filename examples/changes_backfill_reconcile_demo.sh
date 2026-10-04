#!/usr/bin/env bash
# 考古地层关系核验服务 —— 启动历史回填与成果包复核端到端示例
# 前提：
#   1) 服务已启动（docker compose up --build），监听 http://localhost:8000
#      —— 启动时自动执行历史回填：每个未封口修订以紧邻上一正式修订的当时
#         生效断言为前态、本修订为后态补记变化并封口；重复启动幂等。
#   2) 本脚本用普通在线提交演示“修订被正常封口”，再用分页与复核 API 验证。
#      部署前历史（无封口标记的修订）会在服务下次启动时被同一口径自动补齐，
#      无需任何额外调用。
#
# 场景：
#   修订1：E1 甲同一期丙、E2 丙早于乙 → 甲早于乙（路径 [E1,E2]），定版并封存成果包；
#   修订2：撤回 E2（唯一早于桥）→ 甲对乙变未定；
#   分页可见修订2 的撤回记录（旧侧 later_than/[E1,E2] → 新侧 undetermined/[]）；
#   复核成果包：结论改变，首次偏离 = 修订2，来源 formal_batch；
#   修订3：重新断言甲早于乙（直达证据 E3）→ 当前关系恢复，复核变为“仅路径改变”，
#   但首次偏离仍保留在修订2（已恢复一致也保留曾偏离记录）。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }
jget() { python3 -c "import sys,json; print(json.load(sys.stdin)$1)"; }

echo "== 0. 健康检查（启动时已自动完成历史回填） =="
curl -s "$BASE/health" | pp

echo; echo "== 1. 登记地层 甲 / 乙 / 丙 =="
for s in 甲 乙 丙; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"回填复核示例遗址\"}" >/dev/null
done
echo done

echo; echo "== 2. 修订1：E1 甲同一期丙、E2 丙早于乙（甲早于乙，路径 [E1,E2]） =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "BF-E1", "stratum_a": "甲", "stratum_b": "丙", "relation": "same_period"},
    {"op": "assert", "evidence_id": "BF-E2", "stratum_a": "丙", "stratum_b": "乙", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 3. 在修订1定版并把 {甲,乙} 封存为成果包 =="
VERSION=$(curl -s -X POST "$BASE/snapshots" -H 'Content-Type: application/json' \
  -d '{"label": "修订1定版"}' | jget "['version']")
echo "snapshot version=$VERSION"
PKG=$(curl -s -X POST "$BASE/packages" -H 'Content-Type: application/json' \
  -d "{\"version\": $VERSION, \"strata\": [\"甲\", \"乙\"]}" | jget "['id']")
echo "package id=$PKG"

echo; echo "== 4. 修订2：撤回 BF-E2，甲对乙变未定（提交即封口并记录变化） =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "operations": [
    {"op": "retract", "evidence_id": "BF-E2"}
  ]}' | pp

echo; echo "== 5. 关系变化分页：修订2 的撤回以前一正式修订（修订1）为前态 =="
curl -s "$BASE/revision-changes?limit=100" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for c in d['changes']:
    if c['revision'] == 2:
        print(json.dumps(c, ensure_ascii=False, indent=2))
"

echo; echo "== 6. 本轮复核操作（一）：修订2 后复核成果包 =="
echo "    POST /packages/$PKG/reconcile  body {\"expected_revision\": 2}"
curl -s -X POST "$BASE/packages/$PKG/reconcile" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 2}' | python3 -c "
import json,sys
d=json.load(sys.stdin)
p=d['pairs'][0]
print('分类:', p['comparison_label'], '| 封存:', p['sealed']['relation'], p['sealed']['evidence_path'],
      '| 当前:', p['current']['relation'], p['current']['evidence_path'])
fd=p['first_deviation']
print('首次偏离: 修订', fd['revision'], '/', fd['source_label'],
      '|', fd['previous_relation_label'], fd['previous_evidence_path'], '->',
      fd['relation_label'], fd['evidence_path'])
print('汇总:', d['summary'])
"

echo; echo "== 7. 修订3：重新断言甲早于乙（直达证据 BF-E3），关系恢复、路径改变 =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 2,
  "operations": [
    {"op": "assert", "evidence_id": "BF-E3", "stratum_a": "甲", "stratum_b": "乙", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 8. 本轮复核操作（二）：修订3 后复核（仅路径改变，首次偏离仍保留） =="
echo "    POST /packages/$PKG/reconcile  body {\"expected_revision\": 3}"
curl -s -X POST "$BASE/packages/$PKG/reconcile" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 3}' | python3 -c "
import json,sys
d=json.load(sys.stdin)
p=d['pairs'][0]
print('分类:', p['comparison_label'], '| 当前:', p['current']['relation'], p['current']['evidence_path'])
fd=p['first_deviation']
print('首次偏离(保留): 修订', fd['revision'], '/', fd['source_label'])
print('汇总:', d['summary'])
"

echo; echo "== 9. 错误约定：过期修订号 409 STALE_REVISION =="
curl -s -w " [HTTP %{http_code}]\n" -X POST "$BASE/packages/$PKG/reconcile" \
  -H 'Content-Type: application/json' -d '{"expected_revision": 1}'

echo; echo "完成。重启服务（docker compose restart app 或再次启动）可验证："
echo "  回填只处理未封口修订，已封口修订不重写、不重复（分页与复核结果逐字节不变）。"
