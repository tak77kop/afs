#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AFS Toio Node.

Handles BLE connections to toio cubes, publishes cube positions, and executes
movement scripts. Simple pure logic such as BLE address extraction, clamping values,
and loading configuration is delegated to `toio_backend` (independent of rclpy),
while this file handles ROS2 publish/subscribe (communication layer) and managing
background BLE control tasks.

`_scan_connect_toio` / `handle_user_intervention` / `handle_command_sequence`
are state transitions where "BLE communication -> state update -> publish" are
tightly coupled, so per CLAUDE.md principles, they remain directly within the Node.

Beginner's Guide:
  1. At startup, `_scan_connect_toio` discovers and connects to configured toio
     cubes via BLE, then starts `_periodic_position_publish` (publishing each
     cube's (x, y) coordinates to `afs_toio_position` every 0.5s).
  2. `execute_script_cb` receives movement commands from family members
     (`afs_toio_move_script`) and executes them via `handle_command_sequence`.
  3. `user_intervention_cb` responds to user speech start/end events, stopping
     movements and orienting cubes toward the user via `handle_user_intervention`.

ROS2 Interface:
  Publish   `afs_toio_status` (String, TRANSIENT_LOCAL) - Initially "initializing",
            then "toios_ready" along with connected cube positions.
  Publish   `afs_toio_position` (String) - Cube positions {x, y, angle} at 2Hz.
  Publish   `afs_toio_move_finished` (String) - Signals completion of movement so
            members know they can advance to the next step.
  Subscribe `afs_toio_move_script` (String) - CSV-formatted movement commands.
  Subscribe `afs_user_intervention_toio_move` (String) - User speech start/end notifications.

This node is started only when `toio_move` in config.json is non-zero,
as launching it inevitably initiates Bluetooth scanning.

Note: `handle_command_sequence` currently parses commands and reports completion;
actual movement execution is pending implementation (see comments in code).
"""

import os
import sys
import json
import asyncio
import threading
import signal
import csv
import io
import math
import subprocess
import re
from typing import Tuple, Optional

import rclpy
from rclpy.node import Node
from rclpy.executors import MultiThreadedExecutor
from std_msgs.msg import String
from rclpy.qos import QoSProfile, QoSReliabilityPolicy, QoSHistoryPolicy, QoSDurabilityPolicy
from ament_index_python.packages import get_package_share_directory

from toio import BLEScanner, ToioCoreCube, MovementType, Speed, SpeedChangeType, TargetPosition, CubeLocation, Point, RotationOption, WriteMode, PositionId, PositionIdMissed, IdInformation
from toio.cube.api.indicator import IndicatorParam, Color

from afs_toio.toio_backend import best_addr_name, clamp, load_role_toio_map

# Configuration
try:
    SAVE_DIR = os.path.join(get_package_share_directory('afs_config'), 'config')
except Exception:
    SAVE_DIR = os.path.expanduser("~/afs/src/afs_config/config")

CONFIG_FILE = os.path.join(SAVE_DIR, 'config.json')

# Constants
SCAN_TIMEOUT     = 8     # Seconds before giving up on BLE scanning
MAX_SPEED        = 30    # Motor speed ceiling, kept low enough that cubes do not slide off mat
CONTROL_INTERVAL = 0.1   # Seconds between movement control steps
# Usable area of the toio mat (mat coordinate system).
# Restricted inside the physical printed mat boundary to prevent cubes from driving off the edge and losing position tracking.
MAT_X_MIN, MAT_X_MAX = 130, 360
MAT_Y_MIN, MAT_Y_MAX = 180, 320
COLLISION_THRESHOLD = 35         # Distance below which two cubes are considered colliding
AVOIDANCE_LOOP_THRESHOLD = 35    # Clearance threshold used when maneuvering around other cubes


class AFSToio(Node):
    """ROS2 node for toio cube connection, position publishing, and movement control (communication layer)."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__('afs_toio')
        self.loop = loop
        self.cube_map: dict[str, ToioCoreCube] = {}      # role -> connected cube object
        self.role_toio_map: dict[str, str] = {}          # role -> toio MAC address (from config)
        self.latest_positions: dict[str, dict] = {}      # role -> {x, y, angle}
        self.current_move_task: Optional[asyncio.Task] = None  # Retained so ongoing movements can be cancelled
        self.movement_blocked_by_intervention = False    # True while the user is actively speaking

        # Estimated position where the user is seated (mat coordinates).
        # When the user speaks, cubes turn toward this point so the robot appears
        # to look at the speaker.
        self.USER_X = 250
        self.USER_Y = 320

        # --- Load configuration (logic layer) ---
        self.role_toio_map = load_role_toio_map(CONFIG_FILE, logger=self.get_logger())

        # --- ROS2 communication setup ---
        self.create_subscription(String, 'afs_toio_move_script', self.execute_script_cb, 10)
        self.create_subscription(String, 'afs_user_intervention_toio_move', self.user_intervention_cb, 10)
        qos_pl = QoSProfile(reliability=QoSReliabilityPolicy.RELIABLE, history=QoSHistoryPolicy.KEEP_LAST, depth=1, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL)
        self.status_publisher = self.create_publisher(String, 'afs_toio_status', qos_pl)
        self.move_finished_pub = self.create_publisher(String, 'afs_toio_move_finished', 10)
        self.position_publisher = self.create_publisher(String, 'afs_toio_position', 10)

        # Signal that cubes are not ready yet. This prevents downstream nodes
        # (like TTS) from mistaking a stale "toios_ready" from a previous run
        # as the current state.
        self.status_publisher.publish(String(data=json.dumps({"status": "initializing"})))
        # BLE scanning is asynchronous; delegate to the event loop thread
        # without blocking the node constructor.
        if self.role_toio_map:
            self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self._scan_connect_toio()))

        self.get_logger().info('AFS Toio Node Started.')

    async def _scan_connect_toio(self):
        """Scan and connect to configured toio cubes via BLE, then publish readiness.

        Missing or failed cubes are simply skipped:
        the session continues regardless of how many cubes successfully connect.
        """
        expected_macs = set(self.role_toio_map.values())
        try:
            devs = await asyncio.wait_for(BLEScanner.scan(num=len(expected_macs)), timeout=SCAN_TIMEOUT)
        except asyncio.TimeoutError:
            # Non-fatal: proceed with empty list (i.e. no cubes connected)
            devs = []

        for role, toio_id in self.role_toio_map.items():
            # Match by MAC address, as discovery order is non-deterministic.
            dev = next((d for d in devs if (best_addr_name(d)[0] or "").upper() == toio_id.upper()), None)
            if not dev: continue
            try:
                cube = ToioCoreCube(dev.interface)
                await cube.connect()
                self.cube_map[role] = cube
                # Position updates arrive as BLE notifications rather than polling.
                # `r=role` binds role at definition time; without it, all handlers
                # would report the last role in the loop.
                await cube.api.id_information.register_notification_handler(
                    lambda payload, handler_info, r=role: self._position_notification_handler(payload, r)
                )
                # Spin slightly in place so the operator can visually confirm
                # that this specific cube successfully connected.
                await cube.api.motor.motor_control(50, -50, 500)
            except Exception as e:
                self.get_logger().error(f"Failed to connect {role}: {e}")

        # Always publish status even if zero cubes connected, so waiting nodes do not hang.
        self.status_publisher.publish(String(data=json.dumps({"status": "toios_ready", "positions": self.latest_positions})))
        if self.cube_map:
            self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self._periodic_position_publish()))

    def _position_notification_handler(self, payload: bytearray, role: str):
        """BLE notification handler: store the latest position for this cube.

        Only buffers the position; publishing is handled periodically by
        `_periodic_position_publish` on a timer, as raw BLE notifications
        are too frequent to forward directly to ROS2 topics.
        """
        id_info = IdInformation.is_my_data(payload)
        # Off-mat coordinates produce PositionIdMissed instead; ignore to retain
        # the last known valid position.
        if isinstance(id_info, PositionId):
            self.latest_positions[role] = {'x': id_info.center.point.x, 'y': id_info.center.point.y, 'angle': id_info.center.angle}

    async def _periodic_position_publish(self):
        """Publish the latest positions of all toio cubes every 0.5 seconds.

        Fixed 2Hz rate keeps ROS2 topic throughput predictable regardless of BLE notification frequency.
        """
        while rclpy.ok():
            if self.latest_positions:
                self.position_publisher.publish(String(data=json.dumps(self.latest_positions)))
            await asyncio.sleep(0.5)

    def execute_script_cb(self, msg: String):
        """`afs_toio_move_script`: execute a movement command from a family member.

        Message format: 1-line CSV: <role>,<recipient>,<type>,<command>.
        Only rows with type == 'move' are processed.
        """
        data = msg.data.strip()
        # Same marker as TTS node: responses to the user take priority over normal conversation
        # and are permitted to move even during an intervention block.
        is_leader = data.startswith("[LEADER_RESPONSE]")
        if is_leader: data = data[len("[LEADER_RESPONSE]"):]

        # Discard normal movement commands while user speaks so cubes remain stationary
        # facing the user.
        if self.movement_blocked_by_intervention and not is_leader: return
        if is_leader: self.movement_blocked_by_intervention = False

        try:
            reader = csv.reader(io.StringIO(data), skipinitialspace=True)
            parts = next(reader)
            if len(parts) >= 4 and parts[2].lower() == 'move':
                role, recipient, command_str = parts[0].lower(), parts[1].lower(), parts[3]
                # BLE control is async; delegate to event loop thread
                self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self.handle_command_sequence(command_str, role, recipient)))
        except Exception: pass

    def user_intervention_cb(self, msg: String):
        """`afs_user_intervention_toio_move`: respond to user speech start/end events.

        Message format: {"role": "<role>", "state": "start"|"end"}.
        """
        try:
            data = json.loads(msg.data)
            role, state = data.get("role").lower(), data.get("state")
            if state == "start":
                self.movement_blocked_by_intervention = True
                # Cancel ongoing move immediately so cube halts the instant user begins speaking.
                if self.current_move_task: self.current_move_task.cancel()
            elif state == "end":
                self.movement_blocked_by_intervention = False
            self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self.handle_user_intervention(role, state)))
        except Exception: pass

    async def handle_user_intervention(self, role: str, state: str):
        """Turn the member's toio toward the user or turn off indicator LEDs on intervention start/end.

        Solid red = "listening to you". On "end", turn off LEDs on ALL cubes regardless
        of which one was lit.
        """
        cube = self.cube_map.get(role)
        # Disconnections can happen frequently; verify before every BLE call
        if not cube or not cube.is_connect(): return
        if state == "start":
            # duration_ms=0 means keep LED on continuously until explicitly turned off
            await cube.api.indicator.turn_on(IndicatorParam(duration_ms=0, color=Color(r=255, g=0, b=0)))
            # Simplified orientation toward the user
            await cube.api.motor.motor_control_target(timeout=2, target=TargetPosition(cube_location=CubeLocation(point=Point(x=self.USER_X, y=self.USER_Y), angle=270)))
        elif state == "end":
            # Color (0,0,0) is how the API turns off the indicator LED
            for c in self.cube_map.values():
                await c.api.indicator.turn_on(IndicatorParam(duration_ms=0, color=Color(r=0, g=0, b=0)))

    async def handle_command_sequence(self, command_sequence_str: str, role: str, recipient: str):
        """Execute a sequence of movement commands and report movement completion.

        Commands are separated by ';'.

        Actual motor control is pending implementation (see comments below),
        but `afs_toio_move_finished` is guaranteed to be published in `finally`.
        This is critical: family members wait for this message before proceeding,
        so omitting it (even if cancelled by an intervention) causes dialogue to stall.
        """
        # Stored so user_intervention_cb can cancel this move task
        self.current_move_task = asyncio.current_task()
        try:
            if command_sequence_str.lower() != 'none':
                # Simplified command execution
                commands = command_sequence_str.split(';')
                for cmd in commands:
                    if 'motor_control_target' in cmd:
                        # Dummy parse - real implementation would use regex-based parser
                        pass
        finally:
            self.move_finished_pub.publish(String(data=json.dumps({"role": role, "recipient": recipient})))
            self.current_move_task = None


def main():
    """Entry point for `ros2 run afs_toio afs_toio`.

    Dual-world architecture matching the TTS node: ROS2 callbacks on this thread,
    asyncio event loop driving BLE on a background daemon thread.
    """
    rclpy.init()
    loop = asyncio.get_event_loop()
    node = AFSToio(loop)
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    t = threading.Thread(target=lambda: loop.run_forever(), daemon=True)
    t.start()
    try: executor.spin()
    except KeyboardInterrupt: pass
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
