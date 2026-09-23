###############################################################################
# AFS (Agent Family System) - Docker Environment
# Ubuntu 24.04 + ROS2 Jazzy + XFCE4 + TigerVNC + noVNC
# Access GUI via browser: http://localhost:6080
###############################################################################

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=en_US.UTF-8
ENV LANGUAGE=en_US:en
ENV LC_ALL=en_US.UTF-8
ENV TZ=Asia/Tokyo

# ─── System base packages ────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y \
    locales \
    curl \
    wget \
    gnupg2 \
    lsb-release \
    software-properties-common \
    sudo \
    git \
    vim \
    nano \
    ca-certificates \
    && locale-gen en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

# ─── ROS2 Jazzy ──────────────────────────────────────────────────────────────
RUN curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key \
    -o /usr/share/keyrings/ros-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] \
    http://packages.ros.org/ros2/ubuntu $(. /etc/os-release && echo $UBUNTU_CODENAME) main" \
    > /etc/apt/sources.list.d/ros2.list \
    && apt-get update && apt-get install -y \
    ros-jazzy-ros-base \
    ros-jazzy-ament-cmake \
    ros-jazzy-rosidl-default-generators \
    ros-jazzy-rosidl-default-runtime \
    python3-colcon-common-extensions \
    python3-rosdep \
    && rm -rf /var/lib/apt/lists/*

# ─── Desktop environment (XFCE4) + VNC + noVNC ──────────────────────────────
RUN apt-get update && apt-get install -y \
    xfce4 \
    xfce4-terminal \
    xterm \
    dbus-x11 \
    tigervnc-standalone-server \
    tigervnc-common \
    novnc \
    websockify \
    && rm -rf /var/lib/apt/lists/*

# ─── Audio (PulseAudio) + AFS system dependencies ───────────────────────────
RUN apt-get update && apt-get install -y \
    pulseaudio \
    alsa-utils \
    ffmpeg \
    python3-tk \
    libportaudio2 \
    python3-pip \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# ─── Python dependencies for AFS ─────────────────────────────────────────────
RUN pip3 install --break-system-packages --ignore-installed \
    openai \
    google-genai \
    numpy \
    sounddevice \
    webrtcvad \
    matplotlib \
    toio-py \
    Pillow \
    websockets

# ─── User setup ──────────────────────────────────────────────────────────────
RUN id -u ubuntu &>/dev/null || useradd -m -s /bin/bash ubuntu \
    && usermod -aG sudo ubuntu \
    && echo "ubuntu ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# ─── Audio overlay for noVNC (inject while still root) ───────────────────────
COPY docker/audio_overlay.js /usr/share/novnc/audio_overlay.js
RUN sed -i 's|</body>|<script defer src="audio_overlay.js"></script></body>|' /usr/share/novnc/vnc.html

USER ubuntu
WORKDIR /home/ubuntu

# ─── VNC configuration ───────────────────────────────────────────────────────
RUN mkdir -p /home/ubuntu/.vnc \
    && echo "#!/bin/sh" > /home/ubuntu/.vnc/xstartup \
    && echo "unset SESSION_MANAGER" >> /home/ubuntu/.vnc/xstartup \
    && echo "unset DBUS_SESSION_BUS_ADDRESS" >> /home/ubuntu/.vnc/xstartup \
    && echo "exec startxfce4" >> /home/ubuntu/.vnc/xstartup \
    && chmod +x /home/ubuntu/.vnc/xstartup

# ─── Copy AFS source and build ───────────────────────────────────────────────
COPY --chown=ubuntu:ubuntu . /home/ubuntu/afs

WORKDIR /home/ubuntu/afs

# Source ROS2 and build the workspace
RUN /bin/bash -c "source /opt/ros/jazzy/setup.bash && colcon build"

# ─── Add ROS2 source to bashrc ───────────────────────────────────────────────
RUN echo "" >> /home/ubuntu/.bashrc \
    && echo "# ROS2 Jazzy" >> /home/ubuntu/.bashrc \
    && echo "source /opt/ros/jazzy/setup.bash" >> /home/ubuntu/.bashrc \
    && echo "source /home/ubuntu/afs/install/setup.bash" >> /home/ubuntu/.bashrc

# ─── Desktop shortcut for AFS launch ─────────────────────────────────────────
RUN mkdir -p /home/ubuntu/Desktop \
    && echo "[Desktop Entry]\n\
    Version=1.0\n\
    Type=Application\n\
    Name=AFS Launch\n\
    Comment=Start Agent Family System\n\
    Exec=xterm -fa 'Monospace' -fs 12 -hold -e bash -c 'source /home/ubuntu/afs/install/setup.bash && ros2 launch afs_bringup afs_all.launch.py'\n\
    Icon=utilities-terminal\n\
    Terminal=false\n\
    Categories=Application;" > /home/ubuntu/Desktop/afs-launch.desktop \
    && chmod +x /home/ubuntu/Desktop/afs-launch.desktop

# ─── Entrypoint ──────────────────────────────────────────────────────────────
COPY --chown=ubuntu:ubuntu docker/entrypoint.sh /home/ubuntu/entrypoint.sh
# Strip Windows CRLF line endings (prevents "exec: no such file or directory")
RUN sed -i 's/\r$//' /home/ubuntu/entrypoint.sh && chmod +x /home/ubuntu/entrypoint.sh

EXPOSE 6080

ENTRYPOINT ["/home/ubuntu/entrypoint.sh"]
