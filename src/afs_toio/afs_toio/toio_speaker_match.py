#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
toio_speaker_match node.

A manual calibration tool run once during setup. Scans for toio cubes via BLE,
pairs them in discovery order with family roles (from config.json's family_config)
and available audio speakers, and writes the resulting mapping back into config.json's
`toio_speaker_match` key.
Not launched by `launch all` (afs_all.launch.py) — executed standalone via
`ros2 run afs_toio toio_speaker_match`.

System TTS announcements and BLE address resolution are handled by
`speaker_matching_backend` (independent of rclpy); this file manages the ROS2 node
lifecycle and executes each calibration step sequentially (communication layer + control flow).

Why this tool is necessary: Cubes and Bluetooth speakers are separate hardware pools,
and neither contains metadata indicating which physical speaker sits atop which cube.
This tool pairs them by index and triggers physical self-identification — the speaker
announces its role while its paired cube rotates in place — allowing the human operator
to visually and audibly verify and place the correct hardware.

This node does not publish or subscribe to topics: its sole output is updating the
`toio_speaker_match` section in config.json, which afs_toio and afs_tts read at startup.

Note: This file is executed directly as `python3 src/afs_toio/afs_toio/toio_speaker_match.py`
or via ros2 run.
"""

import rclpy
from rclpy.node import Node
import asyncio
import os
import json
from toio import BLEScanner, ToioCoreCube

from afs_toio.speaker_matching_backend import SystemTTS, best_addr_name, fallback_query_name_addr

# Config file path
CONFIG_PATH = os.path.expanduser('~/afs/src/afs_config/config/config.json')


class ToioSpeakerMatcher(Node):
    """ROS2 node that performs automated pairing between toio cubes and audio speakers."""

    def __init__(self):
        super().__init__('toio_speaker_matcher')
        self.tts = SystemTTS(self.get_logger())
        self.roles = self.load_roles()
        self.get_logger().info("Starting automatic Toio and Speaker pairing.")

    def load_roles(self):
        """Read `family_config` from config.json. The order dictates pairing sequence."""
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            return cfg.get('family_config', [])
        except (FileNotFoundError, json.JSONDecodeError) as e:
            self.get_logger().error(f"Failed to load config '{CONFIG_PATH}': {e}")
            return []

    async def run_automatic_matching(self):
        """Workflow: BLE scan -> assign speakers -> verify connection (audio + rotate) -> write config.json.

        Roles, cubes, and speakers are paired strictly by index: the i-th role receives
        the i-th discovered cube and the i-th prioritized speaker. If list lengths differ,
        pairs are created up to the length of the shortest list, leaving remaining devices unpaired.
        """
        # 1. Robust device discovery scan
        self.get_logger().info("Confirming Toio cube order via initial scan...")
        try:
            self.get_logger().info(f"Scanning for {len(self.roles)} Toio cubes...")
            devs = await BLEScanner.scan(num=len(self.roles))
            if not devs:
                self.get_logger().error("No Toio cubes found. Aborting.")
                return

            toio_info_list = []
            for d in devs:
                addr, name = best_addr_name(d)
                # On some platforms, scan advertisements omit device names. Connecting over
                # GATT is slower, so only query if the scan genuinely omitted the name.
                if not name or name == "UNKNOWN":
                    self.get_logger().info(f"Retrieving name for {addr} via connection...")
                    addr2, name2 = await fallback_query_name_addr(d)
                    addr = addr2 or addr
                    name = name2 or "UNKNOWN"
                toio_info_list.append({"address": addr, "name": name, "device": d})
                self.get_logger().info(f"Found Toio: {name} ({addr})")

            toio_addresses = [info["address"] for info in toio_info_list]
            self.get_logger().info(f"Confirmed Toio pairing order: {toio_addresses}")
        except Exception as e:
            self.get_logger().error(f"Error during initial scan: {e}")
            return

        # Prioritize Bluetooth speakers, followed by internal outputs (see SystemTTS)
        available_sinks = self.tts.sinks_in_priority.copy()
        if not available_sinks:
            self.get_logger().error("No available speakers found. Aborting.")
            return

        self.get_logger().info(f"Available speakers: {available_sinks}")

        match_list = []
        # Create pairs up to the shortest list length among roles, cubes, and sinks
        num_pairs = min(len(self.roles), len(toio_info_list), len(available_sinks))

        for i in range(num_pairs):
            role = self.roles[i]
            toio_info = toio_info_list[i]
            toio_address = toio_info["address"]
            toio_name = toio_info["name"]
            sink = available_sinks[i]

            self.get_logger().info(f"--- Pairing Start: Role='{role}', Toio='{toio_name} ({toio_address})' ---")

            connected_cube = None
            # Retry up to 3 times: initial BLE connection to cubes frequently fails
            for attempt in range(3):
                try:
                    self.get_logger().info(f"Connecting to Toio {toio_name} ({toio_address})... (Attempt {attempt + 1}/3)")
                    cube = ToioCoreCube(toio_info["device"].interface)
                    await cube.connect()
                    connected_cube = cube
                    self.get_logger().info(f"Successfully connected to {toio_name}.")
                    break
                except Exception as e:
                    self.get_logger().warn(f"Connection attempt failed: {e}")
                    if attempt < 2:
                        await asyncio.sleep(1)

            if connected_cube:
                try:
                    # Physical identification: speaker announces role name and corresponding
                    # cube rotates in place so the operator can position the speaker correctly.
                    speech = f"Speaker {role}. Please place this speaker on the rotating Toio."
                    self.tts.speak(speech, sink)
                    await connected_cube.api.motor.motor_control(50, -50, 1000)

                    match_info = {
                        "role": role,
                        "toio_id": toio_address,
                        "toio_name": toio_name,
                        "speaker_id": sink
                    }
                    match_list.append(match_info)
                    self.get_logger().info(f"Pair confirmed: {role} <-> {toio_name} ({toio_address}) <-> {sink}")

                finally:
                    # Disconnect before advancing: keeping multiple BLE connections open
                    # simultaneously destabilizes subsequent connection attempts.
                    await connected_cube.disconnect()
                    self.get_logger().info(f"Disconnected {toio_name}.")
                    await asyncio.sleep(1)  # Allow BLE driver stack to settle
            else:
                self.get_logger().error(f"Failed to connect to {toio_name}. Skipping this pair.")

        # 3. Save results
        # If no pairs were formed, do not write to prevent wiping previous valid configurations.
        if not match_list:
            self.get_logger().error("No pairs were formed. Config file not updated.")
            return

        try:
            # Read-modify-write via 'r+': update `toio_speaker_match` while preserving other settings
            with open(CONFIG_PATH, 'r+', encoding='utf-8') as f:
                config_data = json.load(f)
                config_data['toio_speaker_match'] = match_list
                f.seek(0)
                json.dump(config_data, f, ensure_ascii=False, indent=2)
                # Truncate file in case new JSON content is shorter than previous file size
                f.truncate()
            self.get_logger().info(f"Pairing results written to {CONFIG_PATH}")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            self.get_logger().error(f"Failed to update config '{CONFIG_PATH}': {e}")

        self.get_logger().info("Pairing process completed.")


def main(args=None):
    rclpy.init(args=args)
    node = ToioSpeakerMatcher()
    try:
        asyncio.run(node.run_automatic_matching())
    except KeyboardInterrupt:
        node.get_logger().info("Interrupted by user.")
    except Exception as e:
        node.get_logger().fatal(f"Unexpected error during execution: {e}")
    finally:
        node.get_logger().info("Shutting down.")
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
