#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
MARKER=".voice-assistant-install"

if [[ ! -f "$SOURCE_DIR/main.py" || ! -f "$SOURCE_DIR/requirements.txt" || ! -f "$SOURCE_DIR/.env.example" ]]; then
	echo "Run install.sh from a complete Voice Assistant project folder." >&2
	exit 1
fi

OWNER="${SUDO_USER:-${USER:-}}"
if [[ -z "$OWNER" || "$OWNER" == root ]]; then
	echo "Run this script as your normal user; it will request sudo only when needed." >&2
	exit 1
fi
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
OWNER_UID="$(id -u "$OWNER")"
OWNER_GROUP="$(id -gn "$OWNER")"
if [[ -z "$OWNER_HOME" ]]; then
	echo "Could not find the home directory for $OWNER." >&2
	exit 1
fi

run_root() {
	if [[ "$(id -u)" -eq 0 ]]; then "$@"; else sudo "$@"; fi
}

as_owner() {
	if [[ "$(id -u)" -eq "$OWNER_UID" ]]; then
		"$@"
	else
		run_root runuser -u "$OWNER" -- "$@"
	fi
}

destinations=(
	"/opt/voice_assistant"
	"/usr/local/lib/voice_assistant"
	"/usr/local/bin/voice_assistant"
	"/usr/local/sbin/voice_assistant"
	"/usr/bin/voice_assistant"
	"/usr/sbin/voice_assistant"
)

cat <<'EOF'
Choose where to install the Voice Assistant project:
  1) /opt/voice_assistant             (recommended)
  2) /usr/local/lib/voice_assistant
  3) /usr/local/bin/voice_assistant
  4) /usr/local/sbin/voice_assistant
  5) /usr/bin/voice_assistant
  6) /usr/sbin/voice_assistant
  7) Enter a custom absolute path

The project is installed in its own directory under the selected location.
EOF
read -r -p "Selection [1-7]: " choice
if [[ "$choice" =~ ^[1-6]$ ]]; then
	DEST="${destinations[$((choice - 1))]}"
elif [[ "$choice" == 7 ]]; then
	read -r -p "Custom absolute install path: " DEST
	if [[ "$DEST" != /* || "$DEST" == "/" || "$DEST" == *[[:space:]]* ]]; then
		echo "Choose an absolute path without spaces (for example /opt/voice_assistant)." >&2
		exit 1
	fi
else
	echo "Invalid selection." >&2
	exit 1
fi
case "$DEST/" in
	"$SOURCE_DIR/"*)
		echo "The install directory cannot be inside the project source directory." >&2
		exit 1
		;;
esac

if [[ -e "$DEST" ]]; then
	if [[ ! -d "$DEST" ]]; then
		echo "The install path exists and is not a directory: $DEST" >&2
		exit 1
	fi
	if [[ -e "$DEST/$MARKER" ]]; then
		read -r -p "An installation is already there. Update it and keep its data? [y/N] " answer
		[[ "$answer" =~ ^[Yy]$ ]] || { echo "Installation cancelled."; exit 0; }
	elif [[ -n "$(find "$DEST" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
		echo "The selected directory is not empty and is not marked as this app's installation." >&2
		echo "Choose an empty directory or one previously created by install.sh." >&2
		exit 1
	fi
fi

echo "Installing to $DEST ..."
run_root mkdir -p -- "$DEST"
tar -C "$SOURCE_DIR" \
	--exclude='./.git' --exclude='./.venv' --exclude='./venv' \
	--exclude='./__pycache__' --exclude='*/__pycache__' \
	--exclude='./recordings' --exclude='./logs' --exclude='./history.md' \
	--exclude='./.env' --exclude='./web/node_modules' \
	-cf - . | run_root tar -C "$DEST" -xf -

if [[ ! -f "$DEST/.env" ]]; then
	if [[ -f "$SOURCE_DIR/.env" ]]; then
		run_root install -m 600 -o "$OWNER" -g "$OWNER_GROUP" "$SOURCE_DIR/.env" "$DEST/.env"
	else
		run_root install -m 600 -o "$OWNER" -g "$OWNER_GROUP" "$SOURCE_DIR/.env.example" "$DEST/.env"
	fi
fi
printf 'voice_assistant_install=1\n' | run_root tee "$DEST/$MARKER" >/dev/null
run_root chown -R "$OWNER:$OWNER_GROUP" "$DEST"
run_root chmod 600 "$DEST/.env"

if [[ ! -x "$DEST/.venv/bin/python" ]]; then
	as_owner python3 -m venv "$DEST/.venv"
fi
as_owner "$DEST/.venv/bin/python" -m pip install --upgrade pip
as_owner "$DEST/.venv/bin/python" -m pip install -r "$DEST/requirements.txt"

echo "Installation files and Python dependencies are ready at $DEST."
read -r -p "Install and enable a user systemd service to start at boot? [y/N] " service_answer
if [[ "$service_answer" =~ ^[Yy]$ ]]; then
	if ! command -v systemctl >/dev/null 2>&1 || ! command -v loginctl >/dev/null 2>&1; then
		echo "systemd/logind was not found; skipping service installation."
	else
		unit_dir="$OWNER_HOME/.config/systemd/user"
		unit_tmp="$(mktemp)"
		cat >"$unit_tmp" <<EOF
[Unit]
Description=Voice Assistant
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DEST
ExecStart=$DEST/.venv/bin/python $DEST/main.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
		run_root install -d -m 755 -o "$OWNER" -g "$OWNER_GROUP" "$unit_dir"
		run_root install -m 644 -o "$OWNER" -g "$OWNER_GROUP" "$unit_tmp" "$unit_dir/voice-assistant.service"
		rm -f -- "$unit_tmp"
		run_root loginctl enable-linger "$OWNER"
		if as_owner env \
			HOME="$OWNER_HOME" \
			XDG_RUNTIME_DIR="/run/user/$OWNER_UID" \
			DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$OWNER_UID/bus" \
			systemctl --user daemon-reload &&
			as_owner env HOME="$OWNER_HOME" XDG_RUNTIME_DIR="/run/user/$OWNER_UID" \
				DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$OWNER_UID/bus" \
				systemctl --user enable --now voice-assistant.service; then
			echo "User service enabled for boot. Check it with: systemctl --user status voice-assistant.service"
		else
			echo "The service unit was installed, but the user systemd manager was not reachable."
			echo "After logging in, run: systemctl --user daemon-reload && systemctl --user enable --now voice-assistant.service"
		fi
	fi
fi

echo "Done. Edit $DEST/.env before starting the assistant if you have not configured it yet."
