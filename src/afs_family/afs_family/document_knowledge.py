#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Logic layer for loading clinical reference documents (FACES tables, reference
examples) and building behavior descriptions.

Handles loading Markdown tables and building the behavioral guideline text for
a given (x, y) value. Does not depend on ROS2 communication. Called from the
afs_document_processor node.
"""

import os


class DocumentKnowledgeBase:
    """Logic class that holds the FACES behavior description tables and reference example (Glass Castle analysis)."""

    def __init__(self, logger, faces_tables_file: str, glass_castle_file: str):
        self.logger = logger
        self.faces_tables_file = faces_tables_file
        self.glass_castle_file = glass_castle_file
        self.faces_tables = self._load_faces_tables()
        self.glass_castle_data = self._load_glass_castle_analysis()

    def _load_faces_tables(self) -> dict:
        """Load the clinical behavior description tables from `faces_iv_tables.md` (Markdown table format)."""
        tables = {"cohesion": {}, "flexibility": {}}
        if not os.path.exists(self.faces_tables_file):
            self.logger.error(f"FACES tables file not found: {self.faces_tables_file}")
            return tables

        current_section = None
        try:
            with open(self.faces_tables_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in lines:
                    line = line.strip()
                    if "## 1. Cohesion" in line:
                        current_section = "cohesion"
                    elif "## 2. Flexibility" in line:
                        current_section = "flexibility"
                    if line.startswith("|"):
                        parts = [p.strip() for p in line.split("|")]
                        if parts and not parts[0]:
                            parts.pop(0)
                        if parts and not parts[-1]:
                            parts.pop()
                        if len(parts) < 2 or "---" in parts[0] or "Level" in parts[1] or "Low" in parts[1]:
                            continue

                        cat = parts[0].replace("**", "")
                        if current_section:
                            tables[current_section][cat] = parts[1:]
        except Exception as e:
            self.logger.error(f"Failed to load FACES tables: {e}")
        return tables

    def _load_glass_castle_analysis(self) -> str:
        """Load the reference example (Glass Castle analysis) text."""
        try:
            if not os.path.exists(self.glass_castle_file):
                return ""
            with open(self.glass_castle_file, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            self.logger.error(f"Failed to load Glass Castle analysis: {e}")
            return ""

    def build_behavioral_guidelines(self, x: float, y: float) -> str:
        """Build cohesion and flexibility behavioral guidelines for circumplex coordinates (x, y)."""

        def get_column_index(score):
            """Each guideline table has 5 columns (very low to very high); pick the column for this score."""
            if score <= 15:
                return 0
            if score <= 35:
                return 1
            if score <= 65:
                return 2
            if score <= 85:
                return 3
            return 4

        cohesion_column = get_column_index(x)
        flexibility_column = get_column_index(y)

        description = "# Behavioral Guidelines\n"
        description += "## Detailed Cohesion Guidelines\n"
        for category, values in self.faces_tables.get("cohesion", {}).items():
            if cohesion_column < len(values):
                description += f"- {category}: {values[cohesion_column]}\n"

        description += "\n## Detailed Flexibility Guidelines\n"
        for category, values in self.faces_tables.get("flexibility", {}).items():
            if flexibility_column < len(values):
                description += f"- {category}: {values[flexibility_column]}\n"

        return description
