#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FACES-IV Validation Web App.
Used by external family psychology researchers to validate LLM-generated FACES-IV assessments.

This file handles only the Flask web application itself (routing). ROS2-independent
logic, such as FACES-IV item definitions and parsing conversation history or
evaluation CSVs, is delegated to `faces_data` (logic layer).

Purpose of this app: It is not part of the active robot runtime. It reads archives
left by completed sessions, presents evaluators with the exact dialogue observed by
the LLM, and records the human evaluator's own FACES-IV scores alongside the LLM's scores.
Comparing these two columns constitutes the validation of the LLM assessments.

Routes:
  GET  /                   Single-page UI
  GET  /api/archives       Archive folders containing both required files
  GET  /api/archive/<name> Single archive: conversation, LLM scores, item list
  POST /api/save_csv       Write human evaluator scores alongside LLM scores
  POST /api/open_file      Open saved file in the OS file manager

Runs on port 5001 and has no runtime ROS2 dependencies, allowing it to be used
on separate machines from the experimental robot setup.
"""

import os
import platform
import subprocess
from datetime import datetime
from flask import Flask, jsonify, render_template, request

from afs_evaluator_app.faces_data import (
    FACES_ITEMS,
    SUBSCALES,
    get_subscale,
    parse_conversation_history,
    parse_evaluation_csv,
    parse_conversation_line,
)

# -- Paths -------------------------------------------------------------------
from ament_index_python.packages import get_package_share_directory

try:
    PACKAGE_SHARE_DIR = get_package_share_directory('afs_evaluator_app')
    STATIC_DIR = os.path.join(PACKAGE_SHARE_DIR, 'static')
    TEMPLATE_DIR = os.path.join(PACKAGE_SHARE_DIR, 'templates')
    # Determine default archive directory: look relative to source if running in place,
    # or use standard package path if installed.
    # Robust fallback strategy:
    # 1. Check relative to user home directory (safest in this environment)
    # 2. Check relative to package location (source tree)

    # First attempt user home configuration (most likely pattern in this setup)
    POSSIBLE_ARCHIVE_DIR = os.path.join(os.path.expanduser("~"), "afs", "src", "afs_database", "archive")
    if os.path.isdir(POSSIBLE_ARCHIVE_DIR):
        ARCHIVE_DIR = POSSIBLE_ARCHIVE_DIR
    else:
        # Fallback relative to this file (development mode)
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        ARCHIVE_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "..", "..", "afs_database", "archive"))

except Exception as e:
    # Fallback when running directly with Python outside a ROS2 environment
    print(f"[WARN] Could not resolve ROS2 package share directory: {e}")
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    # Expected standard source layout: src/afs_evaluator_app/afs_evaluator_app/app.py
    # meaning static is src/afs_evaluator_app/static -> ../../static
    PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, "..", ".."))
    STATIC_DIR = os.path.join(PROJECT_ROOT, "static")
    TEMPLATE_DIR = os.path.join(PROJECT_ROOT, "templates")
    ARCHIVE_DIR = os.path.join(PROJECT_ROOT, "..", "afs_database", "archive")

app = Flask(__name__, static_folder=STATIC_DIR, template_folder=TEMPLATE_DIR)


# -- Routes ------------------------------------------------------------------

@app.route("/")
def index():
    """Serve single-page UI. All subsequent data is retrieved via /api routes."""
    return render_template("index.html")


@app.route("/api/archives")
def list_archives():
    """List available archive folders."""
    if not os.path.isdir(ARCHIVE_DIR):
        return jsonify({"archives": []})

    archives = []
    # reverse=True: folder names are timestamps, so newest sessions appear first
    # (what researchers typically want to inspect).
    for name in sorted(os.listdir(ARCHIVE_DIR), reverse=True):
        path = os.path.join(ARCHIVE_DIR, name)
        if os.path.isdir(path):
            # Verify required files exist. Sessions aborted early may have plots
            # but lack evaluation scores; exclude them since they cannot be validated.
            has_conv = os.path.isfile(os.path.join(path, "conversation_history.txt"))
            has_eval = os.path.isfile(os.path.join(path, "evaluation_history.csv"))
            if has_conv and has_eval:
                # Parse timestamp for display ("20260213_184820" -> "2026-02-13 18:48:20")
                try:
                    dt = datetime.strptime(name, "%Y%m%d_%H%M%S")
                    display = dt.strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    display = name
                archives.append({"name": name, "display": display})
    return jsonify({"archives": archives})


@app.route("/api/archive/<name>")
def get_archive(name: str):
    """Load and return parsed archive data.

    Returns all data needed by the UI in a single response: session-split
    conversations, LLM scores, 62 item texts, and subscale groupings.
    """
    archive_path = os.path.join(ARCHIVE_DIR, name)
    if not os.path.isdir(archive_path):
        return jsonify({"error": "Archive not found"}), 404

    conv_path = os.path.join(archive_path, "conversation_history.txt")
    eval_path = os.path.join(archive_path, "evaluation_history.csv")

    # Parse conversation history
    with open(conv_path, "r", encoding="utf-8") as f:
        conv_text = f.read()
    sessions_conv = parse_conversation_history(conv_text)

    # Parse into structured lines
    sessions_structured = {}
    for sid, lines in sessions_conv.items():
        sessions_structured[sid] = [parse_conversation_line(l) for l in lines]

    # Parse evaluation scores
    sessions_eval = parse_evaluation_csv(eval_path)

    # Assemble sorted session list. Use union of both sources since a session
    # may have conversation without scores or vice-versa. Sort numerically by
    # integer after 'S' so S10 does not sort before S2.
    all_sessions = sorted(set(list(sessions_conv.keys()) + list(sessions_eval.keys())),
                          key=lambda s: int(s[1:]))

    # Assemble FACES item list
    items = [{"num": k, "text": v, "subscale": get_subscale(k)} for k, v in FACES_ITEMS.items()]

    return jsonify({
        "archive_name": name,
        "sessions": all_sessions,
        "conversations": sessions_structured,
        "evaluations": sessions_eval,
        "items": items,
        "subscales": SUBSCALES,
    })


@app.route("/api/save_csv", methods=["POST"])
def save_csv():
    """Save evaluator scores as CSV.

    Writes one file per session directly into the archive directory so human
    evaluations remain co-located with their corresponding session.
    Each row contains item text, per-member LLM scores, LLM mean, and human
    evaluator scores for direct side-by-side comparison.
    """
    import csv

    data = request.json
    archive_name = data.get("archive_name")
    # File naming element distinguishing files from different evaluators
    evaluator_name = data.get("evaluator_name", "anonymous")
    results = data.get("results", {})  # {session_id: {item_num: score}}

    if not archive_name:
        return jsonify({"error": "Missing archive_name"}), 400

    archive_path = os.path.join(ARCHIVE_DIR, archive_name)
    if not os.path.isdir(archive_path):
        return jsonify({"error": "Archive not found"}), 404

    # Load robot scores
    eval_path = os.path.join(archive_path, "evaluation_history.csv")
    sessions_eval = parse_evaluation_csv(eval_path)

    saved_files = []

    for session_id, evaluator_scores in results.items():
        robot_data = sessions_eval.get(session_id, {"members": {}, "mean": {}})
        members = robot_data["members"]
        mean_scores = robot_data["mean"]
        member_names = sorted(members.keys())

        # Assemble CSV
        filename = f"human_evaluation_{session_id}_{evaluator_name}.csv"
        filepath = os.path.join(archive_path, filename)

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            header = ["Item", "Item_Text", "Subscale"] + member_names + ["robot_mean", "evaluator"]
            writer.writerow(header)

            # Always write all 62 items including unanswered ones to maintain
            # consistent schema for automated downstream analysis.
            for item_num in range(1, 63):
                item_key = str(item_num)
                row = [
                    item_num,
                    FACES_ITEMS.get(item_num, ""),
                    get_subscale(item_num),
                ]
                for member in member_names:
                    score = members.get(member, {}).get(item_key, "")
                    row.append(score)
                row.append(round(float(mean_scores.get(item_key, 0)), 2))
                row.append(evaluator_scores.get(item_key, ""))
                writer.writerow(row)

        saved_files.append(filepath)

    return jsonify({"saved_files": saved_files})


@app.route("/api/open_file", methods=["POST"])
def open_file():
    """Open file in native OS file explorer with target selected.

    Convenience feature for evaluators: archive paths are long, so the app
    reveals the file directly. Branching handles OS-specific command differences.
    """
    data = request.json
    filepath = data.get("filepath", "")

    if not os.path.isfile(filepath):
        return jsonify({"error": "File not found"}), 404

    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(["explorer", "/select,", filepath.replace("/", "\\")])
        elif system == "Darwin":
            subprocess.Popen(["open", "-R", filepath])
        else:  # Linux
            parent_dir = os.path.dirname(filepath)
            subprocess.Popen(["xdg-open", parent_dir])
        return jsonify({"status": "ok", "system": system})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -- Main --------------------------------------------------------------------
def main():
    """Entry point for `ros2 run afs_evaluator_app afs_evaluator_app`.

    host="0.0.0.0" allows access from external machines on the same local network
    in addition to localhost.
    """
    print(f"[INFO] Archive directory: {ARCHIVE_DIR}")
    print(f"[INFO] Starting FACES-IV Validation App on http://localhost:5001")
    app.run(host="0.0.0.0", port=5001, debug=True)

if __name__ == "__main__":
    main()
