#!/usr/bin/env bash
# H1 nightly launcher (Track H; CC-AGENTS-DESIGN §4).
#
# Runs the deterministic QA runner, then the sandboxed CC triage agent, with
# the operational guarantees the design promises and the runner itself cannot
# provide: a hard timeout, a minimal environment for the agent, an owner-only
# persisted transcript, and a clean, logged no-op when the environment is down
# (the DB proxy needs ADC, which expires — a 3am cron must degrade to a
# visible skip, not a crash loop).
#
# Cron line (NOT installed by default — the crontab records "stop all live
# crons", 2026-07-08, so enabling a nightly spend is David's call):
#
#   17 3 * * * /home/david/code/x1/dev/x1-advisor/qa/nightly.sh
#
# Cost when it runs: roughly $3/night (full tier + judge, OpenAI company key).
# Exit code is the runner's verdict (0 clean / 1 regression / 2 not-comparable
# / 3 stale); 75 means the preflight skipped the night.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORTS="$REPO/.qa-artifacts/reports"
TODAY="$(date +%F)"
LOG="$REPORTS/${TODAY}_launcher.log"
TRANSCRIPT="$REPORTS/${TODAY}_triage_transcript.jsonl"
# the 2026-07-31 full+judge run took ~90 minutes wall clock; 2h is headroom,
# not slack — a runner that hangs past it is dead, not slow
RUNNER_TIMEOUT="${ADVISOR_NIGHTLY_TIMEOUT:-7200}"   # the whole night's jobs
TRIAGE_TIMEOUT="${ADVISOR_TRIAGE_TIMEOUT:-900}"

mkdir -p "$REPORTS"
chmod 700 "$REPO/.qa-artifacts" "$REPORTS"
touch "$LOG" && chmod 600 "$LOG"
exec >>"$LOG" 2>&1
echo "== nightly launcher $(date -Is) =="

cd "$REPO"

# --- preflight: is the environment even up? -------------------------------
# A dead proxy or expired ADC is an environment problem, not a QA result.
# Skip loudly: the log and the marker say why there is no report today.
if ! timeout 45 uv run python -c \
    "from x1_advisor.db import connect
with connect() as c: c.execute('SELECT 1').fetchone()" ; then
    echo "SKIPPED: database unreachable (proxy down or ADC expired) — no QA ran"
    printf '{"date":"%s","skipped":"db-unreachable"}\n' "$TODAY" \
        > "$REPORTS/${TODAY}_nightly_skipped.json"
    chmod 600 "$REPORTS/${TODAY}_nightly_skipped.json"
    exit 75   # EX_TEMPFAIL: distinct from every runner verdict
fi

# --- the deterministic half ------------------------------------------------
set +e
timeout "$RUNNER_TIMEOUT" uv run python -m experiments.nightly --full --judge
NIGHTLY_EXIT=$?
set -e
echo "runner exit: $NIGHTLY_EXIT"

# --- the triage half -------------------------------------------------------
# The agent reads artifacts and writes prose; its sandbox (triage-settings)
# denies interpreters, credentials and git writes. `env -i` goes further: the
# secrets are not merely unreadable, they are absent from the process.
touch "$TRANSCRIPT" && chmod 600 "$TRANSCRIPT"
set +e
timeout "$TRIAGE_TIMEOUT" env -i \
    HOME="$HOME" PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
    USER="$USER" TERM=dumb \
    CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude-max}" \
    claude -p --settings "$REPO/qa/triage-settings.json" \
        --append-system-prompt "$(cat "$REPO/qa/triage-prompt.md")" \
        --output-format stream-json --verbose --max-turns 40 \
        "Triage last night's QA run (today is ${TODAY})." \
    >>"$TRANSCRIPT"
TRIAGE_EXIT=$?
set -e
echo "triage exit: $TRIAGE_EXIT (transcript: $TRANSCRIPT)"

# the night's verdict is the RUNNER's — triage is commentary on it, and a
# triage failure must not mask a clean run or upgrade a dirty one
exit "$NIGHTLY_EXIT"
