from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import subprocess
from typing import Callable, Iterable


@dataclass(frozen=True, slots=True)
class QuickAction:
    label: str
    callback: Callable[[], None]


class ApprovalDialog:
    """Non-blocking approval window that the Desktop lifecycle can dismiss."""

    def __init__(self, window, on_decision: Callable[[bool], None]) -> None:
        self.window = window
        self.on_decision = on_decision
        self.closed = False

    def close(self, approved: bool) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.window.grab_release()
        except Exception:
            pass
        try:
            self.window.destroy()
        except Exception:
            pass
        self.on_decision(bool(approved))


def filter_quick_actions(actions: Iterable[QuickAction], query: str) -> tuple[QuickAction, ...]:
    needle = query.strip().casefold()
    return tuple(action for action in actions if not needle or needle in action.label.casefold())


def elapsed_time_label(started_at: str, completed_at: str) -> str:
    if not started_at or not completed_at:
        return "Pending"
    try:
        elapsed = max(0.0, (datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)).total_seconds())
    except (TypeError, ValueError):
        return "Unavailable"
    if elapsed < 60:
        return f"{elapsed:.1f}s"
    minutes, seconds = divmod(round(elapsed), 60)
    return f"{minutes}m {seconds:02d}s"


def show_command_palette(root, tk, ttk, actions: tuple[QuickAction, ...]) -> None:
    palette = tk.Toplevel(root)
    palette.title("Quick Actions")
    palette.geometry("520x360")
    palette.transient(root)
    palette.configure(background="#f7f8fb")
    palette.columnconfigure(0, weight=1)
    palette.rowconfigure(1, weight=1)
    query = tk.StringVar()
    entry = ttk.Entry(palette, textvariable=query, style="Prompt.TEntry")
    entry.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 10))
    results = tk.Listbox(
        palette, borderwidth=0, highlightthickness=1, highlightbackground="#dce2eb",
        background="#ffffff", foreground="#253047", selectbackground="#dce8ff",
        selectforeground="#203b70", font=("Helvetica Neue", 12), activestyle="none",
    )
    results.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 18))
    visible_actions: list[QuickAction] = []

    def refresh(*_args) -> None:
        visible_actions[:] = filter_quick_actions(actions, query.get())
        results.delete(0, "end")
        for action in visible_actions:
            results.insert("end", action.label)
        if visible_actions:
            results.selection_set(0)

    def invoke(_event=None) -> None:
        selected = results.curselection()
        if not selected:
            return
        action = visible_actions[selected[0]]
        palette.destroy()
        action.callback()

    query.trace_add("write", refresh)
    results.bind("<Double-Button-1>", invoke)
    results.bind("<Return>", invoke)
    entry.bind("<Return>", invoke)
    palette.bind("<Escape>", lambda _event: palette.destroy())
    refresh()
    entry.focus_set()


def show_approval_dialog(
    root,
    tk,
    ttk,
    prompt: str,
    on_decision: Callable[[bool], None],
) -> ApprovalDialog:
    window = tk.Toplevel(root)
    dialog = ApprovalDialog(window, on_decision)
    window.title("SaiWorks action approval")
    window.geometry("580x430")
    window.transient(root)
    window.configure(background="#f7f8fb")
    window.columnconfigure(0, weight=1)
    window.rowconfigure(1, weight=1)
    ttk.Label(window, text="Review requested action", style="Title.TLabel").grid(
        row=0, column=0, sticky="w", padx=22, pady=(20, 12)
    )
    details = tk.Text(
        window, wrap="word", state="normal", borderwidth=0,
        highlightthickness=1, highlightbackground="#dce2eb",
        background="#ffffff", foreground="#253047", padx=14, pady=12,
        font=("Helvetica Neue", 11),
    )
    details.grid(row=1, column=0, sticky="nsew", padx=22)
    details.insert("1.0", prompt)
    details.configure(state="disabled")
    buttons = ttk.Frame(window, style="Activity.TFrame")
    buttons.grid(row=2, column=0, sticky="ew", padx=22, pady=20)
    buttons.columnconfigure(0, weight=1)
    ttk.Button(
        buttons, text="Deny", style="Secondary.TButton",
        command=lambda: dialog.close(False),
    ).grid(row=0, column=1, padx=(0, 8))
    ttk.Button(
        buttons, text="Allow once", style="Primary.TButton",
        command=lambda: dialog.close(True),
    ).grid(row=0, column=2)
    window.protocol("WM_DELETE_WINDOW", lambda: dialog.close(False))
    window.bind("<Escape>", lambda _event: dialog.close(False))
    window.grab_set()
    window.focus_set()
    return dialog


def show_diagnostics_window(root, tk, ttk, diagnostic, open_logs: Callable[[str], None]) -> None:
    window = tk.Toplevel(root)
    window.title("Task diagnostics")
    window.geometry("620x460")
    window.transient(root)
    frame = ttk.Frame(window, style="Surface.TFrame", padding=24)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(0, weight=1)
    frame.columnconfigure(1, weight=2)
    ttk.Label(frame, text="Task diagnostics", style="Title.TLabel").grid(
        row=0, column=0, sticky="w", pady=(0, 18)
    )
    values = (
        ("Session", diagnostic.session_id),
        ("Run", diagnostic.run_id or "No completed run yet"),
        ("Outcome", diagnostic.outcome or "Pending"),
        ("Duration", elapsed_time_label(diagnostic.started_at, diagnostic.completed_at)),
        ("Runtime facts", f"{diagnostic.turn_count} turns · {diagnostic.event_count} events"),
        ("Verification", diagnostic.verification_phase.replace("_", " ").title()),
        ("Actions", ", ".join(diagnostic.tool_names) or "None"),
        ("Open issue", diagnostic.open_issue or "None"),
        ("Recovery", diagnostic.recovery_hint or "No recovery action required"),
    )
    for row, (label, value) in enumerate(values, 1):
        ttk.Label(frame, text=label, style="Title.TLabel").grid(
            row=row, column=0, sticky="nw", pady=(4, 0)
        )
        ttk.Label(
            frame, text=value, style="Title.TLabel", wraplength=540,
            font=("Helvetica Neue", 10),
        ).grid(row=row, column=1, sticky="nw", padx=(18, 0), pady=(4, 0))
    buttons = ttk.Frame(frame, style="Surface.TFrame")
    buttons.grid(row=len(values) + 1, column=0, columnspan=2, sticky="ew", pady=(24, 0))
    ttk.Button(
        buttons, text="Open run logs", style="Secondary.TButton",
        command=lambda: open_logs(diagnostic.log_path),
    ).pack(side="left")
    ttk.Button(buttons, text="Close", style="Primary.TButton", command=window.destroy).pack(side="right")


def open_run_logs(log_path: str, messagebox, *, opener=subprocess.Popen) -> bool:
    if not log_path:
        messagebox.showinfo("No logs yet", "This task does not have a completed run yet.")
        return False
    path = Path(log_path)
    if not path.exists():
        messagebox.showerror("Logs not found", f"Run log directory is unavailable:\n{path}")
        return False
    try:
        opener(["open", str(path)])
    except OSError as error:
        messagebox.showerror("Unable to open logs", f"Could not open the run log directory:\n{error}")
        return False
    return True
