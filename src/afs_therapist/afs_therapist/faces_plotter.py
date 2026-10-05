#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Logic layer for visualizing and logging the FACES IV circumplex model.

A plain Python class with no ROS2 dependency. Handles plot image generation using
matplotlib, loading the clinical behavior description tables, logging each session
to CSV, and appending targets to the history file. Called from the afs_therapist node.
"""

import csv
import datetime
import math
import os


class FacesReportGenerator:
    """Logic class that handles FACES IV visualization and logging (no rclpy dependency)."""

    # --- Scaling anchors -----------------------------------------------
    # Anchor A: grid_dim == 1 (a single, full-size panel).
    #   Matches the look of the original single-plot `generate_plot`.
    # Anchor B: grid_dim == 2 (a 2x2 grid, e.g. father/mother/daughter/family).
    #   Matches the already-tuned `_generate_member_plots` look.
    # For grid_dim >= 3 the values are linearly extrapolated from A -> B and
    # then clamped to a sane floor so text never becomes unreadably tiny.
    _SCALE_ANCHOR_1 = {
        "panel_w": 12.0, "panel_h": 12.0,
        "title": 16, "axis_label": 14, "cat_label": 10, "tick_label": 8,
        "labelpad_x": 30, "labelpad_y": 40,
        "marker_size": 16, "box_fontsize": 10, "legend_fontsize": 12,
    }
    _SCALE_ANCHOR_2 = {
        "panel_w": 6.0, "panel_h": 5.5,
        "title": 14, "axis_label": 10, "cat_label": 8, "tick_label": 6,
        "labelpad_x": 20, "labelpad_y": 25,
        "marker_size": 13, "box_fontsize": 7, "legend_fontsize": 10,
    }
    _SCALE_FLOOR = {
        "panel_w": 4.0, "panel_h": 3.8,
        "title": 10, "axis_label": 8, "cat_label": 6, "tick_label": 5,
        "labelpad_x": 10, "labelpad_y": 12,
        "marker_size": 8, "box_fontsize": 6, "legend_fontsize": 8,
    }

    def __init__(self, logger, db_dir: str):
        self.logger = logger
        self.db_dir = db_dir
        self.faces_tables = self._load_faces_tables()

    # ------------------------------------------------------------------
    # Dynamic scaling helper
    # ------------------------------------------------------------------
    def _get_scale_style(self, ncols: int, nrows: int) -> dict:
        """Compute figure/font sizing for the given grid shape.

        grid_dim == 1  -> looks like the original big single-panel plot.
        grid_dim == 2  -> looks like the existing tuned 2x2 layout.
        grid_dim >= 3  -> linearly extrapolated further down, then clamped.
        """
        grid_dim = max(ncols, nrows, 1)
        t = grid_dim - 1  # 0 at anchor A, 1 at anchor B, >1 beyond B

        style = {}
        for key, a_val in self._SCALE_ANCHOR_1.items():
            b_val = self._SCALE_ANCHOR_2[key]
            floor = self._SCALE_FLOOR[key]
            raw = a_val + t * (b_val - a_val)
            style[key] = max(raw, floor)
        return style

    @staticmethod
    def _get_cmap(name: str, n: int):
        """Get an `n`-color discretized colormap in a way that works across
        matplotlib versions.

        `matplotlib.cm.get_cmap` was deprecated in 3.7 and removed in later
        releases, so we try the modern APIs first and fall back to the old
        one only if needed.
        """
        import matplotlib
        try:
            # Modern API (matplotlib >= 3.7)
            return matplotlib.colormaps[name].resampled(n)
        except Exception:
            pass
        try:
            # Slightly older modern API
            import matplotlib.pyplot as plt
            return plt.get_cmap(name, n)
        except Exception:
            pass
        # Legacy fallback for very old matplotlib versions
        import matplotlib.cm as cm
        return cm.get_cmap(name, n)

    def _load_faces_tables(self) -> dict:
        """Load the clinical behavior description tables from `faces_iv_tables.md` (Markdown table format)."""
        tables = {"cohesion": {}, "flexibility": {}, "communication": {}}
        path = os.path.join(self.db_dir, "faces_iv_tables.md")
        if not os.path.exists(path):
            return tables

        current_section = None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if "## 1. Cohesion" in line:
                        current_section = "cohesion"
                    elif "## 2. Flexibility" in line:
                        current_section = "flexibility"
                    elif "## 3. Communication" in line:
                        current_section = "communication"

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

    def get_detailed_behavior(self, x: float, y: float) -> str:
        """Return the clinical behavior description (cohesion/flexibility) text for coordinates (x, y)."""

        def get_col_idx(val):
            if val <= 15:
                return 0
            if val <= 35:
                return 1
            if val <= 65:
                return 2
            if val <= 85:
                return 3
            return 4

        c_idx = get_col_idx(x)
        f_idx = get_col_idx(y)

        lines = []
        if "cohesion" in self.faces_tables:
            for cat, rows in self.faces_tables["cohesion"].items():
                if len(rows) > c_idx:
                    lines.append(f"- {cat}: {rows[c_idx]}")
        if "flexibility" in self.faces_tables:
            for cat, rows in self.faces_tables["flexibility"].items():
                if len(rows) > f_idx:
                    lines.append(f"- {cat}: {rows[f_idx]}")
        return "\n".join(lines)

    def get_visual_coord(self, score: float) -> float:
        """Convert a FACES IV percentile value (0-100) into a coordinate (0-5) for the 5x5 grid drawing.

        Within each segment (bounded by 0/16/36/66/86), the value is spread smoothly from min to mid to max.
        """
        gap = 0.04
        ranges = [(0, 0, 8, 15), (1, 16, 25, 35), (2, 36, 50, 65), (3, 66, 75, 85), (4, 86, 95, 100)]
        target_range = None
        for r in ranges:
            if r[1] <= score <= r[3]:
                target_range = r
                break
        if target_range is None:
            target_range = ranges[0] if score < 0 else ranges[4]
        idx, min_s, mid_s, max_s = target_range
        vis_start, vis_mid, vis_end = idx + gap, idx + 0.5, idx + 1.0 - gap
        if score <= mid_s:
            if mid_s == min_s:
                return vis_start
            return vis_start + (score - min_s) / (mid_s - min_s) * (vis_mid - vis_start)
        else:
            if max_s == mid_s:
                return vis_end
            return vis_mid + (score - mid_s) / (max_s - mid_s) * (vis_end - vis_mid)

    def _draw_circumplex_panel(self, ax, x, y, title, marker='o', color='blue',
                                coh_ratio=None, flex_ratio=None, tot_ratio=None,
                                trajectory=None, tx=None, ty=None,
                                show_trajectory=False, style=None):
        """Draw one FACES IV circumplex panel.

        `style` is the dict returned by `_get_scale_style`; when omitted, the
        2x2-grid anchor values are used as a safe default.
        """
        import matplotlib.patches as patches

        if style is None:
            style = self._SCALE_ANCHOR_2

        ax.set_xlim(0, 5)
        ax.set_ylim(0, 5)

        # Color the 5x5 grid background (corners = extreme type gray, center 3x3 = balanced type white, rest = mid gray)
        gap = 0.04
        rect_size = 1.0 - gap * 2
        for i in range(5):
            for j in range(5):
                if (i == 0 and j == 0) or (i == 0 and j == 4) or (i == 4 and j == 0) or (i == 4 and j == 4):
                    color_bg = '#A9A9A9'
                elif 1 <= i <= 3 and 1 <= j <= 3:
                    color_bg = 'white'
                else:
                    color_bg = '#D3D3D3'
                rect = patches.Rectangle(
                    (i + gap, j + gap),
                    rect_size,
                    rect_size,
                    facecolor=color_bg,
                    edgecolor='black',
                    linewidth=1
                )
                ax.add_patch(rect)

        labels_x = [
            (0.5, "Disengaged"),
            (1.5, "Somewhat\nConnected"),
            (2.5, "Connected"),
            (3.5, "Very\nConnected"),
            (4.5, "Enmeshed")
        ]
        for pos, text in labels_x:
            ax.text(pos, -0.15, text, ha='center', va='top',
                    fontsize=style["cat_label"], fontweight='bold')
        ax.set_xlabel("COHESION", fontsize=style["axis_label"], fontweight='bold',
                      labelpad=style["labelpad_x"])

        labels_y = [
            (0.5, "Rigid"),
            (1.5, "Somewhat\nFlexible"),
            (2.5, "Flexible"),
            (3.5, "Very\nFlexible"),
            (4.5, "Chaotic")
        ]
        for pos, text in labels_y:
            ax.text(-0.15, pos, text, ha='right', va='center',
                    rotation=0, fontsize=style["cat_label"], fontweight='bold')
        ax.set_ylabel("FLEXIBILITY", fontsize=style["axis_label"], fontweight='bold',
                      labelpad=style["labelpad_y"])

        tick_vals = [0, 8, 15, 16, 25, 35, 36, 50, 65, 66, 75, 85, 86, 95, 100]
        tick_pos = [self.get_visual_coord(v) for v in tick_vals]
        ax.set_xticks(tick_pos)
        ax.set_xticklabels([str(v) for v in tick_vals], fontsize=style["tick_label"])
        ax.set_yticks(tick_pos)
        ax.set_yticklabels([str(v) for v in tick_vals], fontsize=style["tick_label"])

        ax.set_title(title, fontsize=style["title"], fontweight='bold', pad=12)

        # Individual / family current result
        if x is not None and y is not None:
            v_x = self.get_visual_coord(x)
            v_y = self.get_visual_coord(y)

            ax.plot(
                v_x, v_y,
                marker=marker,
                color=color,
                markersize=style["marker_size"],
                markeredgecolor='black',
                markeredgewidth=1.2,
                zorder=10
            )

            if coh_ratio is not None and flex_ratio is not None and tot_ratio is not None:
                label_text = (
                    f"Result: ({x:.1f}, {y:.1f})\n"
                    f"Coh Ratio: {coh_ratio:.2f}\n"
                    f"Flex Ratio: {flex_ratio:.2f}\n"
                    f"Tot Ratio: {tot_ratio:.2f}"
                )
                ax.text(
                    1.02, 0.98,
                    label_text,
                    transform=ax.transAxes,
                    color='black',
                    fontsize=style["box_fontsize"],
                    fontweight='bold',
                    va='top',
                    ha='left',
                    bbox=dict(
                        facecolor='white',
                        alpha=0.9,
                        edgecolor='black'
                    )
                )

        # Family target / trajectory
        if show_trajectory and trajectory:
            results_coords = []
            for step in trajectory:
                rx, ry = step.get("result_x"), step.get("result_y")
                # S0 stores the initial point as a target; later steps store
                # evaluated points as result_x/result_y.
                if step.get("step") == "S0" and (rx is None or ry is None):
                    rx = step.get("target_x")
                    ry = step.get("target_y")
                if rx is not None and ry is not None:
                    results_coords.append((self.get_visual_coord(rx), self.get_visual_coord(ry)))

            if len(results_coords) > 1:
                for i in range(len(results_coords) - 1):
                    p1 = results_coords[i]
                    p2 = results_coords[i + 1]
                    ax.annotate(
                        "",
                        xy=p2,
                        xytext=p1,
                        arrowprops=dict(
                            arrowstyle="->",
                            linestyle=':',
                            color='gray',
                            lw=1.8,
                            alpha=0.7
                        ),
                        zorder=5
                    )
                    ax.plot(
                        p1[0], p1[1],
                        marker='o',
                        color='blue',
                        markersize=style["marker_size"],
                        alpha=0.5,
                        markeredgecolor='black',
                            markeredgewidth=1.2,
                        zorder=6
                    )

            if x is not None and y is not None and tx is not None and ty is not None:
                v_x = self.get_visual_coord(x)
                v_y = self.get_visual_coord(y)
                v_tx = self.get_visual_coord(tx)
                v_ty = self.get_visual_coord(ty)

                ax.annotate(
                    "",
                    xy=(v_tx, v_ty),
                    xytext=(v_x, v_y),
                    arrowprops=dict(
                        arrowstyle="->",
                        color='red',
                        lw=2.5,
                        alpha=0.8
                    ),
                    zorder=9
                )

    # Target marker for the current family result (元ファイル仕様の赤丸プロット)
            if show_trajectory and tx is not None and ty is not None:
                v_tx = self.get_visual_coord(tx)
                v_ty = self.get_visual_coord(ty)
                ax.plot(
                    v_tx, v_ty,
                    marker='o',
                    color='red',
                    markersize=13,
                    markeredgecolor='black',
                    zorder=11
                )

    def _generate_member_plots(self, x, y, coh_ratio, flex_ratio, tot_ratio,
                                trajectory=None, tx=None, ty=None,
                                member_scores=None):
            """Generate the 2x2 FACES IV plot for father, mother, daughter, and family."""
            try:
                import matplotlib.pyplot as plt
                from matplotlib.lines import Line2D
            except Exception:
                return None

            fig, axes = plt.subplots(2, 2, figsize=(16, 14))
            axes = axes.flatten()

            member_info = [
                ("father", "Father", 'o', 'blue'),
                ("mother", "Mother", 'o', 'blue'),
                ("daughter", "Daughter", 'o', 'blue'),
            ]

            for idx, (role_key, title, marker, color) in enumerate(member_info):
                ax = axes[idx]
                member = member_scores.get(role_key, {}) if member_scores else {}

                member_x = member.get("x")
                member_y = member.get("y")

                if member_x is None or member_y is None:
                    ax.set_title(f"{title} - No Data", fontsize=14, fontweight='bold')
                    self._draw_circumplex_panel(
                        ax,
                        None,
                        None,
                        title,
                        marker=marker,
                        color=color
                    )
                else:
                    self._draw_circumplex_panel(
                        ax,
                        member_x,
                        member_y,
                        title,
                        marker=marker,
                        color=color
                    )

            # 家族全体（Family Overall）パネル：元の単一プロットと同じ「青丸 (blue)」を使用
            self._draw_circumplex_panel(
                axes[3],
                x,
                y,
                "Family Overall",
                marker='o',
                color='blue',
                coh_ratio=coh_ratio,
                flex_ratio=flex_ratio,
                tot_ratio=tot_ratio,
                trajectory=trajectory,
                tx=tx,
                ty=ty,
                show_trajectory=True
            )

            # 凡例（Legend）も元ファイル仕様（青丸＝Result / 赤丸＝Target）に合わせて更新
            legend_elements = [
                Line2D([0], [0], marker='o', color='w', label='Father',
                    markerfacecolor='#1f77b4', markersize=9, markeredgecolor='black'),
                Line2D([0], [0], marker='s', color='w', label='Mother',
                    markerfacecolor='#d62728', markersize=9, markeredgecolor='black'),
                Line2D([0], [0], marker='^', color='w', label='Daughter',
                    markerfacecolor='#2ca02c', markersize=9, markeredgecolor='black'),
                Line2D([0], [0], marker='o', color='w', label='Family Overall',
                    markerfacecolor='blue', markersize=9, markeredgecolor='black'),
                Line2D([0], [0], marker='o', color='w', label='Next Target',
                    markerfacecolor='red', markersize=9, markeredgecolor='black'),
            ]

#            fig.legend(
#                handles=legend_elements,
#                loc='lower center',
#                ncol=5,
#                bbox_to_anchor=(0.5, 0.01),
#                fontsize=10
#            )

            fig.suptitle(
                "FACES IV Circumplex Model - Family Members",
                fontsize=18,
                fontweight='bold',
                y=0.99
            )

            fig.subplots_adjust(
                left=0.08,
                right=0.88,
                top=0.94,
                bottom=0.08,
                wspace=0.45,
                hspace=0.35
            )

            bg_save_path = os.path.join(self.db_dir, "evaluation_plot_bg.png")
            plt.savefig(bg_save_path, dpi=150)

            save_path = os.path.join(self.db_dir, "evaluation_plot.png")
            plt.savefig(save_path, dpi=150)

            plt.close()
            return save_path

    def generate_plot(self, x, y, coh_ratio, flex_ratio, tot_ratio,
                       trajectory=None, tx=None, ty=None, member_scores=None):
        """Generate the FACES IV circumplex model plot image dynamically and return the saved file path.

        The figure/font sizing is scaled automatically based on how many panels
        end up being drawn: a single "Family Overall" panel gets the large,
        easy-to-read single-plot look, while multi-member grids (2x2, 3x3, ...)
        scale down proportionally so nothing overlaps.
        """
        try:
            import matplotlib.pyplot as plt
            from matplotlib.lines import Line2D
        except Exception:
            return None

        # 1. Build the panel list dynamically.
        panels = []
        legend_elements = []

        if member_scores:
            for idx, (role, score) in enumerate(member_scores.items()):
                display_title = str(role).replace('_', ' ').title()
                marker = 'o'
                color = 'blue'
                m_x = score.get("x") if isinstance(score, dict) else None
                m_y = score.get("y") if isinstance(score, dict) else None
                has_data = m_x is not None and m_y is not None

                panels.append({
                    "type": "member",
                    "title": display_title if has_data else f"{display_title} - No Data",
                    "marker": marker,
                    "color": color,
                    "x": m_x,
                    "y": m_y
                })

        # Family Overall panel always goes last.
        panels.append({
            "type": "overall",
            "title": "Family Overall",
            "marker": 'o',
            "color": 'blue',
            "x": x,
            "y": y
        })

        # 2. Work out the grid shape, then the scaling for that shape.
        num_panels = len(panels)
        ncols = max(1, math.ceil(math.sqrt(num_panels)))
        nrows = math.ceil(num_panels / ncols)
        style = self._get_scale_style(ncols, nrows)

        # Build the legend using scaled marker sizes now that `style` exists.
        for p in panels:
            if p["type"] == "member":
                legend_elements.append(
                    Line2D([0], [0], marker=p["marker"], color='w', label=p["title"],
                           markerfacecolor=p["color"], markersize=style["marker_size"] * 0.7,
                           markeredgecolor='black')
                )
        legend_elements.append(
            Line2D([0], [0], marker='D', color='w', label='Family Overall',
                   markerfacecolor='#6a3d9a', markersize=style["marker_size"] * 0.7,
                   markeredgecolor='black')
        )
        if tx is not None and ty is not None:
            legend_elements.append(
                Line2D([0], [0], marker='o', color='red', label='Next Target',
                       markerfacecolor='red', markersize=style["marker_size"] * 0.9,
                       markeredgecolor='black')
            )

        fig, axes = plt.subplots(
            nrows, ncols,
            figsize=(style["panel_w"] * ncols, style["panel_h"] * nrows)
        )

        if num_panels == 1:
            axes_flat = [axes]
        else:
            axes_flat = axes.flatten()

        # 3. Draw each panel using the shared, scaled style.
        for idx, panel in enumerate(panels):
            ax = axes_flat[idx]

            if panel["type"] == "member":
                # ------------------------------------------------------
                # 各家族メンバー自身の過去履歴を作成
                # Family Overallと同じtrajectory仕様で描画する
                # ------------------------------------------------------
                role_key = str(panel["title"]).lower().replace(" ", "_")

                member_trajectory = []

                if trajectory:
                    for step in trajectory:
                        step_member_scores = step.get("member_scores", {})

                        # role名の揺れに対応
                        member_score = None

                        if role_key in step_member_scores:
                            member_score = step_member_scores.get(role_key)
                        else:
                            # Father / Mother / Daughter などのtitleから検索
                            for role, score in step_member_scores.items():
                                normalized_role = (
                                    str(role)
                                    .lower()
                                    .replace(" ", "_")
                                    .replace("-", "_")
                                )

                                if normalized_role == role_key:
                                    member_score = score
                                    break

                        if isinstance(member_score, dict):
                            member_x = member_score.get("x")
                            member_y = member_score.get("y")

                            if member_x is not None and member_y is not None:
                                member_trajectory.append({
                                    "result_x": member_x,
                                    "result_y": member_y
                                })

                self._draw_circumplex_panel(
                    ax,
                    panel["x"],
                    panel["y"],
                    panel["title"],
                    marker=panel["marker"],
                    color=panel["color"],
                    trajectory=member_trajectory,
                    show_trajectory=True,
                    style=style
                )

            else:
                # ------------------------------------------------------
                # Family Overall
                # 従来どおりFamily Overall自身のtrajectoryを使用
                # Next TargetもFamily Overallだけに表示
                # ------------------------------------------------------
                self._draw_circumplex_panel(
                    ax,
                    panel["x"],
                    panel["y"],
                    panel["title"],
                    marker=panel["marker"],
                    color=panel["color"],
                    coh_ratio=coh_ratio,
                    flex_ratio=flex_ratio,
                    tot_ratio=tot_ratio,
                    trajectory=trajectory,
                    tx=tx,
                    ty=ty,
                    show_trajectory=True,
                    style=style
                )

        # Remove unused axes (e.g. 5 panels needed but a 2x3=6 grid was created).
        for idx in range(num_panels, len(axes_flat)):
            fig.delaxes(axes_flat[idx])

        # 4. Legend and overall layout, all scaled to match.
#        fig.legend(
#            handles=legend_elements,
#            loc='lower center',
#            ncol=min(len(legend_elements), 6),
#            bbox_to_anchor=(0.5, 0.01),
#            fontsize=style["legend_fontsize"]
#        )

        fig.suptitle(
            "FACES IV Circumplex Model",
            fontsize=style["title"] + (4 if num_panels > 1 else 2),
            fontweight='bold', y=0.99
        )

        fig.subplots_adjust(
            left=0.08, right=0.88 if num_panels > 1 else 0.80,
            top=0.92 if num_panels > 1 else 0.90,
            bottom=0.10 if num_panels > 1 else 0.12,
            wspace=0.35, hspace=0.35
        )

        # 5. Save images (marker-less background for the blink effect, then the real one).
        bg_save_path = os.path.join(self.db_dir, "evaluation_plot_bg.png")
        plt.savefig(bg_save_path, dpi=150)

        save_path = os.path.join(self.db_dir, "evaluation_plot.png")
        plt.savefig(save_path, dpi=150)

        plt.close()
        return save_path

    def log_evaluation_to_csv(self, step_id, member_results, mean_ratings, current_pcts, x, y, tx, ty,
                               coh_ratio, flex_ratio, tot_ratio, target_scores):
        """Append one step's evaluation result to `evaluation_history.csv`."""
        csv_file = os.path.join(self.db_dir, "evaluation_history.csv")
        file_exists = os.path.exists(csv_file)

        try:
            with open(csv_file, 'a', encoding='utf-8', newline='') as f:
                writer = csv.writer(f)
                if not file_exists:
                    header = ["Timestamp", "StepID", "Cohesion_Dim", "Flexibility_Dim", "Target_X", "Target_Y", "Coh_Ratio", "Flex_Ratio", "Tot_Ratio"]
                    for k in target_scores.keys():
                        header.append(f"Target_{k}")
                    header.extend(["Member_Raw_Scores_JSON", "Mean_Ratings_JSON"])
                    writer.writerow(header)

                import json
                row = [datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), step_id, round(x, 2), round(y, 2), round(tx, 2), round(ty, 2), round(coh_ratio, 2), round(flex_ratio, 2), round(tot_ratio, 2)]
                for k in target_scores.keys():
                    row.append(round(target_scores[k], 2))
                row.append(json.dumps(member_results, ensure_ascii=False))
                row.append(json.dumps(mean_ratings, ensure_ascii=False))
                writer.writerow(row)
        except Exception as e:
            self.logger.error(f"Failed to log evaluation to CSV: {e}")

    def update_history_with_targets(self, history_file: str, scores: dict, tx: float, ty: float):
        """Append the next session's therapeutic target to the conversation history file (used as LLM context)."""
        update = f"\n[THERAPIST_STALL_SESSION_ANALYSIS]\n"
        update += f"Determined Therapeutic Target for Next Session: ({tx:.1f}, {ty:.1f})\n"
        update += "Targeted FACES IV Percentile Scores (Steering towards center):\n"
        for k, v in scores.items():
            update += f"- {k}: {int(v)}\n"
        update += "(Strategic Objective: Aggressively maneuver family towards the 'Balanced' zone (50, 50))\n"
        with open(history_file, "a", encoding="utf-8") as f:
            f.write(update)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
