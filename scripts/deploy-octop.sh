#!/usr/bin/env bash
# Octop 部署脚本（内网测试机/正式机）
#
# 用法:
#   ./scripts/deploy-octop.sh                          # 构建 + 部署到测试机(206)
#   ./scripts/deploy-octop.sh --target prod            # 构建 + 部署到正式机(99)
#   ./scripts/deploy-octop.sh --skip-build             # 跳过构建，用 dist/ 下最新 wheel
#   ./scripts/deploy-octop.sh --wheel dist/xxx.whl     # 指定 wheel 文件
#
# 目标机器:
#   test / 206  →  claw@192.168.220.206  用户级 systemd (octop.service, ~/.octop/venv, uv 安装)
#   prod / 99   →  hjh@192.168.220.99    系统级 systemd (root, /root/.octop/venv, venv 自带 pip)
#                 需要环境变量 OCTOP_PROD_ROOT_PWD（root 密码），缺省时交互输入
#
# 依赖: ssh/scp 免密已配、本地 uv (~/.local/bin/uv)、node/npm（构建时）

set -euo pipefail

# ── 配置 ────────────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DASHBOARD_DIR="$REPO_ROOT/dashboard"
DIST_DIR="$REPO_ROOT/dist"

TEST_HOST="claw@192.168.220.206"
PROD_HOST="hjh@192.168.220.99"
PROD_ROOT_PWD="${OCTOP_PROD_ROOT_PWD:-}"

TARGET="test"
SKIP_BUILD=0
WHEEL=""

# ── 参数解析 ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)      TARGET="$2"; shift 2 ;;
    --skip-build)  SKIP_BUILD=1; shift ;;
    --wheel)       WHEEL="$2"; shift 2 ;;
    -h|--help)     grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1（--help 查看用法）" >&2; exit 1 ;;
  esac
done

case "$TARGET" in
  test|206) TARGET="test"; HOST="$TEST_HOST" ;;
  prod|99)  TARGET="prod"; HOST="$PROD_HOST" ;;
  *) echo "--target 仅支持 test(206) / prod(99)" >&2; exit 1 ;;
esac

# ── 工具函数 ────────────────────────────────────────────────────────────────
info()  { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m  ✅\033[0m %s\n' "$*"; }
fail()  { printf '\033[1;31m  ❌\033[0m %s\n' "$*"; exit 1; }

# 本地 uv（Git Bash 不在 PATH）
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null 2>&1 || fail "本地未找到 uv（~/.local/bin/uv）"

# ── 阶段 1: 构建 ────────────────────────────────────────────────────────────
if [[ $SKIP_BUILD -eq 0 ]]; then
  info "阶段 1/4: 构建前端 + wheel"
  if [[ ! -d "$DASHBOARD_DIR/node_modules" ]]; then
    (cd "$DASHBOARD_DIR" && npm ci) || fail "npm ci 失败"
  fi
  (cd "$DASHBOARD_DIR" && NODE_ENV=production NODE_OPTIONS="--max-old-space-size=2048" npm run build) \
    || fail "前端构建失败"

  # 按 Makefile build-wheel 步骤打包（结束后还原）
  cleanup_build() {
    [[ -f "$REPO_ROOT/pyproject.toml.bak" ]] && mv -f "$REPO_ROOT/pyproject.toml.bak" "$REPO_ROOT/pyproject.toml"
    rm -f "$REPO_ROOT/README.pypi.md"
  }
  trap cleanup_build EXIT
  { cat "$REPO_ROOT/CHANGELOG.md"; echo; cat "$REPO_ROOT/README.md"; } > "$REPO_ROOT/README.pypi.md"
  cp "$REPO_ROOT/pyproject.toml" "$REPO_ROOT/pyproject.toml.bak"
  (cd "$REPO_ROOT" && uv run python -c "from pathlib import Path; p=Path('pyproject.toml'); p.write_text(p.read_text().replace('readme = \"README.md\"', 'readme = \"README.pypi.md\"', 1))") \
    || { cleanup_build; fail "替换 readme 字段失败"; }
  rm -rf "$DIST_DIR"/*
  (cd "$REPO_ROOT" && uv build) || { cleanup_build; fail "uv build 失败"; }
  cleanup_build; trap - EXIT
  WHEEL=$(ls -t "$DIST_DIR"/octop-*.whl 2>/dev/null | head -1)
  [[ -n "$WHEEL" ]] || fail "构建后未找到 wheel"
  ok "wheel: $(basename "$WHEEL")"
else
  info "阶段 1/4: 跳过构建"
  if [[ -z "$WHEEL" ]]; then
    WHEEL=$(ls -t "$DIST_DIR"/octop-*.whl 2>/dev/null | head -1)
  fi
  [[ -n "$WHEEL" && -f "$WHEEL" ]] || fail "未找到 wheel（dist/ 为空或路径无效）"
  ok "使用现有 wheel: $(basename "$WHEEL")"
fi

REMOTE_VER_BEFORE=""
REMOTE_TMP="/tmp/$(basename "$WHEEL")"

# ── 阶段 2: 传输 ────────────────────────────────────────────────────────────
info "阶段 2/4: 传输 wheel → $HOST"
scp -q "$WHEEL" "$HOST:$REMOTE_TMP" || fail "scp 传输失败"
ok "已传输 $REMOTE_TMP"

# ── 阶段 3: 安装 + 重启 ─────────────────────────────────────────────────────
info "阶段 3/4: 安装并重启（target=$TARGET）"
if [[ "$TARGET" == "test" ]]; then
  # 206: claw 用户级 systemd，venv 无 pip，用 uv 安装
  REMOTE_VER_BEFORE=$(ssh "$HOST" '/home/claw/.octop/venv/bin/octop --version 2>/dev/null || true')
  ssh "$HOST" "/home/claw/.local/bin/uv pip install --python /home/claw/.octop/venv/bin/python --no-deps $REMOTE_TMP" \
    || fail "uv pip install 失败"
  ssh "$HOST" "systemctl --user restart octop" || fail "服务重启失败"
else
  # 99: root 系统级 systemd，hjh 无 sudo，经 su -c 提权；venv 自带 pip
  if [[ -z "$PROD_ROOT_PWD" ]]; then
    read -rsp "正式机(99) root 密码: " PROD_ROOT_PWD; echo
  fi
  run_root() { ssh "$PROD_HOST" "echo '$PROD_ROOT_PWD' | su -c \"$1\" 2>&1 | grep -v '^Password:'"; }
  REMOTE_VER_BEFORE=$(run_root "cat /root/.octop/venv/lib/python3.*/site-packages/octop-*.dist-info/METADATA 2>/dev/null | grep '^Version:' | head -1" || true)
  run_root "/root/.octop/venv/bin/pip install --no-deps --quiet $REMOTE_TMP" \
    || fail "pip install 失败"
  run_root "systemctl restart octop" || fail "服务重启失败"
fi
ok "安装完成（升级前: ${REMOTE_VER_BEFORE:-未知}）"

# ── 阶段 4: 验证 ────────────────────────────────────────────────────────────
info "阶段 4/4: 健康验证（等待 8089 就绪，最多 90s）"
verify() {  # $1 = 远程验证命令前缀（直接 ssh 即可执行的 shell 片段）
  local i
  for i in $(seq 1 18); do
    sleep 5
    if ssh "$HOST" 'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8089/' 2>/dev/null | grep -q 200; then
      return 0
    fi
    printf '  … 等待服务就绪 (%ss)\n' $((i*5))
  done
  return 1
}

if verify; then
  ok "8089 首页 HTTP 200"
else
  fail "8089 未在 90s 内就绪，请查日志：journalctl ${TARGET:+} -u octop"
fi

if [[ "$TARGET" == "test" ]]; then
  REMOTE_VER_AFTER=$(ssh "$HOST" '/home/claw/.octop/venv/bin/octop --version 2>/dev/null')
  SVC_STATUS=$(ssh "$HOST" 'systemctl --user is-active octop')
else
  REMOTE_VER_AFTER=$(run_root "cat /root/.octop/venv/lib/python3.*/site-packages/octop-*.dist-info/METADATA 2>/dev/null | grep '^Version:' | head -1 | cut -d\  -f2")
  SVC_STATUS=$(run_root "systemctl is-active octop")
fi

ok "服务状态: $SVC_STATUS"
ok "升级后版本: ${REMOTE_VER_AFTER:-未知}"
ssh "$HOST" "rm -f $REMOTE_TMP" 2>/dev/null || true

info "部署完成 🎉  ${REMOTE_VER_BEFORE:-?} → ${REMOTE_VER_AFTER:-?}  ($HOST:8089)"
