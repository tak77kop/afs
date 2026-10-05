#!/usr/bin/env python3
"""
Launch all AFS nodes in a single launch file (colloquially known as "launch all").

Entry point for `ros2 launch afs_bringup afs_all.launch.py`.
The ROS2 launch infrastructure loads this file standalone via `exec_module`
without adding its parent directory to `sys.path`. Therefore, all logic must be
self-contained in this single file (splitting into separate helper modules as in
other packages risks ImportErrors during `ros2 launch`).
The helper classes below are grouped for readability while remaining inside this file.

Components:
- SessionArchiver: Moves DB output files from the previous session to the archive
- LeaderElection: Selects the initial speaker via majority vote using the Gemini API
- TerminalCommandBuilder: Assembles commands to launch each node in GUI terminals (gnome-terminal/xterm)
- launch_nodes / generate_launch_description: ROS2 launch entry points

Startup sequence (`generate_launch_description` -> `launch_nodes`):
  1. Archive files from the previous session to start with a clean DB_DIR.
  2. Read `config.json` (family members, theme, terminal mode, toio_move flag).
  3. Terminate leftover processes from prior runs (audio players and AFS nodes).
  4. Query Gemini to decide which role speaks first (`LeaderElection.determine`).
  5. Launch all nodes. Only the elected role receives the `--initiate` flag,
     ensuring exactly one family member starts the conversation.

Nodes with visible terminal windows:
- Family members, therapist, and STT open dedicated GUI terminal windows arranged
  in a grid because the operator needs to monitor or input text.
- TTS, toio, evaluator, optimizer, generator, member_evaluator, and
  document_processor run in the background without dedicated windows.
"""
import os
# Must be set before google-auth is imported. Without this, google-auth attempts
# to reach the GCE metadata server and hangs indefinitely in proxied environments.
os.environ["NO_GCE_CHECK"] = "true"
import json
import random
import shlex
import sys
import re
import shutil
import datetime
import time
import atexit
import threading
from launch import LaunchDescription
from launch.actions import OpaqueFunction, ExecuteProcess
import subprocess
from ament_index_python.packages import get_package_share_directory

# Configuration paths
HOME = os.path.expanduser("~")
DB_DIR = os.path.join(HOME, "afs/src/afs_database")


class SessionArchiver:
    """Archive database output files (conversation history, evaluation plots, etc.) from previous sessions."""

    #: Target archive files (logical name -> file name in DB_DIR)
    SESSION_FILE_NAMES = {
        "conversation": "conversation_history.txt",
        "plot": "evaluation_plot.png",
        "trajectory": "evaluation_trajectory.json",
        "plot_bg": "evaluation_plot_bg.png",
        "history_csv": "evaluation_history.csv",
        "debug_log": "last_evaluation_debug.log",
    }

    @classmethod
    def archive(cls, context_label: str = "Shutdown"):
        """Archive session data to safeguard outputs.

        Called twice per run: at startup (to start the new session with a clean state)
        and on exit via `atexit`. `context_label` changes only the log prefix,
        allowing the two invocations to be distinguished in console output.
        """
        # One folder per archive event, named with the archive timestamp.
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_dir = os.path.join(DB_DIR, "archive", timestamp)

        session_files = {name: os.path.join(DB_DIR, fname) for name, fname in cls.SESSION_FILE_NAMES.items()}

        # Check if any primary files exist
        # (sessions may stop early, leaving only a subset of files)
        existing = {name: f for name, f in session_files.items() if os.path.exists(f)}

        if existing:
            try:
                os.makedirs(archive_dir, exist_ok=True)
                print(f"\n[afs_launch][{context_label}] Archiving session to {archive_dir}...")

                count = 0
                for name, f_path in existing.items():
                    try:
                        dest = os.path.join(archive_dir, os.path.basename(f_path))
                        # Safely copy before deleting
                        shutil.copy2(f_path, dest)
                        if os.path.exists(dest):
                            os.remove(f_path)
                            count += 1
                            print(f"[afs_launch][{context_label}] Successfully archived {name}")
                    except Exception as e:
                        print(f"[afs_launch][{context_label}] Failed to archive {name}: {e}")

                print(f"[afs_launch][{context_label}] Total {count} files archived.\n")
            except Exception as e:
                print(f"[afs_launch][{context_label}] Critical error during archival: {e}")
        else:
            # An empty DB_DIR at startup is normal, so suppress output;
            # report "no session data" only when shutting down.
            if context_label == "Shutdown":
                 print(f"\n[afs_launch][{context_label}] No session data found to archive.\n")


# Register exit handler for archival.
# `ros2 launch` is typically stopped with Ctrl-C; atexit ensures session files
# are reliably preserved even without an explicit shutdown hook.
atexit.register(SessionArchiver.archive, "Shutdown")

# Prioritize installed share directory, falling back to source tree
# if the workspace has not yet been built/installed.
try:
    SHARE_DIR = get_package_share_directory('afs_config')
    SAVE_DIR = os.path.join(SHARE_DIR, 'config')
except Exception:
    SAVE_DIR = os.path.join(os.path.dirname(DB_DIR), "afs_config/config")

os.makedirs(DB_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(SAVE_DIR, "config.json")
HISTORY_FILE = os.path.join(DB_DIR, "conversation_history.txt")
TRAJECTORY_FILE = os.path.join(DB_DIR, "evaluation_trajectory.json")


class LeaderElection:
    """Determine the conversation initiator via majority vote among family members using Gemini API."""

    @staticmethod
    def determine(roles: list, theme: str) -> str:
        """Select the first speaker via majority vote using Gemini API.
        Each family member votes on who should start the conversation.

        Returns the winning role name in lowercase. All failure paths fall back
        to a random role, ensuring failed voting never blocks launch.
        """
        if not roles:
            return ""

        print("[afs_launch] Determining leader by Gemini vote...")

        import requests
        import socket
        import urllib3.util.connection as urllib3_cn
        # Force IPv4. In lab proxy environments, IPv6 resolution to Gemini endpoints
        # hangs until connection timeout.
        urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("[afs_launch] GEMINI_API_KEY not set. Falling back to random.")
            leader = random.choice(roles)
            print(f"[afs_launch] Leader selected (random): {leader}")
            return leader

        votes = {}  # voter role -> voted target role

        def cast_vote(voter_role):
            """Prompt Gemini impersonating `voter_role` to ask who should speak first.

            Returns the target role in lowercase, or None if the call fails or
            does not match any name in `roles`.
            """
            try:
                prompt = f"""
You are "{voter_role}" in a family simulation.
The family members are: {roles}
The conversation theme is: "{theme}"

Who should speak FIRST to start the conversation about this theme?
Consider which family member would most naturally initiate this topic.

Respond with ONLY the name of ONE family member from the list above, in lowercase.
"""
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={api_key}"
                headers = {"Content-Type": "application/json"}
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.3}
                }
                res = requests.post(url, headers=headers, json=payload, timeout=15.0)
                if res.status_code == 200:
                    ans = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip().lower()
                    # The model may respond with a full sentence; check for
                    # substring presence of known role names rather than strict equality.
                    for m in roles:
                        if m.lower() in ans:
                            return m.lower()
                return None
            except Exception as e:
                print(f"[afs_launch] Vote error for {voter_role}: {e}")
                return None

        # Parallel voting via threads.
        # Running one API call per member concurrently keeps total pre-launch delay
        # close to the latency of a single API call rather than accumulating sequentially.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(roles)) as executor:
            futures = {executor.submit(cast_vote, role): role for role in roles}
            try:
                # 20-second global timeout. Use whatever votes arrived by then.
                for future in concurrent.futures.as_completed(futures, timeout=20.0):
                    voter = futures[future]
                    result = future.result()
                    if result:
                        votes[voter] = result
                        print(f"[afs_launch] {voter} voted for: {result}")
            except concurrent.futures.TimeoutError:
                print("[afs_launch] Vote timeout. Using collected votes.")

        if not votes:
            leader = random.choice(roles)
            print(f"[afs_launch] No votes collected. Leader selected (random): {leader}")
            return leader

        # Tally votes per candidate
        counts = {}
        for voted_for in votes.values():
            counts[voted_for] = counts.get(voted_for, 0) + 1

        print(f"[afs_launch] Vote count: {counts}")

        max_count = max(counts.values())
        # Break ties using family_config declaration order.
        # Iterating `roles` in order ensures identical ties resolve deterministically
        # to the same member across runs.
        for member in roles:
            if counts.get(member.lower(), 0) == max_count:
                leader = member.lower()
                print(f"[afs_launch] Leader selected by vote: {leader}")
                return leader

        leader = max(counts, key=counts.get)
        print(f"[afs_launch] Leader selected: {leader}")
        return leader


class TerminalCommandBuilder:
    """Assemble command arrays to launch individual nodes in GUI terminals (gnome-terminal/xterm)."""

    @staticmethod
    def _get_setup_bash_path():
        """Dynamically resolve the workspace setup.bash path.

        Nodes run inside fresh terminal windows that do not inherit the sourced
        ROS2 environment, so each terminal must source setup.bash independently.
        The five checks below are attempted in order from most specific to generic,
        using the first valid match found.
        """
        # 1. Traverse upward from afs_bringup package share directory
        try:
            from ament_index_python.packages import get_package_share_directory
            pkg_share = get_package_share_directory('afs_bringup')
            curr = pkg_share
            for _ in range(5):
                candidate = os.path.join(curr, 'setup.bash')
                if os.path.exists(candidate):
                    return candidate
                parent = os.path.dirname(curr)
                if parent == curr:
                    break
                curr = parent
        except Exception:
            pass

        # 2. Traverse relative to current file path (__file__)
        try:
            curr = os.path.abspath(__file__)
            for _ in range(6):
                candidate = os.path.join(curr, 'install/setup.bash')
                if os.path.exists(candidate):
                    return candidate
                candidate2 = os.path.join(curr, 'setup.bash')
                if os.path.exists(candidate2):
                    return candidate2
                parent = os.path.dirname(curr)
                if parent == curr:
                    break
                curr = parent
        except Exception:
            pass

        # 3. Check COLCON_PREFIX_PATH environment variable
        colcon_prefix = os.environ.get('COLCON_PREFIX_PATH', '')
        if colcon_prefix:
            for path in colcon_prefix.split(os.pathsep):
                candidate = os.path.join(path, 'setup.bash')
                if os.path.exists(candidate):
                    return candidate

        # 4. Check relative to DB_DIR as fallback
        try:
            ws_root = os.path.dirname(os.path.dirname(DB_DIR))
            candidate = os.path.join(ws_root, 'install/setup.bash')
            if os.path.exists(candidate):
                return candidate
        except Exception:
            pass

        # 5. Default fallback
        default_path = os.path.join(HOME, 'afs/install/setup.bash')
        if os.path.exists(default_path):
            return default_path

        # Last resort: try ROS distribution setup.bash
        ros_distro = os.environ.get('ROS_DISTRO')
        if ros_distro:
            ros_path = f"/opt/ros/{ros_distro}/setup.bash"
            if os.path.exists(ros_path):
                return ros_path

        return default_path

    @classmethod
    def build(cls, terminal_mode, geometry, inner_cmd):
        """Assemble terminal command based on configured terminal mode.

        `geometry` positions windows in a screen grid, and `inner_cmd`
        is the `ros2 run ...` command executed after sourcing setup.bash.
        """
        setup_bash = cls._get_setup_bash_path()
        print(f"[afs_launch] Terminal setup.bash path: {setup_bash}")

        # Propagate environment variables (GEMINI_API_KEY and proxy settings) to new terminal windows.
        # Disable GCE checks so google-auth does not hang on metadata server lookups.
        env_vars = ['GEMINI_API_KEY', 'HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy', 'NO_PROXY', 'no_proxy']
        exports = ["export NO_GCE_CHECK=true"]
        for var in env_vars:
            val = os.environ.get(var)
            if val is not None:
                # Wrap in single quotes and escape embedded single quotes,
                # ensuring proxy URLs with special characters pass safely to bash.
                escaped_val = val.replace("'", "'\\''" )
                exports.append(f"export {var}='{escaped_val}'")

        env_export = "; ".join(exports) + "; "

        # Add diagnostic prefix to trace terminal startup in the spawned window
        debug_prefix = f"echo '[AFS Terminal] Starting: {inner_cmd}'; echo '[AFS Terminal] setup.bash: {setup_bash}'; "

        full_bash_cmd = f"{env_export}{debug_prefix}source {setup_bash}; {inner_cmd}"

        if terminal_mode == "xterm":
            # `-hold` keeps the window open after node termination so error messages remain readable.
            return ['xterm', '-geometry', geometry, '-fa', 'Monospace', '-fs', '10',
                    '-hold', '-e', 'bash', '-c', full_bash_cmd]
        else:
            # Default: gnome-terminal.
            # Trailing `exec bash` is equivalent to xterm `-hold`, leaving an active shell.
            return ['gnome-terminal', '--geometry', geometry, '--', 'bash', '-c',
                    f"{full_bash_cmd}; exec bash"]


def launch_nodes(context, *args, **kwargs):
    """Assemble ExecuteProcess actions to launch all nodes in grid-positioned terminals.

    Called by OpaqueFunction at launch execution time rather than description construction,
    so `config` and `initial_role` are already known.
    """
    config = kwargs.get('config', {})
    initial_role = kwargs.get('initial_role')
    roles = config.get('family_config', [])
    terminal_mode = config.get('terminal_mode', 'gnome-terminal')

    actions = []

    # Grid layout configuration
    GRID_W = 500  # pixels (narrow to minimize overlap)
    GRID_H = 500  # pixels
    TERM_GEOM = "58x18"  # character dimensions (reduced from 75x18)

    # Family member nodes (top row: father, mother, daughter)
    # One process per role, differentiated by --role.
    for i, role in enumerate(roles):
        x_pos = i * GRID_W
        y_pos = 0
        geometry = f"{TERM_GEOM}+{x_pos}+{y_pos}"

        inner_cmd = f"ros2 run afs_family afs_family_member --role {role}"
        # Only the elected leader gets --initiate; others wait for their turn,
        # ensuring exactly one member starts the conversation.
        if role == initial_role:
            inner_cmd += " --initiate"

        cmd = TerminalCommandBuilder.build(terminal_mode, geometry, inner_cmd)
        actions.append(ExecuteProcess(cmd=cmd, output='screen'))

    # Therapist (bottom-left: col 0, row 1)
    therapist_geometry = f"{TERM_GEOM}+0+{GRID_H}"
    therapist_cmd = TerminalCommandBuilder.build(terminal_mode, therapist_geometry,
                                        "ros2 run afs_therapist afs_therapist")
    actions.append(ExecuteProcess(cmd=therapist_cmd, output='screen'))

    # STT (bottom-center: col 1, row 1)
    stt_geometry = f"{TERM_GEOM}+{GRID_W}+{GRID_H}"
    stt_cmd = TerminalCommandBuilder.build(terminal_mode, stt_geometry,
                                  "ros2 run afs_stt afs_stt")
    actions.append(ExecuteProcess(cmd=stt_cmd, output='screen'))

    # Plot viewer (bottom-right: col 2, row 1 - GUI only)
    # Opens its own Tkinter window, so no terminal wrapper is needed;
    # geometry is passed directly as command-line arguments.
    viewer_geometry = f"500x500+{2*GRID_W}+{GRID_H}"
    actions.append(ExecuteProcess(cmd=['ros2', 'run', 'afs_viewer', 'afs_viewer', '--geometry', viewer_geometry], output='log'))

    # Infrastructure nodes (background)
    # No terminal windows: no interactive input needed; logs route to this console.
    actions.append(ExecuteProcess(cmd=['ros2', 'run', 'afs_tts', 'afs_tts'], output='screen'))
    # Launching afs_toio triggers Bluetooth scanning; skip launching if cubes are disabled.
    if config.get('toio_move', 0) != 0:
        actions.append(ExecuteProcess(cmd=['ros2', 'run', 'afs_toio', 'afs_toio'], output='screen'))
    else:
        print("[afs_launch] toio_move is 0. Skipping afs_toio node launch (Bluetooth not required).")
    actions.append(ExecuteProcess(cmd=['ros2', 'run', 'afs_therapist', 'afs_evaluator'], output='screen'))
    actions.append(ExecuteProcess(cmd=['ros2', 'run', 'afs_therapist', 'afs_optimizer'], output='screen'))
    actions.append(ExecuteProcess(cmd=['ros2', 'run', 'afs_family', 'afs_generator'], output='screen'))
    actions.append(ExecuteProcess(cmd=['ros2', 'run', 'afs_family', 'afs_member_evaluator'], output='screen'))
    actions.append(ExecuteProcess(cmd=['ros2', 'run', 'afs_family', 'afs_document_processor'], output='screen'))

    return actions


def generate_launch_description():
    """ROS2 launch entry point. Executes pre-launch cleanup -> leader election -> launch all nodes."""
    # --- Pre-launch cleanup ---
    SessionArchiver.archive("Startup")

    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)

    roles = config.get('family_config', [])
    theme = config.get('theme', '')

    # --- Pre-launch cleanup ---
    # Prior runs terminated with Ctrl-C may leave orphaned nodes or audio players
    # running and publishing/playing on topics. Clean up before launching anew.
    print("[afs_launch] Cleaning up previous AFS processes and audio tasks...")
    # Terminate potentially orphaned ffplay or spd-say audio processes.
    # Target specific patterns to avoid catching this launch process itself.
    subprocess.run(["pkill", "-f", "ffplay"], stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-f", "spd-say"], stderr=subprocess.DEVNULL)
    # Terminate AFS nodes by name to avoid matching afs_bringup or afs_all.launch.py
    afs_nodes = ["afs_family_member", "afs_generator", "afs_member_evaluator", "afs_document_processor", "afs_therapist", "afs_evaluator", "afs_optimizer", "afs_stt", "afs_tts", "afs_toio", "afs_viewer"]
    for node in afs_nodes:
        subprocess.run(["pkill", "-f", node], stderr=subprocess.DEVNULL)
    # --------------------------

    initial_role = LeaderElection.determine(roles, theme)

    # OpaqueFunction defers execution of `launch_nodes` until the launch system
    # actually runs, allowing resolved config and elected leader to be passed through.
    return LaunchDescription([
        OpaqueFunction(function=launch_nodes, kwargs={'config': config, 'initial_role': initial_role})
    ])
