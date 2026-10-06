#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  개발 PC 전용 종단 시험 — run.sh 전체 흐름을 실제 도커로 돌려 본다.
#
#  - 도면 분석기 모델 서버 대신 가짜 모델 서버(dev/fake_vllm.py)를 e2e-fake-vllm 으로 띄운다.
#  - run.sh 는 도커 CLI 컨테이너(e2e-runner) 안에서 돌린다. 저장소를 호스트와 같은 경로로 붙이므로
#    run.sh 가 거는 바인드 마운트가 그대로 풀린다 (Windows Docker Desktop: /run/desktop/mnt/host/c/...).
#  - 실제 이름(pfdvlm-*)은 쓰지 않는다. 번역기 컨테이너 이름(translator-*)과 포트 17870 을 쓴다.
#
#  bash dev/e2e_docker.sh                         전체 시험
#  E2E_UI_AUTH=1 bash dev/e2e_docker.sh           로그인을 켠 상태로 시험
#  E2E_SELFTEST=optional bash dev/e2e_docker.sh   selftest 실패를 결과에 넣지 않음 (앱 개발 중)
#  E2E_KEEP=1 bash dev/e2e_docker.sh              끝난 뒤 번역기·가짜 모델 서버를 그대로 둠 (디버깅)
#  필요: docker, git, curl. 인터넷(처음 한 번: docker:29-cli 이미지, apk 패키지, pip 패키지)
# ─────────────────────────────────────────────────────────────
set -uo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
case "$REPO" in
  /[a-zA-Z]/*) d="${REPO:1:1}"; HOST="/run/desktop/mnt/host/${d,,}${REPO:2}";;   # Git Bash → Docker Desktop 경로
  *) HOST="$REPO";;
esac
PY_IMAGE="python:3.12-slim"
RUNNER_IMAGE="${E2E_RUNNER_IMAGE:-docker:29-cli}"
PORT="${E2E_UI_PORT:-17870}"
AUTH="${E2E_UI_AUTH:-0}"
PASSWORD="${E2E_UI_PASSWORD:-}"
LBL="translator.project"   # 라벨 키만 본다 (값은 NAME_PREFIX: 어느 복사본의 것이든 남으면 안 된다)

PASS=0; FAIL=0; NOTES=()
pass() { printf '[PASS] %s\n' "$*"; PASS=$((PASS+1)); }
fail() { printf '[FAIL] %s\n' "$*"; FAIL=$((FAIL+1)); }
note() { printf '[INFO] %s\n' "$*"; NOTES+=("$*"); }
check() { local name="$1"; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
title() { printf '\n######## %s ########\n' "$*"; }
indent() { sed 's/^/    /'; }

R() {  # runner 안에서 run.sh 실행 (E2E_ALLOW 로 UI_ALLOW 를 바꿔 볼 수 있다)
  docker exec -e TRANSLATOR_CONFIG_EXTRA=dev/e2e.env -e E2E_UI_PORT="$PORT" -e E2E_UI_AUTH="$AUTH" \
    -e E2E_UI_PASSWORD="$PASSWORD" -e E2E_UI_ALLOW="${E2E_ALLOW:-}" e2e-runner bash run.sh "$@"
}

fake_up() {
  docker run -d --name e2e-fake-vllm --label e2e.test=1 --network e2e-llm-net \
    --mount "type=bind,src=$HOST/dev,dst=/fake,readonly" "$PY_IMAGE" \
    python /fake/fake_vllm.py --host 0.0.0.0 --port 8000 --delay-ms 5 >/dev/null
}

e2e_down() {
  docker rm -f e2e-runner e2e-fake-vllm >/dev/null 2>&1
  docker network rm e2e-llm-net >/dev/null 2>&1
  return 0
}

app_llm_model() {
  docker exec translator-app python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://llm:8000/v1/models', timeout=5))['data'][0]['id'])" 2>/dev/null
}

wait_for() {  # wait_for 초 명령...
  local limit="$1" t0=$SECONDS; shift
  until "$@"; do (( SECONDS - t0 >= limit )) && return 1; sleep 0.5; done
  return 0
}

# shellcheck disable=SC2329  # wait_for 로 부른다
fake_linked() { [[ -n "$(docker inspect -f '{{with index .NetworkSettings.Networks "translator-int"}}y{{end}}' e2e-fake-vllm 2>/dev/null)" ]]; }
fake_nets()   { docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' e2e-fake-vllm 2>/dev/null | xargs; }
leftover_containers() { docker ps -aq --filter "label=$LBL"; }
leftover_networks()   { docker network ls -q --filter "label=$LBL"; }
# shellcheck disable=SC2329  # check 로 부른다
linker_alive()        { docker exec e2e-runner pgrep -f 'scripts/linker[.]sh' >/dev/null; }   # pgrep 은 자기 자신을 빼고 찾는다
# shellcheck disable=SC2329  # check 로 부른다
events_alive()        { docker exec e2e-runner pgrep -f 'docker events --filter type=container' >/dev/null; }   # 감시 안의 docker events
# shellcheck disable=SC2329  # wait_for 로 부른다
followers_gone()      { ! docker exec e2e-runner pgrep -f 'docker logs -f' >/dev/null; }   # 화면 로그 옮겨 적기
export -f linker_alive events_alive

# ================================================================== 준비
title "준비"
cd "$REPO" || exit 1
if ! docker info >/dev/null 2>&1; then echo "도커에 연결할 수 없습니다."; exit 1; fi
if [[ -n "$(leftover_containers)$(leftover_networks)" ]]; then
  echo "translator.project 라벨이 붙은 컨테이너·네트워크가 이미 있습니다. 먼저 정리하세요 (bash run.sh stop)."; exit 1
fi
e2e_down
PY_BEFORE=0; docker image inspect "$PY_IMAGE" >/dev/null 2>&1 && PY_BEFORE=1
GLOSSARY_BEFORE=$(git hash-object data/glossary/default.json)
GLOSSARY_COPY=$(mktemp); cp data/glossary/default.json "$GLOSSARY_COPY"
for d in cache logs state data/jobs; do [[ -e "$d" ]] && { echo "$d 가 이미 있습니다. 시험 전에 지우세요."; exit 1; }; done
IGNORED_BEFORE=$(git status --porcelain --ignored --untracked-files=all | grep '^!!' | sort)
info_host() { printf '  저장소: %s\n  도커 경로: %s\n' "$REPO" "$HOST"; }
info_host

docker network create --internal --label e2e.test=1 e2e-llm-net >/dev/null
fake_up
check "가짜 모델 서버 시작 (e2e-fake-vllm)" wait_for 20 docker exec e2e-fake-vllm python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

docker image inspect "$RUNNER_IMAGE" >/dev/null 2>&1 || docker pull -q "$RUNNER_IMAGE" >/dev/null
SOCK_GID=$(docker run --rm -v /var/run/docker.sock:/var/run/docker.sock "$RUNNER_IMAGE" stat -c %g /var/run/docker.sock)
docker run -d --init --name e2e-runner --label e2e.test=1 --user 1000:1000 --group-add "$SOCK_GID" -e HOME=/tmp \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --mount "type=bind,src=$HOST,dst=$HOST" -w "$HOST" "$RUNNER_IMAGE" sleep infinity >/dev/null
docker exec -u 0 e2e-runner apk add --no-cache -q bash coreutils findutils grep sed gawk iproute2 util-linux procps >/dev/null
check "runner 준비 (uid 1000, 도커 소켓 그룹 $SOCK_GID)" docker exec -e DOCKER_CONFIG=/tmp/e2e-docker-config e2e-runner docker version --format '{{.Server.Version}}'
docker exec -u 0 e2e-runner sh -c 'rm -rf /tmp/e2e-docker-config; touch /e2e.marker'   # 이 뒤로 runner 안 프로젝트 폴더 밖에 생기는 파일을 찾는다

# ================================================================== setup / check / start
title "setup"
check "run.sh setup" R setup
title "check"
check "run.sh check" R check
title "start"
check "run.sh start" R start

title "접속·연결 확인"
check "Windows 에서 /health" bash -c "curl -fsS -m 10 http://127.0.0.1:$PORT/health | grep -q ok"
code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "http://127.0.0.1:$PORT/")
check "Windows 에서 / (HTTP $code)" bash -c "[[ '$code' == 200 || '$code' == 302 || '$code' == 307 ]]"
check "앱 → http://llm:8000/v1/models" [ "$(app_llm_model)" == "Fake-Qwen" ]
check "앱 컨테이너에서 인터넷 차단" bash -c "! docker exec translator-app python -c \"import socket; socket.create_connection(('1.1.1.1', 443), timeout=3)\" 2>/dev/null"
hard=$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}} {{.Config.User}} {{.HostConfig.SecurityOpt}}' translator-app)
check "앱 컨테이너 읽기 전용·권한 최소 ($hard)" [ "$hard" == "true [ALL] 1000:1000 [no-new-privileges:true]" ]
# shellcheck disable=SC2329  # check 로 부른다
limits_ok() {  # limits_ok 컨테이너 → 메모리(스왑 없음)·프로세스 수·/tmp 크기 상한, 비정상 종료 시 다시 시작
  local mem swap pids pol tmp
  read -r mem swap pids pol tmp <<<"$(docker inspect -f '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.PidsLimit}} {{.HostConfig.RestartPolicy.Name}}:{{.HostConfig.RestartPolicy.MaximumRetryCount}} {{index .HostConfig.Tmpfs "/tmp"}}' "$1")"
  printf '    %s: 메모리 %s · 스왑 포함 %s · 프로세스 %s · %s · /tmp %s\n' "$1" "$mem" "$swap" "$pids" "$pol" "$tmp"
  (( mem > 0 && swap == mem && pids > 0 )) && [[ "$pol" == "on-failure:5" && "$tmp" == *size=* ]]
}
check "앱 컨테이너 자원 상한·자동 재시작" limits_ok translator-app
check "중계기 자원 상한·자동 재시작" limits_ok translator-gate
check "자동 재연결 감시 실행 중" linker_alive
check "화면 로그가 logs/ 에 쌓임" docker exec e2e-runner grep -q "Application startup complete" logs/app_latest.log
if [[ "$AUTH" == "1" ]]; then
  pw="${PASSWORD:-$(docker exec e2e-runner cat state/ui_password 2>/dev/null)}"
  check "로그인 비밀번호 준비" [ -n "$pw" ]
  check "비밀번호가 컨테이너 명령줄에 없음" bash -c "! docker inspect -f '{{json .Config.Cmd}} {{json .Args}}' translator-app | grep -qF -- '$pw'"
  check "비밀번호는 환경 변수로 전달" bash -c "docker inspect -f '{{json .Config.Env}}' translator-app | grep -qF -- 'UI_PASSWORD=$pw'"
fi

title "도면 분석기 재시작 흉내 (가짜 모델 서버 다시 만들기)"
docker rm -f e2e-fake-vllm >/dev/null
fake_up
t0=$SECONDS
if wait_for 10 fake_linked; then pass "10초 안에 다시 연결 ($((SECONDS - t0))초)"; else fail "10초 안에 다시 연결되지 않음"; fi
check "다시 연결 후 앱 → 모델 서버" wait_for 20 app_llm_model
docker exec e2e-runner tail -n 3 logs/linker.log | indent

title "status"
out=$(R status 2>&1); echo "$out"
check "status: 연결 표시" grep -q "번역기 내부 네트워크에 연결됨" <<<"$out"
check "status: 모델 이름" grep -q "모델 Fake-Qwen" <<<"$out"

title "UI_ALLOW (틀린 값은 시작 전에 막는다)"
out=$(E2E_ALLOW="10.1.20.0/33" R start 2>&1); rc=$?; echo "$out" | indent
check "틀린 UI_ALLOW 면 start 실패" [ "$rc" != 0 ]
check "틀린 값 안내" grep -q "UI_ALLOW" <<<"$out"
check "떠 있던 번역기는 그대로" bash -c "curl -fsS -m 10 http://127.0.0.1:$PORT/health | grep -q ok"
out=$(E2E_ALLOW="0.0.0.0/0 ::/0" R start 2>&1); rc=$?
check "올바른 UI_ALLOW 로 start" [ "$rc" == 0 ]
check "허용 범위 안내" grep -q "접속 허용 범위: 0.0.0.0/0" <<<"$out"
check "허용 범위 안에서 접속" bash -c "curl -fsS -m 10 http://127.0.0.1:$PORT/health | grep -q ok"
check "다시 시작 후 앱 → 모델 서버" wait_for 20 app_llm_model

title "selftest"
if R selftest; then pass "run.sh selftest"
elif [[ "${E2E_SELFTEST:-required}" == "optional" ]]; then note "run.sh selftest 실패 (E2E_SELFTEST=optional 이라 결과에서 뺌)"
else fail "run.sh selftest"; fi

title "모델 서버 없이 시작 (도면 분석기가 꺼진 상태)"
docker rm -f e2e-fake-vllm >/dev/null
out=$(R start 2>&1); rc=$?; echo "$out"
check "run.sh start 는 성공" [ "$rc" == 0 ]
check "꺼져 있다는 안내" grep -q "도면 분석기 모델 서버가 꺼져 있습니다" <<<"$out"
check "화면은 뜸" bash -c "curl -fsS -m 10 http://127.0.0.1:$PORT/health | grep -q ok"
fake_up
t0=$SECONDS
if wait_for 10 fake_linked; then pass "모델 서버가 켜지자 10초 안에 연결 ($((SECONDS - t0))초)"; else fail "모델 서버가 켜진 뒤 연결되지 않음"; fi
check "연결 후 앱 → 모델 서버" wait_for 20 app_llm_model

if [[ "${E2E_KEEP:-0}" == "1" ]]; then
  note "E2E_KEEP=1: 번역기와 가짜 모델 서버를 남겨 둡니다 (http://127.0.0.1:$PORT). 정리: docker exec e2e-runner bash run.sh clean --yes; docker rm -f e2e-runner e2e-fake-vllm; docker network rm e2e-llm-net"
  printf '\n결과: 통과 %d · 실패 %d\n' "$PASS" "$FAIL"; exit $((FAIL > 0))
fi

# ================================================================== stop / clean
title "stop"
check "run.sh stop" R stop
check "라벨 붙은 컨테이너 없음" bash -c "[[ -z \"\$(docker ps -aq --filter label=$LBL)\" ]]"
check "라벨 붙은 네트워크 없음" bash -c "[[ -z \"\$(docker network ls -q --filter label=$LBL)\" ]]"
nets=$(fake_nets)
check "가짜 모델 서버는 원래 네트워크만 ($nets)" [ "$nets" == "e2e-llm-net" ]
check "가짜 모델 서버는 계속 실행 중" bash -c "[[ \"\$(docker inspect -f '{{.State.Running}}' e2e-fake-vllm)\" == true ]]"
check "자동 재연결 감시 종료" bash -c "! linker_alive && docker exec e2e-runner test ! -e state/linker.pid"
check "감시 안의 docker events 도 종료" bash -c "! events_alive"
check "화면 로그 옮겨 적기 종료" wait_for 10 followers_gone

title "clean --yes"
docker exec e2e-runner sh -c 'mkdir -p data/jobs/e2e-leftover && echo x > data/jobs/e2e-leftover/input.txt'
check "run.sh clean --yes" R clean --yes
# shellcheck disable=SC2016  # runner 안의 sh 가 푼다
check "clean 이 남은 문서(data/jobs)를 지움" docker exec e2e-runner sh -c '[ -d data/jobs ] && [ -z "$(ls -A data/jobs)" ]'
if ((PY_BEFORE)); then
  check "원래 있던 $PY_IMAGE 는 남김" docker image inspect "$PY_IMAGE" >/dev/null
fi

title "흔적 확인"
outside=$(docker exec e2e-runner find / -xdev \( -path /proc -o -path /sys -o -path /dev -o -path "$HOST" \) -prune -o -newer /e2e.marker -print 2>/dev/null | grep -v -e '^/e2e.marker$' || true)
if [[ -z "$outside" ]]; then pass "프로젝트 폴더 밖(runner 파일 시스템)에 새 파일 없음"
else fail "프로젝트 폴더 밖에 생긴 파일:"; indent <<<"$outside"; fi
IGNORED_AFTER=$(git status --porcelain --ignored --untracked-files=all | grep '^!!' | sort)
new_ignored=$(comm -13 <(echo "$IGNORED_BEFORE") <(echo "$IGNORED_AFTER") | sed 's/^!! //')
bad=$(grep -v -E '^(cache|logs|state|data/jobs)/' <<<"$new_ignored" | grep . || true)
echo "  새로 생긴 무시 파일: $(wc -l <<<"$new_ignored" | xargs)개 ($(cut -d/ -f1 <<<"$new_ignored" | sort -u | xargs))"
if [[ -z "$bad" ]]; then pass "남은 파일은 cache/ logs/ state/ data/jobs/ 안에만"
else fail "그 밖의 남은 파일:"; indent <<<"$bad"; fi

# ================================================================== 뒷정리
title "뒷정리"
e2e_down
docker run --rm --mount "type=bind,src=$HOST,dst=/w" "$PY_IMAGE" rm -rf /w/cache /w/logs /w/state /w/data/jobs
check "저장소에 실행 흔적 없음 (cache logs state data/jobs)" bash -c "[[ ! -e cache && ! -e logs && ! -e state && ! -e data/jobs ]]"
if [[ "$(git hash-object data/glossary/default.json)" == "$GLOSSARY_BEFORE" ]]; then pass "용어집(data/glossary/default.json) 그대로"
else fail "앱이 용어집(data/glossary/default.json)을 고쳐 썼음 — 시험 전 내용으로 되돌림"; cp "$GLOSSARY_COPY" data/glossary/default.json; fi
rm -f "$GLOSSARY_COPY"

printf '\n결과: 통과 %d · 실패 %d\n' "$PASS" "$FAIL"
for n in "${NOTES[@]}"; do printf '  참고: %s\n' "$n"; done
exit $((FAIL > 0))
