"""Local coordinator window around the existing, guarded runner workflow."""

from dataclasses import asdict
import importlib.util
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading

from .api import ApiError
from .config import RunnerConfig, atomic_write
from .models import ContractError, canonical_bytes, canonical_subject, strict_json
from .pgl_adapter import RunSettings
from .runner import StudyRunner


def runtime_problem():
    if sys.platform != "darwin" or sys.version_info < (3, 12):
        return "Presentation requires macOS and Python 3.12+. Preparation and upload are still available."
    if importlib.util.find_spec("pgl") is None:
        return "PGL is not installed in this Python. Install the experiment extra before starting a test."
    return None


class LauncherModel:
    def __init__(self, runner):
        self.runner = runner
        self.context = None
        self.subject = None
        self.ready = False
        self.pairing = None

    def load(self):
        self.context = None
        self.subject = None
        self.ready = False
        self.pairing = None
        pairing = self.runner.pairing_identity()
        context = self.runner.study()
        if (pairing != self.runner.pairing_identity()
                or pairing["experiment_id"] != context["experiment_id"]):
            raise ContractError("The connection changed. Refresh the study before proceeding")
        self.pairing = pairing
        self.context = context
        return self.context

    def select(self, subject):
        self.subject = None
        self.ready = False
        if not self.context or subject not in {row["subject_id"] for row in self.context["subjects"]}:
            raise ContractError("Select a subject assigned to this paired study")
        self.subject = subject

    def _selected(self):
        if not self.context or not self.subject:
            raise ContractError("Select a paired study and an assigned subject first")
        if self.pairing != self.runner.pairing_identity():
            self.ready = False
            raise ContractError("The connection changed. Refresh the study before proceeding")
        return self.subject

    def prepare(self):
        self.ready = False
        self.runner.prepare(self._selected())
        return self.status()

    def status(self):
        self.ready = False
        result = self.runner.status(self._selected())
        self.ready = result.get("preparation_ready") is True and "attempt" not in result
        return result

    def run(self, *, integration_test=False, **options):
        subject = self._selected()
        if integration_test is not True or not self.ready:
            raise ContractError("Prepare first and acknowledge non-participant testing")
        self.status()
        if not self.ready:
            raise ContractError("This subject already has an attempt or needs preparation; do not replay")
        self.ready = False
        return run_in_child(self.runner, subject, expected_pairing=self.pairing, **options)

    def sync(self):
        self.ready = False
        return self.runner.sync(self._selected())


def run_in_child(runner, subject, *, ffmpeg=None, settings=None, expected_pairing=None):
    subject = canonical_subject(subject)
    config = RunnerConfig.load(runner.config_dir)
    pairing = {"server_origin": config.server_origin, "device_id": config.device_id,
               "experiment_id": config.experiment_id}
    if expected_pairing is not None and pairing != expected_pairing:
        raise ContractError("The connection changed. Refresh the study before proceeding")
    token = config.read_token(runner.config_dir)
    settings = settings or RunSettings()
    command = [sys.executable, "-m", "dbp_pgl_runner",
               "--cache-root", str(runner.cache_root), "--work-root", str(runner.work_root),
               "run", subject, "--integration-test", "--day", str(settings.day),
               "--block", str(settings.block), "--description-seconds", str(settings.description_seconds),
               "--display-width", str(settings.display_width)]
    for flag, value in (("--ffmpeg", ffmpeg), ("--settings-name", settings.settings_name),
                        ("--display-name", settings.display_name)):
        if value:
            command.extend([flag, value])
    with tempfile.TemporaryDirectory(prefix="dbp-pgl-launch-") as temporary:
        snapshot = Path(temporary)
        atomic_write(snapshot / config.token_ref, token.encode("ascii"))
        atomic_write(snapshot / "config.json", canonical_bytes(asdict(config)))
        command[3:3] = ["--config-dir", str(snapshot)]
        output = bytearray()
        with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT) as child:
            while chunk := child.stdout.read(8192):
                output.extend(chunk)
                if len(output) > 65536:
                    del output[:-65536]
            returncode = child.wait()
        lines = output.splitlines()
    if lines:
        try:
            result = strict_json(lines[-1])
        except ContractError:
            result = None
        if (type(result) is dict and result.get("status") in {"completed", "terminated"}
                and (returncode == 0 or result["status"] == "terminated")):
            return result
    raise ContractError("PGL did not report completion. Inspect local status before any retry; "
                        "check Python 3.12+, the PGL installation, FFmpeg and lab profiles. "
                        "An interrupted task must not be replayed automatically.")


class LauncherWindow:
    def __init__(self, root, *, config_dir, cache_root, work_root):
        import tkinter as tk
        from tkinter import ttk
        from .profiles import ConnectionProfiles

        self.root = root
        self.tk = tk
        self.ttk = ttk
        self.profiles = ConnectionProfiles(config_dir)
        self.cache_root = cache_root
        self.work_root = work_root
        self.model = None
        self.runtime_problem = runtime_problem()
        self.busy = False
        self.events = queue.Queue()
        self.controls = []
        root.title("Digital Brain · Pilot launcher")
        root.minsize(660, 590)
        root.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(root, padding=24)
        body.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="Digital Brain pilot", font=("Helvetica", 20, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(body, text="Non-participant testing only · No task starts automatically").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(4, 20))
        ttk.Label(body, text="1  Connection").grid(row=2, column=0, sticky="w", padx=(0, 14))
        self.connection = tk.StringVar()
        self.connections = ttk.Combobox(body, textvariable=self.connection, state="readonly")
        self.connections.grid(row=2, column=1, sticky="ew")
        self.connections.bind("<<ComboboxSelected>>", lambda event: self.load_connection())
        self.controls.append((self.connections, "readonly"))
        self.button(body, "Pair study…", self.pair).grid(row=2, column=2, padx=(10, 0))
        self.context_text = tk.StringVar(value="Choose a saved connection, or pair a study from the website.")
        ttk.Label(body, textvariable=self.context_text, wraplength=600).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(12, 18))
        ttk.Label(body, text="2  Subject").grid(row=4, column=0, sticky="w")
        self.subject = tk.StringVar()
        self.subjects = ttk.Combobox(body, textvariable=self.subject, state="readonly")
        self.subjects.grid(row=4, column=1, sticky="ew")
        self.subjects.bind("<<ComboboxSelected>>", lambda event: self.choose_subject())
        self.controls.append((self.subjects, "readonly"))
        self.button(body, "Refresh study", self.load_connection).grid(row=4, column=2, padx=(10, 0))
        self.trials_text = tk.StringVar(value="Select an assigned subject; IDs are never typed into code.")
        ttk.Label(body, textvariable=self.trials_text).grid(row=5, column=0, columnspan=3, sticky="w", pady=12)
        options = ttk.LabelFrame(body, text="Workstation settings (configured by the lab)", padding=10)
        options.grid(row=6, column=0, columnspan=3, sticky="ew")
        options.columnconfigure(1, weight=1)
        self.ffmpeg = tk.StringVar()
        self.settings_name = tk.StringVar()
        self.display_name = tk.StringVar()
        for row, (label, variable) in enumerate((("FFmpeg path (optional)", self.ffmpeg),
                                                ("PGL settings profile", self.settings_name),
                                                ("PGL display profile", self.display_name))):
            ttk.Label(options, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
            entry = ttk.Entry(options, textvariable=variable)
            entry.grid(row=row, column=1, sticky="ew")
            self.controls.append((entry, "normal"))
        self.ttk.Label(options, text=self.runtime_problem or "PGL package found; lab profiles and hardware still need validation.",
                       wraplength=560).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.ack = tk.BooleanVar(value=False)
        acknowledgement = ttk.Checkbutton(body, text="This is a non-participant rehearsal, not data collection.",
                                           variable=self.ack, command=self.update_controls)
        acknowledgement.grid(row=7, column=0, columnspan=3, sticky="w", pady=16)
        self.controls.append((acknowledgement, "normal"))
        actions = ttk.Frame(body)
        actions.grid(row=8, column=0, columnspan=3, sticky="w")
        self.prepare_button = self.button(actions, "3  Prepare videos", lambda: self.action("prepare"))
        self.prepare_button.pack(side="left", padx=(0, 8))
        self.start_button = self.button(actions, "4  Start test…", self.start)
        self.start_button.pack(side="left", padx=(0, 8))
        self.status_button = self.button(actions, "Check status", lambda: self.action("status"))
        self.status_button.pack(side="left", padx=(0, 8))
        self.sync_button = self.button(actions, "Retry upload", lambda: self.action("sync"))
        self.sync_button.pack(side="left")
        self.status_text = tk.StringVar(value="Pairing links this workstation to one published assignment. "
                                            "Save separate connections to switch studies.")
        ttk.Label(body, textvariable=self.status_text, wraplength=600, justify="left").grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(18, 0))
        self.refresh_profiles()
        self.update_controls()
        self.poll_id = root.after(100, self.poll)

    def button(self, parent, text, command):
        button = self.ttk.Button(parent, text=text, command=command)
        self.controls.append((button, "normal"))
        return button

    def refresh_profiles(self):
        self.connections.configure(values=self.profiles.names())

    def update_controls(self):
        for widget, state in self.controls:
            widget.configure(state="disabled" if self.busy else state)
        selected = self.model is not None and self.model.subject is not None and not self.busy
        for button in (self.prepare_button, self.status_button, self.sync_button):
            button.configure(state="normal" if selected else "disabled")
        self.start_button.configure(state="normal" if selected and self.model.ready and self.ack.get()
                                    and not self.runtime_problem else "disabled")

    def submit(self, label, operation, callback):
        if self.busy:
            return
        self.busy = True
        self.status_text.set(label)
        self.update_controls()
        def work():
            try:
                self.events.put((callback, operation(), None))
            except (ContractError, ApiError) as error:
                self.events.put((callback, None, str(error)))
            except Exception:
                self.events.put((callback, None, "Operation failed. Check local status before retrying; "
                                 "no automatic replay was requested."))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            try:
                callback, result, error = self.events.get_nowait()
            except queue.Empty:
                return
            else:
                self.busy = False
                if error:
                    self.status_text.set(error)
                else:
                    callback(result)
        except Exception:
            self.model = None
            self.subject.set("")
            self.subjects.configure(values=())
            self.status_text.set("Updating the launcher failed. Refresh the study and inspect local status before retrying.")
        finally:
            self.update_controls()
            self.poll_id = self.root.after(100, self.poll)

    def load_connection(self):
        if self.busy:
            return
        self.subject.set("")
        self.subjects.configure(values=())
        self.ack.set(False)
        self.model = None
        self.context_text.set("Loading the paired assignment…")
        self.trials_text.set("Select a subject after the assignment loads.")
        try:
            directory = self.profiles.directory(self.connection.get())
        except ContractError as error:
            self.status_text.set(str(error))
            self.update_controls()
            return
        model = LauncherModel(StudyRunner(config_dir=directory, cache_root=self.cache_root, work_root=self.work_root))
        def loaded(context):
            self.model = model
            self.subjects.configure(values=[row["subject_id"] for row in context["subjects"]])
            self.context_text.set(f"{context['study_name']}\n{model.pairing['server_origin']}\n"
                                  f"Published assignment: {context['experiment_id']}")
            self.status_text.set("Assignment verified. Choose a subject to prepare or inspect.")
        self.submit("Verifying connection and fetching assigned subjects…", model.load, loaded)

    def choose_subject(self):
        if self.model and not self.busy:
            self.model.select(self.subject.get())
            self.ack.set(False)
            subject = next(row for row in self.model.context["subjects"] if row["subject_id"] == self.model.subject)
            self.trials_text.set(f"{subject['trial_count']} assigned trials · Includes assigned repeats and foils")
            self.status_text.set("Prepare videos before starting. Check status to inspect existing results.")
            self.update_controls()

    def show_result(self, result):
        attempt = result.get("attempt", result)
        if "status" in attempt:
            upload = "Uploaded" if attempt.get("sync_status") == "synced" else "Upload pending"
            self.status_text.set(f"Task: {attempt['status']} · {upload}\n"
                                 f"Completed trials: {attempt.get('completed_trials', 'unknown')}\n"
                                 f"Saved results: {attempt.get('attempt_root', 'inspect local status')}\n"
                                 "Retry upload does not replay videos. Interrupted attempts require review.")
        else:
            self.status_text.set("Videos prepared and hashes verified. A full decode check runs before PGL starts. "
                                 "Confirm the subject and non-participant acknowledgement to start.")

    def action(self, name):
        if self.model and self.model.subject and not self.busy:
            labels = {"prepare": "Preparing videos; no task is running…", "status": "Checking local results…",
                      "sync": "Retrying upload only; videos will not replay…"}
            self.submit(labels[name], getattr(self.model, name), self.show_result)

    def start(self):
        from tkinter import messagebox
        if self.busy or not self.model or not self.model.ready or not self.ack.get() or self.runtime_problem:
            return
        context = self.model.context
        confirmed = messagebox.askokcancel("Confirm non-participant test",
            f"Study: {context['study_name']}\nAssignment: {context['experiment_id']}\n"
            f"Subject: {self.model.subject}\n\nPGL will open its task display. "
            "The entire assigned integration block will play; day/block labels are 1/1. "
            "This is not an approved participant schedule. Continue?", parent=self.root)
        if confirmed:
            settings = RunSettings(settings_name=self.settings_name.get().strip() or None,
                                   display_name=self.display_name.get().strip() or None)
            ffmpeg = self.ffmpeg.get().strip() or None
            model = self.model
            self.submit("Checking decoding and running PGL. Use the task's Escape control to interrupt. "
                        "Keep this launcher open; results will save locally before upload.",
                        lambda: model.run(integration_test=True, ffmpeg=ffmpeg, settings=settings), self.show_result)

    def pair(self):
        from tkinter import messagebox
        if self.busy:
            return
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Pair a published study")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = self.ttk.Frame(dialog, padding=20)
        frame.grid(sticky="nsew")
        frame.columnconfigure(1, weight=1)
        self.ttk.Label(frame, wraplength=480, text="In the website, publish the study's integration assignment "
                       "and choose Pair workstation. Its one-time code identifies the assignment.").grid(
                           row=0, column=0, columnspan=2, pady=(0, 14))
        variables = {}
        for row, (key, label, value) in enumerate((("name", "Connection name", ""),
                ("origin", "Website origin", "http://127.0.0.1:8769"),
                ("code", "One-time pairing code", "")), start=1):
            variable = self.tk.StringVar(value=value)
            variables[key] = variable
            self.ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=6)
            self.ttk.Entry(frame, textvariable=variable, show="•" if key == "code" else "", width=36).grid(
                row=row, column=1, sticky="ew")
        self.ttk.Label(frame, text="Use a new name, e.g. hands-pilot. Existing connections are not replaced.").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=8)
        consent = self.tk.BooleanVar(value=False)
        self.ttk.Checkbutton(frame, text="Store the device credential in a private file on this computer.",
                             variable=consent).grid(row=5, column=0, columnspan=2, sticky="w")
        def connect():
            if not consent.get():
                messagebox.showerror("Storage consent needed", "Confirm private-file storage or cancel pairing.", parent=dialog)
                return
            name = variables["name"].get().strip()
            origin = variables["origin"].get().strip()
            code = variables["code"].get().strip()
            variables["code"].set("")
            dialog.destroy()
            def paired(result):
                self.refresh_profiles()
                self.connection.set(name)
                self.load_connection()
            self.submit("Pairing with the published assignment…", lambda: self.profiles.connect(
                name, origin, code, socket.gethostname(), allow_file_token=True), paired)
        self.ttk.Button(frame, text="Pair study", command=connect).grid(row=6, column=1, sticky="e", pady=(16, 0))

    def close(self):
        from tkinter import messagebox
        if self.busy:
            messagebox.showinfo("Operation in progress", "Keep the launcher open. During presentation, use "
                                "PGL's Escape control to interrupt safely.", parent=self.root)
            return
        self.root.after_cancel(self.poll_id)
        self.root.destroy()


def launch(*, config_dir, cache_root, work_root):
    try:
        import tkinter as tk
    except ImportError:
        raise ContractError("The launcher requires Python with Tk support; CLI commands remain available") from None
    try:
        root = tk.Tk()
    except tk.TclError:
        raise ContractError("The launcher needs a local graphical desktop and working Tk installation") from None
    LauncherWindow(root, config_dir=config_dir, cache_root=cache_root, work_root=work_root)
    root.mainloop()
    return 0
