#!/bin/zsh
# 网络抖动期间：交付对象被标坏时，先测 App 令牌签发和 GitHub 都通，再 resume；连续稳定 20 分钟或满 --max 分钟就退出。
# 用法：autoresume.sh [交付对象，默认 workshop] [最长分钟，默认 60]
# 缺的机制：车间对令牌签发、GitHub 的瞬时失败自己重试，不把整个交付对象标坏。
set -u
target=${1:-workshop}; max=${2:-60}
repo=${0:A:h:h:h}
cd "$repo"
end=$((SECONDS + max * 60)); ok=0
while (( SECONDS < end )); do
  broken=$(.venv/bin/python -c "
import os
from pathlib import Path
from workshop.core.log import Log
s = Path(os.environ.get('WORKSHOP_STATE_DIR', '~/.local/state/workshop')).expanduser()
print((Log(s / 'workshop.db').get('controls', '$target') or {}).get('broken'))")
  if [[ $broken == True ]]; then
    ok=0
    if env -i HOME=$HOME PATH=/usr/bin:/bin .venv/bin/python -m workshop.github_token \
         --config ~/workspace/runtime/tools/agent_github_identity/config.json --app lambdatheory-agent \
         --key-service lambdatheory-agent --key-account private-key --repo kun-g/workshop >/dev/null 2>&1 \
       && curl -s -o /dev/null --max-time 10 -w '%{http_code}' https://api.github.com/rate_limit | grep -q 200; then
      echo "$(date +%T) 标坏 → 签发和 GitHub 都通，resume：$(uv run workshop resume "$target" --select none --seen none 2>&1 | tail -1)"
    else
      echo "$(date +%T) 标坏，网络还不通"
    fi
  else
    ok=$((ok + 1)); echo "$(date +%T) 正常（$ok）"
    (( ok >= 10 )) && { echo "稳定 20 分钟，退出"; exit 0; }
  fi
  sleep 120
done
echo "满 $max 分钟，退出"
