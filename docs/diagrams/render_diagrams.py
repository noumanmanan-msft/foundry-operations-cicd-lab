"""
Renders two architecture diagrams as PNG files:
  1. cicd-pipeline.png   — CI/CD promotion pipeline
  2. azure-resources.png — Per-environment Azure resource topology
"""

import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

OUT_DIR = os.path.dirname(__file__)


# ─── colour palette ───────────────────────────────────────────────────────────
C_GITHUB   = "#24292E"
C_GATE     = "#107C10"
C_CI       = "#5C2D91"
C_AZURE    = "#0078D4"
C_HEADING  = "#1B1B1B"
C_BG       = "#FAFAFA"
C_WHITE    = "#FFFFFF"
C_BORDER   = "#CCCCCC"
C_MI       = "#7719AA"
C_PROJ     = "#0063B1"
C_KV       = "#E07B00"
C_ACR      = "#107C10"
C_ACA      = "#00BCF2"
C_SRCH     = "#E93B81"
C_OBS      = "#5B2D91"
C_DEV_BG   = "#EBF3FB"
C_QA_BG    = "#FFF8E1"
C_PROD_BG  = "#FBE9E7"
C_ARROW    = "#555555"


def box(ax, x, y, w, h, label, color, textcolor="#FFFFFF", fontsize=8.5,
        radius=0.06, alpha=1.0, style="round,pad=0"):
    fancy = FancyBboxPatch(
        (x - w / 2, y - h / 2), w, h,
        boxstyle=f"round,pad={radius}",
        facecolor=color, edgecolor="#FFFFFF",
        linewidth=1.2, alpha=alpha, zorder=3
    )
    ax.add_patch(fancy)
    ax.text(x, y, label, ha="center", va="center", color=textcolor,
            fontsize=fontsize, fontweight="bold", zorder=4,
            multialignment="center", linespacing=1.4)


def subgraph_rect(ax, x, y, w, h, title, bg_color, border_color="#AAAAAA"):
    rect = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.04",
        facecolor=bg_color, edgecolor=border_color,
        linewidth=1.5, zorder=1
    )
    ax.add_patch(rect)
    ax.text(x + w / 2, y + h - 0.18, title, ha="center", va="top",
            fontsize=9, fontweight="bold", color=C_HEADING, zorder=5)


def arrow(ax, x1, y1, x2, y2, label="", color=C_ARROW, lw=1.5):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle="-|>", color=color,
                                lw=lw, mutation_scale=14), zorder=2)
    if label:
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        ax.text(mx + 0.05, my, label, fontsize=7, color="#333333",
                va="center", zorder=6)


# ══════════════════════════════════════════════════════════════════════════════
#  DIAGRAM 1 — CI/CD PROMOTION PIPELINE
# ══════════════════════════════════════════════════════════════════════════════
def draw_cicd():
    fig, ax = plt.subplots(figsize=(14, 18))
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 18)
    ax.axis("off")
    fig.patch.set_facecolor(C_BG)

    title = "Foundry Operations — CI/CD Promotion Pipeline"
    ax.text(7, 17.6, title, ha="center", va="center", fontsize=14,
            fontweight="bold", color=C_HEADING)

    # ── Developer ──
    box(ax, 7, 16.8, 2.8, 0.55, "Developer", C_GITHUB, fontsize=9)

    # ── GitHub Repository ──
    subgraph_rect(ax, 3.6, 14.5, 6.8, 1.9, "GitHub Repository", "#EFEFEF", C_GITHUB)
    box(ax, 5.0, 15.5, 2.2, 0.55, "develop branch", C_GITHUB, fontsize=8.5)
    box(ax, 7.2, 15.5, 1.8, 0.55, "Pull Request", C_GITHUB, fontsize=8.5)
    box(ax, 9.5, 15.5, 2.0, 0.55, "main branch", C_GITHUB, fontsize=8.5)

    # ── PR Quality Gates ──
    subgraph_rect(ax, 2.0, 12.3, 10.0, 1.8, "PR Quality Gates", "#E8F5E9", C_GATE)
    box(ax, 5.5, 13.2, 4.2, 0.65,
        "secret-scan.yml\nNo credentials in repo", C_GATE, fontsize=8)
    box(ax, 10.5, 13.2, 3.8, 0.65,
        "validate-foundry-assets.yml\nSchema · refs · quality gates", C_GATE, fontsize=8)

    # ── CI Dev ──
    subgraph_rect(ax, 3.0, 10.2, 8.0, 1.7,
                  "Auto-Deploy to Dev  (on push to main)", "#EDE7F6", C_CI)
    box(ax, 5.5, 11.0, 3.5, 0.6, "deploy-dev-foundry.yml", C_CI, fontsize=8.5)
    box(ax, 9.5, 11.0, 3.5, 0.6,
        "render_foundry_bundle.py\ndev-bundle.json", C_CI, fontsize=8)

    # ── CI QA ──
    subgraph_rect(ax, 3.0, 8.1, 8.0, 1.7,
                  "Promote to QA  ─  Approval Gate", "#FFF3E0", "#E07B00")
    box(ax, 5.5, 8.9, 3.5, 0.6, "promote-foundry-qa.yml", "#E07B00", fontsize=8.5)
    box(ax, 9.5, 8.9, 3.5, 0.6, "qa-bundle.json artifact", "#E07B00", fontsize=8.5)

    # ── CI Prod ──
    subgraph_rect(ax, 3.0, 6.0, 8.0, 1.7,
                  "Promote to Prod  ─  Approval Gate", "#FBE9E7", "#C62828")
    box(ax, 5.5, 6.8, 3.5, 0.6, "promote-foundry-prod.yml", "#C62828", fontsize=8.5)
    box(ax, 9.5, 6.8, 3.5, 0.6, "prod-bundle.json artifact", "#C62828", fontsize=8.5)

    # ── Azure Dev ──
    subgraph_rect(ax, 1.5, 3.7, 11.0, 1.8,
                  "Azure Dev  ·  rg-dev-foundry-operation-lab-eastus2",
                  C_DEV_BG, C_AZURE)
    box(ax, 5.2, 4.6, 4.5, 0.65,
        "AI Foundry + Project\ngpt-4.1-mini · incident-triage-agent", C_AZURE, fontsize=8)
    box(ax, 10.2, 4.6, 4.0, 0.65,
        "Key Vault · ACR · AI Search\nContainer Apps · App Insights", C_AZURE, fontsize=8)

    # ── Azure QA ──
    subgraph_rect(ax, 1.5, 1.6, 11.0, 1.8,
                  "Azure QA  ·  rg-qa-foundry-operation-lab-eastus2",
                  C_QA_BG, "#F9A825")
    box(ax, 5.2, 2.5, 4.5, 0.65,
        "AI Foundry + Project\ngpt-4.1-mini · incident-triage-agent", "#F9A825",
        textcolor="#000", fontsize=8)
    box(ax, 10.2, 2.5, 4.0, 0.65,
        "Key Vault · ACR · AI Search\nContainer Apps · App Insights", "#F9A825",
        textcolor="#000", fontsize=8)

    # ── Azure Prod ──
    subgraph_rect(ax, 1.5, -0.5, 11.0, 1.8,
                  "Azure Prod  ·  rg-prod-foundry-operation-lab-eastus2",
                  C_PROD_BG, "#C62828")
    box(ax, 5.2, 0.4, 4.5, 0.65,
        "AI Foundry + Project\ngpt-4.1-mini · incident-triage-agent", "#C62828", fontsize=8)
    box(ax, 10.2, 0.4, 4.0, 0.65,
        "Key Vault · ACR · AI Search\nContainer Apps · App Insights", "#C62828", fontsize=8)

    # ── Arrows ──
    arrow(ax, 7, 16.52, 7, 16.38, color=C_GITHUB)   # dev → PR
    # developer to develop branch
    ax.annotate("", xy=(5.8, 15.77), xytext=(7, 16.52),
                arrowprops=dict(arrowstyle="-|>", color=C_GITHUB, lw=1.5, mutation_scale=12))
    ax.text(6.1, 16.25, "git push", fontsize=7, color="#333")
    # develop → PR
    arrow(ax, 6.1, 15.5, 6.2, 15.5, color=C_GITHUB)
    # PR → main
    arrow(ax, 8.1, 15.5, 8.5, 15.5, color=C_GITHUB)
    # main → CI Dev subgraph
    ax.annotate("", xy=(7, 11.87), xytext=(9.5, 15.22),
                arrowprops=dict(arrowstyle="-|>", color=C_CI, lw=1.5,
                                mutation_scale=12,
                                connectionstyle="arc3,rad=0.15"))
    ax.text(9.3, 13.8, "paths: foundry/**", fontsize=7, color=C_CI, rotation=-50)
    # PR gates arrow (PR → gates)
    arrow(ax, 7.2, 15.22, 7.0, 14.08, color=C_GATE)
    ax.text(7.1, 14.65, "checks", fontsize=7, color=C_GATE)
    # gates → main
    ax.annotate("", xy=(9.5, 15.22), xytext=(8.5, 14.08),
                arrowprops=dict(arrowstyle="-|>", color=C_GATE, lw=1.5, mutation_scale=12))
    ax.text(9.3, 14.7, "merge", fontsize=7, color=C_GATE)
    # CI Dev → Azure Dev
    arrow(ax, 7, 10.2, 7, 5.5, color=C_AZURE, lw=1.5)
    # Azure Dev → CI QA
    arrow(ax, 7, 3.7, 7, 9.8, color="#E07B00", lw=1.5)
    ax.text(7.1, 6.75, "workflow_run\ncompleted", fontsize=7, color="#E07B00")
    # CI QA → Azure QA
    arrow(ax, 7, 8.1, 7, 3.4, color="#F9A825", lw=1.5)
    # Azure QA → CI Prod
    arrow(ax, 7, 1.6, 7, 7.7, color="#C62828", lw=1.5)
    ax.text(7.1, 4.65, "workflow_run\ncompleted", fontsize=7, color="#C62828")
    # CI Prod → Azure Prod
    arrow(ax, 7, 6.0, 7, 1.3, color="#C62828", lw=1.5)

    # ── Legend ──
    legend_items = [
        mpatches.Patch(color=C_GITHUB, label="GitHub"),
        mpatches.Patch(color=C_GATE, label="Quality Gates"),
        mpatches.Patch(color=C_CI, label="CI Workflows"),
        mpatches.Patch(color=C_AZURE, label="Azure Dev"),
        mpatches.Patch(color="#F9A825", label="Azure QA"),
        mpatches.Patch(color="#C62828", label="Azure Prod"),
    ]
    ax.legend(handles=legend_items, loc="lower right", fontsize=8,
              framealpha=0.9, ncol=3)

    plt.tight_layout(pad=0.3)
    out = os.path.join(OUT_DIR, "cicd-pipeline.png")
    plt.savefig(out, dpi=150, bbox_inches="tight", facecolor=C_BG)
    plt.close()
    print(f"Saved: {out}")


# ══════════════════════════════════════════════════════════════════════════════
#  DIAGRAM 2 — AZURE RESOURCE ARCHITECTURE PER ENVIRONMENT
# ══════════════════════════════════════════════════════════════════════════════
def draw_azure_resources():
    fig, ax = plt.subplots(figsize=(22, 11))
    ax.set_xlim(0, 22)
    ax.set_ylim(0, 11)
    ax.axis("off")
    fig.patch.set_facecolor(C_BG)

    ax.text(11, 10.7, "Foundry Operations — Azure Resource Architecture (Per Environment)",
            ha="center", va="center", fontsize=14, fontweight="bold", color=C_HEADING)

    # Helper: draw one environment column
    def env_column(cx, bg, border, title, rg_label,
                   foundry_name, project_name, kv_name="Key Vault",
                   acr_name="Container Registry"):
        W = 6.2
        x0 = cx - W / 2
        subgraph_rect(ax, x0, 0.3, W, 10.0, title, bg, border)
        ax.text(cx, 9.9, rg_label, ha="center", va="center",
                fontsize=7.5, color="#444444", style="italic")

        bw, bh = 4.8, 0.72
        # row positions (y centres)
        rows = {
            "mi":    8.9,
            "aif":   7.9,
            "proj":  6.9,
            "kv":    5.9,
            "acr":   4.9,
            "aca":   3.9,
            "srch":  2.9,
            "obs":   1.8,
        }
        labels = {
            "mi":   f"Managed Identity\nAI Developer · Secrets User · AcrPull",
            "aif":  f"AI Foundry\n{foundry_name}",
            "proj": f"Foundry Project\n{project_name}",
            "kv":   f"Key Vault\n{kv_name}",
            "acr":  f"Container Registry\n{acr_name}",
            "aca":  "Container Apps Env\nincident-triage-agent",
            "srch": "AI Search\nops-knowledge-index · hybrid",
            "obs":  "App Insights + Log Analytics",
        }
        colors = {
            "mi": C_MI, "aif": C_AZURE, "proj": C_PROJ,
            "kv": C_KV, "acr": C_ACR, "aca": C_ACA,
            "srch": C_SRCH, "obs": C_OBS,
        }
        txt_colors = {
            "aca": "#000000",
        }
        for key, y in rows.items():
            tc = txt_colors.get(key, "#FFFFFF")
            box(ax, cx, y, bw, bh - 0.05, labels[key], colors[key],
                textcolor=tc, fontsize=8)

        # MI → Foundry, KV, ACR, Search  (vertical arrows on left/right sides)
        def va(y1, y2, side=0):
            xoff = cx - bw / 2 - 0.12 if side == 0 else cx + bw / 2 + 0.12
            arrow(ax, xoff, y1 - bh / 2, xoff, y2 + bh / 2,
                  color=C_MI, lw=1.2)

        # MI → AIF (centre)
        arrow(ax, cx, rows["mi"] - bh / 2 + 0.05,
              cx, rows["aif"] + bh / 2 - 0.05, color=C_AZURE)
        # AIF → PROJ
        arrow(ax, cx, rows["aif"] - bh / 2 + 0.05,
              cx, rows["proj"] + bh / 2 - 0.05, color=C_PROJ)
        # PROJ → ACA
        arrow(ax, cx, rows["proj"] - bh / 2 + 0.05,
              cx, rows["aca"] + bh / 2 - 0.05, color=C_ACA)
        # MI → KV (left side)
        va(rows["mi"], rows["kv"], side=0)
        # MI → ACR (left side, further)
        arrow(ax, cx - bw / 2 - 0.3, rows["mi"] - bh / 2,
              cx - bw / 2 - 0.3, rows["acr"] + bh / 2, color=C_ACR, lw=1.2)
        # MI → Search (right side)
        arrow(ax, cx + bw / 2 + 0.12, rows["mi"] - bh / 2,
              cx + bw / 2 + 0.12, rows["srch"] + bh / 2, color=C_SRCH, lw=1.2)

    # ── Dev column ──
    env_column(
        cx=3.8, bg=C_DEV_BG, border=C_AZURE,
        title="Azure Dev",
        rg_label="rg-dev-foundry-operation-lab-eastus2",
        foundry_name="aif-dev-foundry-operation-eastus2",
        project_name="default-dev-project",
        kv_name="kv-dev-foundry-oplabhjt",
        acr_name="acrdevfoundryoplab.azurecr.io",
    )

    # ── QA column ──
    env_column(
        cx=11.0, bg=C_QA_BG, border="#F9A825",
        title="Azure QA",
        rg_label="rg-qa-foundry-operation-lab-eastus2",
        foundry_name="aif-qa-foundry-operation-eastus2",
        project_name="default-qa-project",
        kv_name="kv-qa-foundry-oplab",
        acr_name="acrqafoundryoplab.azurecr.io",
    )

    # ── Prod column ──
    env_column(
        cx=18.2, bg=C_PROD_BG, border="#C62828",
        title="Azure Prod",
        rg_label="rg-prod-foundry-operation-lab-eastus2",
        foundry_name="aif-prod-foundry-operation-eastus2",
        project_name="default-prod-project",
        kv_name="kv-prod-foundry-oplab",
        acr_name="acrprodfoundryoplab.azurecr.io",
    )

    # ── Inter-env promotion arrows ──
    arrow(ax, 7.0, 5.5, 7.9, 5.5, label="auto-promote", color=C_AZURE, lw=2)
    arrow(ax, 14.1, 5.5, 15.0, 5.5, label="manual approve", color="#C62828", lw=2)

    # ── Legend ──
    legend_items = [
        mpatches.Patch(color=C_MI,    label="Managed Identity"),
        mpatches.Patch(color=C_AZURE, label="AI Foundry"),
        mpatches.Patch(color=C_PROJ,  label="Foundry Project"),
        mpatches.Patch(color=C_KV,    label="Key Vault"),
        mpatches.Patch(color=C_ACR,   label="Container Registry"),
        mpatches.Patch(color=C_ACA,   label="Container Apps Env", ),
        mpatches.Patch(color=C_SRCH,  label="AI Search"),
        mpatches.Patch(color=C_OBS,   label="App Insights / Log Analytics"),
    ]
    ax.legend(handles=legend_items, loc="lower center", fontsize=8.5,
              framealpha=0.9, ncol=4, bbox_to_anchor=(0.5, -0.01))

    plt.tight_layout(pad=0.3)
    out = os.path.join(OUT_DIR, "azure-resources.png")
    plt.savefig(out, dpi=150, bbox_inches="tight", facecolor=C_BG)
    plt.close()
    print(f"Saved: {out}")


if __name__ == "__main__":
    draw_cicd()
    draw_azure_resources()
    print("Done.")
