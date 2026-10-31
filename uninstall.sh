#!/usr/bin/env bash
set -Eeuo pipefail

MARKER=".voice-assistant-install"
OWNER="${SUDO_USER:-${USER:-}}"
if [[ -z "$OWNER" || "$OWNER" == root ]]; then
	echo "Run this script as the user who installed the Voice Assistant; it will request sudo only when needed." >&2
	exit 1
fi
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
OWNER_UID="$(id -u "$OWNER")"

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

user_unit="$OWNER_HOME/.config/systemd/user/voice-assistant.service"
system_unit="/etc/systemd/system/voice-assistant.service"
declare -a CANDIDATES=()
declare -a FOUND=()
add_candidate() {
	local path="$1" existing
	[[ -n "$path" && "$path" == /* && -d "$path" && -f "$path/$MARKER" ]] || return 0
	for existing in "${CANDIDATES[@]:-}"; do [[ "$existing" == "$path" ]] && return 0; done
	CANDIDATES+=("$path")
}

for root in /opt /usr/local/lib /usr/local/bin /usr/local/sbin /usr/bin /usr/sbin; do
	add_candidate "$root/voice_assistant"
	add_candidate "$root/voice-assistant"
done
for unit in "$user_unit" "$system_unit"; do
	if [[ -f "$unit" ]]; then
		unit_path="$(sed -n 's/^WorkingDirectory=//p' "$unit" | head -n 1)"
		add_candidate "$unit_path"
	fi
done
FOUND=()
if ((${#CANDIDATES[@]} > 0)); then FOUND=("${CANDIDATES[@]}"); fi

if ((${#FOUND[@]} == 0)) && [[ ! -f "$user_unit" && ! -f "$system_unit" ]]; then
	echo "No Voice Assistant installation or service created by install.sh was found in the standard locations."
	exit 0
fi

echo "The uninstaller found:"
for path in "${FOUND[@]}"; do echo "  $path"; done
[[ -f "$user_unit" ]] && echo "  user service: $user_unit"
[[ -f "$system_unit" ]] && echo "  system service: $system_unit"
read -r -p "Uninstall the service and these marked app installations? [y/N] " answer
[[ "$answer" =~ ^[Yy]$ ]] || { echo "Uninstall cancelled."; exit 0; }

if [[ -f "$user_unit" ]]; then
	set +e
	as_owner env HOME="$OWNER_HOME" XDG_RUNTIME_DIR="/run/user/$OWNER_UID" \
		DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$OWNER_UID/bus" \
		systemctl --user disable --now voice-assistant.service
	set -e
	run_root rm -f -- "$user_unit"
	run_root rm -f -- "$OWNER_HOME/.config/systemd/user/default.target.wants/voice-assistant.service"
	set +e
	as_owner env HOME="$OWNER_HOME" XDG_RUNTIME_DIR="/run/user/$OWNER_UID" \
		DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$OWNER_UID/bus" \
		systemctl --user daemon-reload
	set -e
fi
if [[ -f "$system_unit" ]]; then
	set +e
	run_root systemctl disable --now voice-assistant.service
	set -e
	run_root rm -f -- "$system_unit"
	run_root rm -f -- "/etc/systemd/system/multi-user.target.wants/voice-assistant.service"
	run_root systemctl daemon-reload
fi

if ((${#FOUND[@]} > 0)); then
	stamp="$(date +%Y%m%d-%H%M%S)"
	backup_root="$OWNER_HOME/.local/share/voice-assistant/uninstall-backup-$stamp"
	backup_index=0
	for path in "${FOUND[@]}"; do
		backup_index=$((backup_index + 1))
		backup_dir="$backup_root/install-$backup_index"
		as_owner mkdir -p -- "$backup_dir"
		for item in .env history.md logs recordings wakewords not_found.wav; do
			if [[ -e "$path/$item" ]]; then run_root cp -a -- "$path/$item" "$backup_dir/"; fi
		done
		run_root chown -R "$OWNER:$(id -gn "$OWNER")" "$backup_root"
		run_root chmod 700 "$backup_root" "$backup_dir"
		run_root rm -rf -- "$path"
	done
	echo "Saved .env and local data under $backup_root before removing the installations."
fi

echo "Voice Assistant service and marked installation(s) removed."
