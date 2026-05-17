#!/usr/bin/env bash
# Pre-commit guard: reject any staged file that contains a value from .env.
#
# Re-reads .env on every invocation, so rotating a secret in .env
# automatically updates the denylist. Skips .env files themselves, since
# .env is .gitignore'd and should never be staged anyway.

set -uo pipefail

DOTENV_PATH="${DOTENV_PATH:-.env}"
MIN_LEN="${MIN_LEN:-8}"

# Nothing to enforce if .env is absent (e.g., a CI runner without secrets).
[ -f "$DOTENV_PATH" ] || exit 0

VALUES=()
while IFS= read -r line || [ -n "$line" ]; do
  # Skip blank lines and comments.
  [[ -z "${line// }" || "$line" =~ ^[[:space:]]*# ]] && continue
  # Parse KEY=value.
  if [[ "$line" =~ ^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=(.*)$ ]]; then
    val="${BASH_REMATCH[1]}"
    # Trim surrounding whitespace.
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    # Strip surrounding double or single quotes.
    if [[ "$val" =~ ^\"(.*)\"$ ]] || [[ "$val" =~ ^\'(.*)\'$ ]]; then
      val="${BASH_REMATCH[1]}"
    fi
    # Ignore short values to avoid false positives on trivial strings.
    if [ ${#val} -ge "$MIN_LEN" ]; then
      VALUES+=("$val")
    fi
  fi
done < "$DOTENV_PATH"

[ ${#VALUES[@]} -gt 0 ] || exit 0

mapfile -t VALUES < <(printf '%s\n' "${VALUES[@]}" | sort -u)

failed=0
for f in "$@"; do
  case "$f" in
    .env|.env.*) continue ;;
  esac
  [ -f "$f" ] || continue
  for v in "${VALUES[@]}"; do
    if grep -F -q -- "$v" "$f"; then
      printf 'error: %s contains a value from %s (length=%d)\n' "$f" "$DOTENV_PATH" "${#v}" >&2
      failed=1
    fi
  done
done

exit "$failed"
