#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

tmp_probe_ok="$(mktemp)"
tmp_probe_skip="$(mktemp)"
trap 'rm -f "$tmp_probe_ok" "$tmp_probe_skip"' EXIT

cat > "$tmp_probe_ok" <<'EOF'
session_name=oc-codex-openclaw-control-ui-fulcrum
session_exists=1
session_status=dead
session_state_note=stale_running_missing_session_pid
git_dirty=0
service_listening=1
service_healthz=1
recommended_state=idle
recovery_guard_ok=1
recovery_action=recreate_session
recovery_reason=stale_running_missing_session_pid
recovery_prompt_file=docs/cron-job-message.txt
EOF

ok_output="$(MBA_CODEX_DRY_RUN=1 bash scripts/mba_recover_codex_session.sh --probe-file "$tmp_probe_ok")"
printf '%s\n' "$ok_output" | grep -F "recovery_executed=0" >/dev/null
printf '%s\n' "$ok_output" | grep -F "recovery_skip_reason=" >/dev/null
printf '%s\n' "$ok_output" | grep -F "recovery_cancel_cmd=" >/dev/null
printf '%s\n' "$ok_output" | grep -F "recovery_recreate_cmd=" >/dev/null
printf '%s\n' "$ok_output" | grep -F "recovery_dispatch_cmd=scripts/mba_codex_dispatch.sh docs/cron-job-message.txt" >/dev/null

cat > "$tmp_probe_skip" <<'EOF'
session_name=oc-codex-openclaw-control-ui-fulcrum
session_exists=1
session_status=dead
session_state_note=stale_running_missing_session_pid
git_dirty=1
service_listening=1
service_healthz=1
recommended_state=dirty
recovery_guard_ok=0
recovery_action=none
recovery_reason=
recovery_prompt_file=docs/cron-job-message.txt
EOF

skip_output="$(MBA_CODEX_DRY_RUN=1 bash scripts/mba_recover_codex_session.sh --probe-file "$tmp_probe_skip")"
printf '%s\n' "$skip_output" | grep -F "recovery_executed=0" >/dev/null
printf '%s\n' "$skip_output" | grep -F "recovery_skip_reason=recovery_action_not_recreate_session" >/dev/null
