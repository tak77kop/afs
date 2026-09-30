#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Logic layer for toio cube control.

Handles extracting BLE device information, coordinate clamping, and loading
role-to-toio mappings from configuration files. Does not depend on ROS2 communication.
Called from the afs_toio node.
"""

import json
from typing import Tuple, Optional


def best_addr_name(dev) -> Tuple[Optional[str], Optional[str]]:
    """Extract MAC address from a BLE scan result object as reliably as possible.

    Different versions of bleak and platforms store the address under varying attributes,
    so multiple candidate properties are inspected. The second return value (name) is
    kept as None here to maintain signature compatibility with the matching function
    in `speaker_matching_backend`.
    """
    addr, name = None, None
    # Later matches take precedence; more specific attributes override general ones
    for a in ("address", "mac", "addr"):
        if hasattr(dev, a):
            addr = getattr(dev, a) or addr
    if hasattr(dev, "interface"):
        if hasattr(dev.interface, "address"):
            addr = dev.interface.address or addr
    return addr, name


def clamp(v, lo, hi):
    """Clamp value v within range [lo, hi]."""
    return lo if v < lo else hi if v > hi else v


def load_role_toio_map(config_file: str, logger=None) -> dict:
    """Load mapping of lowercase role name to toio MAC address from config file.

    Reads the `toio_speaker_match` section populated by `toio_speaker_match.py`.
    Returns an empty dictionary on any error, allowing the node to start up in cubeless
    mode without failing.
    """
    role_toio_map = {}
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
        for item in config_data.get('toio_speaker_match', []):
            role, toio_id = item.get('role'), item.get('toio_id')
            if role and toio_id:
                role_toio_map[role.lower()] = toio_id
    except Exception as e:
        if logger:
            logger.error(f"Failed to load config: {e}")
    return role_toio_map
