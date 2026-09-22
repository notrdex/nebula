import os, secrets, sqlite3, shlex
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
import docker
from docker.errors import DockerException, NotFound, APIError

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))
DB = os.getenv("DB_PATH", "panel.db")

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin")

DOCKER_HOST = os.getenv("DOCKER_HOST", "")
IMAGE = os.getenv("CONTAINER_IMAGE", "ubuntu:24.04")
MAX_VMS = int(os.getenv("MAX_VMS", "3"))

# These are the resource-pool limits shown/enforced by the panel.
# Set them to the resources available to the Docker host you actually control.
POOL_RAM_MB = int(os.getenv("POOL_RAM_MB", "32768"))
POOL_CPU = float(os.getenv("POOL_CPU", "8"))
POOL_STORAGE_GB = float(os.getenv("POOL_STORAGE_GB", "100"))

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS vms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        container_id TEXT NOT NULL,
        ram_mb INTEGER NOT NULL,
        cpu REAL NOT NULL,
        storage_gb REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'created'
    )""")
    con.commit()
    return con

def docker_client():
    # Uses local Docker socket by default. For Render, set DOCKER_HOST/TLS
    # to a Docker daemon you control.
    if DOCKER_HOST:
        return docker.DockerClient(base_url=DOCKER_HOST)
    return docker.from_env()

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

@app.context_processor
def inject():
    con = db()
    rows = con.execute("SELECT * FROM vms ORDER BY id DESC").fetchall()
    con.close()
    used_ram = sum(r["ram_mb"] for r in rows)
    used_cpu = sum(r["cpu"] for r in rows)
    used_storage = sum(r["storage_gb"] for r in rows)
    return {
        "vm_count": len(rows),
        "used_ram": used_ram,
        "used_cpu": used_cpu,
        "used_storage": used_storage,
        "pool_ram": POOL_RAM_MB,
        "pool_cpu": POOL_CPU,
        "pool_storage": POOL_STORAGE_GB
    }

@app.route("/")
def index():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if secrets.compare_digest(u, ADMIN_USER) and secrets.compare_digest(p, ADMIN_PASS):
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        flash("Invalid credentials.")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    con = db()
    rows = con.execute("SELECT * FROM vms ORDER BY id DESC").fetchall()
    con.close()
    return render_template("dashboard.html", vms=rows)

@app.route("/vm/create", methods=["POST"])
@login_required
def create_vm():
    name = request.form.get("name", "").strip()
    ram = int(request.form.get("ram_mb", "512"))
    cpu = float(request.form.get("cpu", "1"))
    storage = float(request.form.get("storage_gb", "1"))

    if not name or not name.replace("-", "").replace("_", "").isalnum():
        flash("Use a simple VM name (letters, numbers, - or _).")
        return redirect(url_for("dashboard"))

    con = db()
    rows = con.execute("SELECT * FROM vms").fetchall()
    if len(rows) >= MAX_VMS:
        con.close()
        flash(f"VM limit reached ({MAX_VMS}).")
        return redirect(url_for("dashboard"))

    used_ram = sum(r["ram_mb"] for r in rows)
    used_cpu = sum(r["cpu"] for r in rows)
    used_storage = sum(r["storage_gb"] for r in rows)

    if ram <= 0 or cpu <= 0 or storage <= 0:
        con.close()
        flash("Resource values must be positive.")
        return redirect(url_for("dashboard"))
    if used_ram + ram > POOL_RAM_MB or used_cpu + cpu > POOL_CPU or used_storage + storage > POOL_STORAGE_GB:
        con.close()
        flash("Not enough resource-pool capacity.")
        return redirect(url_for("dashboard"))

    try:
        client = docker_client()
        client.ping()
        # Docker enforces CPU/RAM. Storage is tracked by the panel; a true
        # hard per-container disk quota depends on the host's storage driver.
        container = client.containers.run(
            IMAGE,
            command="bash -lc 'while true; do sleep 3600; done'",
            name=f"panel_{name}",
            detach=True,
            tty=True,
            stdin_open=True,
            mem_limit=f"{ram}m",
            nano_cpus=int(cpu * 1_000_000_000),
            labels={"panel.vm": "true", "panel.storage_gb": str(storage)},
        )
        con.execute(
            "INSERT INTO vms(name,container_id,ram_mb,cpu,storage_gb,status) VALUES(?,?,?,?,?,?)",
            (name, container.id, ram, cpu, storage, "running")
        )
        con.commit()
        flash(f"{name} created.")
    except DockerException as e:
        flash("Docker host is unavailable. Configure a reachable Docker daemon before creating containers.")
    except Exception as e:
        flash(f"Create failed: {e}")
    finally:
        con.close()
    return redirect(url_for("dashboard"))

def get_vm(vm_id):
    con = db()
    row = con.execute("SELECT * FROM vms WHERE id=?", (vm_id,)).fetchone()
    con.close()
    return row

@app.route("/vm/<int:vm_id>")
@login_required
def vm_detail(vm_id):
    vm = get_vm(vm_id)
    if not vm:
        return "VM not found", 404
    return render_template("vm.html", vm=vm)

@app.post("/vm/<int:vm_id>/action")
@login_required
def vm_action(vm_id):
    action = request.form.get("action")
    vm = get_vm(vm_id)
    if not vm:
        return jsonify(ok=False, error="VM not found"), 404
    try:
        c = docker_client().containers.get(vm["container_id"])
        if action == "start": c.start()
        elif action == "stop": c.stop()
        elif action == "restart": c.restart()
        elif action == "delete":
            c.remove(force=True)
            con = db(); con.execute("DELETE FROM vms WHERE id=?", (vm_id,)); con.commit(); con.close()
            return redirect(url_for("dashboard"))
        else:
            return jsonify(ok=False, error="Unknown action"), 400
        return redirect(url_for("vm_detail", vm_id=vm_id))
    except DockerException as e:
        flash(f"Docker error: {e}")
        return redirect(url_for("vm_detail", vm_id=vm_id))

@app.post("/vm/<int:vm_id>/exec")
@login_required
def vm_exec(vm_id):
    vm = get_vm(vm_id)
    if not vm:
        return jsonify(ok=False, error="VM not found"), 404
    command = request.json.get("command", "").strip()
    if not command:
        return jsonify(ok=False, output="")
    try:
        c = docker_client().containers.get(vm["container_id"])
        exit_code, output = c.exec_run(["bash", "-lc", command], tty=False)
        return jsonify(ok=True, code=exit_code, output=output.decode("utf-8", "replace"))
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

@app.get("/api/stats")
@login_required
def stats():
    con = db()
    rows = con.execute("SELECT * FROM vms").fetchall()
    con.close()
    return jsonify(
        vms=len(rows), max_vms=MAX_VMS,
        ram={"used": sum(r["ram_mb"] for r in rows), "total": POOL_RAM_MB},
        cpu={"used": sum(r["cpu"] for r in rows), "total": POOL_CPU},
        storage={"used": sum(r["storage_gb"] for r in rows), "total": POOL_STORAGE_GB},
    )

if __name__ == "__main__":
    db().close()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
