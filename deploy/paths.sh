# Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# shellcheck shell=bash
#
# Path checks shared by install.sh and add-instance.sh. Sourced, not run —
# hence the directive above, which tells ShellCheck what dialect to assume
# for a file with no shebang.
#
# Both scripts run as root, and both turn a path — from the command line, or
# from an env file somebody edited months ago — into the target of mkdir,
# chown, a recursive chmod, and in one case rsync --delete. A wrong path is
# therefore not a misconfiguration to be corrected on the next run; it is a
# directory that no longer contains what it did. Everything here is a check
# to run before the first write.

# The canonical form of a path: trailing slashes gone, "." and ".." resolved
# as far as the path exists, symlinks followed. Comparing the strings alone
# let "/opt/footnote/" through a check that "/opt/footnote" would have
# failed, and a symlink through both.
canon_path() {
  local path="$1" parent base
  while [ "$path" != "/" ] && [ "${path%/}" != "$path" ]; do
    path="${path%/}"
  done
  if [ -d "$path" ]; then
    (cd -P -- "$path" 2>/dev/null && pwd -P) || printf '%s' "$path"
    return 0
  fi
  # Not there yet — an output directory usually is not. Resolve the parent,
  # which is what decides where the thing will actually be created.
  base="$(basename -- "$path")"
  parent="$(dirname -- "$path")"
  if [ -d "$parent" ]; then
    parent="$(cd -P -- "$parent" 2>/dev/null && pwd -P)" || parent="$(dirname -- "$path")"
    case "$parent" in
      /) printf '/%s' "$base" ;;
      *) printf '%s/%s' "$parent" "$base" ;;
    esac
    return 0
  fi
  printf '%s' "$path"
}

# Directories whose contents belong to something else. None of them may be a
# destination: what these scripts do to a destination is create it, take
# ownership of it, set it to mode 700, and — for the application directory —
# empty it first.
is_broad_dir() {
  case "$1" in
    "" | / | /bin | /boot | /dev | /etc | /home | /lib | /lib32 | /lib64 \
      | /media | /mnt | /opt | /proc | /root | /run | /sbin | /srv | /sys \
      | /tmp | /usr | /var)
      return 0 ;;
  esac
  return 1
}

# A value that survives a round trip through the env file. systemd's
# EnvironmentFile treats $ and backslashes as syntax, and the parser that
# reads the file back on the next run refuses both rather than guess. A path
# that goes in and cannot come out again works on the first run and breaks
# every repair after it — the run that was supposed to be idempotent.
check_storable() {
  local label="$1" value="$2"
  case "$value" in
    *'$'*|*\\*|*'"'*|*"'"*)
      echo "$label must not contain \$ \\ \" or ': $value" >&2
      echo "systemd would read it differently from the way it is written," >&2
      echo "and the next run could not read it back at all." >&2
      return 1 ;;
  esac
  # $'…' rather than $(printf …): command substitution strips the trailing
  # newline, so the pattern became "**" and refused every path there is.
  case "$value" in
    *$'\n'*|*$'\t'*|*$'\r'*)
      echo "$label must not contain a newline or a tab: $value" >&2
      return 1 ;;
  esac
  return 0
}

# An absolute path, free of . and .., that is not one of the above. `label`
# names the setting so the message says which one is wrong.
check_target_dir() {
  local label="$1" dir="$2" bare resolved
  case "$dir" in
    /*) ;;
    *) echo "$label must be an absolute path, not: $dir" >&2; return 1 ;;
  esac
  case "$dir" in
    */..|*/../*|*/.|*/./*|..|.)
      echo "$label must not contain . or ..: $dir" >&2; return 1 ;;
  esac
  bare="$dir"
  while [ "$bare" != "/" ] && [ "${bare%/}" != "$bare" ]; do
    bare="${bare%/}"
  done
  resolved="$(canon_path "$dir")"
  # Both forms: the name as given, so "/var" is refused on a system where it
  # is a symlink to somewhere else, and the resolved one, so a link that
  # lands on a system directory is refused under whatever name it was given.
  if is_broad_dir "$bare" || is_broad_dir "$resolved"; then
    echo "$label=$dir is a system directory (it resolves to $resolved)." >&2
    echo "This script would create it, give it away and set it to mode 700." >&2
    echo "Point it at a directory of its own instead." >&2
    return 1
  fi
  return 0
}
