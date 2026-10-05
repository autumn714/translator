#!/usr/bin/env bash
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"   # 'sh run.sh' 로 불러도 bash 로 다시 실행 #
if grep -q $'\r' "$0" 2>/dev/null || LC_ALL=C grep -q $'^\xEF\xBB\xBF' "$0" 2>/dev/null; then if LC_ALL=C sed -i -e 's/\r$//' -e '1s/^\xEF\xBB\xBF//' "$0"; then echo "[주의] 윈도 줄바꿈(CRLF)·BOM 을 고쳤습니다: run.sh"; exec bash "$0" "$@"; fi; fi #
# ─────────────────────────────────────────────────────────────
#  번역기 — 실행 스크립트
#  사용법: bash run.sh <명령>      (명령 목록: bash run.sh help)
#  모델 서버는 같은 서버의 도면 분석기가 띄운 것을 같이 쓴다.
# ─────────────────────────────────────────────────────────────
set -Eeuo pipefail
WS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CMD="${1:-help}"
[[ $# -gt 0 ]] && shift
if grep -q $'\r' "$WS/scripts/lib.sh" 2>/dev/null; then sed -i 's/\r$//' "$WS/scripts/lib.sh"; fi
# shellcheck source=scripts/lib.sh
source "$WS/scripts/lib.sh"

LLM_HINT="도면 분석기를 먼저 시작하세요: cd /home/user/workspace && bash run.sh start"

usage() {
  cat <<'EOF'
사용법: bash run.sh <명령>

  setup                   파이썬 패키지 설치 (인터넷 필요, 처음 한 번과 업데이트 뒤)
  check                   서버 환경 점검 (도커·이미지·패키지·도면 분석기 모델 서버·포트·디스크)
  start                   번역기 시작 (도면 분석기 모델 서버에 연결)
  stop                    번역기 중지 (도면 분석기는 그대로)
  status                  실행 상태, 모델 연결, 접속 주소
  logs [app|gate|linker]  로그 보기 (Ctrl+C 로 나가기)
  selftest                동작 시험 (짧은 문장·작은 문서 번역)
  clean [--yes]           컨테이너·네트워크·이미지 정리 (프로젝트 폴더는 마지막에 직접 삭제)

순서: (도면 분석기 start) → setup → check → start → (브라우저 접속) → stop → clean
EOF
}

# ================================================================== check
cmd_check() {
  load_config
  local fails=0
  step "운영체제"
  # shellcheck disable=SC1091
  info "$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME") · 커널 $(uname -r)"

  step "도커"
  need_docker
  info "Docker $(docker version --format '{{.Server.Version}}')"
  if have_image "$PY_IMAGE"; then ok "이미지 있음: $PY_IMAGE"; else warn "이미지 없음: $PY_IMAGE  → bash run.sh setup"; fi
  if pylib_current; then ok "파이썬 패키지 설치됨"
  elif pylib_ready; then warn "requirements.txt 가 바뀌었습니다 → bash run.sh setup"
  else warn "파이썬 패키지 미설치 → bash run.sh setup"; fi

  step "모델 서버 (도면 분석기)"
  if llm_external; then
    info "외부 모델 서버: $LLM_URL (config.env 의 LLM_URL)"
    warn "이 방식에서는 번역기 화면 컨테이너가 일반 네트워크에도 붙습니다(격리 약해짐)."
  elif ! exists "$LLM_CONTAINER"; then
    err "모델 서버 컨테이너가 없습니다: $LLM_CONTAINER"; info "$LLM_HINT"; fails=$((fails+1))
  elif ! running "$LLM_CONTAINER"; then
    err "모델 서버 컨테이너가 멈춰 있습니다: $LLM_CONTAINER"; info "$LLM_HINT"; fails=$((fails+1))
  else
    info "컨테이너 $LLM_CONTAINER · 이미지 $(docker inspect -f '{{.Config.Image}}' "$LLM_CONTAINER" 2>/dev/null)"
    if llm_healthy; then
      ok "모델 서버 응답 정상 · 모델 $(llm_model_name || echo '확인 불가')"
    else
      warn "모델 서버가 아직 준비 중이거나 응답하지 않습니다 (도면 분석기에서 bash run.sh status 로 확인)."
    fi
  fi

  step "디스크"
  info "프로젝트 폴더: $(df -Ph "$WS" | awk 'NR==2 {print $4" 남음 ("$6")"}')"

  step "네트워크"
  if port_busy && ! running "$N_GATE"; then
    err "포트 $UI_PORT 를 다른 프로그램이 쓰고 있습니다. config.env 의 UI_PORT 를 바꾸세요."; fails=$((fails+1))
  else ok "포트 $UI_PORT 사용 가능"; fi
  info "접속 주소(시작 후): $(lan_ips | sed "s#.*#http://&:$UI_PORT#" | xargs)"
  [[ -n "$UI_ALLOW" ]] && info "접속 허용 범위: $UI_ALLOW"

  echo
  if ((fails)); then err "점검 결과 문제 ${fails}건"; return 1; fi
  ok "점검 통과"
}

# ================================================================== setup
cmd_setup() {
  load_config; need_docker; ensure_dirs
  step "도커 이미지"
  if have_image "$PY_IMAGE"; then ok "이미 있음: $PY_IMAGE"
  else
    info "받는 중: $PY_IMAGE"; docker pull "$PY_IMAGE"
    grep -qxF "$PY_IMAGE" "$WS/state/pulled_images" 2>/dev/null || echo "$PY_IMAGE" >> "$WS/state/pulled_images"
  fi

  step "파이썬 패키지 (프로젝트 폴더 cache/pylib 에 설치)"
  local h penv=()
  h=$(req_hash)
  if pylib_current; then
    ok "이미 설치됨"
  else
    find "$WS/cache/pylib" -mindepth 1 -delete 2>/dev/null || true
    rm -f "$WS/state/pylib.sha256"
    if [[ -n "$SETUP_PROXY" ]]; then penv+=(-e "HTTP_PROXY=$SETUP_PROXY" -e "HTTPS_PROXY=$SETUP_PROXY" -e "http_proxy=$SETUP_PROXY" -e "https_proxy=$SETUP_PROXY"); fi
    if [[ -n "$SETUP_PIP_INDEX_URL" ]]; then penv+=(-e "PIP_INDEX_URL=$SETUP_PIP_INDEX_URL"); fi
    # 인터넷이 되는 컨테이너이므로 pip 에 필요한 것만 붙인다 (용어집·문서가 있는 data/ 는 붙이지 않음)
    if ! docker run --rm "${COMMON_ARGS[@]}" --user "$RUN_UID:$RUN_GID" "${penv[@]}" \
        -e HOME=/tmp -e PIP_CACHE_DIR=/w/pip -e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PIP_NO_INPUT=1 \
        --mount "type=bind,src=$WS/cache/pylib,dst=/w/pylib" --mount "type=bind,src=$WS/cache/pip,dst=/w/pip" \
        --mount "type=bind,src=$WS/requirements.txt,dst=/w/requirements.txt,readonly" \
        "$PY_IMAGE" python -m pip install --quiet --no-warn-script-location --require-hashes --prefer-binary \
        --target /w/pylib -r /w/requirements.txt; then
      find "$WS/cache/pip" -mindepth 1 -delete 2>/dev/null || true
      die "파이썬 패키지 설치에 실패했습니다. 인터넷 연결을 확인하세요 (프록시가 필요하면 config.env 의 SETUP_PROXY)."
    fi
    echo "$h" > "$WS/state/pylib.sha256"
    find "$WS/cache/pip" -mindepth 1 -delete 2>/dev/null || true
    ok "설치 완료 ($(du -sh "$WS/cache/pylib" | cut -f1))"
  fi
  fix_owner
  echo; ok "준비 끝. 다음: bash run.sh check → bash run.sh start"
}

# ================================================================== start
cmd_start() {
  load_config; need_docker; ensure_dirs
  have_image "$PY_IMAGE" || die "도커 이미지가 없습니다. bash run.sh setup 을 먼저 하세요."
  pylib_ready || die "파이썬 패키지가 없습니다. bash run.sh setup 을 먼저 하세요."
  pylib_current || die "requirements.txt 가 바뀌었습니다. bash run.sh setup 을 먼저 하세요 (인터넷 필요)."

  local pw secret
  pw=$(ui_password)
  if [[ "$UI_AUTH" == "1" && ( -z "$pw" || -z "$UI_USER" ) ]]; then
    die "로그인이 켜져 있는데 아이디/비밀번호가 비었습니다 (config.env 의 UI_USER, UI_PASSWORD 또는 state/ui_password)."
  fi
  secret=$(session_secret)
  if port_busy && ! running "$N_GATE"; then die "포트 $UI_PORT 를 다른 프로그램이 쓰고 있습니다. config.env 의 UI_PORT 를 바꾸세요."; fi

  cmd_stop --quiet
  local TS; TS=$(date +%Y%m%d-%H%M%S)

  step "네트워크"
  create_networks
  ok "$NET_INT (내부 전용: 인터넷·서버 자신의 서비스 차단) / $NET_PUB (내부망 접속 중계용)"
  info "도커가 쓰는 주소 대역: $NET_SUBNETS — 이 대역에 속한 PC에서는 접속이 안 됩니다(README 문제 해결 참고)"

  step "모델 서버 연결"
  local llm_ok=0 rc=0
  if llm_external; then
    warn "외부 모델 서버를 씁니다: $LLM_URL — 번역기 화면 컨테이너가 일반 네트워크에도 붙습니다(격리 약해짐)."
    llm_ok=1
  else
    llm_link || rc=$?
    case $rc in
      0|2)
        ok "$LLM_CONTAINER 를 번역기 내부 네트워크에 연결했습니다 (주소 http://$LLM_ALIAS:$LLM_PORT)"
        if ! running "$LLM_CONTAINER"; then warn "도면 분석기 모델 서버가 멈춰 있습니다 ($LLM_CONTAINER)."
        elif llm_healthy; then ok "모델 서버 응답 정상 · 모델 $(llm_model_name || echo '확인 불가')"; llm_ok=1
        else warn "도면 분석기 모델 서버가 아직 준비 중입니다. 준비되면 바로 번역할 수 있습니다."; llm_ok=2; fi;;
      1) warn "모델 서버 연결 실패: $LINK_ERR";;
      *) warn "도면 분석기 모델 서버가 꺼져 있습니다 ($LLM_CONTAINER 없음).";;
    esac
    ((llm_ok == 0)) && info "$LLM_HINT"
    if linker_start; then ok "자동 재연결 감시 시작 — 도면 분석기를 다시 시작해도 저절로 다시 연결합니다 (기록: logs/linker.log)"
    else warn "자동 재연결 감시를 시작하지 못했습니다 (logs/linker.log). 도면 분석기를 다시 시작한 뒤에는 bash run.sh start 를 한 번 더 하세요."; fi
  fi

  step "번역기 화면 시작"
  # 비밀번호·서명 키는 docker 명령줄(ps 로 보임)이 아니라 환경 변수로 넘긴다
  UI_PASSWORD="$pw" SESSION_SECRET="$secret" LLM_API_KEY="$LLM_API_KEY" \
  docker run -d --name "$N_APP" "${COMMON_ARGS[@]}" --network "$NET_INT" --user "$RUN_UID:$RUN_GID" \
    --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
    -w /workspace/cache/home-app -e HOME=/workspace/cache/home-app \
    -e PYTHONPATH=/workspace/cache/pylib:/workspace/app -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1 \
    -e "TZ=${CONTAINER_TZ:-UTC}" -e APP_HOST=0.0.0.0 -e APP_PORT=7860 \
    -e TRANSLATOR_DATA_DIR=/workspace/data -e GLOSSARY_PATH=/workspace/data/glossary/default.json \
    -e ENGINE_TYPE=openai_compatible -e "LLM_BASE_URL=$LLM_BASE" -e "LLM_MODEL=$LLM_MODEL" -e LLM_API_KEY \
    -e "LLM_MAX_PARALLEL=$LLM_MAX_PARALLEL" -e "LLM_DOC_PARALLEL=$LLM_DOC_PARALLEL" \
    -e "DOC_MAX_MB=$DOC_MAX_MB" -e "DOC_RETENTION_HOURS=$DOC_RETENTION_HOURS" -e "DOC_JOB_CONCURRENCY=$DOC_JOB_CONCURRENCY" \
    -e "UI_AUTH=$UI_AUTH" -e "UI_USER=$UI_USER" -e UI_PASSWORD -e SESSION_SECRET \
    -e HF_HUB_OFFLINE=1 -e DO_NOT_TRACK=1 \
    --mount "type=bind,src=$WS/translator_app,dst=/workspace/app/translator_app,readonly" \
    --mount "type=bind,src=$WS/cache/pylib,dst=/workspace/cache/pylib,readonly" \
    --mount "type=bind,src=$WS/cache/home-app,dst=/workspace/cache/home-app" \
    --mount "type=bind,src=$WS/data,dst=/workspace/data" \
    "$PY_IMAGE" python -m uvicorn translator_app.main:app --host 0.0.0.0 --port 7860 --no-access-log >/dev/null
  if llm_external; then docker network connect "$NET_PUB" "$N_APP"; fi
  : > "$WS/logs/app_$TS.log"
  setsid nohup docker logs -f "$N_APP" >> "$WS/logs/app_$TS.log" 2>&1 < /dev/null &
  ln -sfn "app_$TS.log" "$WS/logs/app_latest.log"
  prune_logs
  info "로그: logs/app_$TS.log"

  # 내부망 접속 중계기: 포트를 공개하는 일반 네트워크 + 내부 전용 네트워크에 함께 붙는다 (읽기 전용·권한 최소)
  docker create --name "$N_GATE" "${COMMON_ARGS[@]}" --network "$NET_PUB" -p "${UI_BIND}:${UI_PORT}:7860" \
    --user "$RUN_UID:$RUN_GID" --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
    -e PYTHONDONTWRITEBYTECODE=1 -e "TZ=${CONTAINER_TZ:-UTC}" \
    --mount "type=bind,src=$WS/translator_app/gateway.py,dst=/workspace/gateway.py,readonly" \
    "$PY_IMAGE" python /workspace/gateway.py --listen 0.0.0.0:7860 --target "${N_APP}:7860" --allow "${UI_ALLOW:-}" >/dev/null
  docker network connect "$NET_INT" "$N_GATE"
  if ! docker start "$N_GATE" >/dev/null 2>"$WS/logs/gate_start.err"; then
    err "접속 중계기를 시작하지 못했습니다: $(tail -1 "$WS/logs/gate_start.err")"
    die "포트 $UI_PORT 또는 UI_BIND=$UI_BIND 설정을 확인하세요 (bash run.sh check). 고친 뒤 bash run.sh start"
  fi
  rm -f "$WS/logs/gate_start.err"
  wait_app || { fix_owner; die "번역기 화면이 뜨지 않습니다. 로그: logs/app_$TS.log"; }
  local h="$UI_BIND"; [[ "$h" == "0.0.0.0" ]] && h=127.0.0.1
  if timeout 5 bash -c "exec 3<>/dev/tcp/$h/$UI_PORT" 2>/dev/null; then ok "접속 중계 확인 ($h:$UI_PORT)"
  else warn "서버에서 $h:$UI_PORT 로 접속 시험이 실패했습니다 (bash run.sh logs gate)."; fi
  fix_owner
  show_access --password
  if ((llm_ok == 0)); then echo; warn "번역하려면 도면 분석기 모델 서버가 필요합니다. $LLM_HINT"; fi
  return 0
}

wait_app() {
  local i
  for ((i = 0; i < 90; i++)); do
    running "$N_APP" || { err "번역기 화면 컨테이너가 멈췄습니다:"; docker logs --tail 30 "$N_APP" 2>&1 | sed 's/^/    /'; return 1; }
    if docker exec "$N_APP" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/health', timeout=3)" >/dev/null 2>&1; then
      ok "번역기 화면 준비 완료"; return 0
    fi
    sleep 2
  done
  err "180초 안에 준비되지 않았습니다."
  return 1
}

# 화면 로그는 시작할 때마다 새 파일로 남는다. 최근 20개만 둔다
prune_logs() {
  local logs=("$WS"/logs/app_2*.log) i
  [[ -e "${logs[0]}" ]] || return 0
  for ((i = 0; i < ${#logs[@]} - 20; i++)); do rm -f "${logs[i]}"; done
  return 0
}

show_access() {
  step "접속"
  if [[ "$UI_BIND" == "0.0.0.0" ]]; then
    local ip
    for ip in $(lan_ips); do info "http://${ip}:${UI_PORT}"; done
    info "(같은 내부망의 PC 브라우저에서 위 주소로 접속)"
  else
    info "http://${UI_BIND}:${UI_PORT}"
  fi
  [[ -n "$UI_ALLOW" ]] && info "접속 허용 범위: $UI_ALLOW"
  if [[ "$UI_AUTH" != "1" ]]; then info "로그인 없이 열려 있습니다 (켜려면 config.env 의 UI_AUTH=\"1\")."
  elif [[ "${1:-}" == "--password" ]]; then info "아이디: ${UI_USER}   비밀번호: $(ui_password)"
  else info "아이디: ${UI_USER}   비밀번호: config.env 의 UI_PASSWORD 또는 state/ui_password"; fi
  return 0
}

# ================================================================== stop / status / logs / selftest
cmd_stop() {
  [[ -n "${N_APP:-}" ]] || load_config
  need_docker
  linker_stop
  llm_unlink_all "${1:-}"
  local ids
  ids=$(docker ps -aq --filter "label=$LABEL")
  if [[ -n "$ids" ]]; then
    # shellcheck disable=SC2086
    docker rm -f $ids >/dev/null
  fi
  ids=$(docker network ls -q --filter "label=$LABEL")
  if [[ -n "$ids" ]]; then
    # shellcheck disable=SC2086
    docker network rm $ids >/dev/null 2>&1 || true
  fi
  [[ "${1:-}" == "--quiet" ]] || ok "중지했습니다 (번역기 컨테이너·네트워크 삭제). 도면 분석기는 그대로입니다."
  return 0
}

cmd_status() {
  load_config; need_docker
  step "컨테이너"
  docker ps -a --filter "label=$LABEL" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

  step "모델 서버"
  if llm_external; then
    info "외부 모델 서버: $LLM_URL"
  elif ! exists "$LLM_CONTAINER"; then
    warn "도면 분석기 모델 서버가 꺼져 있습니다 ($LLM_CONTAINER 없음). $LLM_HINT"
  else
    if running "$LLM_CONTAINER"; then ok "도면 분석기 모델 서버 실행 중 ($LLM_CONTAINER)"; else warn "도면 분석기 모델 서버가 멈춰 있습니다 ($LLM_CONTAINER)."; fi
    if llm_connected; then ok "번역기 내부 네트워크에 연결됨 (주소 http://$LLM_ALIAS:$LLM_PORT)"
    elif running "$N_APP"; then warn "번역기 내부 네트워크에 연결되어 있지 않습니다 → bash run.sh start"; fi
  fi
  if ! llm_external && running "$N_APP"; then
    local pid
    if pid=$(linker_pid); then ok "자동 재연결 감시 중 (pid $pid, 기록 logs/linker.log)"
    else warn "자동 재연결 감시가 꺼져 있습니다 → bash run.sh start"; fi
  fi
  if running "$N_APP"; then
    local probe
    if probe=$(docker exec "$N_APP" python -m translator_app.selftest --probe 2>/dev/null); then ok "$probe"
    else warn "번역기 화면에서 모델 서버에 닿지 않습니다${probe:+ ($probe)}"; fi
  fi
  if running "$N_GATE"; then show_access; fi
  return 0
}

cmd_logs() {
  load_config
  case "${1:-app}" in
    app)    local f="$WS/logs/app_latest.log"; [[ -e "$f" ]] || die "로그가 없습니다."; tail -n 100 -F "$f";;
    linker) local f="$WS/logs/linker.log"; [[ -e "$f" ]] || die "로그가 없습니다."; tail -n 100 -F "$f";;
    gate)   need_docker; docker logs --tail 100 -f "$N_GATE";;
    *)      die "bash run.sh logs [app|gate|linker]";;
  esac
}

cmd_selftest() {
  load_config; need_docker
  running "$N_APP" || die "번역기가 꺼져 있습니다. bash run.sh start 를 먼저 하세요."
  docker exec "$N_APP" python -m translator_app.selftest "$@"
}

# ================================================================== clean
cmd_clean() {
  [[ "${1:-}" == "--yes" ]] && ASSUME_YES=1
  load_config; need_docker
  step "컨테이너·네트워크"
  cmd_stop
  step "파일 소유권 정리"
  if have_image "$PY_IMAGE"; then
    local who; who=$(getent passwd "$RUN_UID" 2>/dev/null | cut -d: -f1 || true)
    docker run --rm "${COMMON_ARGS[@]}" --network none --mount "type=bind,src=$WS,dst=/w" "$PY_IMAGE" chown -R "$RUN_UID:$RUN_GID" /w \
      && ok "프로젝트 폴더의 모든 파일을 ${who:-uid $RUN_UID} 소유로 맞췄습니다 (지울 때 sudo 불필요)."
  fi
  step "도커 이미지"
  local img="$PY_IMAGE" users
  if ! have_image "$img"; then
    ok "삭제할 이미지 없음"
  else
    users=$(docker ps -a --filter "ancestor=$img" --format '{{.Names}}' | xargs)
    if [[ -n "$users" ]]; then
      info "$img 는 다른 컨테이너($users)가 쓰고 있어 남겨 둡니다."
    elif grep -qxF "$img" "$WS/state/pulled_images" 2>/dev/null; then
      info "setup 이 받은 이미지: $img"
      if confirm "삭제할까요?" y; then docker image rm "$img" >/dev/null && ok "삭제했습니다."; else info "남겨 둡니다."; fi
    else
      info "setup 전부터 있던 이미지(도면 분석기 등 다른 작업용): $img"
      if confirm "이것도 삭제할까요?" n; then docker image rm "$img" >/dev/null && ok "삭제했습니다."; else info "남겨 둡니다 (지우려면: docker image rm $img)."; fi
    fi
  fi
  docker container prune -f --filter "label=$LABEL" >/dev/null 2>&1 || true
  docker network prune -f --filter "label=$LABEL" >/dev/null 2>&1 || true
  step "마지막 단계 (직접 실행)"
  info "프로젝트 폴더를 지우면 이 프로젝트가 만든 파일(용어집·문서·패키지·로그)이 모두 사라집니다:"
  info "  rm -rf \"$WS\""
  info "이 프로젝트는 도커 이미지를 빌드하지 않으므로 도커 빌드 캐시는 생기지 않습니다."
}

# ================================================================== main
trap 'fix_owner' EXIT
case "$CMD" in
  check)    cmd_check "$@";;
  setup)    cmd_setup "$@";;
  start)    cmd_start "$@";;
  stop)     load_config; cmd_stop "$@";;
  status)   cmd_status "$@";;
  logs)     cmd_logs "$@";;
  selftest) cmd_selftest "$@";;
  clean)    cmd_clean "$@";;
  help|-h|--help) usage;;
  *) err "알 수 없는 명령: $CMD"; usage; exit 2;;
esac
