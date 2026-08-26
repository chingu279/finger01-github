#!/bin/sh
# 아침 체크인 알람 설치 (macOS)
#
#   sh scripts/morning-alarm.sh            07:30 에 설치
#   sh scripts/morning-alarm.sh 08:00      시각 지정
#   sh scripts/morning-alarm.sh --uninstall
#
# LaunchAgent 두 개를 만든다:
#   1) 서버  — 로그인 시 자동 실행, 죽으면 다시 뜬다 (127.0.0.1 전용)
#   2) 알람  — 지정한 시각에 알림을 띄우고 체크인 화면을 연다
#
# 왜 두 개인가: 알람이 울렸을 때 서버가 떠 있어야 화면이 열린다.
# 서버를 알람 시각에만 띄우면 낮에 추세를 보려 할 때 꺼져 있다.
set -e

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
AGENTS="$HOME/Library/LaunchAgents"
SERVER_ID="com.health.server"
ALARM_ID="com.health.morning"
PORT=8765
URL="http://127.0.0.1:$PORT/"

if [ "$(uname)" != "Darwin" ]; then
    echo "이 스크립트는 macOS 전용입니다." >&2
    echo "리눅스라면 systemd user timer 나 cron 을 쓰세요:" >&2
    echo "  30 7 * * *  cd $REPO && ./health serve --no-open" >&2
    exit 1
fi

unload() {
    for id in "$SERVER_ID" "$ALARM_ID"; do
        launchctl bootout "gui/$(id -u)/$id" 2>/dev/null || \
        launchctl unload "$AGENTS/$id.plist" 2>/dev/null || true
    done
}

if [ "$1" = "--uninstall" ]; then
    unload
    rm -f "$AGENTS/$SERVER_ID.plist" "$AGENTS/$ALARM_ID.plist"
    echo "알람과 서버를 제거했습니다."
    exit 0
fi

TIME=${1:-07:30}
HOUR=${TIME%%:*}
MIN=${TIME##*:}
case "$HOUR$MIN" in
    *[!0-9]*|"") echo "시각 형식이 잘못됐습니다: $TIME (예: 07:30)" >&2; exit 2 ;;
esac
# 앞의 0 을 직접 떼어낸다. printf '%d' 08 은 8진수로 해석돼 실패하고,
# set -e 와 만나면 스크립트가 아무 말 없이 죽는다 — 8시·9시 알람이
# 조용히 설치되지 않는 버그였다.
HOUR=${HOUR#0}; HOUR=${HOUR:-0}
MIN=${MIN#0};   MIN=${MIN:-0}
if [ "$HOUR" -gt 23 ] || [ "$MIN" -gt 59 ]; then
    echo "시각 범위를 벗어났습니다: $TIME" >&2; exit 2
fi

mkdir -p "$AGENTS" "$REPO/data"
unload

cat > "$AGENTS/$SERVER_ID.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$SERVER_ID</string>
  <key>ProgramArguments</key>
  <array>
    <string>$REPO/health</string>
    <string>serve</string>
    <string>--port</string><string>$PORT</string>
    <string>--no-open</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$REPO/data/server.log</string>
  <key>StandardErrorPath</key><string>$REPO/data/server.log</string>
</dict></plist>
PLIST

cat > "$AGENTS/$ALARM_ID.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$ALARM_ID</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>/usr/bin/osascript -e 'display notification "오늘 몸 상태를 기록할 시간입니다 (90초)" with title "건강 체크인" sound name "Glass"'; /usr/bin/open "$URL"</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MIN</integer></dict>
  <key>RunAtLoad</key><false/>
</dict></plist>
PLIST

for id in "$SERVER_ID" "$ALARM_ID"; do
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/$id.plist" 2>/dev/null || \
    launchctl load -w "$AGENTS/$id.plist"
done

printf '설치 완료.\n'
printf '  체크인 화면  %s\n' "$URL"
printf '  알람         매일 %02d:%02d — 알림 + 브라우저 자동 열기\n' "$HOUR" "$MIN"
printf '\n'
printf '지금 열어보기:  open %s\n' "$URL"
printf '알람 시각 변경:  sh scripts/morning-alarm.sh 08:00\n'
printf '제거:           sh scripts/morning-alarm.sh --uninstall\n'
printf '\n'
printf '첫 알림이 안 뜨면 시스템 설정 → 알림 → 스크립트 편집기를 허용해 주세요.\n'
