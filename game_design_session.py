#!/usr/bin/env python3
"""
game_design_session.py — Multi-agent iterative game design
Version 4: Real-time kill, input optimization, complexity guardrail

Usage:
  python3 ~/game_design_session.py --game backprop
  python3 ~/game_design_session.py --game backprop --loops 3
  python3 ~/game_design_session.py --game backprop --note "Focus on Layer cards"
  python3 ~/game_design_session.py --game backprop --advance
  python3 ~/game_design_session.py --game backprop --phase 1
  python3 ~/game_design_session.py --list
"""

import os
import sys
import json
import argparse
import re
import signal
import subprocess
from datetime import datetime
from pathlib import Path
import anthropic

# ── Usage monitoring ──────────────────────────────────────────────────────────
USAGE_FILE = Path.home() / ".cleo/workspace/usage.json"
FEEDS_FILE = Path.home() / ".cleo/workspace/feeds.json"
LOOPER_PRICE_INPUT_PER_M = 3.00
LOOPER_PRICE_OUTPUT_PER_M = 15.00
_current_session_cost: float = 0.0  # in-memory accumulator, reset each session

def _looper_usage_start(task_id):
    try:
        data = json.loads(USAGE_FILE.read_text()) if USAGE_FILE.exists() else {}
        data.setdefault("tasks", {})[task_id] = {
            "tokens_in": 0, "tokens_out": 0, "cost": 0.0,
            "task_type": "looper",
            "baseline_oauth_5h": 0.0,
            "baseline_api_cost": float(data.get("api", {}).get("session_cost", 0.0)),
            "killed": False,
        }
        USAGE_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"[usage] failed to snapshot baseline: {e}")

def _looper_usage_update(task_id, tokens):
    try:
        data = json.loads(USAGE_FILE.read_text()) if USAGE_FILE.exists() else {}
        cost = (tokens["input"] / 1_000_000 * LOOPER_PRICE_INPUT_PER_M) + \
               (tokens["output"] / 1_000_000 * LOOPER_PRICE_OUTPUT_PER_M)
        task_entry = data.get("tasks", {}).get(task_id, {})
        baseline = float(task_entry.get("baseline_api_cost", 0.0))
        api_delta = float(data.get("limits", {}).get("api_delta", 1.00))
        data.setdefault("tasks", {})[task_id] = {
            "tokens_in": tokens["input"],
            "tokens_out": tokens["output"],
            "cost": round(cost, 6),
            "task_type": task_entry.get("task_type", "looper"),
            "baseline_oauth_5h": 0.0,
            "baseline_api_cost": baseline,
            "killed": task_entry.get("killed", False),
        }
        USAGE_FILE.write_text(json.dumps(data, indent=2))
        if cost >= api_delta:
            return True, f"looper {task_id} spent ${cost:.4f} (limit: ${api_delta:.2f})"
        return False, ""
    except Exception as e:
        print(f"[usage] update failed: {e}")
        return False, ""

def _looper_should_kill(task_id):
    """Intra-loop kill check. Reads live cost written after each agent call."""
    try:
        data = json.loads(USAGE_FILE.read_text()) if USAGE_FILE.exists() else {}
        task = data.get("tasks", {}).get(task_id)
        if not task:
            return False, ""
        if task.get("killed", False):
            return True, f"looper {task_id} already marked killed"
        api_delta = float(data.get("limits", {}).get("api_delta", 1.00))
        if _current_session_cost >= api_delta:
            return True, f"looper {task_id} spent ${_current_session_cost:.4f} (limit: ${api_delta:.2f})"
        return False, ""
    except Exception:
        return False, ""

def _looper_mark_killed(task_id):
    """Write a kill flag file so summarizer skip check is reliable even after task cleanup."""
    try:
        flag_file = USAGE_FILE.parent / f".killed_{task_id}"
        flag_file.write_text("killed")
    except Exception:
        pass

def _looper_was_killed(task_id):
    """Check if this session was killed — reads flag file, not usage.json."""
    flag_file = USAGE_FILE.parent / f".killed_{task_id}"
    return flag_file.exists()

def _looper_clear_kill_flag(task_id):
    """Clean up kill flag file on normal completion."""
    try:
        flag_file = USAGE_FILE.parent / f".killed_{task_id}"
        flag_file.unlink(missing_ok=True)
    except Exception:
        pass

def _notify_match(feed_id, text):
    """Inject a feed and wake Match via SIGUSR1."""
    try:
        data = json.loads(FEEDS_FILE.read_text()) if FEEDS_FILE.exists() else {"feeds": {}, "unread": []}
        data["feeds"][feed_id] = {
            "group_id": None,
            "messages": [{"sender": "system", "text": text,
                          "timestamp": datetime.now().strftime("%H:%M")}],
            "unread_count": 1,
        }
        if feed_id not in data["unread"]:
            data["unread"].append(feed_id)
        FEEDS_FILE.write_text(json.dumps(data, indent=2))
        result = subprocess.run(["pgrep", "-f", "gateway.py"], capture_output=True, text=True)
        pid = result.stdout.strip().split('\n')[0]
        if pid:
            os.kill(int(pid), signal.SIGUSR1)
    except Exception as e:
        print(f"[notify] failed: {e}")

def _looper_notify_kill(task_id, reason):
    _notify_match(f"system:kill:{task_id}", f"🚨 looper {task_id} killed — {reason}")

def _looper_notify_loop_complete(task_id, game, session_num, loop_num,
                                  approved, cost, phase, loop_summary,
                                  advance_pending=False):
    status = "✅ approved" if approved else "⚠️ not approved"
    advance_note = (
        f"\n\n🔁 phase advancement recommended — reply 'advance {game}' to confirm"
        if advance_pending else ""
    )
    summary_short = (loop_summary[:400] + "...") if len(loop_summary or "") > 400 else (loop_summary or "no summary")
    text = (
        f"🧠 {game} session {session_num} loop {loop_num} done — {status}\n"
        f"phase {phase} | cost ${cost:.4f}\n\n"
        f"{summary_short}"
        f"{advance_note}"
    )
    _notify_match(f"system:loop:{task_id}:{loop_num}", text)

def _looper_notify_player_count_conflict(game, current_count, proposed_count):
    text = (
        f"⚠️ {game}: designers want to expand to {proposed_count} players "
        f"(currently testing at {current_count}). "
        f"reply 'expand {game}' to allow, or ignore to block."
    )
    _notify_match(f"system:expand:{game}", text)

def _looper_usage_done(task_id):
    try:
        data = json.loads(USAGE_FILE.read_text()) if USAGE_FILE.exists() else {}
        # Capture final task cost into looper_spend_today before removing task entry
        if _current_session_cost > 0:
            data.setdefault("api", {})["looper_spend_today"] = round(
                float(data["api"].get("looper_spend_today", 0.0)) + _current_session_cost, 6
            )
        data.get("tasks", {}).pop(task_id, None)
        USAGE_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"[usage] cleanup failed: {e}")


# ── Config ────────────────────────────────────────────────────────────────────
SESSIONS_DIR = Path.home() / "game-sessions"
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 600           # default — agents are prompted to stay under 300 words
MAX_TOKENS_RATIFY = 1200   # ratification doc — agents prompted to stay under 500 words
MAX_TOKENS_MONITOR = 1000  # monitor JSON output
MAX_RATIFICATION_RETRIES = 2

# Brief checklist reminder for debate agents — saves ~175 tokens vs full checklist
CHECKLIST_REMINDER = (
    "Required fields your ratified doc must answer (all 6): "
    "Player Count & Setup | Win/End Condition | Turn/Round Structure | "
    "Core Action | Component Flow | Replenishment"
)


# ── Completeness Checklists ───────────────────────────────────────────────────
PHASE1_CHECKLIST = [
    {
        "id": "player_count_setup",
        "label": "Player Count & Setup",
        "question": "How many players? What does each player start with (cards, tokens, dice, tiles, etc.)? What shared components go in the center? Every named component must have a starting location and quantity stated.",
    },
    {
        "id": "win_condition",
        "label": "Win / End Condition",
        "question": "How does the game end, and who wins? 'Most points' requires defining what points are and how they are scored. 'First to X' requires defining X explicitly. Must be unambiguous.",
    },
    {
        "id": "turn_structure",
        "label": "Turn / Round Structure",
        "question": "What happens on a turn or in a round, in what order? Must be a numbered or explicitly sequenced list of steps that players can follow without inference.",
    },
    {
        "id": "core_action",
        "label": "Core Action",
        "question": "What is the primary decision a player makes each turn or round? What are its inputs (what the player chooses from), outputs (what changes in game state), and constraints (what limits the choice)?",
    },
    {
        "id": "component_flow",
        "label": "Component Flow",
        "question": "What happens to each component type after it is used? Cards played, tokens spent, dice rolled — where does each go? Discard, remove, keep, return, flip, exhaust — must be stated explicitly for every type.",
    },
    {
        "id": "replenishment",
        "label": "Replenishment",
        "question": "How does a player's hand, pool, or available resources refresh? When, how many, from where? If there is no replenishment, that must be stated explicitly.",
    },
]

PHASE2_CHECKLIST = [
    {
        "id": "component_visibility",
        "label": "Component State Visibility",
        "question": "Which components are face-up, face-down, public, or private at all times? Must be stated for each component type in each game state (in hand, in play, in discard, etc.).",
    },
    {
        "id": "correction_phase",
        "label": "Correction / Adjustment Phase",
        "question": "If the game has a punishment, correction, or adjustment phase: who triggers it, under exactly what condition, and what is the step-by-step procedure? If no such phase exists, state that explicitly.",
    },
    {
        "id": "persistent_lifecycle",
        "label": "Persistent Component Lifecycle",
        "question": "If any component changes state during play (flips, upgrades, degrades, moves on a track), what happens to it during and after the correction/adjustment phase? Can it return to its original state?",
    },
    {
        "id": "supply_exhaustion",
        "label": "Supply / Deck Exhaustion",
        "question": "What happens when any deck, pool, supply, or shared resource runs out of components? Must be stated for every depletable component type.",
    },
    {
        "id": "tie_rules",
        "label": "Tie Rules",
        "question": "Ties during scoring, round resolution, or win condition must be resolved explicitly. If a tie cannot occur, state why.",
    },
    {
        "id": "no_valid_action",
        "label": "Empty Hand / No Valid Action",
        "question": "What happens if a player cannot take their core action (empty hand, no valid plays, no components remaining)? Must be stated explicitly.",
    },
]

def get_active_checklist(phase):
    return PHASE1_CHECKLIST + PHASE2_CHECKLIST if phase >= 2 else PHASE1_CHECKLIST

def format_checklist_for_designers(phase):
    items = get_active_checklist(phase)
    phase_note = (
        "Phase 2 — Full Gate (12 criteria)"
        if phase >= 2 else
        "Phase 1 — Core Gate (6 criteria)"
    )
    lines = [f"  {i+1}. **{item['label']}**: {item['question']}" for i, item in enumerate(items)]
    return f"[{phase_note}]\n" + "\n".join(lines)

def format_checklist_for_monitor(phase):
    return "\n".join(
        f"- id={item['id']} | {item['label']}: {item['question']}"
        for item in get_active_checklist(phase)
    )


# ── Pre-flight check ─────────────────────────────────────────────────────────
def preflight_check(game):
    """
    Run before every looper session. Diagnoses and fixes common issues
    left by a previously killed or crashed loop. Returns list of actions taken.
    """
    actions = []
    game_dir = get_game_dir(game)
    seed_file = get_seed_file(game)

    if not game_dir.exists() or not seed_file.exists():
        return ["no game directory or seed found — cannot run preflight"]

    # 1. Find last approved design doc (highest session number with a design_doc file)
    import glob as _glob
    design_docs = sorted(_glob.glob(str(game_dir / "session_*_design_doc.md")))
    if design_docs:
        last_doc_path = design_docs[-1]
        try:
            last_doc = Path(last_doc_path).read_text().strip()
            seed = seed_file.read_text().strip()
            seed_words = len(seed.split())
            doc_words = len(last_doc.split())
            # If seed is more than 40% shorter than last approved doc, it was likely
            # corrupted by a killed loop mid-write. Restore from last approved doc.
            if doc_words > 100 and seed_words < doc_words * 0.6:
                seed_file.write_text(last_doc)
                actions.append(
                    f"seed restored from {Path(last_doc_path).name} "
                    f"({seed_words} words → {doc_words} words)"
                )
        except Exception as e:
            actions.append(f"seed check failed: {e}")

    # 2. Clean stale kill flag files for this game
    try:
        import glob as _glob2
        for flag in _glob2.glob(str(USAGE_FILE.parent / f".killed_{game}-*")):
            Path(flag).unlink(missing_ok=True)
            actions.append(f"cleared stale kill flag: {Path(flag).name}")
    except Exception as e:
        actions.append(f"kill flag cleanup failed: {e}")

    # 3. Clean stale task entries in usage.json
    try:
        if USAGE_FILE.exists():
            data = json.loads(USAGE_FILE.read_text())
            stale = [tid for tid, t in data.get("tasks", {}).items()
                     if tid.startswith(f"{game}-")]
            for tid in stale:
                data["tasks"].pop(tid, None)
                actions.append(f"cleared stale task entry: {tid}")
            if stale:
                USAGE_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        actions.append(f"task cleanup failed: {e}")

    # 3. Verify state.json loops_completed matches actual transcript count
    try:
        state = load_state(game)
        actual_sessions = len(list(game_dir.glob("session_*_transcript.md")))
        # If loops_completed is wildly off, note it but don't auto-fix
        # (could be multi-loop sessions)
        if state["loops_completed"] > actual_sessions * 5:
            actions.append(
                f"warning: loops_completed={state['loops_completed']} "
                f"seems high for {actual_sessions} sessions"
            )
    except Exception as e:
        actions.append(f"state check failed: {e}")

    if not actions:
        actions.append("all clear")
    return actions


# ── Complexity guardrail (coded, not prompted) ────────────────────────────────
def _check_complexity_growth(prev_doc, new_doc):
    """
    Compare word count of new doc vs previous. If >25% growth, return a
    directive warning designers not to add more mechanics next loop.
    This is a code check, not a prompt — no token cost.
    """
    if not prev_doc or not new_doc:
        return None
    prev_words = len(prev_doc.split())
    new_words = len(new_doc.split())
    if prev_words == 0:
        return None
    growth = (new_words - prev_words) / prev_words
    if growth > 0.25:
        return (
            f"COMPLEXITY GUARDRAIL: The design doc grew by {new_words - prev_words} words "
            f"this loop ({prev_words} → {new_words}, +{growth*100:.0f}%). "
            f"Do NOT add new components, mechanics, or steps next loop. "
            f"Resolve and fully specify what already exists before expanding."
        )
    return None


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    env_file = Path.home() / "looper.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("#"):
                continue
            if "ANTHROPIC_API_KEY" in line:
                _, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                if val:
                    return val
    print("ERROR: No API key found. Set ANTHROPIC_API_KEY or add it to ~/looper.env")
    sys.exit(1)

def get_game_dir(game):
    return SESSIONS_DIR / game

def get_seed_file(game):
    return get_game_dir(game) / "current_seed.md"

def get_session_num(game):
    existing = get_game_dir(game).glob("session_*_transcript.md")
    nums = []
    for f in existing:
        m = re.match(r"session_(\d+)_transcript\.md", f.name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) + 1 if nums else 1

def extract_player_count(doc):
    for pat in [
        r"for\s+(\d+)\s*players?",
        r"(\d+)\s*[-\u2013to]+\s*\d+\s*player",
        r"player\s+count[:\s]+(\d+)",
        r"(\d+)\s*players?",
    ]:
        m = re.search(pat, doc, re.IGNORECASE)
        if m:
            return max(1, min(4, int(m.group(1))))
    return 2


# ── State management ──────────────────────────────────────────────────────────
def load_state(game):
    state_file = get_game_dir(game) / "state.json"
    defaults = {
        "phase": 1,
        "loops_completed": 0,
        "stability_streak": 0,
        "tested_player_count": None,
        "advance_pending": False,
        "expand_pending": False,
        "stability_threshold": 2,
    }
    if not state_file.exists():
        return defaults
    try:
        data = json.loads(state_file.read_text())
        defaults.update(data)
        return defaults
    except Exception:
        return defaults

def save_state(game, state):
    try:
        state_file = get_game_dir(game) / "state.json"
        state_file.write_text(json.dumps(state, indent=2))
    except Exception as e:
        print(f"[state] save failed: {e}")


# ── Agent Definitions ─────────────────────────────────────────────────────────
AGENTS = {
    "knizia": {
        "name": "KNIZIA",
        "system": """You are a game designer channeling Reiner Knizia's philosophy.
You believe that theme is decoration — what matters is mathematical elegance.
A great game is a set of interesting decisions constrained by tight, interlocking rules.
You have designed Tigris & Euphrates, Ra, Medici, Modern Art, Through the Desert.
You are terse, direct, and precise.

CRITICAL RESPONSIBILITY: You are jointly responsible for ensuring the design doc is
COMPLETE and PLAYABLE before any playtester sees it. Ambiguity is a defect.
When asked to co-produce a ratified ruleset, it must answer every required field.
If you leave a gap, the session halts and returns to you.

Elegant does NOT mean simple. Elegance means every rule earns its place and interlocks.
A tight, fully specified ruleset is the goal — not a stripped-down one.

When writing rule text, write it as it would appear in a published rulebook —
complete sentences, no placeholders, no "TBD". Be terse.""",
    },
    "thematist": {
        "name": "THEMATIST",
        "system": """You are a passionate game designer focused on theme-mechanic fit.
You believe a game should feel like its subject, not merely describe it.
You are influenced by Cole Wehrle (Root, Oath) and Vital Lacerda.

CRITICAL RESPONSIBILITY: You are jointly responsible for ensuring the design doc is
COMPLETE and PLAYABLE before any playtester sees it. Thematic resonance means nothing
if the game cannot be played. Every rule must be written in full — not sketched or implied.

You push back on anything that strips thematic resonance, but you do NOT hide
incomplete rules behind thematic language. Write it down explicitly.

When writing rule text, write it as it would appear in a published rulebook.
End contributions with concrete rule proposals that preserve mechanical integrity
AND thematic accuracy. Be concise.""",
    },
    "runner": {
        "name": "PLAYTEST RUNNER",
        "system": """You are an experienced playtest facilitator. You've written rulebooks for
published games and run playtests at GenCon for five years.

Your ONLY job is to present the rules you were given. You do NOT invent rules.
You do NOT fill gaps. Flag any gap as [AMBIGUITY: description].

Give: concise setup walkthrough, one example turn with actual component states,
any key phase explained briefly. Keep it under 200 words total.""",
    },
    "newcomer": {
        "name": "NEWCOMER PLAYTESTER",
        "system": """You are a casual gamer with about 50 games on BGG — mostly gateway games.
Enthusiastic but get lost when rules are dense.

Simulate 2 turns. For each: name components and choices, show reasoning briefly.
Report: one moment of confusion (exact quote), one moment of fun, one arbitrary rule.
Do not invent rules. Keep under 250 words.""",
    },
    "hobbyist": {
        "name": "HOBBYIST PLAYTESTER",
        "system": """You are a dedicated board game hobbyist with 300+ plays on BGG.
You love midweight euros and notice runaway leaders and feel-bad moments.

Simulate 2 turns. For each: name components and choices, show reasoning briefly.
Flag: one dominant strategy, one collapsed decision space moment, one good surprise.
Do not invent rules. Keep under 250 words.""",
    },
    "veteran": {
        "name": "VETERAN PLAYTESTER",
        "system": """You are a veteran board gamer with 800+ plays and 15 years of convention attendance.
You play fully optimally and find exploits immediately.

Simulate 2 turns. For each: name components and choices, show reasoning briefly.
Flag: one solved optimal play, one exploit or edge case, one genuine design hole.
Do not invent rules. Keep under 250 words.""",
    },
    "solo_coop": {
        "name": "SOLO/COOP SPECIALIST",
        "system": """You primarily play solo and cooperative games (Gloomhaven, Spirit Island).

Simulate 2 turns. For each: name components and choices, show reasoning briefly.
Report: does this work at current player count, could it scale to 1 player?
Do not invent rules. Keep under 250 words.""",
    },
    "critic": {
        "name": "CRITIC",
        "system": """You are terminally online about board games. You write reviews and track sales.

Give commercial and critical assessment only — no design advice.
Cover: the hook (or lack of one), shelf appeal, replayability ceiling, target audience.
Frame as "this sells because..." or "this dies because...". Keep under 200 words.""",
    },
    "monitor": {
        "name": "PROCESS MONITOR",
        "system": """You are a meta-agent responsible for game design session integrity.
Your job is process enforcement and document quality control. NOT game design.

You operate in two modes. Output ONLY the JSON object — no prose before or after.

MODE 1 — COMPLETENESS CHECK:
Evaluate a design doc against a checklist. Be ruthlessly literal.
"Implied" or "obvious from context" does NOT count. Must be explicitly written.
{
  "passed": true or false,
  "missing": [{"id": "field_id", "label": "Label", "reason": "exact gap"}],
  "patched_doc": null
}

MODE 2 — LOOP REVIEW:
Evaluate: (1) did designers produce NEW rule text or restate positions,
(2) did playtesters play from actual rules or invent them,
(3) genuine forward progress, (4) doc consistency,
(5) is CORE MECHANIC stable — same fundamental structure, only refined not replaced.
stable_core=true only if core loop survived without structural change.
{
  "approved": true or false,
  "flags": ["specific actionable issue"],
  "designer_directive": "exact instruction naming specific rules",
  "loop_summary": "2-3 sentences on what actually changed",
  "design_doc": "complete updated doc if approved, else null",
  "stable_core": true or false,
  "stability_note": "one sentence why core is or is not stable"
}

MODE 3 -- AGREEMENT CHECK:
Given Knizia's position and Thematist's position, identify which specific rules they agree on.
AGREED: both stated the same rule text, or one explicitly adopted the other's exact text.
DISPUTED: texts differ, or one objected without the other conceding.
Be literal -- "sounds good" without restating rule text is NOT agreement.
{
  "consensus": true or false,
  "agreed_rules": [{"section": "Section Name", "rule_text": "complete agreed rule text"}],
  "disputed": [{"section": "Section Name", "knizia_position": "brief", "thematist_position": "brief"}],
  "directive": "exact instruction naming disputed rules if not consensus, else null"
}
""",
    },
    "scribe": {
        "name": "RULEBOOK SCRIBE",
        "system": 'You are a technical rulebook writer. You do not design games.\nYou receive a current rulebook and a list of rules both designers explicitly agreed to.\nApply the agreed changes and output the complete updated rulebook.\n\nRules:\n- Apply ONLY the agreed rules. Do not add anything not in the agreed list.\n- Do not resolve disputed rules -- leave those sections exactly as they were in the current rulebook.\n- Write every section fully. No placeholders, no TBD, no "as discussed".\n- Rulebook style: complete sentences, numbered steps where sequence matters.\n- Keep under 500 words. Core mode only. No variants, designer notes, or flavor.\n\nOutput the COMPLETE rulebook as a single markdown document. Nothing else.',
    },
    "summarizer": {
        "name": "SESSION SUMMARIZER",
        "system": """You produce end-of-session summaries for a multi-agent game design project.

Produce a structured summary:
1. WHERE WE STARTED — design state at session open
2. KEY DEBATES — important disagreements and how they resolved
3. WHERE WE LANDED — agreed rule text (write actual rules, not descriptions)
4. OPEN QUESTIONS — unresolved issues with each designer's position
5. KNOWN BAD MECHANICS — explicitly ruled out this session and why
6. RECOMMENDED FOCUS NEXT SESSION — the single most important thing to resolve

Be specific. Name actual rule texts. Keep under 600 words.""",
    },
}


SEATING = {
    1: ["solo_coop"],
    2: ["newcomer", "veteran"],
    3: ["newcomer", "hobbyist", "veteran"],
    4: ["newcomer", "hobbyist", "veteran", "solo_coop"],
}

# ── Core helpers ──────────────────────────────────────────────────────────────
def call_agent(client, agent_key, prompt, tokens, max_tokens=None, task_id=None):
    """
    Call an agent. Writes real-time cost to usage.json after each call
    so intra-loop kill checks read current spend, not end-of-loop totals.
    """
    agent = AGENTS[agent_key]
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens or MAX_TOKENS,
        system=agent["system"],
        messages=[{"role": "user", "content": prompt}]
    )
    text_out = response.content[0].text
    tokens["input"] += response.usage.input_tokens
    tokens["output"] += response.usage.output_tokens
    # Real-time cost write — this is what makes intra-loop kill actually work
    if task_id:
        try:
            global _current_session_cost
            call_cost = (
                (response.usage.input_tokens / 1_000_000 * LOOPER_PRICE_INPUT_PER_M) +
                (response.usage.output_tokens / 1_000_000 * LOOPER_PRICE_OUTPUT_PER_M)
            )
            _current_session_cost = round(_current_session_cost + call_cost, 6)
        except Exception:
            pass
    return text_out

def log(transcript_lines, agent_key, text):
    name = AGENTS[agent_key]["name"]
    header = f"\n{'='*60}\n[{name}]\n{'='*60}"
    transcript_lines.append(header)
    transcript_lines.append(text)
    print(header)
    print(text)

def parse_json_response(text):
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None

def estimate_cost(tokens):
    return (tokens["input"] / 1_000_000) * 3.00 + (tokens["output"] / 1_000_000) * 15.00

def print_cost(tokens):
    print(f"\n[TOKEN USAGE] Input: {tokens['input']:,} | Output: {tokens['output']:,} | Est. cost: ${estimate_cost(tokens):.4f}")


# ── Completeness Gate ─────────────────────────────────────────────────────────
def run_completeness_check(client, doc, tokens, phase=1, task_id=None):
    checklist_text = format_checklist_for_monitor(phase)
    prompt = f"""MODE 1 — COMPLETENESS CHECK

Design doc:
---
{doc}
---

Checklist:
{checklist_text}

Is each field explicitly and completely answered? "Implied" does NOT count.
Output ONLY this JSON:
{{
  "passed": true or false,
  "missing": [{{"id": "field_id", "label": "Label", "reason": "what is missing"}}],
  "patched_doc": null
}}"""

    response = call_agent(client, "monitor", prompt, tokens,
                          max_tokens=MAX_TOKENS_MONITOR, task_id=task_id)
    data = parse_json_response(response)

    if data is None:
        print("[WARNING] Could not parse completeness check JSON. Treating as failed.")
        return False, [{"id": "json_parse", "label": "Parse Error",
                        "reason": "Monitor response was not valid JSON."}]

    passed = data.get("passed", False)
    missing = data.get("missing", [])

    if missing:
        print(f"\n[COMPLETENESS GATE] {len(missing)} missing field(s):")
        for item in missing:
            print(f"  ✗ {item['label']}: {item['reason']}")
    else:
        print("\n[COMPLETENESS GATE] All required fields present. ✓")

    return passed, missing



# ── Agreement Gate ────────────────────────────────────────────────────────────
def run_agreement_gate(client, knizia_pos, thematist_pos, current_doc,
                       transcript_lines, tokens, task_id=None):
    """
    Call monitor in MODE 3 to find consensus between designers.
    Up to 2 rounds of dispute resolution.
    Returns (agreed_rules, directive_or_None).
    agreed_rules is a list of {"section": ..., "rule_text": ...} dicts.
    directive is set if no consensus after max rounds, else None.
    """
    MAX_GATE_ROUNDS = 2
    knizia_current = knizia_pos
    thematist_current = thematist_pos

    for round_num in range(1, MAX_GATE_ROUNDS + 1):
        print(f"\n[AGREEMENT GATE] Round {round_num}/{MAX_GATE_ROUNDS}...")
        prompt = (
            "MODE 3 -- AGREEMENT CHECK\n\n"
            f"Knizia position:\n---\n{knizia_current}\n---\n\n"
            f"Thematist position:\n---\n{thematist_current}\n---\n\n"
            "Identify which specific rules they explicitly agree on and which are disputed.\n"
            "Output ONLY the JSON object."
        )
        response = call_agent(client, "monitor", prompt, tokens,
                              max_tokens=MAX_TOKENS_MONITOR, task_id=task_id)
        transcript_lines.append(
            f"\n[AGREEMENT GATE round {round_num}]\n{response}"
        )
        data = parse_json_response(response)
        if data is None:
            print("[AGREEMENT GATE] Could not parse monitor JSON. Treating as no consensus.")
            return [], "Agreement gate monitor response was not valid JSON."

        agreed_rules = data.get("agreed_rules", [])
        disputed = data.get("disputed", [])
        consensus = data.get("consensus", False)
        directive = data.get("directive")

        print(f"[AGREEMENT GATE] Consensus: {consensus} | Agreed: {len(agreed_rules)} | Disputed: {len(disputed)}")

        if consensus or (agreed_rules and not disputed):
            print("[AGREEMENT GATE] Consensus reached. ✓")
            return agreed_rules, None

        if round_num == MAX_GATE_ROUNDS:
            print(f"[AGREEMENT GATE] No consensus after {MAX_GATE_ROUNDS} rounds.")
            return agreed_rules, directive or "Designers could not reach consensus. Disputed rules left unchanged."

        # Another round — designers address disputed points
        disputed_summary = "; ".join(
            f"{d['section']}: Knizia={d.get('knizia_position','?')} vs Thematist={d.get('thematist_position','?')}"
            for d in disputed
        )
        print(f"[AGREEMENT GATE] Disputed: {disputed_summary}")
        knizia_current = call_agent(client, "knizia",
            f"These rules are still disputed after round {round_num}:\n{disputed_summary}\n\n"
            "For each disputed rule: either adopt the Thematist's exact text, or restate your "
            "complete rule text. No summaries. Write actual rule text only. Under 200 words.",
            tokens, task_id=task_id)
        thematist_current = call_agent(client, "thematist",
            f"These rules are still disputed after round {round_num}:\n{disputed_summary}\n\n"
            f"Knizia now says:\n{knizia_current}\n\n"
            "For each disputed rule: either adopt Knizia's exact text, or restate your "
            "complete rule text. No summaries. Write actual rule text only. Under 200 words.",
            tokens, task_id=task_id)

    return agreed_rules, directive


def run_scribe(client, current_doc, agreed_rules, transcript_lines, tokens, task_id=None):
    """
    Call scribe agent to apply agreed_rules to current_doc.
    Returns complete updated rulebook string.
    """
    if not agreed_rules:
        print("[SCRIBE] No agreed rules to apply — returning current doc unchanged.")
        return current_doc

    agreed_text = "\n".join(
        f"- {r['section']}: {r['rule_text']}" for r in agreed_rules
    )
    prompt = (
        f"Current rulebook:\n---\n{current_doc}\n---\n\n"
        f"Agreed rules to apply:\n{agreed_text}\n\n"
        "Apply ONLY these agreed rules. Leave all other sections exactly as written. "
        "Output the COMPLETE updated rulebook as a single markdown document. Nothing else."
    )
    result = call_agent(client, "scribe", prompt, tokens,
                        max_tokens=MAX_TOKENS_RATIFY, task_id=task_id)
    transcript_lines.append(f"\n[SCRIBE OUTPUT]\n{result}")
    print("[SCRIBE] Rulebook updated. ✓")
    return result

# ── Ratification Phase ────────────────────────────────────────────────────────
def run_ratification(client, loop_num, knizia_pos, thematist_pos, current_doc,
                     transcript_lines, tokens, phase=1, task_id=None):
    """
    Agreement gate + scribe replaces single-designer ratification.
    Signature: knizia_pos, thematist_pos, current_doc instead of designer_debate.
    Returns (ratified_doc, gate_directive_or_None).
    """
    print(f"\n[RATIFICATION] Running agreement gate (phase {phase})...")

    agreed_rules, gate_directive = run_agreement_gate(
        client, knizia_pos, thematist_pos, current_doc,
        transcript_lines, tokens, task_id=task_id
    )

    if not agreed_rules:
        print("[RATIFICATION] No agreed rules -- gate failed completely.")
        return None, gate_directive or "No consensus reached. Resolve disputed rules next loop."

    joint_doc = run_scribe(client, current_doc, agreed_rules, transcript_lines, tokens, task_id=task_id)
    transcript_lines.append("\n[PRE-PLAYTEST RATIFICATION -- from scribe]\n\n" + joint_doc)

    for attempt in range(MAX_RATIFICATION_RETRIES + 1):
        passed, missing = run_completeness_check(client, joint_doc, tokens,
                                                  phase=phase, task_id=task_id)
        if passed:
            print(f"[RATIFICATION] Passed on attempt {attempt + 1}. \u2713")
            return joint_doc, None

        if attempt == MAX_RATIFICATION_RETRIES:
            missing_labels = ", ".join(m["label"] for m in missing)
            directive = (
                "COMPLETENESS GATE FAILED after " + str(MAX_RATIFICATION_RETRIES + 1) + " attempts. "
                "Missing fields: " + missing_labels + ". "
                "Designers must explicitly state rules covering these fields next loop."
            )
            print("[RATIFICATION] Gate failed after max retries. Issuing directive.")
            return None, directive

        missing_list = "\n".join("  " + m["label"] + ": " + m["reason"] for m in missing)
        print(f"\n[RATIFICATION] Attempt {attempt + 1} failed. {len(missing)} gap(s). Returning to designers.")

        knizia_fix = call_agent(client, "knizia",
            "COMPLETENESS CHECK FAILED. These fields are missing:\n" + missing_list + "\n\n"
            "Current doc:\n---\n" + joint_doc + "\n---\n\n"
            "State your complete rule text for each missing field. Under 200 words.",
            tokens, max_tokens=MAX_TOKENS_RATIFY, task_id=task_id)
        thematist_fix = call_agent(client, "thematist",
            "COMPLETENESS CHECK FAILED. These fields are missing:\n" + missing_list + "\n\n"
            "Knizia proposes:\n---\n" + knizia_fix + "\n---\n\n"
            "Agree or counter with complete rule text for each missing field. Under 200 words.",
            tokens, max_tokens=MAX_TOKENS_RATIFY, task_id=task_id)

        fix_agreed, _ = run_agreement_gate(
            client, knizia_fix, thematist_fix, joint_doc,
            transcript_lines, tokens, task_id=task_id
        )
        joint_doc = run_scribe(client, joint_doc, fix_agreed or [], transcript_lines, tokens, task_id=task_id)
        transcript_lines.append("\n[PRE-PLAYTEST RATIFICATION -- Attempt " + str(attempt + 2) + "]\n\n" + joint_doc)

    return None, None  # should not reach


# ── Main Loop ─────────────────────────────────────────────────────────────────
def run_loop(client, loop_num, current_doc, transcript_lines, tokens,
             note=None, task_id=None, phase=1, tested_player_count=None, game=None):
    """
    Run one full design loop.
    Returns (new_doc, approved, directive, loop_summary, stable_core).
    """
    print(f"\n{'#'*70}")
    print(f"# LOOP {loop_num}  [Phase {phase}]")
    print(f"{'#'*70}")

    note_text = f"\n\nSESSION OWNER NOTE: {note}" if note else ""

    # Kill check helper — used after every agent call
    def check_kill(after):
        kill, reason = _looper_should_kill(task_id)
        if kill:
            print(f"\n🚨 INTRA-LOOP KILL after {after} — {reason}")
            _looper_mark_killed(task_id)
            return True, reason
        return False, ""

    # ── 1. Knizia opens ───────────────────────────────────────────────────────
    # Receives: current_doc + brief checklist reminder (NOT full checklist)
    # Saves ~175 tokens vs sending full checklist
    knizia_open = call_agent(client, "knizia", f"""Design loop {loop_num} (Phase {phase}). Current doc:
---
{current_doc}
---
{note_text}

{CHECKLIST_REMINDER}

State your opening position (keep under 300 words):
1. What is worth keeping right now
2. What should be cut or simplified immediately
3. What specific issue must be resolved this loop
4. Your proposed player count and why
5. Your concrete rule proposals — write actual rule text, not descriptions""",
    tokens, task_id=task_id)
    log(transcript_lines, "knizia", knizia_open)

    if task_id:
        killed, reason = check_kill("knizia open")
        if killed:
            return current_doc, False, f"killed: {reason}", "killed mid-loop", False

    # ── 2. Thematist responds ─────────────────────────────────────────────────
    # Receives: knizia_open only — NOT current_doc (Knizia already analyzed it)
    # This is the biggest single input savings in the loop
    thematist_response = call_agent(client, "thematist", f"""Knizia's opening for loop {loop_num}:
---
{knizia_open}
---
{note_text}

{CHECKLIST_REMINDER}

Respond to Knizia (keep under 300 words). Agree briefly where you agree.
Argue with specific thematic justification where you disagree — but arguments must
resolve into written rules, not design intentions. State concrete rule proposals
with full rule text.""",
    tokens, task_id=task_id)
    log(transcript_lines, "thematist", thematist_response)

    if task_id:
        killed, reason = check_kill("thematist")
        if killed:
            return current_doc, False, f"killed: {reason}", "killed mid-loop", False

    # ── 3. Knizia final position ──────────────────────────────────────────────
    knizia_final = call_agent(client, "knizia", f"""Thematist responded:
---
{thematist_response}
---

Reply directly to Thematist's points (under 300 words).
For each rule they proposed: agree and adopt their exact text, or counter with your own complete rule text.
Do not summarize. Write actual rule text for every point you address.""",
    tokens, task_id=task_id)
    log(transcript_lines, "knizia", knizia_final)

    if task_id:
        killed, reason = check_kill("knizia final")
        if killed:
            return current_doc, False, f"killed: {reason}", "killed mid-loop", False

    # ── 4. Ratification phase ─────────────────────────────────────────────────
    print("\n[RATIFICATION PHASE]")
    ratified_doc, gate_directive = run_ratification(
        client, loop_num, knizia_final, thematist_response, current_doc,
        transcript_lines, tokens, phase=phase, task_id=task_id
    )
    if gate_directive:
        print(f"\n[GATE FAILED] Playtest skipped. Directive carries to next loop.")
        transcript_lines.append(
            f"\n[GATE FAILED — playtest skipped]\nDirective: {gate_directive}"
        )
        return current_doc, False, gate_directive, "Completeness gate failed.", False

    if task_id:
        killed, reason = check_kill("ratification")
        if killed:
            return current_doc, False, f"killed: {reason}", "killed mid-loop", False

    # ── 5. Player count check ─────────────────────────────────────────────────
    actual_players = extract_player_count(ratified_doc)
    if tested_player_count and actual_players != tested_player_count:
        print(f"\n[PLAYER COUNT CONFLICT] Designers propose {actual_players}, testing at {tested_player_count}.")
        _looper_notify_player_count_conflict(
            game or game_name_from_doc(ratified_doc),
            tested_player_count, actual_players
        )
        actual_players = tested_player_count
        print(f"[PLAYER COUNT] Expansion blocked. Staying at {tested_player_count}.")

    # ── 6. Runner presents ────────────────────────────────────────────────────
    runner_present = call_agent(client, "runner",
        f"""Playtest with exactly {actual_players} player(s). Ruleset:
---
{ratified_doc}
---
Present: setup, one example turn, key system, win condition.
Flag ANY gap as [AMBIGUITY: description]. Do not invent rules.""",
    tokens, task_id=task_id)
    log(transcript_lines, "runner", runner_present)

    if task_id:
        killed, reason = check_kill("runner present")
        if killed:
            return current_doc, False, f"killed: {reason}", "killed mid-loop", False

    ambiguity_count = runner_present.count("[AMBIGUITY:")
    if ambiguity_count >= 3:
        print(f"\n[WARNING] Runner flagged {ambiguity_count} ambiguities.")

    # ── 7. Playtesters ────────────────────────────────────────────────────────
    active_playtesters = SEATING.get(actual_players, SEATING[2])
    print(f"\n[PLAYER COUNT] {actual_players} player(s) — seating: {', '.join(active_playtesters)}")

    # Playtesters receive runner_present + ratified_doc
    # runner_present is short (~200 words), ratified_doc is the core doc
    playtester_context = f"""Runner presented:
---
{runner_present}
---

Ruleset:
---
{ratified_doc}
---

Simulate 2 turns. For each: name components/values, show resolution logic, state decision.
Quote rule text before flagging a gap. If rule missing: RULES GAP: [description]
Do NOT invent rules."""

    playtester_results = {}
    for pt_key in active_playtesters:
        pt_prompt = playtester_context
        if pt_key == "solo_coop":
            pt_prompt += "\n\nAlso: solo adaptation feasible?"
        pt_play = call_agent(client, pt_key, pt_prompt, tokens, task_id=task_id)
        log(transcript_lines, pt_key, pt_play)
        playtester_results[pt_key] = pt_play

        if task_id:
            killed, reason = check_kill(f"playtester {pt_key}")
            if killed:
                return current_doc, False, f"killed: {reason}", "killed mid-loop", False

    newcomer_play = playtester_results.get("newcomer", "(not run)")
    hobbyist_play = playtester_results.get("hobbyist", "(not run)")
    veteran_play  = playtester_results.get("veteran",  "(not run)")
    solo_play     = playtester_results.get("solo_coop","(not run)")

    # ── 8. Critic ─────────────────────────────────────────────────────────────
    # Receives truncated doc and truncated playtester reports
    critic_review = call_agent(client, "critic", f"""Observed a playtest.

Design doc (summary):
---
{ratified_doc[:1500]}
---

Reports: NEWCOMER: {newcomer_play[:250]} | VETERAN: {veteran_play[:250]}

Commercial assessment: hook, shelf appeal, replayability, target audience.""",
    tokens, task_id=task_id)
    log(transcript_lines, "critic", critic_review)

    if task_id:
        killed, reason = check_kill("critic")
        if killed:
            return current_doc, False, f"killed: {reason}", "killed mid-loop", False

    # ── 9. Runner notes ───────────────────────────────────────────────────────
    runner_notes = call_agent(client, "runner", f"""You ran the playtest.

NEWCOMER: {newcomer_play[:200]}
VETERAN: {veteran_play[:200]}
CRITIC: {critic_review[:200]}

Facilitator note:
- Top 2 rules that caused confusion (exact quotes)
- Top 1 moment of genuine engagement
- 1 structural recommendation before next loop""",
    tokens, task_id=task_id)
    log(transcript_lines, "runner", runner_notes)

    if task_id:
        killed, reason = check_kill("runner notes")
        if killed:
            return current_doc, False, f"killed: {reason}", "killed mid-loop", False

    # ── 10. Designers iterate ─────────────────────────────────────────────────
    iteration_context = f"""Playtest complete.

RUNNER: {runner_notes[:400]}
NEWCOMER: {newcomer_play[:200]}
VETERAN: {veteran_play[:200]}
CRITIC: {critic_review[:200]}

Current ratified doc:
---
{ratified_doc}
---"""

    knizia_iter = call_agent(client, "knizia",
        iteration_context + "\n\nPropose rule changes. Write complete new rule text. Keep under 300 words.",
        tokens, task_id=task_id)
    log(transcript_lines, "knizia", knizia_iter)

    if task_id:
        killed, reason = check_kill("knizia iter")
        if killed:
            return current_doc, False, f"killed: {reason}", "killed mid-loop", False

    thematist_iter = call_agent(client, "thematist",
        iteration_context + f"\n\nKnizia proposes:\n---\n{knizia_iter}\n---\n\n"
        "Respond to Knizia's proposed changes (under 300 words). "
        "For each rule proposed: agree and adopt their exact text, or counter with your own complete rule text. "
        "Do NOT assemble a full doc. State your position on each rule only.",
        tokens, task_id=task_id)
    log(transcript_lines, "thematist", thematist_iter)

    if task_id:
        killed, reason = check_kill("thematist iter")
        if killed:
            return current_doc, False, f"killed: {reason}", "killed mid-loop", False

    # Agreement gate + scribe replaces UPDATED DESIGN DOC extraction
    iter_agreed, iter_directive = run_agreement_gate(
        client, knizia_iter, thematist_iter, ratified_doc,
        transcript_lines, tokens, task_id=task_id
    )
    if iter_agreed:
        updated_doc = run_scribe(client, ratified_doc, iter_agreed, transcript_lines, tokens, task_id=task_id)
    else:
        print("[ITERATION] No agreed changes -- ratified_doc carried forward unchanged.")
        updated_doc = ratified_doc

    # ── 11. Monitor loop review ───────────────────────────────────────────────
    # Transcript tail cut from 5000 to 1500 chars — monitor has both docs, doesn't need full play-by-play
    monitor_prompt = f"""MODE 2 — LOOP REVIEW

Previous doc:
---
{ratified_doc[:2000]}
---

Proposed updated doc:
---
{updated_doc[:2000]}
---

Recent transcript:
---
{chr(10).join(transcript_lines[-60:])[-1500:]}
---

Assess forward progress, doc consistency, and core mechanic stability.
Output ONLY the JSON object."""

    monitor_response = call_agent(client, "monitor", monitor_prompt, tokens,
                                   max_tokens=MAX_TOKENS_MONITOR, task_id=task_id)
    log(transcript_lines, "monitor", monitor_response)

    monitor_data = parse_json_response(monitor_response)
    if monitor_data is None:
        print("[WARNING] Could not parse Monitor JSON. Treating as not approved.")
        return updated_doc, False, "Monitor JSON invalid.", "", False

    approved       = monitor_data.get("approved", False)
    flags          = monitor_data.get("flags", [])
    loop_summary   = monitor_data.get("loop_summary", "")
    directive      = monitor_data.get("designer_directive", "")
    doc_from_mon   = monitor_data.get("design_doc")
    stable_core    = monitor_data.get("stable_core", False)
    stability_note = monitor_data.get("stability_note", "")

    print(f"\n[MONITOR VERDICT] Approved: {approved} | Stable core: {stable_core}")
    if stability_note:
        print(f"[STABILITY] {stability_note}")
    if flags:
        print("[MONITOR FLAGS]")
        for f in flags:
            print(f"  • {f}")
    if loop_summary:
        print(f"[LOOP SUMMARY] {loop_summary}")

    if approved:
        return (doc_from_mon or updated_doc), True, None, loop_summary, stable_core
    else:
        if directive:
            print(f"\n[MONITOR DIRECTIVE → carries into next loop]\n{directive}")
        return updated_doc, False, directive, loop_summary, stable_core


def game_name_from_doc(doc):
    """Extract a short game name from first line of doc for notifications."""
    first = doc.strip().split("\n")[0]
    return re.sub(r"[#\*]", "", first).strip()[:30]


# ── Session Runner ────────────────────────────────────────────────────────────
def run_session(game, loops, note=None, phase_override=None, do_advance=False):
    game_dir  = get_game_dir(game)
    seed_file = get_seed_file(game)

    if not game_dir.exists():
        print(f"ERROR: No game directory found at {game_dir}")
        sys.exit(1)
    if not seed_file.exists():
        print(f"ERROR: No seed file at {seed_file}")
        sys.exit(1)

    state = load_state(game)

    if do_advance:
        if state["advance_pending"]:
            state["phase"] = 2
            state["advance_pending"] = False
            state["stability_streak"] = 0
            save_state(game, state)
            print(f"[ADVANCE] {game} advanced to Phase 2.")
            _notify_match(f"system:advance:{game}",
                          f"✅ {game} advanced to phase 2! full 12-criteria gate now active 🎯")
        else:
            print(f"[ADVANCE] No advancement pending for {game}.")
        return

    phase = phase_override if phase_override is not None else state["phase"]

    if not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    client = anthropic.Anthropic(api_key=get_api_key())
    tokens = {"input": 0, "output": 0}

    current_doc = seed_file.read_text().strip()
    session_num = get_session_num(game)
    looper_task_id = f"{game}-{session_num}"
    _looper_usage_start(looper_task_id)
    global _current_session_cost
    _current_session_cost = 0.0

    transcript_file = game_dir / f"session_{session_num:03d}_transcript.md"
    design_doc_file = game_dir / f"session_{session_num:03d}_design_doc.md"
    summary_file    = game_dir / f"session_{session_num:03d}_summary.md"

    print(f"\n{'='*70}")
    print(f"{game.upper()} Design Session {session_num} — {loops} loop(s) — Phase {phase}")
    print(f"Stability streak: {state['stability_streak']}/{state['stability_threshold']}")
    if state['tested_player_count']:
        print(f"Currently testing at: {state['tested_player_count']} player(s)")
    print(f"{'='*70}\n")

    transcript_lines = [
        f"{'='*70}",
        f"{game.upper()} Design Session {session_num} — {loops} loops — Phase {phase}",
        f"Transcript: {transcript_file}",
        f"{'='*70}",
        f"\n## SEED DESIGN DOC\n{current_doc}\n",
    ]

    final_doc = current_doc
    pending_directive = None
    loop_summaries = []

    for loop_num in range(1, loops + 1):
        transcript_lines.append(f"\n{'#'*70}\n# LOOP {loop_num}\n{'#'*70}")
        tokens_loop_start = dict(tokens)

        effective_note = note
        if pending_directive:
            effective_note = (
                f"MONITOR DIRECTIVE (must be addressed this loop): {pending_directive}"
                + (f"\n\nADDITIONAL NOTE: {note}" if note else "")
            )
            pending_directive = None

        new_doc, approved, directive, lsummary, stable_core = run_loop(
            client, loop_num, final_doc, transcript_lines, tokens,
            note=effective_note, task_id=looper_task_id, phase=phase,
            tested_player_count=state["tested_player_count"], game=game
        )

        if lsummary:
            loop_summaries.append(f"Loop {loop_num}: {lsummary}")

        loop_cost = estimate_cost({
            "input": tokens["input"] - tokens_loop_start["input"],
            "output": tokens["output"] - tokens_loop_start["output"],
        })
        state["loops_completed"] += 1

        if approved:
            final_doc = new_doc
            print(f"\n✓ Loop {loop_num} approved.")

            # Complexity growth check (coded guardrail, not prompted)
            complexity_warning = _check_complexity_growth(current_doc, final_doc)
            if complexity_warning:
                print(f"\n[COMPLEXITY GUARDRAIL] {complexity_warning}")
                # Inject as additional note into next loop
                pending_directive = (pending_directive or "") + "\n\n" + complexity_warning

            declared_count = extract_player_count(final_doc)
            if state["tested_player_count"] is None:
                state["tested_player_count"] = declared_count
                print(f"[STATE] Recorded tested player count: {declared_count}")

            if stable_core:
                state["stability_streak"] += 1
                print(f"[STABILITY] Streak: {state['stability_streak']}/{state['stability_threshold']}")
            else:
                state["stability_streak"] = 0
                print(f"[STABILITY] Core changed — streak reset.")

            advance_pending = (
                phase < 2 and
                state["stability_streak"] >= state["stability_threshold"] and
                not state["advance_pending"]
            )
            if advance_pending:
                state["advance_pending"] = True
                print(f"\n[PHASE ADVANCEMENT] Core stable. Run: python3 ~/game_design_session.py --game {game} --advance")
        else:
            print(f"\n⚠ Loop {loop_num} not approved.")
            advance_pending = False
            pending_directive = directive

        save_state(game, state)

        transcript_file.write_text("\n".join(transcript_lines))
        design_doc_file.write_text(final_doc)
        seed_file.write_text(final_doc)

        print_cost(tokens)

        # Kill checks must come BEFORE loop-complete notification
        # so only one notification fires (kill OR complete, never both)

        # 1. Mid-loop kill (intra-loop kill already fired, directive carries it)
        if isinstance(directive, str) and directive.startswith("killed:"):
            _looper_mark_killed(looper_task_id)
            _looper_notify_kill(looper_task_id, directive.replace("killed: ", ""))
            break

        # 2. End-of-loop cost kill
        _kill, _reason = _looper_usage_update(looper_task_id, tokens)
        if _kill:
            print(f"\n\U0001f6a8 LOOPER KILLED \u2014 {_reason}")
            _looper_mark_killed(looper_task_id)
            _looper_notify_kill(looper_task_id, _reason)
            break

        # 3. Normal completion — notify loop done
        _looper_notify_loop_complete(
            looper_task_id, game, session_num, loop_num,
            approved, loop_cost, phase, lsummary,
            advance_pending=state.get("advance_pending", False)
        )

    # ── Session summary ───────────────────────────────────────────────────────
    # Use killed flag from usage.json — reliable even if task entry was cleaned up
    if _looper_was_killed(looper_task_id):
        print("\n[SUMMARIZER] Skipped — session was killed.")
        _looper_usage_done(looper_task_id)
        _looper_clear_kill_flag(looper_task_id)
        return

    print("\n[SUMMARIZER] Generating session summary...")
    loop_summary_block = ""
    if loop_summaries:
        loop_summary_block = "Loop summaries:\n" + "\n".join(loop_summaries) + "\n---\n\n"

    # Transcript tail cut from 12000 to 8000 chars
    summary_prompt = f"""{loop_summary_block}Session transcript:
---
{chr(10).join(transcript_lines)[-8000:]}
---

Produce the structured session summary. Name actual rule texts, not abstractions."""
    summary = call_agent(client, "summarizer", summary_prompt, tokens,
                         max_tokens=800, task_id=looper_task_id)

    transcript_lines.append(f"\n{'='*70}\n[SESSION SUMMARY]\n{'='*70}\n{summary}")
    print(f"\n{'='*70}\n[SESSION SUMMARY]\n{'='*70}\n{summary}")

    footer = [
        f"\n{'='*70}",
        f"SESSION COMPLETE — {game.upper()} — Phase {phase}",
        f"Transcript:  {transcript_file}",
        f"Design doc:  {design_doc_file}",
        f"Summary:     {summary_file}",
        f"Seed updated: {seed_file}",
        f"Total cost:  ${estimate_cost(tokens):.4f}",
        f"{'='*70}",
        f"\nNEXT SESSION:",
        f"  python3 ~/game_design_session.py --game {game}",
        f"  python3 ~/game_design_session.py --game {game} --note 'your note here'",
    ]
    if state.get("advance_pending"):
        footer.append(
            f"\n⚠ PHASE ADVANCEMENT PENDING — run:\n"
            f"  python3 ~/game_design_session.py --game {game} --advance"
        )
    transcript_lines.extend(footer)

    transcript_file.write_text("\n".join(transcript_lines))
    design_doc_file.write_text(final_doc)
    seed_file.write_text(final_doc)
    summary_file.write_text(summary)

    print("\n".join(footer))
    _looper_usage_done(looper_task_id)
    _looper_clear_kill_flag(looper_task_id)


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Game Design Session Runner")
    parser.add_argument("--game",    type=str, help="Game name (e.g. backprop)")
    parser.add_argument("--loops",   type=int, default=1, help="Number of loops (default: 1)")
    parser.add_argument("--note",    type=str, help="Note injected to designers at session start")
    parser.add_argument("--advance", action="store_true", help="Confirm pending phase advancement")
    parser.add_argument("--phase",   type=int, default=None, help="Override phase (1 or 2)")
    parser.add_argument("--list",    action="store_true", help="List all active games")
    args = parser.parse_args()

    if args.list:
        if not SESSIONS_DIR.exists():
            print("No games yet.")
            return
        games = [d.name for d in sorted(SESSIONS_DIR.iterdir()) if d.is_dir()]
        if not games:
            print("No games found.")
        else:
            print("Active games:")
            for g in games:
                seed = get_seed_file(g)
                sessions = len(list(get_game_dir(g).glob("session_*_transcript.md")))
                state = load_state(g)
                status = "✓" if seed.exists() else "✗ seed missing"
                advance = " [ADVANCE PENDING]" if state.get("advance_pending") else ""
                print(f"  {g} — {sessions} session(s) | phase {state['phase']} | "
                      f"streak {state['stability_streak']}/{state['stability_threshold']} | "
                      f"seed: {status}{advance}")
        return

    if not args.game:
        parser.print_help()
        sys.exit(1)

    run_session(
        args.game,
        args.loops,
        note=args.note,
        phase_override=args.phase,
        do_advance=args.advance,
    )


if __name__ == "__main__":
    main()
