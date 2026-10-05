#!/bin/sh
# Install the CLI runtime and every bundled skill for the current user.
# Keep the work inside main so a truncated curl response cannot start installing.
set -eu

die() {
    printf 'boris install: %s\n' "$*" >&2
    exit 1
}

add_path() {
    profile=$1
    path_line=$2
    mkdir -p "$(dirname "$profile")"
    if ! grep -Fqx "$path_line" "$profile" 2>/dev/null; then
        printf '\n# Boris CLI\n%s\n' "$path_line" >> "$profile"
        printf 'Added PATH configuration to %s\n' "$profile"
    fi
}

main() {
    [ -n "${HOME:-}" ] || die 'HOME must be set.'
    for dependency in curl tar python3 bash; do
        command -v "$dependency" >/dev/null 2>&1 || die "Required command not found: $dependency"
    done
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die 'Python 3.9 or newer is required.'

    install_dir="$HOME/.boris"
    bin_dir="$HOME/.local/bin"
    skills_dir="$HOME/.agents/skills"
    ref=${BORIS_REF:-main}

    # Do not overwrite a different command already installed at this location.
    if [ -e "$bin_dir/boris" ] || [ -L "$bin_dir/boris" ]; then
        [ -L "$bin_dir/boris" ] && [ "$(readlink "$bin_dir/boris")" = "$install_dir/boris" ] || die "Refusing to overwrite $bin_dir/boris; move it aside before installing."
    fi

    tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/boris-install.XXXXXXXX")
    trap 'rm -rf "$tmp_dir"' EXIT
    trap 'exit 1' HUP INT TERM
    mkdir "$tmp_dir/source"
    printf 'Downloading Boris (%s)...\n' "$ref"
    curl -fsSL --retry 3 -o "$tmp_dir/boris.tar.gz" "https://codeload.github.com/tyler-schwartz/boris/tar.gz/$ref"
    tar -xzf "$tmp_dir/boris.tar.gz" -C "$tmp_dir/source" --strip-components=1
    source_dir="$tmp_dir/source"
    [ -f "$source_dir/boris" ] && [ -f "$source_dir/lib/boris.py" ] && [ -f "$source_dir/skills/boris/SKILL.md" ] || die 'The downloaded archive is missing the Boris runtime or skill.'
    for skill in "$source_dir/skills/"*; do
        [ -d "$skill" ] || continue
        [ -f "$skill/SKILL.md" ] || die "Missing SKILL.md in $(basename "$skill")."
    done

    mkdir -p "$install_dir/lib" "$bin_dir" "$skills_dir"
    cp "$source_dir/boris" "$install_dir/boris"
    cp -R "$source_dir/lib/." "$install_dir/lib/"
    chmod +x "$install_dir/boris"
    ln -sf "$install_dir/boris" "$bin_dir/boris"
    for skill in "$source_dir/skills/"*; do
        [ -d "$skill" ] || continue
        name=$(basename "$skill")
        mkdir -p "$skills_dir/$name"
        cp -R "$skill/." "$skills_dir/$name/"
        printf 'Installed skill: %s\n' "$name"
    done

    case ":${PATH:-}:" in
        *":$bin_dir:"*) ;;
        *)
            path_line='export PATH="$HOME/.local/bin:$PATH"'
            case "${SHELL:-}" in
                */zsh)
                    add_path "${ZDOTDIR:-$HOME}/.zshrc" "$path_line"
                    add_path "${ZDOTDIR:-$HOME}/.zprofile" "$path_line"
                    ;;
                */bash)
                    add_path "$HOME/.bashrc" "$path_line"
                    if [ -f "$HOME/.bash_profile" ]; then
                        add_path "$HOME/.bash_profile" "$path_line"
                    elif [ -f "$HOME/.bash_login" ]; then
                        add_path "$HOME/.bash_login" "$path_line"
                    else
                        add_path "$HOME/.profile" "$path_line"
                    fi
                    ;;
                */fish)
                    add_path "${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish" 'set -gx PATH "$HOME/.local/bin" $PATH'
                    ;;
                *) add_path "$HOME/.profile" "$path_line" ;;
            esac
            printf '\nOpen a new terminal to use boris. For this terminal, add %s to PATH.\n' "$bin_dir"
            ;;
    esac
    printf '\nInstalled Boris: %s\nInstalled skills: %s\n' "$bin_dir/boris" "$skills_dir"
}

main "$@"
