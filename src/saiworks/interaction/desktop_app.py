from __future__ import annotations

import json
from pathlib import Path
from queue import Queue
import signal
from threading import Event, Thread

from .desktop import DesktopController


def format_approval_prompt(action, reason: str) -> str:
    arguments = json.dumps(action.arguments, ensure_ascii=False, indent=2, default=str)
    return (
        "SaiWorks Runtime is requesting permission to run an action.\n\n"
        f"Action: {action.name}\n"
        f"Reason: {reason}\n\n"
        f"Arguments:\n{arguments}\n\n"
        "On macOS, a later system Automation prompt may name the app that launched "
        "SaiWorks (such as Zed, Terminal, or Python)."
    )


class DesktopProgressReporter:
    """Translate engine progress into presentation-safe Desktop status text."""

    def __init__(self, publish) -> None:
        self.publish = publish

    def model_started(self, message: str = "Model is thinking…"):
        self.publish(message)
        return "model"

    def model_finished(self, _handle) -> None:
        self.publish("Preparing result…")

    def model_stream_delta(self, _handle, _channel: str, _text: str) -> None:
        return None

    def model_retrying(self, _handle, retry: int, max_retries: int, status: str, delay_seconds: float) -> None:
        self.publish(f"Model retry {retry}/{max_retries}: {status}; waiting {delay_seconds:g}s")

    def tool_started(self, action_name: str):
        self.publish(f"Running {action_name}…")
        return action_name

    def tool_finished(self, _handle, action, result) -> None:
        state = "completed" if result.success else "failed"
        self.publish(f"{action.name} {state}")

    def tool_aborted(self, handle) -> None:
        self.publish(f"{handle} interrupted")

    def tool_skipped(self, action, reason: str) -> None:
        self.publish(f"{action.name} skipped: {reason}")


class DesktopApp:
    """Tk reference shell for the Desktop V0.1a vertical slice."""

    def __init__(
        self,
        root,
        runtime,
        *,
        initial_workspace: str | Path | None = None,
        shutdown_timeout_ms: int = 10_000,
    ) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.root = root
        self.runtime = runtime
        self.controller = DesktopController(runtime)
        self._events: Queue[tuple[str, object]] = Queue()
        self._running = False
        self._close_requested = False
        self._shutdown_timeout_ms = shutdown_timeout_ms
        self._shutdown_timer = None
        self._closed = False

        root.title("SaiWorks")
        root.geometry("1040x720")
        root.minsize(820, 560)
        root.configure(background="#f3f5f8")
        root.protocol("WM_DELETE_WINDOW", self.request_close)

        self.workspace = tk.StringVar(value=str(Path(initial_workspace or Path.cwd()).resolve()))
        self.status = tk.StringVar(value="Choose a workspace")
        self.prompt = tk.StringVar()
        self._build_layout()

        runtime.configure_interaction(
            approval_callback=self._request_approval,
            progress_reporter=DesktopProgressReporter(self._post_status),
        )
        root.after(50, self._drain_events)
        self._open_workspace()

    def _build_layout(self) -> None:
        style = self.ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("App.TFrame", background="#f3f5f8")
        style.configure("Header.TFrame", background="#172033")
        style.configure("Sidebar.TFrame", background="#e9edf3")
        style.configure("Surface.TFrame", background="#ffffff")
        style.configure("Composer.TFrame", background="#f7f9fc")
        style.configure("Brand.TLabel", background="#172033", foreground="#ffffff", font=("Helvetica Neue", 19, "bold"))
        style.configure("Tagline.TLabel", background="#172033", foreground="#aebbd0", font=("Helvetica Neue", 11))
        style.configure("Status.TLabel", background="#172033", foreground="#93e2b7", font=("Helvetica Neue", 11, "bold"))
        style.configure("Section.TLabel", background="#e9edf3", foreground="#6b7688", font=("Helvetica Neue", 10, "bold"))
        style.configure("Sidebar.TLabel", background="#e9edf3", foreground="#243044", font=("Helvetica Neue", 11))
        style.configure("Hint.TLabel", background="#e9edf3", foreground="#778295", font=("Helvetica Neue", 10), wraplength=220)
        style.configure("Session.TLabel", background="#e9edf3", foreground="#3b4a61", font=("Menlo", 9), wraplength=220)
        style.configure("Title.TLabel", background="#ffffff", foreground="#202a3b", font=("Helvetica Neue", 16, "bold"))
        style.configure("Primary.TButton", background="#3867e8", foreground="#ffffff", borderwidth=0, padding=(16, 9), font=("Helvetica Neue", 11, "bold"))
        style.map("Primary.TButton", background=[("active", "#2f59cb"), ("disabled", "#aab8df")])
        style.configure("Secondary.TButton", background="#ffffff", foreground="#33415b", bordercolor="#ccd4e0", padding=(12, 8), font=("Helvetica Neue", 10))
        style.map("Secondary.TButton", background=[("active", "#f1f4f8")])
        style.configure("Danger.TButton", background="#fff1f1", foreground="#b23b3b", bordercolor="#efcaca", padding=(12, 8), font=("Helvetica Neue", 10, "bold"))
        style.configure("Workspace.TEntry", fieldbackground="#ffffff", foreground="#253047", bordercolor="#cbd3df", padding=7)
        style.configure("Prompt.TEntry", fieldbackground="#ffffff", foreground="#202a3b", bordercolor="#c8d1df", padding=11, font=("Helvetica Neue", 12))

        app = self.ttk.Frame(self.root, style="App.TFrame")
        app.pack(fill="both", expand=True)
        app.columnconfigure(1, weight=1)
        app.rowconfigure(1, weight=1)

        header = self.ttk.Frame(app, style="Header.TFrame", padding=(22, 14))
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(1, weight=1)
        self.ttk.Label(header, text="SaiWorks", style="Brand.TLabel").grid(row=0, column=0, sticky="w")
        self.ttk.Label(header, text="  Local workspace assistant", style="Tagline.TLabel").grid(row=0, column=1, sticky="w")
        self.ttk.Label(header, textvariable=self.status, style="Status.TLabel").grid(row=0, column=2, sticky="e")

        sidebar = self.ttk.Frame(app, style="Sidebar.TFrame", padding=(18, 22), width=260)
        sidebar.grid(row=1, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.columnconfigure(0, weight=1)
        self.ttk.Label(sidebar, text="WORKSPACE", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.ttk.Entry(sidebar, textvariable=self.workspace, style="Workspace.TEntry").grid(row=1, column=0, sticky="ew")
        self.ttk.Button(sidebar, text="Choose folder…", style="Secondary.TButton", command=self._choose_workspace).grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.ttk.Button(sidebar, text="Open workspace", style="Primary.TButton", command=self._open_workspace).grid(row=3, column=0, sticky="ew", pady=(8, 0))

        self.ttk.Label(sidebar, text="SESSION", style="Section.TLabel").grid(row=4, column=0, sticky="w", pady=(28, 8))
        self.session_label = self.ttk.Label(sidebar, text="No active session", style="Session.TLabel")
        self.session_label.grid(row=5, column=0, sticky="ew")
        self.ttk.Button(sidebar, text="Resume latest", style="Secondary.TButton", command=self._resume_latest).grid(row=6, column=0, sticky="ew", pady=(10, 0))
        self.ttk.Label(
            sidebar,
            text="Your workspace and run history stay local. Actions that can change the system require approval.",
            style="Hint.TLabel",
        ).grid(row=8, column=0, sticky="sw", pady=(30, 0))
        sidebar.rowconfigure(7, weight=1)

        main = self.ttk.Frame(app, style="Surface.TFrame", padding=(26, 22, 26, 18))
        main.grid(row=1, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        self.ttk.Label(main, text="Conversation", style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 14))

        transcript_frame = self.ttk.Frame(main, style="Surface.TFrame")
        transcript_frame.grid(row=1, column=0, sticky="nsew")
        transcript_frame.columnconfigure(0, weight=1)
        transcript_frame.rowconfigure(0, weight=1)
        self.transcript = self.tk.Text(
            transcript_frame,
            wrap="word",
            state="disabled",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#e0e5ed",
            highlightcolor="#e0e5ed",
            background="#ffffff",
            foreground="#263247",
            padx=20,
            pady=18,
            font=("Helvetica Neue", 12),
            spacing1=2,
            spacing3=4,
        )
        self.transcript.grid(row=0, column=0, sticky="nsew")
        scrollbar = self.ttk.Scrollbar(transcript_frame, orient="vertical", command=self.transcript.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.transcript.configure(yscrollcommand=scrollbar.set)
        self.transcript.tag_configure("user_label", foreground="#3867e8", font=("Helvetica Neue", 10, "bold"), spacing1=10, spacing3=5)
        self.transcript.tag_configure("assistant_label", foreground="#647187", font=("Helvetica Neue", 10, "bold"), spacing1=10, spacing3=5)
        self.transcript.tag_configure("user_body", background="#edf3ff", foreground="#22304a", lmargin1=16, lmargin2=16, rmargin=50, spacing1=8, spacing3=12)
        self.transcript.tag_configure("assistant_body", background="#f4f6f9", foreground="#263247", lmargin1=16, lmargin2=16, rmargin=50, spacing1=8, spacing3=12)
        self.transcript.tag_configure("empty", foreground="#8792a4", justify="center", spacing1=80)

        input_row = self.ttk.Frame(main, style="Composer.TFrame", padding=10)
        input_row.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        input_row.columnconfigure(0, weight=1)
        self.prompt_entry = self.ttk.Entry(input_row, textvariable=self.prompt, style="Prompt.TEntry")
        self.prompt_entry.grid(row=0, column=0, sticky="ew")
        self.prompt_entry.bind("<Return>", lambda _event: self._submit())
        self.send_button = self.ttk.Button(input_row, text="Send", style="Primary.TButton", command=self._submit)
        self.send_button.grid(row=0, column=1, padx=(10, 0))
        self.cancel_button = self.ttk.Button(input_row, text="Stop", style="Danger.TButton", command=self._cancel, state="disabled")
        self.cancel_button.grid(row=0, column=2, padx=(8, 0))

    def _choose_workspace(self) -> None:
        selected = self.filedialog.askdirectory(initialdir=self.workspace.get())
        if selected:
            self.workspace.set(selected)
            self._open_workspace()

    def _open_workspace(self) -> None:
        if self._running:
            return
        try:
            view = self.controller.open_workspace(self.workspace.get())
        except Exception as error:
            self.messagebox.showerror("Unable to open workspace", str(error))
            return
        self.session_label.configure(text=f"Active\n{view.session_id}")
        self._render(view.messages)
        self.status.set("Ready")
        self.prompt_entry.focus_set()

    def _resume_latest(self) -> None:
        if self._running:
            return
        try:
            view = self.controller.resume_latest()
        except Exception as error:
            self.messagebox.showinfo("Unable to resume", str(error))
            return
        self.workspace.set(view.cwd)
        self.session_label.configure(text=f"Resumed\n{view.session_id}")
        self._render(view.messages)
        self.status.set("Ready")

    def _submit(self) -> None:
        prompt = self.prompt.get().strip()
        if not prompt or self._running:
            return
        self.prompt.set("")
        self._append("user", prompt)
        self._set_running(True)
        Thread(target=self._run_prompt, args=(prompt,), daemon=True).start()

    def _run_prompt(self, prompt: str) -> None:
        try:
            summary = self.controller.submit(prompt)
        except KeyboardInterrupt:
            self._events.put(("interrupted", None))
        except Exception as error:
            self._events.put(("error", error))
        else:
            self._events.put(("finished", summary))

    def _cancel(self) -> None:
        if not self._running:
            return
        self.controller.cancel_current()
        self.status.set("Cancellation requested; checking the active operation…")

    def _post_status(self, message: str) -> None:
        self._events.put(("status", message))

    def _request_approval(self, action, reason: str) -> bool:
        completed = Event()
        decision: list[bool] = []
        self._events.put(("approval", (action, reason, decision, completed)))
        completed.wait()
        return bool(decision and decision[0])

    def _drain_events(self) -> None:
        while not self._events.empty():
            kind, value = self._events.get_nowait()
            if kind == "status":
                self.status.set(str(value))
            elif kind == "approval":
                action, reason, decision, completed = value
                approved = self.messagebox.askyesno(
                    "SaiWorks action approval",
                    format_approval_prompt(action, reason),
                )
                decision.append(approved)
                completed.set()
            elif kind == "finished":
                if value is not None:
                    self._append("assistant", value.final_message)
                else:
                    self._render(self.controller.view().messages)
                self._set_running(False)
                self.status.set("Ready")
                self._close_if_requested()
            elif kind == "interrupted":
                self._set_running(False)
                self.status.set("Interrupted")
                self._close_if_requested()
            elif kind == "error":
                self._set_running(False)
                self.status.set("Run failed")
                self.messagebox.showerror("Run failed", str(value))
                self._close_if_requested()
        self.root.after(50, self._drain_events)

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.send_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")

    def _render(self, messages: tuple[tuple[str, str], ...]) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        if not messages:
            self.transcript.insert(
                "end",
                "Start with a question, a task, or something you want to create.",
                "empty",
            )
        for role, content in messages:
            self._insert_message(role, content)
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _append(self, role: str, content: str) -> None:
        self.transcript.configure(state="normal")
        if self.transcript.get("1.0", "end-1c").startswith("Start with a question"):
            self.transcript.delete("1.0", "end")
        self._insert_message(role, content)
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _insert_message(self, role: str, content: str) -> None:
        is_user = role.lower() == "user"
        label = "YOU" if is_user else "SAIWORKS"
        self.transcript.insert("end", f"{label}\n", "user_label" if is_user else "assistant_label")
        self.transcript.insert("end", f"{content.strip()}\n\n", "user_body" if is_user else "assistant_body")

    def request_close(self, *, confirm: bool = True) -> None:
        if self._closed or self._close_requested:
            return
        if self._running and confirm and not self.messagebox.askyesno(
            "Close SaiWorks",
            "A task is still running. Closing this V0.1 window requests cancellation. Continue?",
        ):
            return
        if self._running:
            self._close_requested = True
            self.controller.cancel_current()
            self.status.set("Cancellation requested; the window will close after runtime cleanup…")
            self._schedule_shutdown_check()
            return
        self._close_now()

    def _schedule_shutdown_check(self) -> None:
        self._shutdown_timer = self.root.after(self._shutdown_timeout_ms, self._check_shutdown_timeout)

    def _check_shutdown_timeout(self) -> None:
        self._shutdown_timer = None
        if not self._close_requested or not self._running:
            return
        if self.messagebox.askyesno(
            "Still stopping",
            "The active operation is still cleaning up. Continue waiting?\n\nChoose No to force exit.",
        ):
            self._schedule_shutdown_check()
            return
        self._close_now(force=True)

    def _close_if_requested(self) -> None:
        if self._close_requested:
            self._close_now()

    def _close_now(self, *, force: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        if self._shutdown_timer is not None:
            self.root.after_cancel(self._shutdown_timer)
            self._shutdown_timer = None
        self.controller.close(force=force)
        self.root.destroy()


def main() -> None:
    import tkinter as tk
    from tkinter import messagebox

    from ..app import create_runtime

    root = tk.Tk()
    try:
        runtime = create_runtime(workspace_root=Path.cwd(), require_model=True)
        app = DesktopApp(root, runtime)
    except Exception as error:
        messagebox.showerror("Unable to start SaiWorks", str(error))
        root.destroy()
        return

    def request_signal_close(_signum, _frame) -> None:
        root.after(0, lambda: app.request_close(confirm=False))

    signal.signal(signal.SIGINT, request_signal_close)
    signal.signal(signal.SIGTERM, request_signal_close)
    root.mainloop()


if __name__ == "__main__":
    main()
