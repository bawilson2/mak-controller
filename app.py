import os
import csv
import io
import json
import time
import datetime
import sqlite3
import logging
import urllib.request
from flask import Flask, request, Response, jsonify, render_template_string

# --- Logging Configuration ---

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("mak-controller")

app = Flask(__name__)

DB_PATH = os.environ.get("DB_PATH", "cooks.db")

last_seen_epoch = 0.0
prev_flags = None

# Flameout Watchdog State
flameout_start_epoch = None
flameout_triggered = False

# ATSET Notification State
at_set_alerted = False
last_alerted_setpoint = None

# Active runtime settings cache
app_settings = {
    "ntfy_topic": os.environ.get("NTFY_TOPIC", "")
}

# Active outbound commands served to the grill
grill_command = {
    "setPoint": 175,
    "potStatus": "",
    "cookMode": 1,
    "zoneProbe": 1,
    "power": 1
}

# Real-time inbound telemetry reported by the grill
grill_state = {
    "grill_id": "Unknown",
    "temp": "--",
    "power": "OFF",
    "probe1": "",
    "probe2": "",
    "probe3": "",
    "flags": "",
    "last_seen": "Waiting for data..."
}

# Target alarms for meat probes
probe_targets = {
    "probe1": None,
    "probe2": None,
    "probe3": None
}
probe_alerted = {
    "probe1": False,
    "probe2": False,
    "probe3": False
}

# Multi-Stage Recipe Automation State
automation_state = {
    "active": False,
    "name": "",
    "stage_idx": 0,
    "stage_started_epoch": None,
    "stages": []
}

DEFAULT_RECIPES = [
    {
        "name": "Pork Shoulder / Brisket (Stall, Wrap & Rest)",
        "stages": [
            {"name": "Initial Smoke", "setpoint": 200, "trigger_type": "probe1", "trigger_cond": "gte", "trigger_val": 165},
            {"name": "Bark & Finish", "setpoint": 250, "trigger_type": "probe1", "trigger_cond": "gte", "trigger_val": 203},
            {"name": "Cool Down to Slice", "setpoint": 150, "trigger_type": "probe1", "trigger_cond": "lte", "trigger_val": 150},
            {"name": "Safe Hold", "setpoint": 150, "trigger_type": "hold", "trigger_cond": "gte", "trigger_val": 0}
        ]
    },
    {
        "name": "3-2-1 Ribs",
        "stages": [
            {"name": "Initial Smoke", "setpoint": 225, "trigger_type": "time", "trigger_cond": "gte", "trigger_val": 180},
            {"name": "Wrapped / Tenderize", "setpoint": 250, "trigger_type": "time", "trigger_cond": "gte", "trigger_val": 120},
            {"name": "Sauce & Set Glaze", "setpoint": 225, "trigger_type": "time", "trigger_cond": "gte", "trigger_val": 60},
            {"name": "Keep Warm", "setpoint": 170, "trigger_type": "hold", "trigger_cond": "gte", "trigger_val": 0}
        ]
    },
    {
        "name": "Reverse Sear Steak",
        "stages": [
            {"name": "Gentle Smoke", "setpoint": 225, "trigger_type": "probe1", "trigger_cond": "gte", "trigger_val": 115},
            {"name": "High Heat Sear", "setpoint": 450, "trigger_type": "probe1", "trigger_cond": "gte", "trigger_val": 130}
        ]
    }
]

# --- Database Connection & Schema ---

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn

def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with get_db_connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                active INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                timestamp TEXT NOT NULL,
                grill_temp REAL,
                setpoint REAL,
                probe1 REAL,
                probe2 REAL,
                probe3 REAL,
                power TEXT,
                grill_flags TEXT,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS flag_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                field_name TEXT NOT NULL,
                old_val TEXT,
                new_val TEXT,
                bit_diff TEXT,
                context TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recipes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                stages_json TEXT NOT NULL
            )
        """)

        # Load persisted settings or seed from environment
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = 'ntfy_topic'")
        row = cursor.fetchone()
        if row:
            app_settings["ntfy_topic"] = row[0]
        elif app_settings["ntfy_topic"]:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('ntfy_topic', ?)", (app_settings["ntfy_topic"],))

        # Seed default recipes if empty
        cursor.execute("SELECT COUNT(*) FROM recipes")
        if cursor.fetchone()[0] == 0:
            for r in DEFAULT_RECIPES:
                conn.execute(
                    "INSERT INTO recipes (name, stages_json) VALUES (?, ?)",
                    (r["name"], json.dumps(r["stages"]))
                )
        conn.commit()
    logger.info("Database initialized at %s (WAL enabled). NTFY Topic: '%s'", DB_PATH, app_settings["ntfy_topic"])

init_db()

# --- Helper Utilities ---

def send_push_notification(title, message, priority="default"):
    topic = app_settings.get("ntfy_topic", "").strip()
    if not topic:
        return
    url = topic if topic.startswith("http") else f"https://ntfy.sh/{topic}"
    try:
        req = urllib.request.Request(
            url,
            data=message.encode('utf-8'),
            headers={"Title": title, "Priority": priority},
            method='POST'
        )
        urllib.request.urlopen(req, timeout=5)
        logger.debug("Sent push notification via '%s': %s", url, title)
    except Exception as e:
        logger.error("Failed to send ntfy alert: %s", e)

def parse_val(raw_val):
    try:
        if raw_val is not None and str(raw_val).strip() != "":
            return float(raw_val)
    except (ValueError, TypeError):
        pass
    return None

def get_active_session_id():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM sessions WHERE active = 1 ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        return row[0] if row else None

def analyze_bitmask_diff(old_val, new_val):
    try:
        o = int(str(old_val), 0) if old_val not in (None, "") else 0
        n = int(str(new_val), 0) if new_val not in (None, "") else 0
        diff = o ^ n
        bits_changed = [f"Bit {i} ({'HIGH' if (n & (1 << i)) else 'LOW'})" for i in range(16) if (diff & (1 << i))]
        binary_str = f"{n:08b}"
        return f"Bin: {binary_str} | Toggled: {', '.join(bits_changed) if bits_changed else 'None'}"
    except Exception:
        return "Non-integer payload change"

def evaluate_automation(now):
    if not automation_state["active"] or not automation_state["stages"]:
        return

    stages = automation_state["stages"]
    idx = automation_state["stage_idx"]

    if idx >= len(stages):
        automation_state["active"] = False
        return

    current_stage = stages[idx]

    if automation_state["stage_started_epoch"] is None:
        automation_state["stage_started_epoch"] = now
        target_setpoint = int(current_stage["setpoint"])
        grill_command["setPoint"] = target_setpoint
        stage_name = current_stage.get("name", f"Stage {idx + 1}")
        msg = f"Started {stage_name}: SetPoint set to {target_setpoint}°F"
        logger.info("[AUTOMATION] %s", msg)
        send_push_notification(f"Automation: {stage_name}", msg)
        return

    triggered = False
    trig_type = current_stage.get("trigger_type")
    trig_cond = current_stage.get("trigger_cond", "gte")
    trig_val = float(current_stage.get("trigger_val", 0))

    if trig_type == "time":
        elapsed_min = (now - automation_state["stage_started_epoch"]) / 60.0
        if elapsed_min >= trig_val:
            triggered = True
    elif trig_type in ("probe1", "probe2", "probe3"):
        current_probe_temp = parse_val(grill_state.get(trig_type))
        if current_probe_temp is not None:
            if trig_cond == "gte" and current_probe_temp >= trig_val:
                triggered = True
            elif trig_cond == "lte" and current_probe_temp <= trig_val:
                triggered = True
    elif trig_type == "hold":
        triggered = False

    if triggered:
        next_idx = idx + 1
        if next_idx < len(stages):
            automation_state["stage_idx"] = next_idx
            automation_state["stage_started_epoch"] = now
            next_stage = stages[next_idx]
            target_setpoint = int(next_stage["setpoint"])
            grill_command["setPoint"] = target_setpoint
            msg = f"Stage {idx + 1} complete. Advancing to '{next_stage.get('name', f'Stage {next_idx + 1}')}' @ {target_setpoint}°F"
            logger.info("[AUTOMATION] %s", msg)
            send_push_notification("Automation: Stage Advance", msg, "high")
        else:
            automation_state["active"] = False
            msg = f"Recipe '{automation_state['name']}' completed all stages!"
            logger.info("[AUTOMATION] %s", msg)
            send_push_notification("Automation Complete", msg, "high")

# --- Frontend HTML / JS ---

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MAK Pellet Boss Control</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #1a1a1a; color: #f0f0f0; margin: 0; padding: 20px; }
        .container { max-width: 900px; margin: 0 auto; background: #2a2a2a; border-radius: 12px; padding: 24px; box-shadow: 0 4px 15px rgba(0,0,0,0.4); }
        h1 { font-size: 1.5rem; margin-top: 0; color: #ff9800; text-align: center; }
        .card { background: #333; border-radius: 8px; padding: 16px; margin-bottom: 16px; position: relative; }
        .temp-display { display: flex; justify-content: space-around; align-items: center; text-align: center; }
        .temp-val { font-size: 2.8rem; font-weight: bold; margin: 5px 0; color: #fff; }
        .label { font-size: 0.85rem; color: #aaa; text-transform: uppercase; letter-spacing: 1px; }

        .probes { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; text-align: center; margin-top: 10px; }
        @media (max-width: 600px) {
            .probes { grid-template-columns: 1fr; }
        }
        .probe-box { background: #252525; padding: 12px; border-radius: 6px; border: 1px solid #3d3d3d; transition: border-color 0.3s, background 0.3s; }
        .probe-alert { border-color: #e53935 !important; background: #3e1b1b !important; animation: pulse 1.5s infinite; }
        @keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(229,57,53,0.7); } 70% { box-shadow: 0 0 0 10px rgba(229,57,53,0); } 100% { box-shadow: 0 0 0 0 rgba(229,57,53,0); } }
        .probe-target-input { width: 80px; text-align: center; padding: 4px 6px; background: #1a1a1a; border: 1px solid #555; color: #fff; border-radius: 4px; font-size: 0.9rem; margin-top: 6px; }

        .btn-group { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
        button { background: #444; color: #fff; border: 1px solid #555; border-radius: 6px; padding: 10px 14px; font-size: 1rem; cursor: pointer; flex: 1; transition: background 0.2s; }
        button:hover:not(:disabled) { background: #ff9800; border-color: #ff9800; color: #000; }
        button:disabled { opacity: 0.45; cursor: not-allowed; border-color: #444; background: #2a2a2a; color: #777; }
        .set-btn { background: #ff9800; color: #000; font-weight: bold; border: none; }
        .set-btn:hover:not(:disabled) { background: #ffa726; }
        .cancel-btn { background: #c62828; color: #fff; font-weight: bold; border: none; }
        .cancel-btn:hover:not(:disabled) { background: #d32f2f; }
        .sec-btn { background: #2196f3; border-color: #1e88e5; color: #fff; }
        .sec-btn:hover:not(:disabled) { background: #1e88e5; }
        
        .input-row { display: flex; gap: 10px; margin-top: 12px; }
        input[type="text"], input[type="number"], select { background: #222; border: 1px solid #555; color: #fff; padding: 10px; border-radius: 6px; font-size: 1rem; width: 100%; box-sizing: border-box; }
        
        .badge { padding: 4px 8px; border-radius: 4px; font-weight: bold; font-size: 0.85rem; text-transform: uppercase; display: inline-block; }
        .badge-online { background: #1b5e20; color: #a5d6a7; }
        .badge-cooldown { background: #e65100; color: #ffe082; }
        .badge-lost { background: #b71c1c; color: #ffcdd2; }
        
        .power-card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        .btn-power-on { background: #2e7d32; border-color: #388e3c; color: #fff; font-weight: bold; }
        .btn-power-off { background: #c62828; border-color: #d32f2f; color: #fff; font-weight: bold; }
        .btn-cooldown { background: #ef6c00; border-color: #f57c00; color: #fff; font-weight: bold; }
        
        .alert-bar { background: #b71c1c; color: #fff; text-align: center; padding: 8px; border-radius: 6px; margin-bottom: 16px; font-weight: bold; font-size: 0.9rem; }
        .flameout-bar { background: #ff6f00; color: #000; text-align: center; padding: 10px; border-radius: 6px; margin-bottom: 16px; font-weight: bold; font-size: 0.95rem; }
        .chart-container { position: relative; height: 320px; width: 100%; }
        
        .preset-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; margin-top: 12px; }
        @media (max-width: 600px) {
            .preset-grid { grid-template-columns: repeat(3, 1fr); }
        }

        details.card { padding: 0; overflow: hidden; }
        details.card summary {
            padding: 16px;
            cursor: pointer;
            user-select: none;
            list-style: none;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        details.card summary::-webkit-details-marker { display: none; }
        details.card summary::after {
            content: '▾';
            font-size: 1.2rem;
            color: #aaa;
            transition: transform 0.2s ease;
        }
        details.card[open] summary::after {
            transform: rotate(180deg);
        }
        .card-content {
            padding: 0 16px 16px 16px;
            border-top: 1px solid #3d3d3d;
            margin-top: 0;
        }

        .stage-card { background: #242424; border: 1px solid #444; border-radius: 6px; padding: 10px; margin-top: 8px; }
        .stage-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
        .stage-grid { display: grid; grid-template-columns: 2fr 1fr 1.3fr 1.2fr 1fr; gap: 8px; align-items: end; }
        @media (max-width: 750px) {
            .stage-grid { grid-template-columns: 1fr 1fr; }
        }
        .stage-del-btn { background: #b71c1c; border: none; color: #fff; padding: 4px 8px; border-radius: 4px; font-size: 0.8rem; cursor: pointer; flex: 0; }
        .stage-del-btn:hover { background: #d32f2f; }
        .stage-badge { padding: 2px 6px; border-radius: 3px; font-size: 0.75rem; font-weight: bold; background: #444; }

        .diag-grid { display: grid; grid-template-columns: 1fr; gap: 10px; margin-top: 8px; font-family: monospace; }
        .diag-box { background: #1f1f1f; padding: 10px; border-radius: 6px; border: 1px solid #3d3d3d; }
        .diag-val { font-size: 1.1rem; color: #4fc3f7; margin-top: 4px; word-break: break-all; }
        .event-log { background: #181818; border-radius: 6px; padding: 10px; max-height: 140px; overflow-y: auto; font-family: monospace; font-size: 0.8rem; margin-top: 10px; border: 1px solid #333; }
        .log-entry { margin-bottom: 4px; padding-bottom: 4px; border-bottom: 1px solid #282828; color: #bbb; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Pellet Boss Controller</h1>

        <div id="alertLost" class="alert-bar" style="display: none;">
            CONNECTION LOST &mdash; Grill polling timed out
        </div>
        <div id="alertFlameout" class="flameout-bar" style="display: none;">
            ⚠️ FLAMEOUT DETECTED &mdash; Pit temp dropped >35°F below setpoint for 8+ minutes
        </div>

        <!-- 1. Status Section -->
        <div class="card">
            <div class="power-card-header">
                <div>
                    <span class="label">Status: </span>
                    <span id="powerBadge" class="badge">--</span>
                </div>
                <button id="powerToggleBtn" onclick="togglePower()" style="flex: 0 0 160px;">--</button>
            </div>
            <div class="temp-display">
                <div>
                    <div class="label">Current Temp</div>
                    <div class="temp-val" id="currentTemp">--°F</div>
                </div>
                <div>
                    <div class="label">Target SetPoint</div>
                    <div class="temp-val" style="color: #ff9800;" id="targetTemp">--°F</div>
                </div>
            </div>
        </div>

        <!-- 2. Manual SetPoint Controls -->
        <div class="card" id="controlCard">
            <div class="label">Adjust SetPoint</div>
            <div class="input-row">
                <input type="number" id="tempInput" min="150" max="500" step="5" placeholder="Enter °F">
                <button class="set-btn control-elem" onclick="sendCustomTemp()">Set</button>
            </div>
            <div class="preset-grid">
                <button class="control-elem" onclick="adjustTemp(-5)">-5°</button>
                <button class="control-elem" onclick="adjustTemp(5)">+5°</button>
                <button class="control-elem" onclick="setPreset(200)">200°</button>
                <button class="control-elem" onclick="setPreset(225)">225°</button>
                <button class="control-elem" onclick="setPreset(250)">250°</button>
                <button class="control-elem" onclick="setPreset(275)">275°</button>
            </div>
        </div>

        <!-- 3. Meat Probes & Target Alarms -->
        <div class="card">
            <div class="label">Meat Probes & Target Alarms</div>
            <div class="probes">
                <div class="probe-box" id="pBox1">
                    <div class="label" style="color: #42a5f5;">Probe 1</div>
                    <div style="font-size: 1.3rem; margin-top: 4px; font-weight: bold;" id="probe1">--</div>
                    <div style="margin-top: 6px;">
                        <span class="label" style="font-size: 0.75rem;">Target °F:</span><br>
                        <input type="number" class="probe-target-input" id="pTgt1" min="100" max="220" onchange="setProbeTarget('probe1', this.value)">
                    </div>
                </div>
                <div class="probe-box" id="pBox2">
                    <div class="label" style="color: #66bb6a;">Probe 2</div>
                    <div style="font-size: 1.3rem; margin-top: 4px; font-weight: bold;" id="probe2">--</div>
                    <div style="margin-top: 6px;">
                        <span class="label" style="font-size: 0.75rem;">Target °F:</span><br>
                        <input type="number" class="probe-target-input" id="pTgt2" min="100" max="220" onchange="setProbeTarget('probe2', this.value)">
                    </div>
                </div>
                <div class="probe-box" id="pBox3">
                    <div class="label" style="color: #ab47bc;">Probe 3</div>
                    <div style="font-size: 1.3rem; margin-top: 4px; font-weight: bold;" id="probe3">--</div>
                    <div style="margin-top: 6px;">
                        <span class="label" style="font-size: 0.75rem;">Target °F:</span><br>
                        <input type="number" class="probe-target-input" id="pTgt3" min="100" max="220" onchange="setProbeTarget('probe3', this.value)">
                    </div>
                </div>
            </div>
        </div>

        <!-- 4. Telemetry Graph -->
        <div class="card">
            <div class="label">Telemetry Graph</div>
            <div class="chart-container">
                <canvas id="cookChart"></canvas>
            </div>
        </div>

        <!-- 5. Cook Session Management (Collapsed) -->
        <details class="card" id="sessionCard">
            <summary class="label">Cook Session Management</summary>
            <div class="card-content">
                <div style="margin-top: 12px;">
                    <input type="text" id="sessionNameInput" placeholder="Session Name (e.g. Pork Shoulder)">
                    <div class="btn-group" style="margin-top: 10px;">
                        <button id="sessionToggleBtn" onclick="toggleSession()">Start Cook</button>
                        <button id="exportCsvBtn" onclick="exportCsv()" disabled>Export CSV</button>
                    </div>
                </div>
                <div style="font-size: 0.85rem; margin-top: 10px; color: #888;">
                    Active Session: <strong id="activeSessionDisplay" style="color: #fff;">None (Volatile only)</strong>
                </div>
            </div>
        </details>

        <!-- 6. Recipe Automation & Template Editor (Collapsed) -->
        <details class="card" id="automationCard">
            <summary class="label">Recipe Automation & Template Editor</summary>
            <div class="card-content">
                <div id="autoSetupView">
                    <div style="margin-top: 12px;">
                        <label class="label">Saved Recipes</label>
                        <select id="recipeSelect" onchange="onSelectRecipe()" style="margin-top: 4px;"></select>
                    </div>

                    <div id="recipeEditorBody" style="display: none;">
                        <div style="margin-top: 12px;">
                            <label class="label">Recipe Name</label>
                            <input type="text" id="recipeNameInput" placeholder="Enter recipe name..." style="margin-top: 4px;">
                        </div>

                        <div style="margin-top: 14px;">
                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                <span class="label">Cook Stages</span>
                                <button class="sec-btn" onclick="addStageRow()" style="flex: 0 0 120px; padding: 6px 10px; font-size: 0.85rem;">+ Add Stage</button>
                            </div>
                            <div id="stageContainer" style="margin-top: 8px;"></div>
                        </div>

                        <div class="btn-group" style="margin-top: 14px;">
                            <button class="sec-btn" onclick="saveRecipe()">Save Recipe</button>
                            <button class="cancel-btn" onclick="deleteRecipe()" id="deleteRecipeBtn">Delete Recipe</button>
                        </div>

                        <button class="set-btn" onclick="startAutomation()" style="width: 100%; margin-top: 10px; padding: 12px; font-size: 1.1rem;">Start Automated Recipe</button>
                    </div>
                </div>

                <div id="autoActiveView" style="display: none; margin-top: 12px;">
                    <div style="display: flex; justify-content: space-between; align-items: baseline;">
                        <div>
                            <strong id="activeRecipeName" style="font-size: 1.1rem; color: #4fc3f7;">--</strong>
                            <span id="activeStageIndicator" class="stage-badge" style="background: #2e7d32; margin-left: 8px;">Stage 1</span>
                        </div>
                        <span id="stageElapsed" style="font-size: 0.85rem; color: #aaa;">Elapsed: 0m</span>
                    </div>

                    <div style="margin-top: 10px; background: #222; padding: 12px; border-radius: 6px;">
                        <div><strong>Target Setpoint:</strong> <span id="autoStageSetpoint" style="color: #ff9800;">--</span></div>
                        <div style="margin-top: 4px;"><strong>Advancement Rule:</strong> <span id="autoStageCondition">--</span></div>
                    </div>

                    <div class="btn-group" style="margin-top: 12px;">
                        <button onclick="skipAutoStage()">Skip to Next Stage</button>
                        <button class="cancel-btn" onclick="cancelAutomation()">Cancel Automation</button>
                    </div>
                </div>
            </div>
        </details>

        <!-- 7. Diagnostics & Settings (Collapsed) -->
        <details class="card" id="diagnosticsCard">
            <summary class="label">Diagnostics & Settings</summary>
            <div class="card-content">
                <!-- Push Notification Settings -->
                <div style="margin-top: 12px;">
                    <div class="label" style="color: #4fc3f7;">Push Notifications (ntfy)</div>
                    <div style="font-size: 0.8rem; color: #888; margin-top: 4px;">
                        Enter your topic name (e.g. <code>my-mak-grill</code>) or a self-hosted URL.
                    </div>
                    <div class="input-row" style="margin-top: 8px;">
                        <input type="text" id="ntfyTopicInput" placeholder="ntfy topic or URL">
                        <button class="sec-btn" onclick="saveNtfySetting()" style="flex: 0 0 90px;">Save</button>
                        <button onclick="testNtfyNotification()" style="flex: 0 0 90px;">Test</button>
                    </div>
                    <div id="ntfyStatus" style="font-size: 0.8rem; color: #a5d6a7; margin-top: 4px;"></div>
                </div>

                <!-- GrillFlags Diagnostic -->
                <div style="margin-top: 16px; border-top: 1px solid #3d3d3d; padding-top: 12px;">
                    <div class="diag-grid">
                        <div class="diag-box">
                            <div class="label">GrillFlags (Raw from Grill)</div>
                            <div class="diag-val" id="dispFlags">--</div>
                        </div>
                    </div>
                    <div class="label" style="margin-top: 12px;">GrillFlags Transition Log</div>
                    <div class="event-log" id="eventLog">
                        <div style="color: #666;">No flag transitions observed yet...</div>
                    </div>
                </div>

                <!-- Database Maintenance -->
                <div style="margin-top: 16px; border-top: 1px solid #3d3d3d; padding-top: 12px;">
                    <div class="label">Database Maintenance</div>
                    <div style="font-size: 0.8rem; color: #888; margin-top: 4px;">
                        Prunes unassigned volatile telemetry (>24h) and historical cooks older than threshold.
                    </div>
                    <div class="btn-group" style="margin-top: 8px;">
                        <button onclick="pruneDatabase(30)" style="font-size: 0.85rem;">Prune &gt; 30 Days</button>
                        <button onclick="pruneDatabase(7)" style="font-size: 0.85rem;">Prune &gt; 7 Days</button>
                    </div>
                    <div id="pruneStatus" style="font-size: 0.8rem; color: #4fc3f7; margin-top: 6px;"></div>
                </div>
            </div>
        </details>

        <div class="status-footer" style="text-align: center; color: #777; font-size: 0.8rem; margin-top: 16px;">
            Grill ID: <span id="grillId">--</span> | Last Polled: <span id="lastSeen">--</span>
        </div>
    </div>

    <script>
        let currentTarget = 175;
        let isOnline = false;
        let isCooldown = false;
        let commandedPower = 1;
        let activeSession = null;
        let audioContext = null;

        let loadedRecipes = [];
        let activeRecipeId = null;

        function playChime() {
            try {
                if (!audioContext) audioContext = new (window.AudioContext || window.webkitAudioContext)();
                const osc = audioContext.createOscillator();
                const gain = audioContext.createGain();
                osc.connect(gain);
                gain.connect(audioContext.destination);
                osc.type = 'sine';
                osc.frequency.setValueAtTime(587.33, audioContext.currentTime);
                osc.frequency.setValueAtTime(880, audioContext.currentTime + 0.15);
                gain.gain.setValueAtTime(0.3, audioContext.currentTime);
                gain.gain.exponentialRampToValueAtTime(0.01, audioContext.currentTime + 0.6);
                osc.start();
                osc.stop(audioContext.currentTime + 0.6);
            } catch (e) {
                console.warn("Audio chime prevented:", e);
            }
        }

        const ctx = document.getElementById('cookChart').getContext('2d');
        const cookChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [
                    { label: 'Grill', borderColor: '#ff5722', borderWidth: 2, pointRadius: 0, tension: 0.2, data: [] },
                    { label: 'SetPoint', borderColor: '#ffb300', borderWidth: 1.5, borderDash: [5, 5], pointRadius: 0, tension: 0, data: [] },
                    { label: 'Probe 1', borderColor: '#42a5f5', borderWidth: 1.5, pointRadius: 0, tension: 0.2, data: [] },
                    { label: 'Probe 2', borderColor: '#66bb6a', borderWidth: 1.5, pointRadius: 0, tension: 0.2, data: [] },
                    { label: 'Probe 3', borderColor: '#ab47bc', borderWidth: 1.5, pointRadius: 0, tension: 0.2, data: [] }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                scales: {
                    x: { ticks: { color: '#888', maxTicksLimit: 8 }, grid: { color: '#333' } },
                    y: { ticks: { color: '#888', callback: val => val + '°F' }, grid: { color: '#333' }, suggestedMin: 100, suggestedMax: 300 }
                },
                plugins: { legend: { labels: { color: '#ccc', usePointStyle: true, boxWidth: 8 } } }
            }
        });

        // --- Settings Management ---

        async function fetchSettings() {
            try {
                const res = await fetch('/api/settings');
                const data = await res.json();
                document.getElementById('ntfyTopicInput').value = data.ntfy_topic || '';
            } catch (e) {
                console.error("Failed to load settings:", e);
            }
        }

        async function saveNtfySetting() {
            const topic = document.getElementById('ntfyTopicInput').value.trim();
            const res = await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ntfy_topic: topic })
            });
            const data = await res.json();
            const status = document.getElementById('ntfyStatus');
            if (data.success) {
                status.style.color = '#a5d6a7';
                status.innerText = "Setting saved successfully.";
            } else {
                status.style.color = '#e57373';
                status.innerText = "Error saving setting.";
            }
            setTimeout(() => { status.innerText = ''; }, 4000);
        }

        async function testNtfyNotification() {
            const status = document.getElementById('ntfyStatus');
            status.style.color = '#aaa';
            status.innerText = "Sending test alert...";
            try {
                const res = await fetch('/api/settings/test_ntfy', { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    status.style.color = '#a5d6a7';
                    status.innerText = "Test notification sent successfully.";
                } else {
                    status.style.color = '#e57373';
                    status.innerText = data.error || "Failed to send notification.";
                }
            } catch (e) {
                status.style.color = '#e57373';
                status.innerText = "Network error while sending test.";
            }
            setTimeout(() => { status.innerText = ''; }, 5000);
        }

        // --- Recipe Template Manager ---

        async function fetchRecipes() {
            try {
                const res = await fetch('/api/recipes');
                loadedRecipes = await res.json();
                
                const select = document.getElementById('recipeSelect');
                select.innerHTML = '<option value="" selected disabled>-- Select a Recipe --</option><option value="new">+ Create New Recipe</option>';
                loadedRecipes.forEach(r => {
                    const opt = document.createElement('option');
                    opt.value = r.id;
                    opt.innerText = r.name;
                    select.appendChild(opt);
                });

                if (activeRecipeId) {
                    select.value = activeRecipeId;
                }
                onSelectRecipe();
            } catch (e) {
                console.error("Error fetching recipes:", e);
            }
        }

        function onSelectRecipe() {
            const select = document.getElementById('recipeSelect');
            const editorBody = document.getElementById('recipeEditorBody');
            const delBtn = document.getElementById('deleteRecipeBtn');
            const idVal = select.value;

            if (!idVal) {
                editorBody.style.display = 'none';
                return;
            }

            editorBody.style.display = 'block';

            if (idVal === "new") {
                activeRecipeId = null;
                document.getElementById('recipeNameInput').value = "";
                document.getElementById('stageContainer').innerHTML = "";
                addStageRow({ name: "Stage 1", setpoint: 225, trigger_type: "time", trigger_cond: "gte", trigger_val: 60 });
                delBtn.disabled = true;
            } else {
                activeRecipeId = parseInt(idVal, 10);
                const rec = loadedRecipes.find(r => r.id === activeRecipeId);
                if (rec) {
                    document.getElementById('recipeNameInput').value = rec.name;
                    renderStages(rec.stages);
                    delBtn.disabled = false;
                }
            }
        }

        function renderStages(stages) {
            const container = document.getElementById('stageContainer');
            container.innerHTML = "";
            stages.forEach(st => addStageRow(st));
        }

        function addStageRow(st = {}) {
            const container = document.getElementById('stageContainer');
            const idx = container.children.length + 1;
            const stageDiv = document.createElement('div');
            stageDiv.className = 'stage-card';

            const name = st.name || `Stage ${idx}`;
            const setpoint = st.setpoint || 225;
            const trigger_type = st.trigger_type || "time";
            const trigger_cond = st.trigger_cond || "gte";
            const trigger_val = st.trigger_val !== undefined ? st.trigger_val : 60;
            const isProbe = trigger_type.startsWith('probe');
            const isHold = trigger_type === 'hold';

            stageDiv.innerHTML = `
                <div class="stage-header">
                    <span class="label" style="color: #ff9800;">Stage ${idx}</span>
                    <button class="stage-del-btn" onclick="this.closest('.stage-card').remove(); renumberStages();">✕</button>
                </div>
                <div class="stage-grid">
                    <div>
                        <span class="label" style="font-size: 0.75rem;">Name</span>
                        <input type="text" class="stage-name-input" value="${name}">
                    </div>
                    <div>
                        <span class="label" style="font-size: 0.75rem;">SetPoint (°F)</span>
                        <input type="number" class="stage-temp-input" min="150" max="500" step="5" value="${setpoint}">
                    </div>
                    <div>
                        <span class="label" style="font-size: 0.75rem;">Trigger Type</span>
                        <select class="stage-trig-type" onchange="toggleTriggerInputs(this)">
                            <option value="time" ${trigger_type === 'time' ? 'selected' : ''}>Time (min)</option>
                            <option value="probe1" ${trigger_type === 'probe1' ? 'selected' : ''}>Probe 1 Target</option>
                            <option value="probe2" ${trigger_type === 'probe2' ? 'selected' : ''}>Probe 2 Target</option>
                            <option value="probe3" ${trigger_type === 'probe3' ? 'selected' : ''}>Probe 3 Target</option>
                            <option value="hold" ${trigger_type === 'hold' ? 'selected' : ''}>Hold Indefinitely</option>
                        </select>
                    </div>
                    <div class="stage-cond-col" style="${isProbe ? '' : 'display:none;'}">
                        <span class="label" style="font-size: 0.75rem;">Condition</span>
                        <select class="stage-trig-cond">
                            <option value="gte" ${trigger_cond === 'gte' ? 'selected' : ''}>Above or Equal (&ge;)</option>
                            <option value="lte" ${trigger_cond === 'lte' ? 'selected' : ''}>Below or Equal (&le;)</option>
                        </select>
                    </div>
                    <div>
                        <span class="label" style="font-size: 0.75rem;">Trigger Value</span>
                        <input type="number" class="stage-trig-val" value="${trigger_val}" ${isHold ? 'disabled style="opacity:0.3;"' : ''}>
                    </div>
                </div>
            `;
            container.appendChild(stageDiv);
        }

        function toggleTriggerInputs(selectEl) {
            const grid = selectEl.closest('.stage-grid');
            const condCol = grid.querySelector('.stage-cond-col');
            const valInput = grid.querySelector('.stage-trig-val');
            const val = selectEl.value;

            if (val === 'hold') {
                condCol.style.display = 'none';
                valInput.disabled = true;
                valInput.style.opacity = '0.3';
            } else if (val.startsWith('probe')) {
                condCol.style.display = 'block';
                valInput.disabled = false;
                valInput.style.opacity = '1';
            } else {
                condCol.style.display = 'none';
                valInput.disabled = false;
                valInput.style.opacity = '1';
            }
        }

        function renumberStages() {
            const container = document.getElementById('stageContainer');
            Array.from(container.children).forEach((el, i) => {
                el.querySelector('.stage-header span').innerText = `Stage ${i + 1}`;
            });
        }

        function getBuilderStages() {
            const container = document.getElementById('stageContainer');
            const stages = [];
            container.querySelectorAll('.stage-card').forEach((card) => {
                const name = card.querySelector('.stage-name-input').value.trim() || 'Step';
                const setpoint = parseInt(card.querySelector('.stage-temp-input').value, 10) || 225;
                const trig_type = card.querySelector('.stage-trig-type').value;
                const trig_cond = trig_type.startsWith('probe') ? card.querySelector('.stage-trig-cond').value : 'gte';
                const trig_val = trig_type === 'hold' ? 0 : parseFloat(card.querySelector('.stage-trig-val').value) || 0;
                stages.push({
                    name: name,
                    setpoint: setpoint,
                    trigger_type: trig_type,
                    trigger_cond: trig_cond,
                    trigger_val: trig_val
                });
            });
            return stages;
        }

        async function saveRecipe() {
            const name = document.getElementById('recipeNameInput').value.trim();
            if (!name) {
                alert("Please enter a recipe name.");
                return;
            }
            const stages = getBuilderStages();
            if (stages.length === 0) {
                alert("Please add at least one stage.");
                return;
            }

            const payload = { id: activeRecipeId, name: name, stages: stages };
            const res = await fetch('/api/recipes', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (data.success) {
                activeRecipeId = data.id;
                await fetchRecipes();
            }
        }

        async function deleteRecipe() {
            if (!activeRecipeId) return;
            if (!confirm("Are you sure you want to delete this recipe template?")) return;

            await fetch(`/api/recipes/${activeRecipeId}`, { method: 'DELETE' });
            activeRecipeId = null;
            await fetchRecipes();
        }

        async function startAutomation() {
            const name = document.getElementById('recipeNameInput').value.trim() || 'Custom Automation';
            const stages = getBuilderStages();
            if (stages.length === 0) {
                alert("Please configure at least one stage before running.");
                return;
            }

            await fetch('/api/automation/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: name, stages: stages })
            });
            updateStatus();
        }

        async function cancelAutomation() {
            await fetch('/api/automation/stop', { method: 'POST' });
            updateStatus();
        }

        async function skipAutoStage() {
            await fetch('/api/automation/next', { method: 'POST' });
            updateStatus();
        }

        async function pruneDatabase(days) {
            if (!confirm(`Are you sure you want to prune records older than ${days} days?`)) return;
            try {
                const res = await fetch(`/api/maintenance/prune?days=${days}`, { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    const total = data.pruned_session_telemetry + data.pruned_volatile;
                    document.getElementById('pruneStatus').innerText = 
                        `Pruned ${total} telemetry points, ${data.pruned_sessions} sessions, and ${data.pruned_flag_events} flag logs.`;
                    fetchHistory();
                }
            } catch (e) {
                console.error("Prune request failed:", e);
            }
        }

        // --- Core Monitoring & Control ---

        async function updateStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                
                isOnline = data.is_online;
                isCooldown = data.is_cooldown;
                commandedPower = parseInt(data.command.power, 10);
                currentTarget = parseInt(data.command.setPoint, 10);
                activeSession = data.active_session;

                document.getElementById('currentTemp').innerText = isOnline ? (data.state.temp + '°F') : '--';
                document.getElementById('targetTemp').innerText = isOnline ? (data.command.setPoint + '°F') : '--';
                document.getElementById('probe1').innerText = (isOnline && data.state.probe1) ? data.state.probe1 + '°F' : 'Unplugged';
                document.getElementById('probe2').innerText = (isOnline && data.state.probe2) ? data.state.probe2 + '°F' : 'Unplugged';
                document.getElementById('probe3').innerText = (isOnline && data.state.probe3) ? data.state.probe3 + '°F' : 'Unplugged';
                document.getElementById('grillId').innerText = data.state.grill_id;
                document.getElementById('lastSeen').innerText = data.state.last_seen;
                document.getElementById('dispFlags').innerText = data.state.flags || 'None';

                document.getElementById('alertFlameout').style.display = data.flameout_alert ? 'block' : 'none';

                handleProbeDisplay(1, data.state.probe1, data.probe_targets.probe1, data.probe_alerts.probe1);
                handleProbeDisplay(2, data.state.probe2, data.probe_targets.probe2, data.probe_alerts.probe2);
                handleProbeDisplay(3, data.state.probe3, data.probe_targets.probe3, data.probe_alerts.probe3);

                // Automation Rendering
                const auto = data.automation;
                const setupView = document.getElementById('autoSetupView');
                const activeView = document.getElementById('autoActiveView');

                if (auto && auto.active) {
                    setupView.style.display = 'none';
                    activeView.style.display = 'block';
                    document.getElementById('activeRecipeName').innerText = auto.name;
                    document.getElementById('activeStageIndicator').innerText = `Stage ${auto.stage_idx + 1} of ${auto.total_stages}`;
                    
                    const minElapsed = Math.floor(auto.stage_elapsed_sec / 60);
                    const secElapsed = Math.floor(auto.stage_elapsed_sec % 60);
                    document.getElementById('stageElapsed').innerText = `Elapsed: ${minElapsed}m ${secElapsed}s`;

                    if (auto.current_stage) {
                        document.getElementById('autoStageSetpoint').innerText = `${auto.current_stage.setpoint}°F`;
                        
                        let ruleText = auto.current_stage.name || '';
                        const st = auto.current_stage;
                        if (st.trigger_type in ["probe1", "probe2", "probe3"]) {
                            const pKey = st.trigger_type;
                            const cur = data.state[pKey] || '--';
                            const condStr = st.trigger_cond === 'lte' ? 'drops below or equal to (&le;)' : 'reaches or exceeds (&ge;)';
                            ruleText = `When ${pKey.toUpperCase()} ${condStr} ${st.trigger_val}°F (Current: ${cur}°F)`;
                        } else if (st.trigger_type === 'time') {
                            ruleText = `After ${st.trigger_val} minutes in this stage`;
                        } else if (st.trigger_type === 'hold') {
                            ruleText = `Hold indefinitely until stopped`;
                        }
                        document.getElementById('autoStageCondition').innerHTML = ruleText;
                    }
                } else {
                    setupView.style.display = 'block';
                    activeView.style.display = 'none';
                }

                // Session UI
                const sessBtn = document.getElementById('sessionToggleBtn');
                const sessInput = document.getElementById('sessionNameInput');
                const exportBtn = document.getElementById('exportCsvBtn');
                const sessDisp = document.getElementById('activeSessionDisplay');

                if (activeSession) {
                    sessBtn.innerText = 'End Cook';
                    sessBtn.style.background = '#c62828';
                    sessInput.disabled = true;
                    sessInput.value = activeSession.name;
                    exportBtn.disabled = false;
                    sessDisp.innerText = `${activeSession.name} (Started: ${activeSession.started_at})`;
                } else {
                    sessBtn.innerText = 'Start Cook';
                    sessBtn.style.background = '#ff9800';
                    sessInput.disabled = false;
                    exportBtn.disabled = true;
                    sessDisp.innerText = 'None (Volatile buffer only)';
                }

                // Badges & Interlocks
                const alertBar = document.getElementById('alertLost');
                const badge = document.getElementById('powerBadge');
                const powerBtn = document.getElementById('powerToggleBtn');
                const controlElems = document.querySelectorAll('.control-elem, #tempInput');

                if (!isOnline) {
                    alertBar.style.display = 'block';
                    badge.innerText = 'OFFLINE';
                    badge.className = 'badge badge-lost';
                    powerBtn.disabled = true;
                    powerBtn.innerText = 'Disconnected';
                    controlElems.forEach(el => el.disabled = true);
                } else {
                    alertBar.style.display = 'none';
                    if (isCooldown) {
                        badge.innerText = 'COOLING DOWN';
                        badge.className = 'badge badge-cooldown';
                        powerBtn.disabled = true;
                        powerBtn.innerText = 'Cooldown Locked';
                        powerBtn.className = 'btn-cooldown';
                        controlElems.forEach(el => el.disabled = true);
                    } else if (data.state.power.toUpperCase() === 'ON') {
                        badge.innerText = 'RUNNING (' + data.state.power + ')';
                        badge.className = 'badge badge-online';
                        powerBtn.disabled = false;
                        powerBtn.innerText = 'Start Cooldown';
                        powerBtn.className = 'btn-power-off';
                        controlElems.forEach(el => el.disabled = false);
                    } else {
                        badge.innerText = 'OFF';
                        badge.className = 'badge';
                        powerBtn.disabled = false;
                        powerBtn.innerText = 'Turn ON';
                        powerBtn.className = 'btn-power-on';
                        controlElems.forEach(el => el.disabled = true);
                    }
                }
            } catch (e) {
                console.error("Error polling state:", e);
                document.getElementById('alertLost').style.display = 'block';
            }
        }

        function handleProbeDisplay(num, currentVal, targetVal, isAlerting) {
            const box = document.getElementById(`pBox${num}`);
            const input = document.getElementById(`pTgt${num}`);
            
            if (document.activeElement !== input && targetVal !== null && targetVal !== undefined) {
                input.value = targetVal;
            }

            if (isAlerting) {
                if (!box.classList.contains('probe-alert')) playChime();
                box.classList.add('probe-alert');
            } else {
                box.classList.remove('probe-alert');
            }
        }

        async function setProbeTarget(probeKey, val) {
            const target = val ? parseInt(val, 10) : '';
            await fetch(`/api/probe_target?probe=${probeKey}&target=${target}`, { method: 'POST' });
        }

        async function fetchHistory() {
            try {
                const res = await fetch('/api/history');
                const chartData = await res.json();
                cookChart.data.labels = chartData.timestamps;
                cookChart.data.datasets[0].data = chartData.grill_temp;
                cookChart.data.datasets[1].data = chartData.setpoint;
                cookChart.data.datasets[2].data = chartData.probe1;
                cookChart.data.datasets[3].data = chartData.probe2;
                cookChart.data.datasets[4].data = chartData.probe3;
                cookChart.update();
            } catch (e) {
                console.error("Error updating graph:", e);
            }
        }

        async function fetchEvents() {
            try {
                const res = await fetch('/api/flag_events');
                const events = await res.json();
                const logBox = document.getElementById('eventLog');
                if (events.length > 0) {
                    logBox.innerHTML = events.map(ev => 
                        `<div class="log-entry">
                            <span style="color:#ff9800">[${ev.timestamp}]</span> 
                            <strong>${ev.field_name}:</strong> 
                            ${ev.old_val} &rarr; <span style="color:#4fc3f7">${ev.new_val}</span> 
                            <br><span style="color:#888;">${ev.bit_diff}</span>
                        </div>`
                    ).join('');
                }
            } catch (e) {
                console.error("Error fetching flag events:", e);
            }
        }

        async function toggleSession() {
            if (activeSession) {
                await fetch('/api/session/stop', { method: 'POST' });
                document.getElementById('sessionNameInput').value = '';
            } else {
                const name = document.getElementById('sessionNameInput').value || 'Cook Session';
                await fetch(`/api/session/start?name=${encodeURIComponent(name)}`, { method: 'POST' });
            }
            updateStatus();
            fetchHistory();
        }

        function exportCsv() {
            if (activeSession) {
                window.location.href = `/api/session/export?id=${activeSession.id}`;
            }
        }

        async function togglePower() {
            if (isCooldown || !isOnline) return;
            const nextState = commandedPower === 1 ? 0 : 1;
            await fetch(`/api/power?state=${nextState}`, { method: 'POST' });
            updateStatus();
        }

        async function setPreset(temp) {
            if (!isOnline || isCooldown) return;
            await fetch(`/api/setpoint?temp=${temp}`, { method: 'POST' });
            updateStatus();
        }

        async function adjustTemp(delta) {
            const nextTemp = currentTarget + delta;
            await setPreset(nextTemp);
        }

        async function sendCustomTemp() {
            const val = document.getElementById('tempInput').value;
            if (val) {
                await setPreset(val);
                document.getElementById('tempInput').value = '';
            }
        }

        document.body.addEventListener('click', () => {
            if (!audioContext) audioContext = new (window.AudioContext || window.webkitAudioContext)();
        }, { once: true });

        fetchSettings();
        fetchRecipes();
        setInterval(updateStatus, 2500);
        setInterval(fetchHistory, 5000);
        setInterval(fetchEvents, 5000);
        updateStatus();
        fetchHistory();
        fetchEvents();
    </script>
</body>
</html>
"""

# --- Routes ---

@app.route('/')
def dashboard():
    return render_template_string(HTML_TEMPLATE)

@app.route('/GrillService/Service', methods=['POST'])
def grill_service():
    global last_seen_epoch, prev_flags, flameout_start_epoch, flameout_triggered
    global at_set_alerted, last_alerted_setpoint
    now = time.time()
    now_dt = datetime.datetime.now()
    last_seen_epoch = now

    form = request.form
    grill_state["grill_id"] = form.get("GrillId", "Unknown")
    grill_state["temp"] = form.get("Temp", "--")
    grill_state["power"] = form.get("Power", "OFF")
    grill_state["probe1"] = form.get("Probe1", "")
    grill_state["probe2"] = form.get("Probe2", "")
    grill_state["probe3"] = form.get("Probe3", "")
    grill_state["flags"] = form.get("GrillFlags", "")
    grill_state["last_seen"] = now_dt.strftime("%H:%M:%S")

    time_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    session_id = get_active_session_id()
    pit_temp = parse_val(grill_state["temp"])
    setpoint = parse_val(grill_command["setPoint"])

    # 1. Telemetry Persistence
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO telemetry (session_id, timestamp, grill_temp, setpoint, probe1, probe2, probe3, power, grill_flags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id,
            time_str,
            pit_temp,
            setpoint,
            parse_val(grill_state["probe1"]),
            parse_val(grill_state["probe2"]),
            parse_val(grill_state["probe3"]),
            grill_state["power"],
            grill_state["flags"]
        ))

        if prev_flags is not None and grill_state["flags"] != prev_flags:
            analysis = analyze_bitmask_diff(prev_flags, grill_state["flags"])
            logger.debug("GrillFlags Changed: '%s' -> '%s' | %s", prev_flags, grill_state["flags"], analysis)
            conn.execute("""
                INSERT INTO flag_events (timestamp, field_name, old_val, new_val, bit_diff, context)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (time_str, "GrillFlags", str(prev_flags), str(grill_state["flags"]), analysis, f"Temp: {grill_state['temp']} | Power: {grill_state['power']}"))

        conn.commit()

    prev_flags = grill_state["flags"]

    # 2. Automation Engine
    evaluate_automation(now)

    # 3. Probe Target Alarms
    for p_key in ["probe1", "probe2", "probe3"]:
        target = probe_targets[p_key]
        current = parse_val(grill_state[p_key])
        if target is not None and current is not None and current >= target:
            if not probe_alerted[p_key]:
                probe_alerted[p_key] = True
                logger.info("[ALARM] %s reached target %d°F (Current: %d°F)", p_key.upper(), target, current)
                send_push_notification(f"MAK Grill Alert: {p_key.upper()} Done!", f"Temperature is {current}°F (Target: {target}°F)", "high")
        else:
            if current is not None and target is not None and current < (target - 3):
                probe_alerted[p_key] = False

    # 4. Target Setpoint (ATSET) Notification
    flags_upper = grill_state["flags"].upper()
    if setpoint != last_alerted_setpoint:
        at_set_alerted = False

    if "ATSET" in flags_upper:
        if not at_set_alerted:
            at_set_alerted = True
            last_alerted_setpoint = setpoint
            logger.info("[NOTIFICATION] Pit reached setpoint (%d°F)", setpoint)
            send_push_notification(
                "MAK Grill: Target Temp Reached",
                f"Pit reached setpoint of {setpoint}°F (Current: {pit_temp}°F).",
                "default"
            )
    else:
        if pit_temp is not None and setpoint is not None and pit_temp < (setpoint - 15):
            at_set_alerted = False

    # 5. Flameout Watchdog
    reported_pwr = grill_state["power"].upper()
    if reported_pwr == "ON" and pit_temp is not None and setpoint is not None:
        if pit_temp < (setpoint - 35):
            if flameout_start_epoch is None:
                flameout_start_epoch = now
            elif (now - flameout_start_epoch) >= 480:
                if not flameout_triggered:
                    flameout_triggered = True
                    logger.warning("[ALARM] Flameout detected! Pit temp dropped to %s°F (Setpoint: %s°F)", pit_temp, setpoint)
                    send_push_notification("MAK Grill Flameout Warning!", f"Pit temp dropped to {pit_temp}°F (Setpoint: {setpoint}°F)", "urgent")
        else:
            flameout_start_epoch = None
            flameout_triggered = False
    else:
        flameout_start_epoch = None
        flameout_triggered = False

    # 6. Outbound Command Reset on Shutdown
    if grill_command["power"] == 0 and ("COOL" in reported_pwr or "CD" in reported_pwr or reported_pwr == "OFF"):
        logger.info("Cooldown acknowledged by grill (%s). Resetting power command to 1.", reported_pwr)
        grill_command["power"] = 1

    payload = f'"setPoint={grill_command["setPoint"]}&potStatus={grill_command["potStatus"]}&cookMode={grill_command["cookMode"]}&zoneProbe={grill_command["zoneProbe"]}&power={grill_command["power"]}"'
    return Response(payload, status=200, mimetype='text/html')

@app.route('/api/status', methods=['GET'])
def get_status():
    now = time.time()
    is_online = (now - last_seen_epoch) < 15.0 if last_seen_epoch > 0 else False
    reported_pwr = grill_state["power"].upper()
    is_cooldown = is_online and (
        "COOL" in reported_pwr or reported_pwr == "CD" or (grill_command["power"] == 0 and reported_pwr != "OFF")
    )

    active_session = None
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, started_at FROM sessions WHERE active = 1 ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        if row:
            active_session = {"id": row[0], "name": row[1], "started_at": row[2]}

    auto_data = {
        "active": automation_state["active"],
        "name": automation_state["name"],
        "stage_idx": automation_state["stage_idx"],
        "total_stages": len(automation_state["stages"]),
        "stage_elapsed_sec": (now - automation_state["stage_started_epoch"]) if automation_state["stage_started_epoch"] else 0,
        "current_stage": automation_state["stages"][automation_state["stage_idx"]] if (automation_state["active"] and automation_state["stage_idx"] < len(automation_state["stages"])) else None
    }

    return jsonify({
        "state": grill_state,
        "command": grill_command,
        "is_online": is_online,
        "is_cooldown": is_cooldown,
        "active_session": active_session,
        "probe_targets": probe_targets,
        "probe_alerts": probe_alerted,
        "flameout_alert": flameout_triggered,
        "at_set": "ATSET" in grill_state["flags"].upper(),
        "automation": auto_data
    })

# --- Settings Endpoints ---

@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify(app_settings)

@app.route('/api/settings', methods=['POST'])
def save_settings():
    data = request.get_json() or {}
    if "ntfy_topic" in data:
        topic_val = str(data["ntfy_topic"]).strip()
        app_settings["ntfy_topic"] = topic_val
        with get_db_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('ntfy_topic', ?)", (topic_val,))
            conn.commit()
        logger.info("Updated ntfy_topic setting to '%s'", topic_val)
    return jsonify({"success": True, "settings": app_settings})

@app.route('/api/settings/test_ntfy', methods=['POST'])
def test_ntfy():
    topic = app_settings.get("ntfy_topic", "").strip()
    if not topic:
        return jsonify({"success": False, "error": "No ntfy topic configured"}), 400
    try:
        send_push_notification("MAK Controller Test", "If you see this, push notifications are working!", "default")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# --- Recipe Endpoints ---

@app.route('/api/recipes', methods=['GET'])
def get_recipes():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, stages_json FROM recipes ORDER BY id ASC")
        rows = cursor.fetchall()
    return jsonify([
        {"id": r[0], "name": r[1], "stages": json.loads(r[2])}
        for r in rows
    ])

@app.route('/api/recipes', methods=['POST'])
def save_recipe():
    data = request.get_json() or {}
    rec_id = data.get("id")
    name = data.get("name", "Custom Recipe").strip()
    stages = data.get("stages", [])

    if not name or not stages:
        return jsonify({"success": False, "error": "Name and stages required"}), 400

    stages_json = json.dumps(stages)
    with get_db_connection() as conn:
        if rec_id:
            conn.execute("UPDATE recipes SET name = ?, stages_json = ? WHERE id = ?", (name, stages_json, rec_id))
            target_id = rec_id
        else:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO recipes (name, stages_json) VALUES (?, ?)", (name, stages_json))
            target_id = cursor.lastrowid
        conn.commit()

    return jsonify({"success": True, "id": target_id})

@app.route('/api/recipes/<int:rec_id>', methods=['DELETE'])
def delete_recipe_endpoint(rec_id):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM recipes WHERE id = ?", (rec_id,))
        conn.commit()
    return jsonify({"success": True})

# --- Automation Endpoints ---

@app.route('/api/automation/start', methods=['POST'])
def start_auto():
    data = request.get_json() or {}
    name = data.get("name", "Custom Recipe")
    stages = data.get("stages", [])
    if not stages:
        return jsonify({"success": False, "error": "No stages defined"}), 400

    automation_state["active"] = True
    automation_state["name"] = name
    automation_state["stage_idx"] = 0
    automation_state["stage_started_epoch"] = None
    automation_state["stages"] = stages
    return jsonify({"success": True})

@app.route('/api/automation/stop', methods=['POST'])
def stop_auto():
    automation_state["active"] = False
    automation_state["stages"] = []
    automation_state["stage_started_epoch"] = None
    return jsonify({"success": True})

@app.route('/api/automation/next', methods=['POST'])
def next_auto_stage():
    if automation_state["active"]:
        next_idx = automation_state["stage_idx"] + 1
        if next_idx < len(automation_state["stages"]):
            automation_state["stage_idx"] = next_idx
            automation_state["stage_started_epoch"] = time.time()
            next_stage = automation_state["stages"][next_idx]
            grill_command["setPoint"] = int(next_stage["setpoint"])
        else:
            automation_state["active"] = False
    return jsonify({"success": True})

@app.route('/api/probe_target', methods=['POST'])
def set_probe_target():
    probe = request.args.get('probe')
    target = request.args.get('target')
    if probe in probe_targets:
        probe_targets[probe] = int(target) if target and target.isdigit() else None
        probe_alerted[probe] = False
        return jsonify({"success": True, "probe": probe, "target": probe_targets[probe]})
    return jsonify({"success": False, "error": "Invalid probe key"}), 400

# --- Maintenance & Telemetry Pruning ---

@app.route('/api/maintenance/prune', methods=['POST'])
def prune_database():
    days = request.args.get('days', default=30, type=int)
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    volatile_cutoff = (datetime.datetime.now() - datetime.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

    with get_db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("DELETE FROM telemetry WHERE session_id IS NULL AND timestamp < ?", (volatile_cutoff,))
        pruned_volatile = cursor.rowcount

        cursor.execute("SELECT id FROM sessions WHERE active = 0 AND ended_at IS NOT NULL AND ended_at < ?", (cutoff,))
        old_session_ids = [r[0] for r in cursor.fetchall()]

        pruned_session_telemetry = 0
        if old_session_ids:
            placeholders = ','.join('?' * len(old_session_ids))
            cursor.execute(f"DELETE FROM telemetry WHERE session_id IN ({placeholders})", old_session_ids)
            pruned_session_telemetry = cursor.rowcount
            cursor.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", old_session_ids)

        cursor.execute("DELETE FROM flag_events WHERE timestamp < ?", (cutoff,))
        pruned_flag_events = cursor.rowcount

        conn.commit()

    logger.info("Maintenance Prune: Removed %d volatile rows, %d session telemetry rows, %d sessions, %d flag logs.",
                pruned_volatile, pruned_session_telemetry, len(old_session_ids), pruned_flag_events)

    return jsonify({
        "success": True,
        "pruned_volatile": pruned_volatile,
        "pruned_session_telemetry": pruned_session_telemetry,
        "pruned_sessions": len(old_session_ids),
        "pruned_flag_events": pruned_flag_events
    })

@app.route('/api/history', methods=['GET'])
def get_history():
    session_id = get_active_session_id()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if session_id:
            cursor.execute("""
                SELECT timestamp, grill_temp, setpoint, probe1, probe2, probe3 
                FROM telemetry WHERE session_id = ? ORDER BY id ASC
            """, (session_id,))
        else:
            cursor.execute("""
                SELECT timestamp, grill_temp, setpoint, probe1, probe2, probe3 
                FROM telemetry ORDER BY id DESC LIMIT 500
            """)
        rows = cursor.fetchall()

    if not session_id:
        rows.reverse()

    return jsonify({
        "timestamps": [r[0].split(' ')[-1] for r in rows],
        "grill_temp": [r[1] for r in rows],
        "setpoint": [r[2] for r in rows],
        "probe1": [r[3] for r in rows],
        "probe2": [r[4] for r in rows],
        "probe3": [r[5] for r in rows]
    })

@app.route('/api/flag_events', methods=['GET'])
def get_flag_events():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp, field_name, old_val, new_val, bit_diff, context FROM flag_events ORDER BY id DESC LIMIT 20")
        rows = cursor.fetchall()
    return jsonify([
        {"timestamp": r[0].split(' ')[-1], "field_name": r[1], "old_val": r[2], "new_val": r[3], "bit_diff": r[4], "context": r[5]}
        for r in rows
    ])

@app.route('/api/session/start', methods=['POST'])
def start_session():
    name = request.args.get('name', 'Cook Session')
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        conn.execute("UPDATE sessions SET active = 0, ended_at = ? WHERE active = 1", (now,))
        conn.execute("INSERT INTO sessions (name, started_at, active) VALUES (?, ?, 1)", (name, now))
        conn.commit()
    return jsonify({"success": True})

@app.route('/api/session/stop', methods=['POST'])
def stop_session():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        conn.execute("UPDATE sessions SET active = 0, ended_at = ? WHERE active = 1", (now,))
        conn.commit()
    return jsonify({"success": True})

@app.route('/api/session/export', methods=['GET'])
def export_csv():
    session_id = request.args.get('id', type=int)
    if not session_id:
        return "Session ID required", 400

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Timestamp", "Grill Temp", "SetPoint", "Probe 1", "Probe 2", "Probe 3", "Power", "GrillFlags"])

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT timestamp, grill_temp, setpoint, probe1, probe2, probe3, power, grill_flags
            FROM telemetry WHERE session_id = ? ORDER BY id ASC
        """, (session_id,))
        for row in cursor.fetchall():
            writer.writerow(row)

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=cook_session_{session_id}.csv"}
    )

@app.route('/api/setpoint', methods=['POST'])
def set_setpoint():
    temp = request.args.get('temp', type=int)
    if temp:
        grill_command["setPoint"] = temp
        return jsonify({"success": True, "setPoint": temp})
    return jsonify({"success": False, "error": "Invalid temp"}), 400

@app.route('/api/power', methods=['POST'])
def set_power():
    state = request.args.get('state', type=int)
    now = time.time()
    is_online = (now - last_seen_epoch) < 15.0 if last_seen_epoch > 0 else False
    reported_pwr = grill_state["power"].upper()
    is_cooldown = is_online and (
        "COOL" in reported_pwr or reported_pwr == "CD" or (grill_command["power"] == 0 and reported_pwr != "OFF")
    )

    if is_cooldown and state == 1:
        return jsonify({"success": False, "error": "Grill is cooling down"}), 400

    if state in (0, 1):
        grill_command["power"] = state
        return jsonify({"success": True, "power": state})
    return jsonify({"success": False, "error": "Invalid state"}), 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)
