#!/bin/bash
###############################################################################
# AFS Docker Entrypoint
# Starts VNC server, noVNC proxy, PulseAudio, and configures AFS for Docker
###############################################################################
set -e

echo "============================================="
echo "  AFS (Agent Family System) Docker Container"
echo "============================================="

# ─── Configuration ────────────────────────────────────────────────────────────
VNC_RESOLUTION="${VNC_RESOLUTION:-1920x1080}"
VNC_DISPLAY=":1"
VNC_PORT=5901
NOVNC_PORT=6080

# ─── Configure AFS for Docker (switch terminal_mode to xterm) ─────────────────
for CONFIG_FILE in \
    /home/ubuntu/afs/src/afs_config/config/config.json \
    /home/ubuntu/afs/install/afs_config/share/afs_config/config/config.json; do
    if [ -f "$CONFIG_FILE" ]; then
        python3 -c "
import json
with open('$CONFIG_FILE', 'r') as f:
    config = json.load(f)
config['terminal_mode'] = 'xterm'
with open('$CONFIG_FILE', 'w') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
print(f'[entrypoint] {\"$CONFIG_FILE\".split(\"/\")[-3]}: terminal_mode set to xterm')
"
    fi
done

# ─── Start PulseAudio & Audio Bridge ──────────────────────────────────────────
if [ -S "/run/user/1000/pulse/native" ]; then
    echo "[entrypoint] Host PulseAudio detected — using host audio output."
    export PULSE_SERVER=unix:/run/user/1000/pulse/native
else
    echo "[entrypoint] No host PulseAudio — starting internal PulseAudio + audio bridge..."
    pulseaudio --start --exit-idle-time=-1 2>/dev/null || true
    sleep 1
    # Start audio bridge WebSocket server (TTS/STT via browser)
    python3 /home/ubuntu/afs/docker/audio_bridge.py &
    AUDIO_BRIDGE_PID=$!
    echo "[entrypoint] Audio bridge started (PID: $AUDIO_BRIDGE_PID)"
fi

# ─── Start VNC server ─────────────────────────────────────────────────────────
echo "[entrypoint] Starting VNC server (${VNC_RESOLUTION})..."
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1 2>/dev/null || true

VNC_ARGS="${VNC_DISPLAY} -geometry ${VNC_RESOLUTION} -depth 24 -localhost no -xstartup /home/ubuntu/.vnc/xstartup"
if [ -n "$VNC_PASSWORD" ]; then
    echo "$VNC_PASSWORD" | vncpasswd -f > /home/ubuntu/.vnc/passwd
    chmod 600 /home/ubuntu/.vnc/passwd
    echo "[entrypoint] VNC password set."
else
    echo "[entrypoint] VNC running without password."
    VNC_ARGS="${VNC_ARGS} --SecurityTypes=None --I-KNOW-THIS-IS-INSECURE"
fi

vncserver ${VNC_ARGS} 2>&1 | head -5

export DISPLAY=${VNC_DISPLAY}
sleep 2

# ─── Start noVNC proxy ────────────────────────────────────────────────────────
echo "[entrypoint] Starting noVNC on port ${NOVNC_PORT}..."
websockify --web=/usr/share/novnc ${NOVNC_PORT} localhost:${VNC_PORT} &
NOVNC_PID=$!

sleep 1
echo ""
echo "============================================="
echo "  ✅ AFS is ready!"
echo ""
echo "  🌐 Open in browser: http://localhost:${NOVNC_PORT}/vnc.html"
if [ -z "${PULSE_SERVER:-}" ]; then
    echo "  🔊 Audio bridge: enabled (click 🔇 icon in VNC page)"
fi
echo ""
echo "  📋 To launch AFS:"
echo "     Double-click 'AFS Launch' on the desktop"
echo "     or open a terminal and run:"
echo "     ros2 launch afs_bringup afs_all.launch.py"
echo ""
if [ -n "$OPENAI_API_KEY" ]; then
    echo "  🔑 OpenAI API Key: SET"
else
    echo "  🔑 OpenAI API Key: NOT SET"
fi
if [ -n "$GEMINI_API_KEY" ]; then
    echo "  🔑 Gemini API Key: SET"
else
    echo "  🔑 Gemini API Key: NOT SET"
fi
echo "============================================="
echo ""

# ─── Keep container running ───────────────────────────────────────────────────
wait $NOVNC_PID
