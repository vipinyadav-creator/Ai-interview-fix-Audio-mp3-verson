#!/usr/bin/env bash
# Ek hi GitHub job ke andar converter ko baar-baar chalata hai,
# taaki GitHub cron ke 15-60 min delay ka wait na karna pade.
set -u

LOOP_MINUTES="${LOOP_MINUTES:-330}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-60}"
END_TIME=$(( $(date +%s) + LOOP_MINUTES * 60 ))

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

save_state() {
  git add processed.json mp3_links.json synced.json 2>/dev/null
  if git diff --cached --quiet; then
    return 0
  fi
  git commit -q -m "chore: update processed audio state [skip ci]"
  for attempt in 1 2 3; do
    git fetch -q origin main
    if git rebase -q origin/main && git push -q origin HEAD:main; then
      echo "State saved."
      return 0
    fi
    git rebase --abort 2>/dev/null || true
    sleep 2
  done
  echo "WARNING: state push nahi hua, agle round me dobara try hoga."
}

round=0
while [ "$(date +%s)" -lt "$END_TIME" ]; do
  round=$((round + 1))
  echo "===== Round $round | $(date '+%H:%M:%S') ====="
  python fixedaudio.py || echo "Converter error (agle round me retry)"
  save_state
  sleep "$INTERVAL_SECONDS"
done

echo "Loop time khatam. Agla scheduled run loop ko continue karega."
