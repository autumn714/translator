"""run.sh / scripts/lib.sh 운영 함수 시험 (bash 로 직접 돌린다. docker 는 셸 함수로 흉내 낸다).

bash 가 없거나 쓸 수 없는 PC 에서는 건너뛴다. 실제 도커는 쓰지 않는다 (실제 흐름은 dev/e2e_docker.sh).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "scripts" / "lib.sh"


def _find_bash() -> str | None:
    candidates = []
    if sys.platform == "win32":
        candidates += [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"]
    found = shutil.which("bash")
    if found and not found.lower().endswith(r"system32\bash.exe"):  # WSL 의 bash 는 경로가 달라 쓰지 않는다
        candidates.append(found)
    for c in candidates:
        if not Path(c).exists():
            continue
        try:
            out = subprocess.run([c, "-c", 'echo "${BASH_VERSINFO[0]}"'], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip().isdigit() and int(out.stdout.strip()) >= 4:
            return c
    return None


BASH = _find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="bash 4 이상이 없음")


def posix(p: Path) -> str:
    return p.as_posix()


def bash(script: str, cwd: Path, env: dict[str, str] | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
    """script 를 파일로 써서 bash 로 실행한다 (윈도 명령줄 따옴표 처리를 피한다)."""
    full_env = {**os.environ, "LC_ALL": "C.UTF-8", **(env or {})}
    full_env.pop("TRANSLATOR_CONFIG_EXTRA", None)
    path = cwd / "_test_script.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    try:
        return subprocess.run([BASH, path.name], cwd=cwd, capture_output=True, timeout=timeout, env=full_env,
                              encoding="utf-8", errors="replace")
    finally:
        path.unlink(missing_ok=True)


def lib_script(ws: Path, body: str, config: str = "") -> str:
    """임시 프로젝트 폴더(ws)를 WS 로 두고 lib.sh 를 불러 body 를 실행하는 스크립트."""
    (ws / "config.env").write_text(config, encoding="utf-8", newline="\n")
    return f'set -uo pipefail\nWS="{posix(ws)}"\nCMD=test\nsource "{posix(LIB)}"\n{body}\n'


# docker 흉내: 호출을 $CALLS 에 적고, 환경 변수로 정한 상태를 돌려준다
#   NET_EXISTS       : 있는 번역기 네트워크 이름(띄어쓰기로 여러 개)
#   NET_ID           : 그 네트워크의 ID
#   ENDPOINTS        : network inspect 의 실행 중 연결 컨테이너 이름
#   LLM_EXISTS       : 1 이면 pfdvlm-vllm 컨테이너가 있음
#   ENTRY_INT        : pfdvlm-vllm 의 translator-int 연결 정보 출력 ("ID|", "|", 비면 연결 정보 없음)
#   DAEMON_UP        : 0 이면 docker info 실패
DOCKER_STUB = r'''
docker() {
  printf '%s\n' "$*" >> "$CALLS"
  local args=("$@") name="${!#}" fmt="" i
  for ((i = 0; i < ${#args[@]}; i++)); do [[ "${args[i]}" == -f ]] && fmt="${args[i+1]}"; done
  case "$1" in
    info) [[ "${DAEMON_UP:-1}" == 1 ]]; return;;
    network)
      case "$2" in
        inspect)
          [[ "${DAEMON_UP:-1}" == 1 && " ${NET_EXISTS:-} " == *" $name "* ]] || return 1
          case "$fmt" in
            *Labels*) echo "translator";;
            *.Id*) echo "${NET_ID:-NEWID}";;
            *Containers*) echo "${ENDPOINTS:-}";;
          esac
          return 0;;
        connect|disconnect) return 0;;
        ls) return 0;;
      esac;;
    inspect)
      [[ "$name" == pfdvlm-vllm && "${LLM_EXISTS:-1}" == 1 ]] || return 1
      case "$fmt" in
        *NetworkSettings*translator-int*) [[ -n "${ENTRY_INT:-}" ]] && echo "$ENTRY_INT";;
        *) ;;
      esac
      return 0;;
    ps) [[ "$*" == *pfdvlm.project* ]] && echo "${COTENANT_IDS:-}"; return 0;;
    image) [[ "$*" == *vllm/vllm-openai* ]] && echo "${VLLM_IMAGE_ID:-}"; return 0;;
  esac
  return 0
}
'''


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    return d


def run_lib(ws: Path, body: str, config: str = "", env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    calls = ws / "calls.txt"
    calls.write_text("", encoding="utf-8")
    script = lib_script(ws, DOCKER_STUB + "\n" + body, config)
    return bash(script, ws, {"CALLS": posix(calls), **(env or {})})


def calls_of(ws: Path) -> list[str]:
    return (ws / "calls.txt").read_text(encoding="utf-8").splitlines()


# ------------------------------------------------------------------ 설정 읽기
@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", "1"), ("true", "1"), ("TRUE", "1"), (" yes ", "1"), ("On", "1"),
     ("0", "0"), ("", "0"), ("false", "0"), ("off", "0"), ("y", "0"), ("2", "0")],
)
def test_ui_auth_is_normalized_like_the_app(ws: Path, value: str, expected: str) -> None:
    out = run_lib(ws, 'load_config; printf "%s" "$UI_AUTH"', config=f'UI_AUTH="{value}"\n')
    assert out.returncode == 0, out.stderr
    assert out.stdout == expected


def test_label_differs_per_copy(ws: Path) -> None:
    out = run_lib(ws, 'load_config; printf "%s\\n" "$LABEL" "${COMMON_ARGS[*]}" "$N_APP"', config='NAME_PREFIX="translator2"\n')
    assert out.returncode == 0, out.stderr
    label, common, app = out.stdout.splitlines()
    assert label == "translator.project=translator2"
    assert "--label translator.project=translator2" in common
    assert app == "translator2-app"


def test_defaults_include_new_settings(ws: Path) -> None:
    out = run_lib(ws, 'load_config; printf "%s %s %s" "$DOC_DISK_QUOTA_MB" "$APP_MEMORY" "$LABEL"')
    assert out.returncode == 0, out.stderr
    assert out.stdout == "2048 8g translator.project=translator"


# ------------------------------------------------------------------ 모델 서버 연결 / 떼기
@pytest.mark.parametrize(
    ("entry", "connected"),
    [("NEWID|", True), ("|", True), ("OLDID|", False), ("", False)],
)
def test_llm_connected_rejects_stale_entry(ws: Path, entry: str, connected: bool) -> None:
    out = run_lib(ws, "load_config; if llm_connected; then echo yes; else echo no; fi",
                  env={"NET_EXISTS": "translator-int", "NET_ID": "NEWID", "ENTRY_INT": entry})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ("yes" if connected else "no")


def test_llm_link_replaces_stale_entry(ws: Path) -> None:
    out = run_lib(ws, "load_config; rc=0; llm_link || rc=$?; echo rc=$rc",
                  env={"NET_EXISTS": "translator-int", "NET_ID": "NEWID", "ENTRY_INT": "OLDID|"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "rc=0"
    calls = calls_of(ws)
    disc = calls.index("network disconnect translator-int pfdvlm-vllm")
    conn = calls.index("network connect --alias llm translator-int pfdvlm-vllm")
    assert disc < conn


def test_unlink_detaches_stopped_llm_container(ws: Path) -> None:
    # 멈춘 컨테이너는 network inspect 의 목록에 없다 (ENDPOINTS 비움). 그래도 이름으로 확인해 뗀다
    out = run_lib(ws, "load_config; llm_unlink_all --quiet",
                  env={"NET_EXISTS": "translator-int translator-pub", "ENDPOINTS": "", "ENTRY_INT": "NEWID|"})
    assert out.returncode == 0, out.stderr
    assert "network disconnect translator-int pfdvlm-vllm" in calls_of(ws)
    assert "network disconnect translator-pub pfdvlm-vllm" not in calls_of(ws)


def test_unlink_removes_entry_even_when_network_is_gone(ws: Path) -> None:
    # 예전 stop 이 네트워크만 지우고 연결 정보를 남긴 경우: 남은 정보를 지워야 모델 서버를 다시 시작할 수 있다
    out = run_lib(ws, "load_config; llm_unlink_all --quiet", env={"NET_EXISTS": "", "ENTRY_INT": "OLDID|"})
    assert out.returncode == 0, out.stderr
    assert "network disconnect translator-int pfdvlm-vllm" in calls_of(ws)


def test_unlink_skips_llm_in_external_mode(ws: Path) -> None:
    out = run_lib(ws, "load_config; llm_unlink_all --quiet", config='LLM_URL="http://10.0.0.1:8000/v1"\n',
                  env={"NET_EXISTS": "", "ENTRY_INT": "NEWID|"})
    assert out.returncode == 0, out.stderr
    assert not [c for c in calls_of(ws) if c.startswith("network disconnect")]


# ------------------------------------------------------------------ 감시 프로세스
def test_network_gone_waits_for_daemon(ws: Path) -> None:
    body = 'load_config; for s in "0" "1"; do if DAEMON_UP=$s network_gone translator-int; then echo gone; else echo wait; fi; done'
    out = run_lib(ws, body, env={"NET_EXISTS": ""})
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["wait", "gone"]
    out = run_lib(ws, 'load_config; network_gone translator-int && echo gone || echo here', env={"NET_EXISTS": "translator-int"})
    assert out.stdout.strip() == "here"


def test_linker_stop_keeps_pid_file_when_kill_is_refused(ws: Path) -> None:
    body = r'''
load_config; mkdir -p "$WS/state"; echo 4242 > "$WS/state/linker.pid"
linker_pid() { echo 4242; }          # 아직 살아 있는 감시 프로세스
kill() { return 1; }                 # 권한 없음 (sudo 로 띄운 감시)
if linker_stop; then echo stopped; else echo "failed $LINKER_ERR"; fi
[[ -e "$WS/state/linker.pid" ]] && echo kept
'''
    out = run_lib(ws, body)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["failed", "4242", "kept"]


def test_linker_stop_removes_pid_file_after_exit(ws: Path) -> None:
    body = r'''
load_config; mkdir -p "$WS/state"; echo 4242 > "$WS/state/linker.pid"
alive=1
linker_pid() { ((alive)) && echo 4242; }
kill() { alive=0; return 0; }
if linker_stop; then echo stopped; else echo failed; fi
[[ -e "$WS/state/linker.pid" ]] || echo removed
'''
    out = run_lib(ws, body)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["stopped", "removed"]


def test_linker_events_stay_in_process_group() -> None:
    text = (REPO / "scripts" / "linker.sh").read_text(encoding="utf-8")
    assert "timeout --foreground 300 docker events" in text


# ------------------------------------------------------------------ clean: 함께 쓰는 이미지
@pytest.mark.parametrize(
    ("env", "shared"),
    [
        ({"LLM_EXISTS": "1"}, True),
        ({"LLM_EXISTS": "0", "COTENANT_IDS": "abc123"}, True),
        ({"LLM_EXISTS": "0", "VLLM_IMAGE_ID": "sha256:1"}, True),
        ({"LLM_EXISTS": "0"}, False),
    ],
)
def test_cotenant_detected_even_when_its_containers_are_gone(ws: Path, env: dict[str, str], shared: bool) -> None:
    out = run_lib(ws, "load_config; if why=$(cotenant_reason); then echo \"shared:$why\"; else echo alone; fi", env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("shared:") is shared


# ------------------------------------------------------------------ run.sh 자체
def _copy_package(dest: Path) -> None:
    shutil.copy2(REPO / "run.sh", dest / "run.sh")
    (dest / "scripts").mkdir()
    for f in ("lib.sh", "linker.sh"):
        shutil.copy2(REPO / "scripts" / f, dest / "scripts" / f)
    shutil.copy2(REPO / "config.env", dest / "config.env")


@pytest.mark.parametrize("damage", ["bom", "crlf", "both"])
def test_run_sh_repairs_lib_sh_before_sourcing(ws: Path, damage: str) -> None:
    _copy_package(ws)
    lib = ws / "scripts" / "lib.sh"
    data = lib.read_bytes()
    if damage in ("crlf", "both"):
        data = data.replace(b"\n", b"\r\n")
    if damage in ("bom", "both"):
        data = b"\xef\xbb\xbf" + data
    lib.write_bytes(data)
    out = bash("bash run.sh help", ws)
    assert out.returncode == 0, out.stderr
    assert "사용법" in out.stdout
    fixed = lib.read_bytes()
    assert not fixed.startswith(b"\xef\xbb\xbf")
    # 윈도용 grep(Git Bash)은 줄 끝 CR 을 떼고 읽어 CRLF 만 있는 경우를 못 본다 (bash 도 그대로 읽어 문제없음).
    # 서버(리눅스)의 grep 은 본다.
    if not (sys.platform == "win32" and damage == "crlf"):
        assert b"\r" not in fixed


def test_scripts_pass_bash_syntax_check(tmp_path: Path) -> None:
    for f in ("run.sh", "scripts/lib.sh", "scripts/linker.sh", "dev/e2e_docker.sh"):
        out = bash(f'bash -n "{posix(REPO / f)}"', tmp_path)
        assert out.returncode == 0, f"{f}: {out.stderr}"
