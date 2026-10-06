#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  모델 서버 자동 재연결 — run.sh start 가 뒤에서 띄우고 run.sh stop 이 끝낸다.
#
#  도면 분석기를 다시 시작하면 모델 서버 컨테이너가 새로 만들어져, 번역기 내부 네트워크에
#  붙여 둔 연결이 사라진다. 도커 이벤트(그 컨테이너의 시작)를 지켜보다가 다시 붙인다.
#  이벤트를 놓쳐도 되도록 5분마다 한 번은 직접 확인한다.
#  기록: logs/linker.log · 프로세스 번호: state/linker.pid
# ─────────────────────────────────────────────────────────────
set -uo pipefail
WS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CMD="linker"
# shellcheck source=scripts/lib.sh
source "$WS/scripts/lib.sh"
load_config >/dev/null
echo $$ > "$WS/state/linker.pid"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

relink() {
  local rc=0
  llm_link || rc=$?
  case $rc in
    0) log "연결함: $LLM_CONTAINER → $NET_INT (별칭 $LLM_ALIAS)";;
    1) log "연결 실패: $LINK_ERR";;
  esac
  return 0
}

trap 'log "감시 끝"; exit 0' TERM INT HUP
log "감시 시작: $LLM_CONTAINER (pid $$)"
down=0; wait_s=5
while true; do
  if ! docker network inspect "$NET_INT" >/dev/null 2>&1; then
    if network_gone "$NET_INT"; then
      log "네트워크 $NET_INT 가 없어 감시를 끝냅니다."
      exit 0
    fi
    ((down)) || log "도커 데몬이 답하지 않습니다. 다시 답할 때까지 기다립니다."
    down=1
    sleep "$wait_s"
    wait_s=$((wait_s * 2 > 60 ? 60 : wait_s * 2))
    continue
  fi
  if ((down)); then log "도커 데몬이 다시 답합니다."; down=0; wait_s=5; fi
  relink
  # --foreground: timeout 이 따로 프로세스 묶음을 만들지 않게 해, stop 이 묶음째 끝낼 때 docker events 도 함께 끝난다
  while read -r _; do
    sleep 1   # 막 시작된 컨테이너가 자리 잡을 시간
    relink
  done < <(timeout --foreground 300 docker events --filter type=container --filter event=start \
             --filter "container=$LLM_CONTAINER" --format '{{.Actor.ID}}' 2>/dev/null)
  sleep 2
done
