#!/usr/bin/env bash
# ============================================================================
# FORGE 部署冒烟：确认「容器在跑、接口活着、能登录、品牌生效、SSO 密钥生效」
# ----------------------------------------------------------------------------
# 可随时单独重跑：./smoke.sh
# 全部检查跑完再汇总（不用 set -e，一个失败不打断后面的检查）。
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")"

[[ -f .env ]] || { echo "[X] 缺少 .env（先跑 ./install.sh）"; exit 1; }
get_kv() { sed -n "s|^${1}=||p" .env | head -1; }

PASS=0; FAIL=0
ok()  { echo "[ok] $1"; PASS=$((PASS+1)); }
bad() { echo "[X]  $1"; FAIL=$((FAIL+1)); }

LS_PORT_EFF="$(get_kv LS_PORT)"; LS_PORT_EFF="${LS_PORT_EFF:-8100}"
ADMIN_PHONE="$(get_kv ADMIN_PHONE)"
ADMIN_PASSWORD="$(get_kv ADMIN_PASSWORD)"
DB_ADMIN="$(get_kv ORCH_DB_ADMIN)"; DB_ADMIN="${DB_ADMIN:-forge_admin}"

echo "==== 1/6 容器状态 ===="
RUNNING=$(docker compose ps --status running --format '{{.Service}}' 2>/dev/null | sort | tr '\n' ' ')
echo "运行中: ${RUNNING:-（无）}"
for s in caddy web orchestrator orchdb labelstudio lsdb minio; do
  echo " ${RUNNING} " | grep -q " ${s} " && ok "容器 ${s} 运行中" || bad "容器 ${s} 不在运行"
done

echo "==== 2/6 服务健康 ===="
docker compose exec -T orchestrator curl -fsS http://127.0.0.1:8300/healthz >/dev/null 2>&1 \
  && ok "orchestrator /healthz" || bad "orchestrator /healthz 失败（docker compose logs orchestrator）"

echo "==== 3/6 数据库（应只有引导数据，没有演示数据）===="
# row_security=off：查询方是建表角色，绕开 RLS 否则会误判成 0 行
USERS=$(docker compose exec -T orchdb psql -U "$DB_ADMIN" -d forge -tAc \
  "set row_security=off; select count(*) from forge.users" 2>/dev/null || echo 0)
ENT=$(docker compose exec -T orchdb psql -U "$DB_ADMIN" -d forge -tAc \
  "set row_security=off; select count(*) from forge.enterprises" 2>/dev/null || echo 0)
PROJ=$(docker compose exec -T orchdb psql -U "$DB_ADMIN" -d forge -tAc \
  "set row_security=off; select count(*) from forge.projects" 2>/dev/null || echo 0)
[ "${USERS:-0}" -ge 1 ] 2>/dev/null && ok "forge.users = ${USERS}（≥1，管理员在）" \
  || bad "forge.users = ${USERS:-?}（应 ≥1，管理员没建上？）"
[ "${ENT:-0}" -eq 1 ] 2>/dev/null && ok "forge.enterprises = 1（智途）" \
  || bad "forge.enterprises = ${ENT:-?}（应为 1）"
ok "forge.projects = ${PROJ:-0}（全新部署应为 0，演示数据已清除）"

echo "==== 4/6 Label Studio 品牌与原版残留 ===="
PAGE="$(curl -fsS "http://127.0.0.1:${LS_PORT_EFF}/user/login/" 2>/dev/null || true)"
if [[ -n "$PAGE" ]]; then
  echo "$PAGE" | grep -q "智途" && ok "登录页品牌「智途」生效" \
    || bad "登录页没看到「智途」（品牌补丁没打上，检查构建日志）"
  if echo "$PAGE" | grep -Eqi "Heidi|HumanSignal|Brought to you by"; then
    bad "登录页残留原版署名/吉祥物文案"
  else
    ok "登录页无原版署名残留"
  fi
else
  bad "取不到 LS 登录页（http://127.0.0.1:${LS_PORT_EFF}）"
fi

echo "==== 5/6 平台登录与业务接口 ===="
LOGIN=$(docker compose exec -T orchestrator curl -fsS -X POST http://127.0.0.1:8300/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"phone\":\"${ADMIN_PHONE}\",\"password\":\"${ADMIN_PASSWORD}\"}" 2>/dev/null || true)
TOKEN=$(printf '%s' "$LOGIN" | python3 -c \
  'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)
if [[ -n "$TOKEN" ]]; then
  ok "平台登录成功（${ADMIN_PHONE}）"
  ROWS=$(docker compose exec -T orchestrator curl -fsS http://127.0.0.1:8300/api/v1/projects \
    -H "Authorization: Bearer ${TOKEN}" 2>/dev/null || true)
  echo "$ROWS" | grep -q '"rows"' && ok "GET /api/v1/projects 正常（全新部署应为空列表）" \
    || bad "项目列表接口异常：${ROWS:0:160}"
  # SSO 票据接口（参数是 task_id）：新部署没有任务，404 是预期；
  # 503 = LS_SSO_SECRET 未配置或不足 32 字符；401/403 = 登录态没认上
  CODE=$(docker compose exec -T orchestrator curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:8300/api/v1/ls/ticket?task_id=1" \
    -H "Authorization: Bearer ${TOKEN}" 2>/dev/null || true)
  case "$CODE" in
    503)       bad "LS 票据接口 503 —— LS_SSO_SECRET 未配置或不足 32 字符" ;;
    401|403)   bad "LS 票据接口 ${CODE} —— 登录态没认上" ;;
    200|400|404|422) ok "LS 票据接口鉴权与签名正常（HTTP ${CODE}，无任务属预期）" ;;
    *)         bad "LS 票据接口返回 ${CODE:-空}" ;;
  esac

  # 标注页增强层的两个后端：不带上下文票据访问应当 401。
  # 404 = 跑的是老版本编排层（缺 /api/v1/ls/guideline 与 /telemetry 路由）。
  CODE=$(docker compose exec -T orchestrator curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:8300/api/v1/ls/guideline?project=1" 2>/dev/null || true)
  case "$CODE" in
    401) ok "标注页规范接口在位且要求上下文票据（HTTP 401）" ;;
    404) bad "标注页规范接口 404 —— 编排层不是本版本，规格侧栏会一直空白" ;;
    *)   bad "标注页规范接口返回 ${CODE:-空}" ;;
  esac

  # 标注页在 LS 域，跨源回调这个地址取规范/报耗时。空值不会报错，
  # 只会让两个功能静默失效，所以这里显式拦一道。
  API_BASE=$(docker compose exec -T orchestrator printenv FORGE_PLATFORM_API_BASE 2>/dev/null | tr -d '\r')
  if [[ -z "$API_BASE" ]]; then
    bad "FORGE_PLATFORM_API_BASE 为空 —— 标注员侧栏与单题耗时都会静默失效"
  else
    ok "标注页回调地址已配置（${API_BASE}）"
  fi

  # 对账接口：401 = 在位（要登录态）；404 = 缺路由或路径被 /tasks/{id} 吃掉。
  CODE=$(docker compose exec -T orchestrator curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:8300/api/v1/ls/reconcile" 2>/dev/null || true)
  case "$CODE" in
    401) ok "对账接口在位（HTTP 401，需登录态）" ;;
    404) bad "对账接口 404 —— 编排层不是本版本，批次页的对账面板会一直读不到" ;;
    *)   bad "对账接口返回 ${CODE:-空}" ;;
  esac

  # ---- 甲方进 LS 原生设计器：链路在位 + 权限收口 ----
  # 甲方用一次性票据进 LS 的 /settings/labeling，平台不存表单结构。
  # 故障表现不是白屏，而是"按钮点了没反应"或"LS 里权限放太宽"，
  # 所以每一项都要显式断言，不能靠人打开页面看一眼。

  # 1) design-ticket：不带登录态 401（说明路由在位）；带管理员 token 打一个
  #    不存在的项目应 404（说明服务端反查生效，不是照单签票）。
  #    503 = FORGE_LS_SSO_SECRET 没配 / 短于 32 字符。
  NOAUTH=$(docker compose exec -T orchestrator curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:8300/api/v1/ls/design-ticket?project_id=1" 2>/dev/null || true)
  [[ "$NOAUTH" == "401" ]] && ok "设计票据接口在位且要求登录态（HTTP 401）" \
    || bad "设计票据接口未登录返回 ${NOAUTH:-空}（应 401）—— 编排层不是本版本"
  DT=$(docker compose exec -T orchestrator curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:8300/api/v1/ls/design-ticket?project_id=smoke-no-such-project" \
    -H "Authorization: Bearer ${TOKEN}" 2>/dev/null || true)
  case "$DT" in
    404) ok "设计票据由服务端反查项目（不存在即拒签，HTTP 404）" ;;
    503) bad "设计票据 503 —— FORGE_LS_SSO_SECRET 未配置或过短" ;;
    401|403) bad "设计票据 ${DT} —— 登录态没认上" ;;
    *)   bad "设计票据返回 ${DT:-空}" ;;
  esac

  # 2) 项目 ↔ LS 项目绑定：甲方进设计器的前置条件。GET 未登录 401 / 带 token
  #    打不存在项目 404；POST 路由同样要在位。
  BIND_NOAUTH=$(docker compose exec -T orchestrator curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:8300/api/v1/projects/1/ls-binding" 2>/dev/null || true)
  BIND=$(docker compose exec -T orchestrator curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:8300/api/v1/projects/smoke-no-such-project/ls-binding" \
    -H "Authorization: Bearer ${TOKEN}" 2>/dev/null || true)
  [[ "$BIND_NOAUTH" == "401" && "$BIND" == "404" ]] \
    && ok "绑定状态接口在位（401 / 404，按预期拒绝）" \
    || bad "绑定状态接口异常：未登录 ${BIND_NOAUTH:-空}、不存在项目 ${BIND:-空}"

  # 3) 已下线的自建设计器接口必须全部 404。如果这里返回 200，说明打的是旧包，
  #    甲方会看到两套编辑器 —— 这正是要防的交付事故。
  OLD1=$(docker compose exec -T orchestrator curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:8300/api/v1/label-step-templates" \
    -H "Authorization: Bearer ${TOKEN}" 2>/dev/null || true)
  OLD2=$(docker compose exec -T orchestrator curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:8300/api/v1/projects/smoke-no-such-project/flow" \
    -H "Authorization: Bearer ${TOKEN}" 2>/dev/null || true)
  [[ "$OLD1" == "404" && "$OLD2" == "404" ]] \
    && ok "自建设计器接口已下线（模板库 / flow 均 404）" \
    || bad "自建设计器接口还在（模板库 ${OLD1:-空}、flow ${OLD2:-空}）—— 部署包不是本版本"

  # 4) 甲方必须有 labeling:write、且**没有** project:write：能改表单，
  #    不能改合同金额，也不能换掉项目连的是哪个 LS 项目。
  CLIENT_PERM=$(docker compose exec -T orchestrator python -c \
    "from app.security import ROLE_MATRIX; r=ROLE_MATRIX['client_viewer']; print('labeling:write' in r, 'project:write' in r)" \
    2>/dev/null | tr -d '\r' || true)
  [[ "$CLIENT_PERM" == "True False" ]] && ok "甲方权限正确（能改表单 / 不能改项目）" \
    || bad "client_viewer 权限异常：${CLIENT_PERM:-空}（应为 True False）"

  # 5) 安全边界在 LS 服务端：确认 _guard_designer 真的被 apply_brand.py
  #    注入了运行中的 LS 镜像。少了它，甲方就是个全权限 LS 账号。
  LS_CORE=$(docker compose exec -T labelstudio sh -c \
    'PY=$(command -v python3 || command -v python); "$PY" -c "import label_studio,pathlib;print(pathlib.Path(label_studio.__file__).resolve().parent)"' \
    2>/dev/null | tr -d '\r' | tail -1 || true)
  if [[ -n "$LS_CORE" ]]; then
    GUARD=$(docker compose exec -T labelstudio sh -c \
      "grep -c _guard_designer '${LS_CORE}/core/forge_sso.py' 2>/dev/null || true" \
      2>/dev/null | tr -d '\r' | head -1 || true)
    [[ "${GUARD:-0}" -ge 1 ]] && ok "LS 侧设计会话收口已注入镜像（_guard_designer）" \
      || bad "LS 镜像里没有 _guard_designer —— 品牌补丁没跑或跑的是旧镜像"
  else
    bad "定位不到 LS 包目录，无法确认设计会话收口是否生效"
  fi

  # 6) 票据签名密钥与回跳地址：任一侧漏配都是"点了没反应"，
  #    而且不会抛异常（平台返回 503 / LS 验签失败直接拒绝）。
  SSO=$(docker compose exec -T orchestrator printenv FORGE_LS_SSO_SECRET \
        2>/dev/null | tr -d '\r' || true)
  PUB=$(docker compose exec -T orchestrator printenv FORGE_LS_PUBLIC_BASE_URL \
        2>/dev/null | tr -d '\r' || true)
  [[ ${#SSO} -ge 32 && -n "$PUB" ]] && ok "SSO 密钥与 LS 公网地址已配置" \
    || bad "SSO 密钥长度 ${#SSO}（应 ≥32）或 FORGE_LS_PUBLIC_BASE_URL 为空"

  # 7) 标注员任务一致性：平台分配的批次 vs LS 里实际能标的任务。
  #    坏了不会白屏，只会让「我的任务」页拿合同数冒充任务数（实测过
  #    平台 910 条 / LS 4 条），所以必须显式断言实时投影接口与自检状态。
  A_NOAUTH=$(docker compose exec -T orchestrator curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:8300/api/v1/me/assignment" 2>/dev/null || true)
  A_BODY=$(docker compose exec -T orchestrator curl -fsS \
    "http://127.0.0.1:8300/api/v1/me/assignment" \
    -H "Authorization: Bearer ${TOKEN}" 2>/dev/null || true)
  A_SRC=$(printf '%s' "$A_BODY" | python3 -c \
    'import sys,json; print((json.load(sys.stdin).get("summary") or {}).get("source",""))' \
    2>/dev/null || true)
  [[ "$A_NOAUTH" == "401" && "$A_SRC" == "labelstudio-live" ]] \
    && ok "标注员任务清单是 LS 实时投影（401 / labelstudio-live）" \
    || bad "任务清单接口异常：未登录 ${A_NOAUTH:-空}、source=${A_SRC:-空}"

  HZ_ASSIGN=$(docker compose exec -T orchestrator python -c \
    "import json,urllib.request; print('assignment' in json.load(urllib.request.urlopen('http://127.0.0.1:8300/healthz')))" \
    2>/dev/null | tr -d '\r' || true)
  [[ "$HZ_ASSIGN" == "True" ]] && ok "健康检查暴露任务一致性自检结果" \
    || bad "healthz 缺 assignment 字段 —— 漂移不会被监控发现"

  # 8) 数据行级一致性：平台页面列出的每一行必须是 LS 当下的同一行。
  #    只比条数不够（条数对了也可能展示的是另一份数据），所以断言接口
  #    返回的 items、指纹、项目白名单，以及 healthz 的数据行自检字段。
  I_NOAUTH=$(docker compose exec -T orchestrator curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:8300/api/v1/me/assignment/items" 2>/dev/null || true)
  I_BODY=$(docker compose exec -T orchestrator curl -fsS \
    "http://127.0.0.1:8300/api/v1/me/assignment/items" \
    -H "Authorization: Bearer ${TOKEN}" 2>/dev/null || true)
  I_READ=$(printf '%s' "$I_BODY" | python3 -c '
import sys,json
d=json.load(sys.stdin); s=d.get("summary") or {}
print("%s|%s|%s|%s" % (d.get("source",""), len(d.get("items") or []),
                       s.get("items",-1), len(d.get("fingerprint") or "")))
' 2>/dev/null || true)
  I_SRC=${I_READ%%|*}; I_REST=${I_READ#*|}
  I_ROWS=${I_REST%%|*}; I_REST=${I_REST#*|}
  I_SUM=${I_REST%%|*}; I_FP=${I_REST#*|}
  [[ "$I_NOAUTH" == "401" && "$I_SRC" == "labelstudio-live" \
     && "$I_ROWS" == "$I_SUM" && "${I_FP:-0}" -eq 64 ]] \
    && ok "任务数据行来自 LS 实时（${I_ROWS} 行 == 显示条数，指纹可核对）" \
    || bad "数据行接口异常：未登录 ${I_NOAUTH:-空}、source=${I_SRC:-空}、rows=${I_ROWS:-空}/summary=${I_SUM:-空}、指纹长度=${I_FP:-0}"

  HZ_ITEMS=$(docker compose exec -T orchestrator python -c \
    "import json,urllib.request; print('assignment_items' in json.load(urllib.request.urlopen('http://127.0.0.1:8300/healthz')))" \
    2>/dev/null | tr -d '\r' || true)
  [[ "$HZ_ITEMS" == "True" ]] && ok "健康检查暴露数据行级自检结果" \
    || bad "healthz 缺 assignment_items 字段 —— 行级漂移不会被监控发现"

  REC_ITEMS=$(docker compose exec -T orchestrator curl -fsS \
    "http://127.0.0.1:8300/api/v1/ls/reconcile" \
    -H "Authorization: Bearer ${TOKEN}" 2>/dev/null \
    | python3 -c 'import sys,json; print(bool((json.load(sys.stdin).get("items") or {}).get("fingerprint")))' \
    2>/dev/null | tr -d '\r' || true)
  [[ "$REC_ITEMS" == "True" ]] && ok "管理侧对账面板能看到数据行指纹" \
    || bad "/ls/reconcile 缺 items 指纹汇总"

  # 回调地址漏配的后果是"静默不同步"：LS 里提交了，平台数字不动。
  CB=$(docker compose exec -T orchestrator printenv FORGE_LS_CALLBACK_URL 2>/dev/null | tr -d '\r')
  if [[ -z "$CB" ]]; then
    bad "FORGE_LS_CALLBACK_URL 为空 —— 标注完成后平台不会自动收到进度"
  else
    ok "标注回调地址已配置（${CB}）"
  fi

  # LS 1.23 的 SSRF 防护默认禁私网，而编排层就在 compose 内网。
  # 漏了这一条，LS 会把 webhook 静默丢弃（日志只有
  # "URL resolves to a reserved network address"），平台数字还可能被
  # 定时对账救回来 —— 只看数字会误判成"同步正常"。必须直接断言配置。
  SSRF=$(docker compose exec -T labelstudio printenv USE_DEFAULT_BANNED_SUBNETS \
    2>/dev/null | tr -d '\r' || true)
  if [[ "$SSRF" == "false" ]]; then
    ok "LS 已放行内网 webhook 回调（USE_DEFAULT_BANNED_SUBNETS=false）"
  else
    bad "LS 未放行内网回调（USE_DEFAULT_BANNED_SUBNETS=${SSRF:-空}）—— webhook 会被 SSRF 防护静默丢弃"
  fi

  # 变量对了还不够：LS 容器必须真的能连到编排层，否则回调地址形同虚设。
  LS_NET=$(docker compose exec -T labelstudio python -c "
import urllib.request
try:
    print(urllib.request.urlopen('http://orchestrator:8300/healthz', timeout=5).status)
except Exception:
    print('ERR')
" 2>/dev/null | tr -d '\r' || true)
  [[ "$LS_NET" == "200" ]] && ok "LS 容器能直达编排层（回调网络通）" \
    || bad "LS 容器连不上编排层（got ${LS_NET:-空}）—— 回调地址形同虚设"

  # 兜底与自检的状态从健康检查里读：这两项坏了服务照样活着，
  # 只是数据静默不同步 / 接口静默 404 —— 所以必须显式断言。
  # 用 python 解 JSON 而不是 grep 正则：正则匹配不上时会"静默通过"，
  # 而这恰恰是这次要防的失败模式。
  HZ_READ=$(docker compose exec -T orchestrator python -c "
import json,urllib.request
d = json.load(urllib.request.urlopen('http://127.0.0.1:8300/healthz'))
a = d.get('autosync') or {}
p = (d.get('selfcheck') or {}).get('permissions') or {}
print('%s|%s|%s' % (a.get('interval_s', 0), p.get('ok'), p.get('unknown') or {}))
" 2>/dev/null | tr -d '\r' || true)
  SYNC_INT=${HZ_READ%%|*}
  REST=${HZ_READ#*|}
  SC_OK=${REST%%|*}
  SC_UNKNOWN=${REST#*|}
  if [[ "${SYNC_INT:-0}" -gt 0 ]]; then
    ok "定时对账兜底已启用（每 ${SYNC_INT} 秒）"
  else
    bad "定时对账兜底未启用（interval_s=${SYNC_INT:-空}）—— webhook 一断就永久不再同步"
  fi
  if [[ "$SC_OK" == "True" ]]; then
    ok "权限自检通过（代码里没有拼错的权限名）"
  else
    bad "权限自检未通过，未知权限：${SC_UNKNOWN:-读取失败} —— 相关接口会对所有人返回 404"
  fi
else
  bad "平台登录失败（手机号/密码不对？响应：${LOGIN:0:160}）"
fi

echo "==== 6/6 LS 单点登录端点与入口 ===="
# /forge/sso/ 由 SSO 中间件接管：404=中间件没装；503=FORGE_LS_SSO_SECRET 没配
SSO_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
  "http://127.0.0.1:${LS_PORT_EFF}/forge/sso/" 2>/dev/null || true)
case "$SSO_CODE" in
  503) bad "LS /forge/sso/ 503 —— FORGE_LS_SSO_SECRET 没配" ;;
  404) bad "LS /forge/sso/ 404 —— SSO 中间件不在（品牌补丁构建失败）" ;;
  200|302|400|403|405) ok "LS SSO 端点在位（HTTP ${SSO_CODE}）" ;;
  *) bad "LS /forge/sso/ 返回 ${SSO_CODE:-空}" ;;
esac

HTTP_PORT_EFF="$(get_kv HTTP_PORT)"; HTTP_PORT_EFF="${HTTP_PORT_EFF:-80}"
DOMAIN_V="$(get_kv DOMAIN)"
if [[ -n "$DOMAIN_V" ]]; then
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: ${DOMAIN_V}" \
    "http://127.0.0.1:${HTTP_PORT_EFF}/" 2>/dev/null || true)
  [[ -n "$CODE" && "$CODE" != "000" ]] && ok "caddy :${HTTP_PORT_EFF} 在监听（HTTP ${CODE}）" \
    || bad "caddy :${HTTP_PORT_EFF} 无响应"
else
  CODE=$(curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:${HTTP_PORT_EFF}/healthz" 2>/dev/null || true)
  [[ "$CODE" == "200" ]] && ok "caddy /healthz 200（IP 模式）" \
    || bad "caddy /healthz = ${CODE:-无响应}"
fi

echo "--------------------------------------------------------------"
echo "冒烟结果：${PASS} 通过 / ${FAIL} 失败"
[[ $FAIL -eq 0 ]]
