#!/usr/bin/env bash
# Install a per-user Linux Mint launcher for the Serial Protocol Translator.
set -euo pipefail

if [[ "${1:-}" == "--system-deps" ]]; then
  sudo apt-get update
  sudo apt-get install -y python3-venv python3-gi gir1.2-ayatanaappindicator3-0.1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "$script_dir/.." && pwd)"
install_dir="$HOME/.local/share/serial-protocol-translator"
venv_dir="$install_dir/venv"
applications_dir="$HOME/.local/share/applications"
icons_dir="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$install_dir" "$applications_dir" "$icons_dir"

python3 -m venv --system-site-packages "$venv_dir"
"$venv_dir/bin/python" -m pip install --upgrade pip
"$venv_dir/bin/python" -m pip install -r "$project_dir/requirements.txt"

launcher="$install_dir/launch.sh"
printf '%s\n' '#!/usr/bin/env bash' \
  "exec \"$venv_dir/bin/python\" \"$project_dir/translator.py\" --gui \"\$@\"" > "$launcher"
chmod +x "$launcher"

install -m 644 "$project_dir/assets/serial-protocol-translator.svg" \
  "$icons_dir/serial-protocol-translator.svg"

desktop_file="$applications_dir/serial-protocol-translator.desktop"
printf '%s\n' '[Desktop Entry]' 'Type=Application' 'Name=Serial Protocol Translator' \
  'Comment=Bidirectional serial protocol bridge' "Exec=$launcher" 'Terminal=false' \
  'Icon=serial-protocol-translator' 'Categories=Utility;System;' 'StartupNotify=true' > "$desktop_file"

desktop_dir="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
if [[ -z "$desktop_dir" ]]; then
  desktop_dir="$HOME/Desktop"
fi
mkdir -p "$desktop_dir"
install -m 755 "$desktop_file" "$desktop_dir/Serial Protocol Translator.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$applications_dir" || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f "$HOME/.local/share/icons/hicolor" || true
fi

echo "Installed the Linux Mint menu item and Desktop icon: Serial Protocol Translator"
echo "For the tray icon, enable Cinnamon's System Tray applet (Panel right-click → Applets)."
echo "If the icon is missing, rerun with: $0 --system-deps"
