#!/bin/bash
# jetbridge installer for a Mac. EXPERIMENTAL: it hasn't been tried on a real Mac yet.
#
#   curl -fsSL https://raw.githubusercontent.com/xiuruichen/jetbridge/main/install.sh | bash
#
# It checks for Git and Google Chrome, installs uv if you agree, downloads jetbridge to your home
# folder, installs its Python packages, adds a `jetbridge` command, offers to connect Claude Desktop
# if you have it, and starts `jetbridge start`.
#
# Optional settings, as environment variables:
#   JETBRIDGE_DIR     where to put jetbridge (default: your home folder/jetbridge)
#   JETBRIDGE_REPO    where to download it from
#   JETBRIDGE_BRANCH  which branch to download
#   JETBRIDGE_INSTALL_NO_START=1  install, but don't ask anything or start the questions

set -u

fail() {
    printf "\njetbridge wasn't fully installed: %s\n" "$1" >&2
    exit 1
}

has_chrome() {
    [ -d "/Applications/Google Chrome.app" ] || [ -d "$HOME/Applications/Google Chrome.app" ]
}

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
    elif [ -x "$HOME/.local/bin/uv" ]; then
        printf '%s\n' "$HOME/.local/bin/uv"
    fi
}

# Piped into bash, the script itself is what's on standard input, so answers come from the terminal.
ask() {
    if [ "${JETBRIDGE_INSTALL_NO_START:-}" = "1" ]; then
        return 0
    fi
    printf '%s [y/N] ' "$1"
    read -r answer </dev/tty || return 1
    case "$answer" in
        y | Y | yes | YES) return 0 ;;
        *) return 1 ;;
    esac
}

main() {
    [ "$(uname -s)" = "Darwin" ] || fail "this installer is for a Mac. On Windows, use install.ps1."

    printf 'jetbridge on a Mac is EXPERIMENTAL and has not been tried on a real Mac yet.\n'
    printf 'Airline searches, sleep and Claude Desktop may not work as they do on Windows.\n\n'

    command -v git >/dev/null 2>&1 ||
        fail "Git is missing. Run: xcode-select --install   Then run this again."
    has_chrome || fail "Google Chrome is missing. Install it from google.com/chrome, then run this again."

    uv="$(find_uv)"
    if [ -z "$uv" ]; then
        ask "jetbridge needs uv, which installs Python for it. Install it from astral.sh?" ||
            fail "nothing was installed. jetbridge needs uv."
        curl -LsSf https://astral.sh/uv/install.sh | sh || fail "couldn't install uv"
        uv="$(find_uv)"
        [ -n "$uv" ] || fail "uv was installed, but this window can't see it. Open a new one and run this again."
    fi

    target="${JETBRIDGE_DIR:-$HOME/jetbridge}"
    repo="${JETBRIDGE_REPO:-https://github.com/xiuruichen/jetbridge.git}"
    if [ -e "$target" ]; then
        if [ ! -f "$target/pyproject.toml" ] || [ ! -e "$target/.git" ]; then
            fail "$target already exists and isn't jetbridge, so I left it alone. Set JETBRIDGE_DIR to use another folder."
        fi
        printf 'jetbridge is already in %s. Checking for updates...\n' "$target"
        git -C "$target" pull --ff-only || printf 'Could not update it, so I will use the copy you have.\n'
    else
        printf 'Downloading jetbridge to %s...\n' "$target"
        if [ -n "${JETBRIDGE_BRANCH:-}" ]; then
            git clone --branch "$JETBRIDGE_BRANCH" "$repo" "$target" || fail "couldn't download jetbridge"
        else
            git clone "$repo" "$target" || fail "couldn't download jetbridge"
        fi
    fi

    printf 'Installing the Python packages jetbridge needs...\n'
    "$uv" sync --project "$target" || fail "couldn't install the Python packages"

    python="$target/.venv/bin/python"
    launcher="$HOME/.local/bin/jetbridge"
    mkdir -p "$HOME/.local/bin"
    printf '#!/bin/sh\nexec "%s" -m jetbridge "$@"\n' "$python" >"$launcher"
    chmod +x "$launcher"

    printf '\njetbridge is installed in %s.\n' "$target"
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) printf 'To add flights later, run: jetbridge start\n' ;;
        *) printf 'To add flights later, open a new window and run: %s start\n' "$launcher" ;;
    esac
    printf '\n'
    [ "${JETBRIDGE_INSTALL_NO_START:-}" = "1" ] && return 0

    # Neither Claude Desktop nor stopping part way through the questions is an installer failure.
    "$python" -m jetbridge claude connect --ask </dev/tty
    printf '\n'
    "$python" -m jetbridge start </dev/tty
    return 0
}

# Wrapped in a function, so a download cut off part way can't run half a script.
main
