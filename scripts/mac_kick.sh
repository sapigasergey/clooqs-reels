#!/bin/bash
# Пинок с мака: запускает задачи GitHub вручную (расписание GitHub ненадёжно — 29.09 не сработало ни разу).
# Вызывается launchd (~/Library/LaunchAgents/com.clooqs.reels-kick.*.plist). Аргумент: telegram | check
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
REPO=sapigasergey/clooqs-reels
case "$1" in
  telegram) WF=telegram.yml; ARGS=() ;;
  check)    WF=check.yml;    ARGS=(-f force=false) ;;
  *) echo "usage: $0 telegram|check"; exit 2 ;;
esac
for i in 1 2 3 4 5; do  # сеть после сна поднимается не сразу
  if gh workflow run "$WF" -R "$REPO" "${ARGS[@]}"; then
    echo "$(date '+%F %T') $1: запущено"; exit 0
  fi
  sleep 30
done
echo "$(date '+%F %T') $1: НЕ УДАЛОСЬ запустить"; exit 1
