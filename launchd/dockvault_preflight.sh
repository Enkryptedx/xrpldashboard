#!/usr/bin/env bash
#
# DockVault preflight — sourced by dockvault_* wrappers.
#
# Contract: dockvault_preflight() returns 0 iff DockVault is mounted AND
# writeable from THIS process's TCC context (i.e. the calling process
# and its subprocesses have Full Disk Access or Removable Volumes grant).
# On failure, logs a LOUD SKIP and returns 1 so the caller can exit 0
# — never a silent hang, per 2026-08-23 revival doctrine.
#
# Depends on caller having defined:
#   log()   — writes timestamped line to $LOG_FILE + stdout
#
# Exports:
#   DOCKVAULT_ROOT — mount point path
#
# The write-test uses Perl's alarm() for the timeout because macOS has
# no default `timeout` binary; Perl is guaranteed present. Alarm is
# inherited by the exec'd child, which dies on SIGALRM (POSIX default
# action for signal 14 is Term).

DOCKVAULT_ROOT="/Volumes/DockVault"
export DOCKVAULT_ROOT

_dv_timeout() {
  # _dv_timeout SECS COMMAND [ARGS...]
  # Exit code: real command's rc if under timeout, 142 (128+SIGALRM) if killed.
  local secs="$1"; shift
  perl -e 'alarm(shift @ARGV); exec @ARGV or exit 127;' "$secs" "$@"
}

dockvault_preflight() {
  # Cheap mount check first — doesn't touch filesystem, doesn't need FDA.
  if ! mount | grep -q " ${DOCKVAULT_ROOT} "; then
    log "SKIP: DockVault not mounted at ${DOCKVAULT_ROOT}"
    log "  Fix: unlock volume (auto-unlock via keychain, or dialog on plug)"
    return 1
  fi

  # Active write-test — proves this launchd context has FDA / Removable
  # Volumes grant, AND proves Spotlight isn't holding an exclusive lock.
  # 10s timeout catches the exact silent-hang class from the 2026-08-23
  # dock revival (Terminal without FDA hung indefinitely instead of erroring).
  local test_dir="${DOCKVAULT_ROOT}/.heartbeat"
  local test_file="${test_dir}/preflight_$$_$(date +%s).txt"

  if ! _dv_timeout 10 bash -c "mkdir -p '$test_dir' && echo preflight > '$test_file' && cat '$test_file' > /dev/null && rm '$test_file'"; then
    local rc=$?
    log "SKIP: DockVault write-test failed/timed out (rc=${rc})"
    log "  Suspect: launchd context lacks Full Disk Access grant, OR Spotlight lock held, OR volume half-mounted"
    log "  Fix: System Settings -> Privacy & Security -> Full Disk Access -> add executable from plist ProgramArguments"
    log "  Test path attempted: ${test_file}"
    return 1
  fi

  return 0
}
