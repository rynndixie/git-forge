#!/bin/bash
set -u
# Clean up the stray env server from the earlier --help probe (pid 8934, port 8080)
kill 8934 2>/dev/null; sleep 2
kill -9 8934 2>/dev/null; sleep 1
rm -rf /tmp/gf-trial
mkdir -p /tmp/gf-trial/state /tmp/gf-trial/data /tmp/gf-trial/scores
cd /tmp/gf-trial
export MAKEFUN_STATE_DIR=/tmp/gf-trial/state
export MAKEFUN_ENV_HOST=127.0.0.1
export MAKEFUN_ENV_PORT=18080
export MAKEFUN_SHOP_TOKEN="${MAKEFUN_SHOP_TOKEN:-shpat_local_grade_001}"
setsid node /tmp/github-core-clone-01-kit/program/env/server.mjs > /tmp/gf-trial/env.log 2>&1 < /dev/null &
ENV_PID=$!
echo "env pid=$ENV_PID"
for i in $(seq 1 30); do [ -f /tmp/gf-trial/state/ready ] && break; sleep 1; done
ls -la /tmp/gf-trial/state/; cat /tmp/gf-trial/state/ready 2>/dev/null; echo
setsid env MAKEFUN_SHOP_TOKEN="$MAKEFUN_SHOP_TOKEN" python3 /home/hatch/workspace/make-fun/submit/git-forge/main.py --data-dir /tmp/gf-trial/data > /tmp/gf-trial/bot.log 2>&1 < /dev/null &
BOT_PID=$!
echo "bot pid=$BOT_PID"
PORT=""
for i in $(seq 1 20); do
  if [ -f /tmp/gf-trial/data/port.txt ]; then PORT=$(cat /tmp/gf-trial/data/port.txt); break; fi
  sleep 1
done
echo "bot port=$PORT"
head -5 /tmp/gf-trial/bot.log
if [ -z "$PORT" ]; then echo "BOT FAILED TO BIND"; exit 1; fi
curl -s -X POST http://127.0.0.1:18080/register -H 'Content-Type: application/json' -d "{\"port\": $PORT}"; echo
echo "registered; polling /done ..."
for i in $(seq 1 56); do
  RESP=$(curl -s --max-time 15 http://127.0.0.1:18080/done || echo '{"done":false}')
  echo "poll $i: $RESP"
  echo "$RESP" | grep -q '"done":true' && break
  sleep 2
done
echo "--- env log tail ---"; tail -20 /tmp/gf-trial/env.log
echo "--- export dir ---"; ls -la /tmp/gf-trial/state/export/ 2>/dev/null
kill $ENV_PID 2>/dev/null; sleep 2; kill -9 $ENV_PID 2>/dev/null
kill $BOT_PID 2>/dev/null; sleep 1; kill -9 $BOT_PID 2>/dev/null
echo "--- scorer ---"
MAKEFUN_EXPORT_DIR=/tmp/gf-trial/state/export MAKEFUN_SCORES_DIR=/tmp/gf-trial/scores node /tmp/github-core-clone-01-kit/program/scorer/score.mjs; echo "scorer exit=$?"
cat /tmp/gf-trial/scores/scores.json 2>/dev/null
echo "--- state summary ---"
python3 -c "
import json
s=json.load(open('/tmp/gf-trial/state/export/state.json'))
print('keys:',list(s.keys()))
print('counts:',{k:v for k,v in s.items() if not isinstance(v,(dict,list))})
print('groups:',s.get('counts'))
p=json.load(open('/tmp/gf-trial/state/export/perf.json'))
print('perf:',{k:v for k,v in p.items() if k!='workloads'})
print('workloads:',p.get('workloads'))
" 2>&1 | head -30
