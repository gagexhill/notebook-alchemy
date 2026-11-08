#!/bin/sh
# shellcheck disable=SC2016 # backticks in messages and ${SHELL} in the launcher are meant literally
# Installs Notebook Alchemy on macOS or Linux in one step:
#
#   curl -LsSf https://raw.githubusercontent.com/gagexhill/notebook-alchemy/main/install/install.sh | sh
#
# It installs uv (Astral's Python tool installer) if it is missing, installs notebook-alchemy as a uv
# tool, installs the Chromium browser used for Google sign-in, adds a "Notebook Alchemy" shortcut
# (a .command file on the macOS desktop; a .desktop launcher in the Linux applications menu and on
# the desktop), and runs `notebook-alchemy test`. Running it again updates the install in place.
#
# It never asks for or stores a password, key or account, and writes only inside your home folder:
# uv's own folders, the Playwright browser folder, the shortcut and the work folder named below.
#
# Optional settings, read from the environment:
#   ALCHEMY_INSTALL_SOURCE        what `uv tool install` installs (default: the source below)
#   ALCHEMY_INSTALL_WORK_DIR      the folder the shortcut opens in, where settings (.env) and
#                                 output are kept (default: "Notebook Alchemy" in your home folder)
#   ALCHEMY_INSTALL_SHORTCUT_DIR  where the desktop shortcut goes (default: your desktop)
# uv's own variables (UV_TOOL_DIR, UV_TOOL_BIN_DIR, UV_INSTALL_DIR) and PLAYWRIGHT_BROWSERS_PATH are
# honoured as their tools document. UV_NO_MODIFY_PATH=1 also stops this installer changing your PATH.

# The source archive of the main branch on GitHub. An archive, not git+https, so git is not needed.
# Once the package is on PyPI (issue #33) this becomes 'notebook-alchemy'.
NOTEBOOK_ALCHEMY_SOURCE='https://github.com/gagexhill/notebook-alchemy/archive/refs/heads/main.tar.gz'

# Everything is in functions and main runs on the last line, so a download cut short runs nothing.
set -eu

TOTAL=5

say() {
  printf '%s\n' "$*"
}

step() {
  printf '\nStep %s of %s: %s\n' "$1" "$TOTAL" "$2"
}

# fail WHAT NEXT: what failed, then the next step, then stop.
fail() {
  printf 'Error: %s\n%s\n' "$1" "$2" >&2
  exit 1
}

warn() {
  printf 'Warning: %s\n' "$*" >&2
}

# Quote a value for a POSIX shell script: wrap in single quotes, with each ' written as '\''.
shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# Quote a value as one argument of a .desktop Exec line: in double quotes, with \ " ` $ escaped,
# then every \ doubled because the whole value is also a .desktop string, and % written as %%.
desktop_quote() {
  printf '"%s"' "$(printf '%s' "$1" | sed -e 's/[\\"`$]/\\&/g' -e 's/\\/\\\\/g' -e 's/%/%%/g')"
}

find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  # Where Astral's installer puts uv when this shell's PATH does not list it yet.
  for candidate in "${UV_INSTALL_DIR:-}/uv" "${XDG_BIN_HOME:-}/uv" "${XDG_DATA_HOME:+$XDG_DATA_HOME/../bin}/uv" \
    "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    case "$candidate" in /uv) continue ;; esac
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

on_path() {
  case ":$PATH:" in
    *":$1:"* | *":$1/:"*) return 0 ;;
    *) return 1 ;;
  esac
}

# The launcher both shortcuts run: open the work folder, start notebook-alchemy, then leave a shell
# open in that window so its output can be read and more commands typed.
write_launcher() {
  target=$1
  {
    printf '#!/bin/sh\n'
    printf '# Opens Notebook Alchemy. Written by install/install.sh; running the installer again replaces it.\n'
    printf 'cd %s || exit 1\n' "$(shell_quote "$WORK_DIR")"
    printf '%s\n' "$(shell_quote "$EXE")"
    printf 'exec "${SHELL:-/bin/sh}" -l\n'
  } >"$target" && chmod 755 "$target"
}

linux_desktop_dir() {
  if [ -n "${ALCHEMY_INSTALL_SHORTCUT_DIR:-}" ]; then
    printf '%s\n' "$ALCHEMY_INSTALL_SHORTCUT_DIR"
  elif command -v xdg-user-dir >/dev/null 2>&1 && dir=$(xdg-user-dir DESKTOP) && [ -n "$dir" ] && [ "$dir" != "$HOME" ]; then
    printf '%s\n' "$dir"
  else
    printf '%s\n' "$HOME/Desktop"
  fi
}

write_desktop_entry() {
  target=$1
  launcher=$2
  {
    printf '[Desktop Entry]\n'
    printf 'Type=Application\n'
    printf 'Name=Notebook Alchemy\n'
    printf 'Comment=Turn a YouTube channel into NotebookLM sources\n'
    printf 'Exec=%s\n' "$(desktop_quote "$launcher")"
    printf 'Terminal=true\n'
    printf 'Categories=Utility;\n'
  } >"$target" && chmod 755 "$target"
}

main() {
  source=${ALCHEMY_INSTALL_SOURCE:-$NOTEBOOK_ALCHEMY_SOURCE}

  case "$(uname -s)" in
    Darwin) os=macos ;;
    Linux) os=linux ;;
    *) fail "This installer is for macOS and Linux, not $(uname -s)." \
      "On Windows, run in PowerShell: irm https://raw.githubusercontent.com/gagexhill/notebook-alchemy/main/install/install.ps1 | iex" ;;
  esac
  [ -n "${HOME:-}" ] && [ -d "$HOME" ] || fail "Your home folder (HOME) is not set or does not exist." \
    "Run this installer as your own user, not as root through sudo."
  # As root it would install into root's home, or leave files the user cannot change in theirs.
  [ "$(id -u)" -ne 0 ] || fail 'This installer must not run as root or with sudo.' \
    'Run it again as your own user, without sudo.'
  WORK_DIR=${ALCHEMY_INSTALL_WORK_DIR:-$HOME/Notebook Alchemy}

  say 'Installing Notebook Alchemy.'

  # 1. uv
  step 1 'Finding uv, the Python tool installer'
  if uv=$(find_uv); then
    say "Found uv at [$uv]."
  else
    say "uv is not installed. Installing it with Astral's installer from https://astral.sh/uv ..."
    # If the download fails, sh receives nothing and exits 0; the check below catches that.
    if command -v curl >/dev/null 2>&1; then
      curl -LsSf https://astral.sh/uv/install.sh | sh || fail 'Installing uv failed.' \
        'Check your internet connection, or install uv yourself (https://docs.astral.sh/uv/getting-started/installation/), then run this installer again.'
    elif command -v wget >/dev/null 2>&1; then
      wget -qO- https://astral.sh/uv/install.sh | sh || fail 'Installing uv failed.' \
        'Check your internet connection, or install uv yourself (https://docs.astral.sh/uv/getting-started/installation/), then run this installer again.'
    else
      fail 'Neither curl nor wget is installed, so uv cannot be downloaded.' \
        'Install curl with your package manager (for example: sudo apt install curl), then run this installer again.'
    fi
    uv=$(find_uv) || fail 'uv could not be installed or cannot be found.' \
      'Check your internet connection and the messages above, then run this installer again.'
    say "Installed uv at [$uv]."
  fi

  # 2. notebook-alchemy
  step 2 'Installing notebook-alchemy'
  say "From [$source]. This can take a minute the first time."
  # --reinstall-package rebuilds from the source every time, so running this again picks up the
  # newest code even when the version number has not changed. --with-executables-from also puts
  # the `notebooklm` command (sign-in and uploads) on PATH; uv links only notebook-alchemy otherwise.
  # --python 3.12 is a tested version; uv downloads it if it is missing.
  "$uv" tool install --upgrade --reinstall-package notebook-alchemy --with-executables-from notebooklm-py \
    --python 3.12 "$source" || fail \
    'Installing notebook-alchemy failed.' \
    'Check your internet connection and run this installer again. The messages above say what uv could not do.'
  tool_dir=$("$uv" tool dir) && [ -n "$tool_dir" ] || fail "Could not find uv's tool folder." 'Run `uv tool dir` to see why.'
  bin_dir=$("$uv" tool dir --bin) && [ -n "$bin_dir" ] || fail "Could not find uv's command folder." 'Run `uv tool dir --bin` to see why.'
  EXE="$bin_dir/notebook-alchemy"
  python="$tool_dir/notebook-alchemy/bin/python"
  for path in "$EXE" "$bin_dir/notebooklm" "$python"; do
    [ -x "$path" ] || fail "uv reported success, but [$path] does not exist." \
      'Run `uv tool list` to see what is installed, then run this installer again.'
  done
  if on_path "$bin_dir"; then
    say "The notebook-alchemy command is in [$bin_dir], which is on your PATH."
  elif [ -n "${UV_NO_MODIFY_PATH:-}" ]; then
    # The shortcut and the steps below use the full path, so the install still works.
    warn "UV_NO_MODIFY_PATH is set, so [$bin_dir] was not added to your PATH. Add it yourself to run notebook-alchemy from any terminal."
  else
    say "Adding [$bin_dir] to your PATH so that new terminals find notebook-alchemy."
    "$uv" tool update-shell || warn "Could not add [$bin_dir] to your PATH. The shortcut still works; add the folder to your shell's startup file to run notebook-alchemy from any terminal."
  fi

  # 3. Chromium for sign-in
  step 3 'Installing the browser used for Google sign-in (Chromium, about 150 MB the first time)'
  "$python" -m playwright install chromium || fail 'Installing Chromium failed.' \
    'Check your internet connection and run this installer again. You can also sign in later with `notebook-alchemy login --browser-cookies chrome` instead.'
  if [ "$os" = linux ]; then
    say 'If the sign-in window does not open later, Chromium needs system libraries that only an'
    say 'administrator can add. To add them, run:'
    say "  \$ sudo $(shell_quote "$python") -m playwright install-deps chromium"
  fi

  # 4. Shortcut
  step 4 'Creating the shortcut'
  mkdir -p "$WORK_DIR" || fail "Could not create the folder [$WORK_DIR]." 'Check that you can write to it, then run this installer again.'
  # An absolute path, so the shortcut and the self-test use the same folder.
  WORK_DIR=$(cd "$WORK_DIR" && pwd) || fail "Could not open the folder [$WORK_DIR]." 'Check that you can read it, then run this installer again.'
  if [ "$os" = macos ]; then
    shortcut_dir=${ALCHEMY_INSTALL_SHORTCUT_DIR:-$HOME/Desktop}
    mkdir -p "$shortcut_dir" || fail "Could not create the folder [$shortcut_dir]." 'Check that you can write to it, then run this installer again.'
    shortcut="$shortcut_dir/Notebook Alchemy.command"
    write_launcher "$shortcut" || fail "Could not write [$shortcut]." 'Check that you can write to that folder, then run this installer again.'
    say "Created [$shortcut]. Double-click it to open Notebook Alchemy in Terminal."
  else
    data_dir=${XDG_DATA_HOME:-$HOME/.local/share}
    mkdir -p "$data_dir/notebook-alchemy" "$data_dir/applications" || fail "Could not create folders in [$data_dir]." \
      'Check that you can write to it, then run this installer again.'
    launcher="$data_dir/notebook-alchemy/notebook-alchemy-launch"
    write_launcher "$launcher" || fail "Could not write [$launcher]." 'Check that you can write to that folder, then run this installer again.'
    entry="$data_dir/applications/notebook-alchemy.desktop"
    write_desktop_entry "$entry" "$launcher" || fail "Could not write [$entry]." 'Check that you can write to that folder, then run this installer again.'
    say "Added Notebook Alchemy to your applications menu: [$entry]."
    desktop=$(linux_desktop_dir)
    if [ -d "$desktop" ]; then
      cp "$entry" "$desktop/notebook-alchemy.desktop" || fail "Could not copy the shortcut to [$desktop]." \
        'Check that you can write to that folder, then run this installer again.'
      # GNOME starts a desktop file only once it is marked trusted; other desktops ignore this, and
      # without it the shortcut still works after "Allow Launching" in its right-click menu.
      if command -v gio >/dev/null 2>&1 && ! gio set "$desktop/notebook-alchemy.desktop" metadata::trusted true 2>/dev/null; then
        say 'To start it from the desktop, right-click it and choose "Allow Launching".'
      fi
      say "Created [$desktop/notebook-alchemy.desktop]."
    else
      say "No desktop folder at [$desktop], so the shortcut is only in the applications menu."
    fi
  fi
  say "It opens in [$WORK_DIR], where your settings and output are kept."

  # 5. Self-test
  step 5 'Checking the install'
  (cd "$WORK_DIR" && "$EXE" test) || fail 'The self-test failed.' \
    'Read the failed checks above, then report them at https://github.com/gagexhill/notebook-alchemy/issues.'

  say ''
  say 'Notebook Alchemy is installed.'
  if [ "$os" = macos ]; then
    say 'To start, double-click [Notebook Alchemy] on your desktop, or open a new terminal and run:'
  else
    say 'To start, open [Notebook Alchemy] from your applications menu, or open a new terminal and run:'
  fi
  say '  $ notebook-alchemy'
}

main "$@"
