# shellcheck shell=bash disable=SC2034
# (SC2034: 여기서 정한 설정 변수는 run.sh 가 쓴다)
# run.sh 와 scripts/linker.sh 가 불러 쓰는 공통 함수.
#
# 원칙
#  - 이 프로젝트가 만드는 파일은 모두 $WS(프로젝트 폴더) 안에만 생긴다.
#    (파이썬 패키지, 로그, 상태, 용어집, 올린 문서·번역본, 도커 클라이언트 설정까지)
#  - 도커 쪽 흔적(컨테이너·네트워크)에는 라벨을 붙여 두고 stop/clean 에서 지운다.
#    라벨 값은 NAME_PREFIX 라서, 이름 앞머리를 바꾼 두 번째 복사본과 서로 건드리지 않는다.
#  - 번역기 화면은 인터넷으로 나가는 길이 없는 내부 전용 네트워크에서만 돈다.
#    내부망 PC의 브라우저 접속은 포트 중계 컨테이너(gate)만 받는다.
#  - 모델 서버는 도면 분석기가 띄운 컨테이너를 같이 쓴다. 그 컨테이너에는 번역기 내부
#    네트워크 연결 하나만 더했다가 stop 때 뗀다. 도면 분석기의 컨테이너·네트워크·이미지·
#    파일은 멈추거나 지우거나 바꾸지 않는다.

LABEL_KEY="translator.project"
LLM_ALIAS="llm"
# 도면 분석기(같은 서버의 다른 패키지)가 자기 컨테이너·네트워크에 붙이는 라벨과 모델 서버 이미지
COTENANT_LABEL_KEY="pfdvlm.project"
COTENANT_IMAGE_REPO="vllm/vllm-openai"

# ------------------------------------------------------------------ 출력
if [[ -t 1 ]]; then C_G=$'\e[32m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_B=$'\e[1m'; C_0=$'\e[0m'; else C_G=; C_Y=; C_R=; C_B=; C_0=; fi
ok()   { printf '%s[정상]%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '%s[주의]%s %s\n' "$C_Y" "$C_0" "$*"; }
err()  { printf '%s[실패]%s %s\n' "$C_R" "$C_0" "$*"; }
info() { printf '  %s\n' "$*"; }
step() { printf '\n%s== %s ==%s\n' "$C_B" "$*" "$C_0"; }
die()  { err "$*"; exit 1; }

# ------------------------------------------------------------------ 설정
# 시험용 덧붙임 설정 파일 (개발 PC 시험 dev/e2e_docker.sh 전용). 서버에서는 비워 둔다.
extra_config() {
  local f="${TRANSLATOR_CONFIG_EXTRA:-}"
  [[ -n "$f" ]] || return 0
  [[ "$f" == /* ]] || f="$WS/$f"
  echo "$f"
}

# 윈도에서 편집한 파일의 CRLF 줄바꿈과 UTF-8 BOM 을 고친다
fix_text_files() {
  local f files=("$WS/config.env" "$WS"/scripts/*.sh "$WS/run.sh")
  f=$(extra_config); [[ -n "$f" ]] && files+=("$f")
  for f in "${files[@]}"; do
    [[ -f "$f" ]] || continue
    if grep -q $'\r' "$f"; then sed -i 's/\r$//' "$f"; warn "윈도 줄바꿈(CRLF)을 고쳤습니다: ${f#"$WS"/}"; fi
    if LC_ALL=C grep -q $'^\xEF\xBB\xBF' "$f"; then LC_ALL=C sed -i '1s/^\xEF\xBB\xBF//' "$f"; warn "BOM 을 지웠습니다: ${f#"$WS"/}"; fi
  done
  return 0
}

load_config() {
  fix_text_files
  # 새 설정 항목이 없는 예전 config.env 도 돌아가도록 기본값을 먼저 둔다
  UI_BIND="0.0.0.0"; UI_PORT="7870"; UI_ALLOW=""; UI_AUTH="0"; UI_USER="translator"; UI_PASSWORD=""
  LLM_CONTAINER="pfdvlm-vllm"; LLM_PORT="8000"; LLM_MODEL=""; LLM_URL=""; LLM_API_KEY="EMPTY"
  LLM_MAX_PARALLEL="3"; LLM_DOC_PARALLEL="2"; DOC_MAX_MB="50"; DOC_RETENTION_HOURS="24"; DOC_JOB_CONCURRENCY="2"
  DOC_DISK_QUOTA_MB="2048"; APP_MEMORY="8g"
  NET_INT_SUBNET=""; NET_PUB_SUBNET=""; SETUP_PROXY=""; SETUP_PIP_INDEX_URL=""; CONTAINER_TZ="KST-9"
  PY_IMAGE="python:3.12-slim"; NAME_PREFIX="translator"
  # shellcheck source=/dev/null
  source "$WS/config.env"
  local extra; extra=$(extra_config)
  if [[ -n "$extra" ]]; then
    [[ -f "$extra" ]] || die "덧붙임 설정 파일이 없습니다: $extra"
    # shellcheck source=/dev/null
    source "$extra"
  fi
  UI_AUTH=$(normalize_flag "$UI_AUTH")
  N_APP="${NAME_PREFIX}-app"; N_GATE="${NAME_PREFIX}-gate"
  NET_INT="${NAME_PREFIX}-int"; NET_PUB="${NAME_PREFIX}-pub"
  if [[ -n "$LLM_URL" ]]; then LLM_BASE="${LLM_URL%/}"; else LLM_BASE="http://${LLM_ALIAS}:${LLM_PORT}/v1"; fi

  # 정리용 라벨 (복사본마다 다름). 모든 컨테이너 공통: 라벨, 로그 크기 제한,
  # 코어 덤프 금지(문서가 담긴 메모리가 호스트에 떨어지지 않게)
  LABEL="${LABEL_KEY}=${NAME_PREFIX}"
  COMMON_ARGS=(--label "$LABEL" --log-driver json-file --log-opt max-size=20m --log-opt max-file=2 --ulimit core=0)

  # 컨테이너 안에서 파일을 만들 사용자 = 이 스크립트를 실행한 사람 (sudo 로 실행해도 원래 사용자)
  RUN_UID="${SUDO_UID:-$(id -u)}"
  RUN_GID="${SUDO_GID:-$(id -g)}"

  # 도커 클라이언트 설정도 프로젝트 폴더 안에 둔다 (~/.docker 에 흔적을 남기지 않기 위해)
  export DOCKER_CONFIG="$WS/cache/docker-config"
  mkd "$WS/cache" "$DOCKER_CONFIG"
}

# 켜기/끄기 설정값: 1·true·yes·on (대소문자·앞뒤 공백 무관) 이면 "1", 그 밖은 "0".
# 화면 프로그램(translator_app/config.py)도 같은 규칙으로 읽는다.
normalize_flag() {
  local v="${1:-}"
  v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"
  case "${v,,}" in
    1|true|yes|on) echo "1";;
    *) echo "0";;
  esac
}

# sudo 로 실행했을 때 root 소유 파일이 남지 않게 한다
mkd() {
  local d
  for d in "$@"; do
    mkdir -p "$d"
    if [[ $EUID -eq 0 && -n "${SUDO_UID:-}" ]]; then chown "$RUN_UID:$RUN_GID" "$d" 2>/dev/null || true; fi
  done
  return 0
}
fix_owner() {
  if [[ $EUID -eq 0 && -n "${SUDO_UID:-}" ]]; then
    local d
    for d in cache logs state data; do
      if [[ -e "$WS/$d" ]]; then chown -R "${SUDO_UID}:${SUDO_GID:-$SUDO_UID}" "$WS/$d" 2>/dev/null || true; fi
    done
  fi
  return 0
}

ensure_dirs() {
  mkd "$WS/logs" "$WS/state" "$WS/data" "$WS/data/glossary" "$WS/data/jobs" \
      "$WS/cache/pylib" "$WS/cache/pip" "$WS/cache/home-app"
}

# 비밀 값 파일 만들기 (처음 한 번, 본인만 읽을 수 있게)
secret_file() {  # secret_file 경로 바이트수
  local f="$1"
  if [[ ! -s "$f" ]]; then
    mkd "$(dirname "$f")"
    (umask 077; od -An -N"$2" -tx1 /dev/urandom | tr -d ' \n' > "$f")
    if [[ $EUID -eq 0 && -n "${SUDO_UID:-}" ]]; then chown "$RUN_UID:$RUN_GID" "$f" 2>/dev/null || true; fi
  fi
  cat "$f"
}

# 화면 로그인 비밀번호. config.env 에 없으면 처음 한 번 만들어 state/ui_password 에 둔다
ui_password() {
  if [[ "${UI_AUTH:-0}" != "1" ]]; then echo ""; return 0; fi
  if [[ -n "${UI_PASSWORD:-}" ]]; then echo "$UI_PASSWORD"; return 0; fi
  secret_file "$WS/state/ui_password" 8
}

# 로그인 쿠키 서명 키. 다시 시작해도 로그인이 풀리지 않게 state/ 에 둔다
session_secret() { secret_file "$WS/state/session_secret" 32; }

# ------------------------------------------------------------------ 도커
need_docker() {
  command -v docker >/dev/null 2>&1 || die "docker 명령이 없습니다."
  local out
  if ! out=$(docker info --format '{{.ServerVersion}}' 2>&1); then
    if grep -qi 'permission denied' <<<"$out"; then
      die "도커 사용 권한이 없습니다. 'sudo bash run.sh $CMD' 로 실행하세요."
    fi
    die "도커 데몬에 연결할 수 없습니다: $out"
  fi
}
have_image() { docker image inspect "$1" >/dev/null 2>&1; }
running()    { [[ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" == "true" ]]; }
exists()     { docker inspect --type container "$1" >/dev/null 2>&1; }
# 실행 중이고 한 번도 비정상 종료로 다시 시작되지 않았는지 (--restart 로 되살아나는 중이면 아니다)
running_clean() {
  [[ "$(docker inspect -f '{{.State.Running}} {{.State.Restarting}} {{.RestartCount}}' "$1" 2>/dev/null)" == "true false 0" ]]
}
# 번역기가 만든 컨테이너·네트워크인지 (라벨 키만 본다: 예전 판의 translator.project=1 도 포함)
ours_container() { [[ -n "$(docker inspect --type container -f "{{index .Config.Labels \"$LABEL_KEY\"}}" "$1" 2>/dev/null)" ]]; }
ours_network()   { [[ -n "$(docker network inspect -f "{{index .Labels \"$LABEL_KEY\"}}" "$1" 2>/dev/null)" ]]; }

# 컨테이너 로그를 logs/ 의 파일로 옮겨 적는다. 컨테이너가 비정상 종료 뒤 다시 시작되어도(--restart)
# 이어서 적고, 컨테이너가 지워지거나 완전히 멈추면 끝난다.
follow_logs() {  # follow_logs 컨테이너 파일
  # shellcheck disable=SC2016  # 안쪽 bash 가 풀 변수다
  setsid nohup bash -c '
    since=""
    while :; do
      docker logs -f ${since:+--since "$since"} "$1" >> "$2" 2>&1
      since=$(date +%s.%N)
      sleep 1
      case "$(docker inspect -f "{{.State.Status}}" "$1" 2>/dev/null)" in running|restarting) ;; *) exit 0;; esac
    done' follow_logs "$1" "$2" >/dev/null 2>&1 < /dev/null &
}

# 파이썬 패키지가 설치되어 있는지, requirements.txt 와 같은 판인지
req_hash()     { sha256sum "$WS/requirements.txt" | cut -d' ' -f1; }
pylib_ready()  { [[ -f "$WS/state/pylib.sha256" && -d "$WS/cache/pylib/fastapi" ]]; }
pylib_current() { pylib_ready && [[ "$(cat "$WS/state/pylib.sha256")" == "$(req_hash)" ]]; }

# 내부 전용 네트워크(인터넷·호스트 서비스 차단)와 접속 중계용 네트워크
create_networks() {
  local sub_int=() sub_pub=()
  [[ -n "${NET_INT_SUBNET:-}" ]] && sub_int=(--subnet "$NET_INT_SUBNET")
  [[ -n "${NET_PUB_SUBNET:-}" ]] && sub_pub=(--subnet "$NET_PUB_SUBNET")
  # gateway_mode_ipv4=isolated: 브리지에 호스트 주소를 두지 않아, 컨테이너가 서버 자신의 서비스(프록시 등)에도 닿지 못한다 (Docker 28 이상)
  if ! docker network create --internal -o com.docker.network.bridge.gateway_mode_ipv4=isolated \
        "${sub_int[@]}" --label "$LABEL" "$NET_INT" >/dev/null 2>&1; then
    docker network create --internal "${sub_int[@]}" --label "$LABEL" "$NET_INT" >/dev/null
    warn "이 도커 버전은 호스트 차단(isolated) 모드를 지원하지 않아 일반 내부 전용 네트워크로 만들었습니다."
  fi
  docker network create "${sub_pub[@]}" --label "$LABEL" "$NET_PUB" >/dev/null
  NET_SUBNETS=$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' "$NET_INT" "$NET_PUB" 2>/dev/null | xargs)
}

port_busy() { command -v ss >/dev/null 2>&1 && ss -ltnH "sport = :$UI_PORT" 2>/dev/null | grep -q .; }

# UI_ALLOW 를 중계기와 같은 코드(gateway.py --check)로 확인한다. 틀린 값이면 중계기가 시작하자마자 멈추므로
# 컨테이너를 만들기 전에 막는다. 문제가 있으면 ALLOW_ERR 에 설명을 두고 1
check_allow() {
  ALLOW_ERR=""
  [[ -n "${UI_ALLOW//[[:space:],]/}" ]] || return 0
  if ALLOW_ERR=$(docker run --rm "${COMMON_ARGS[@]}" --network none --read-only --cap-drop ALL \
      --security-opt no-new-privileges:true --user "$RUN_UID:$RUN_GID" -e PYTHONDONTWRITEBYTECODE=1 \
      --mount "type=bind,src=$WS/translator_app/gateway.py,dst=/w/gateway.py,readonly" \
      "$PY_IMAGE" python /w/gateway.py --check --allow "$UI_ALLOW" 2>&1); then
    ALLOW_ERR=""; return 0
  fi
  ALLOW_ERR=$(tail -n 1 <<<"$ALLOW_ERR")
  return 1
}

# 이 이미지로 만든 컨테이너 이름들 (멈춘 것 포함)
image_users() { docker ps -a --filter "ancestor=$1" --format '{{.Names}}' 2>/dev/null | xargs; }

# 같은 서버의 도면 분석기가 설치되어 있어 python 이미지를 같이 쓸 수 있는지. 그렇다면 이유를 출력하고 0
#   도면 분석기는 stop 때 자기 컨테이너를 모두 지우므로, 컨테이너가 없어도 설치 흔적(모델 서버 컨테이너,
#   pfdvlm.project 라벨, vllm/vllm-openai 이미지)이 있으면 같은 이미지를 다시 쓸 것으로 본다.
cotenant_reason() {
  if ! llm_external && exists "$LLM_CONTAINER"; then echo "도면 분석기 모델 서버($LLM_CONTAINER)가 있습니다"; return 0; fi
  if [[ -n "$(docker ps -aq --filter "label=$COTENANT_LABEL_KEY" 2>/dev/null)$(docker network ls -q --filter "label=$COTENANT_LABEL_KEY" 2>/dev/null)" ]]; then
    echo "도면 분석기의 컨테이너·네트워크가 있습니다"; return 0
  fi
  if [[ -n "$(docker image ls -q "$COTENANT_IMAGE_REPO" 2>/dev/null)" ]]; then
    echo "도면 분석기 모델 서버 이미지($COTENANT_IMAGE_REPO)가 있습니다"; return 0
  fi
  return 1
}

# ------------------------------------------------------------------ 모델 서버 (도면 분석기) 연결
llm_external() { [[ -n "${LLM_URL:-}" ]]; }

# 모델 서버 컨테이너 안에서 짧은 파이썬을 돌린다 (읽기만 한다: 상태 확인용)
llm_exec_py() { docker exec "$LLM_CONTAINER" python3 -c "$1" 2>/dev/null; }
llm_healthy() {
  llm_exec_py "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${LLM_PORT}/health', timeout=3)" >/dev/null
}
llm_model_name() {
  llm_exec_py "import json, urllib.request; d = json.load(urllib.request.urlopen('http://127.0.0.1:${LLM_PORT}/v1/models', timeout=5)); print(', '.join(m['id'] for m in d.get('data', [])))"
}

# 모델 서버 컨테이너에 번역기 네트워크(이름) 연결 정보가 있는지. 멈춘 컨테이너도 본다.
#   출력: 연결된 네트워크 ID (멈춘 상태에서 붙여 아직 ID 가 없으면 빈 줄). 연결 정보가 없으면 1
llm_entry() {  # llm_entry 네트워크이름
  local out
  out=$(docker inspect --type container -f "{{with index .NetworkSettings.Networks \"$1\"}}{{.NetworkID}}|{{end}}" "$LLM_CONTAINER" 2>/dev/null) || return 1
  [[ -n "$out" ]] || return 1
  echo "${out%|}"
}

# 모델 서버 컨테이너가 지금 있는 번역기 내부 네트워크에 붙어 있는지.
#   연결 정보가 예전(지워진) 네트워크를 가리키면 붙어 있지 않은 것으로 본다.
llm_connected() {
  local want got
  want=$(docker network inspect -f '{{.Id}}' "$NET_INT" 2>/dev/null) || return 1
  got=$(llm_entry "$NET_INT") || return 1
  [[ -z "$got" || "$got" == "$want" ]]
}

# 모델 서버 컨테이너를 번역기 내부 네트워크에 별칭 llm 으로 붙인다 (이미 붙어 있으면 그대로)
#   돌려주는 값: 0 새로 붙임 · 1 실패(LINK_ERR) · 2 이미 붙어 있음 · 3 컨테이너 없음
llm_link() {
  LINK_ERR=""
  exists "$LLM_CONTAINER" || return 3
  llm_connected && return 2
  # 지워진 네트워크를 가리키는 연결 정보(멈춘 컨테이너에 남은 것)는 먼저 지운다
  if llm_entry "$NET_INT" >/dev/null; then docker network disconnect "$NET_INT" "$LLM_CONTAINER" >/dev/null 2>&1 || true; fi
  local out
  if out=$(docker network connect --alias "$LLM_ALIAS" "$NET_INT" "$LLM_CONTAINER" 2>&1); then return 0; fi
  if grep -qi 'already exists' <<<"$out"; then return 2; fi
  LINK_ERR="$out"
  return 1
}

# 번역기 네트워크에 붙은 다른 프로젝트 컨테이너(모델 서버)를 뗀다.
#   떼는 것은 번역기 네트워크 쪽 연결뿐이고, 그 컨테이너의 다른 네트워크(도면 분석기 쪽)는 건드리지 않는다.
#   docker network inspect 는 실행 중인 컨테이너만 보여 주므로, 멈춘 모델 서버 컨테이너는 이름으로
#   직접 확인해 뗀다 (남겨 두면 그 컨테이너를 다시 시작할 수 없게 된다).
llm_unlink_all() {
  local net name names
  for net in "$NET_INT" "$NET_PUB"; do
    names=""
    if ours_network "$net"; then
      names=$(docker network inspect -f '{{range .Containers}}{{.Name}} {{end}}' "$net" 2>/dev/null || true)
    fi
    if ! llm_external && llm_entry "$net" >/dev/null; then names="$names $LLM_CONTAINER"; fi
    # shellcheck disable=SC2086  # 이름 목록을 낱말로 나눈다
    for name in $(printf '%s\n' $names | sort -u); do
      ours_container "$name" && continue
      if docker network disconnect "$net" "$name" >/dev/null 2>&1; then
        [[ "${1:-}" == "--quiet" ]] || ok "모델 서버 연결을 뗐습니다: $name (도면 분석기는 그대로)"
      fi
    done
  done
  return 0
}

# 네트워크가 정말 없어졌는지. 도커 데몬이 다시 시작되는 중이라 답하지 않는 것과 구별한다
# (데몬이 답하고, 그래도 네트워크가 없을 때만 0)
network_gone() {  # network_gone 네트워크
  docker network inspect "$1" >/dev/null 2>&1 && return 1
  docker info >/dev/null 2>&1 || return 1
  ! docker network inspect "$1" >/dev/null 2>&1
}

# 도면 분석기가 다시 시작되어 모델 서버 컨테이너가 새로 만들어지면 자동으로 다시 붙이는 감시 프로세스
linker_pid() {
  local f="$WS/state/linker.pid" pid
  [[ -s "$f" ]] || return 1
  pid=$(cat "$f")
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  grep -qa 'linker\.sh' "/proc/$pid/cmdline" 2>/dev/null || return 1
  echo "$pid"
}
linker_start() {
  linker_stop || return 1
  mkd "$WS/logs" "$WS/state"
  setsid nohup bash "$WS/scripts/linker.sh" >> "$WS/logs/linker.log" 2>&1 < /dev/null &
  local i
  for ((i = 0; i < 20; i++)); do linker_pid >/dev/null && return 0; sleep 0.2; done
  return 1
}
# 감시 프로세스를 끝낸다. 끝내지 못하면(예: sudo 로 띄운 것을 sudo 없이 stop) 프로세스 번호 파일을
# 남기고 1 을 돌려준다 (LINKER_ERR 에 프로세스 번호)
linker_stop() {
  local pid i sent=0
  LINKER_ERR=""
  if pid=$(linker_pid); then
    # 감시 프로세스는 setsid 로 따로 묶여 있으므로 묶음째(docker events 포함) 끝낸다
    if kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null; then sent=1; fi
    if ((sent)); then
      for ((i = 0; i < 25; i++)); do linker_pid >/dev/null || break; sleep 0.2; done
    fi
    if linker_pid >/dev/null; then LINKER_ERR="$pid"; return 1; fi
  fi
  rm -f "$WS/state/linker.pid"
  return 0
}

# ------------------------------------------------------------------ 기타
# 서버의 내부망 주소 (도커가 만든 가상 인터페이스는 뺀다)
lan_ips() {
  if command -v ip >/dev/null 2>&1; then
    ip -4 -o addr show scope global 2>/dev/null \
      | awk '$2 !~ /^(docker|br-|veth|virbr|cni|flannel|vxlan|tun|wg|tailscale)/ {split($4, a, "/"); print a[1]}'
  else
    hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9.]+$'
  fi
  return 0
}

confirm() {  # confirm "질문" [기본값 y|n] → y 면 0
  local def="${2:-n}" a
  if [[ "${ASSUME_YES:-0}" == "1" ]]; then [[ "$def" == "y" ]]; return; fi
  if [[ ! -t 0 ]]; then [[ "$def" == "y" ]]; return; fi
  if [[ "$def" == "y" ]]; then read -r -p "$1 [Y/n] " a; [[ ! "$a" =~ ^[Nn] ]]
  else read -r -p "$1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]; fi
}
