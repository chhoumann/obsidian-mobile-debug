#!/usr/bin/env bash
# Stop OMD's isolated emulator or ReDroid container. REMOVE_SDK=1 also removes
# OMD-owned cached Android state, but never a shared SDK or ~/.android.
set -euo pipefail

AVD="${AVD:-obsidian-debug}"
BACKEND="${BACKEND:-emulator}"
CONSOLE_PORT="${CONSOLE_PORT:-5554}"
ADB_PORT="${ADB_PORT:-5555}"
STATE_DIR="${OMD_ANDROID_STATE_DIR:-${XDG_CACHE_HOME:-${HOME}/.cache}/obsidian-mobile-debug}"
SDK_ROOT="${ANDROID_SDK_ROOT:-${STATE_DIR}/android-sdk}"
ANDROID_USER_HOME="${ANDROID_USER_HOME:-${STATE_DIR}/android-user}"
export ANDROID_SDK_ROOT="$SDK_ROOT" ANDROID_HOME="$SDK_ROOT" ANDROID_USER_HOME
export ANDROID_AVD_HOME="${ANDROID_USER_HOME}/avd"

ADB="${SDK_ROOT}/platform-tools/adb"
AVDMANAGER="${SDK_ROOT}/cmdline-tools/latest/bin/avdmanager"

case "$BACKEND" in
  container)
    if [[ ! "$AVD" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,42}$ ]]; then
      printf 'Refusing unsafe container/AVD name: %s\n' "$AVD" >&2
      exit 2
    fi
    CONTAINER="omd-${AVD}-redroid"
    IMAGE="redroid/redroid@sha256:0a611199ba2e0b5d60af39b3327a517f6407231f4352114ed3bd3cbfe2be69aa"
    DOCKER=()
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
      DOCKER=(docker)
    elif command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
      DOCKER=(sudo -n docker)
    else
      printf 'Docker is unavailable or inaccessible without prompting.\n' >&2
      exit 2
    fi
    EXISTING="$("${DOCKER[@]}" inspect "$CONTAINER" --format '{{.Config.Image}}' 2>/dev/null || true)"
    if [ -n "$EXISTING" ] && [ "$EXISTING" != "$IMAGE" ]; then
      printf 'Refusing to remove %s with unexpected image: %s\n' "$CONTAINER" "$EXISTING" >&2
      exit 2
    fi
    if [ -n "$EXISTING" ]; then
      "${DOCKER[@]}" rm -f "$CONTAINER" >/dev/null
    fi
    if [ -x "$ADB" ]; then
      "$ADB" disconnect "127.0.0.1:${ADB_PORT}" >/dev/null 2>&1 || true
    fi
    ;;
  emulator)
    if [ -x "$ADB" ]; then
      "$ADB" -s "emulator-${CONSOLE_PORT}" emu kill >/dev/null 2>&1 || true
    fi

    if [ -x "$AVDMANAGER" ]; then
      "$AVDMANAGER" delete avd --name "$AVD" >/dev/null 2>&1 || true
    fi

    rm -f -- "${STATE_DIR}/${AVD}.pid"
    ;;
  *)
    printf 'BACKEND must be emulator or container, got: %s\n' "$BACKEND" >&2
    exit 2
    ;;
esac

if [ "${REMOVE_SDK:-0}" = "1" ]; then
  case "$(basename -- "$STATE_DIR")" in
    obsidian-mobile-debug) ;;
    *)
      printf 'Refusing to recursively remove nonstandard state dir: %s\n' "$STATE_DIR" >&2
      exit 2
      ;;
  esac
  case "${SDK_ROOT}/" in
    "${STATE_DIR}/"*) ;;
    *)
      printf 'Refusing to remove shared SDK outside OMD state: %s\n' "$SDK_ROOT" >&2
      exit 2
      ;;
  esac
  rm -rf -- "$STATE_DIR"
  printf 'Removed OMD-owned Android state: %s\n' "$STATE_DIR"
else
  if [ "$BACKEND" = "container" ]; then
    printf 'Stopped ReDroid container %s; SDK/cache and container data retained.\n' "$CONTAINER"
  else
    printf 'Stopped emulator and deleted AVD %s; SDK/cache retained.\n' "$AVD"
  fi
fi
