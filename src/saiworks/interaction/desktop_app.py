from __future__ import annotations

from pathlib import Path
from queue import Queue
import signal
from threading import Event, Thread

from .desktop import DesktopController


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

        root.title("SaiWorks Desktop")
        root.geometry("900x680")
        root.minsize(680, 500)
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
        frame = self.ttk.Frame(self.root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        workspace_row = self.ttk.Frame(frame)
        workspace_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        workspace_row.columnconfigure(1, weight=1)
        self.ttk.Label(workspace_row, text="Workspace").grid(row=0, column=0, padx=(0, 8))
        self.ttk.Entry(workspace_row, textvariable=self.workspace).grid(row=0, column=1, sticky="ew")
        self.ttk.Button(workspace_row, text="Choose…", command=self._choose_workspace).grid(row=0, column=2, padx=(8, 0))
        self.ttk.Button(workspace_row, text="Open", command=self._open_workspace).grid(row=0, column=3, padx=(8, 0))
        self.ttk.Button(workspace_row, text="Resume Latest", command=self._resume_latest).grid(row=0, column=4, padx=(8, 0))

        self.session_label = self.ttk.Label(frame, text="No session")
        self.session_label.grid(row=1, column=0, sticky="w", pady=(0, 8))

        self.transcript = self.tk.Text(frame, wrap="word", state="disabled", padx=10, pady=10)
        self.transcript.grid(row=2, column=0, sticky="nsew")

        input_row = self.ttk.Frame(frame)
        input_row.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        input_row.columnconfigure(0, weight=1)
        self.prompt_entry = self.ttk.Entry(input_row, textvariable=self.prompt)
        self.prompt_entry.grid(row=0, column=0, sticky="ew")
        self.prompt_entry.bind("<Return>", lambda _event: self._submit())
        self.send_button = self.ttk.Button(input_row, text="Send", command=self._submit)
        self.send_button.grid(row=0, column=1, padx=(8, 0))
        self.cancel_button = self.ttk.Button(input_row, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_button.grid(row=0, column=2, padx=(8, 0))

        self.ttk.Label(frame, textvariable=self.status).grid(row=4, column=0, sticky="w", pady=(8, 0))

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
        self.session_label.configure(text=f"Session {view.session_id}")
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
        self.session_label.configure(text=f"Session {view.session_id}")
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
                    "Approve action",
                    f"{action.name}\n\n{reason}\n\nArguments:\n{action.arguments}",
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
        for role, content in messages:
            self.transcript.insert("end", f"{role.title()}\n{content}\n\n")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _append(self, role: str, content: str) -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert("end", f"{role.title()}\n{content}\n\n")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

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
        runtime = create_runtime(workspace_root=Path.cwd())
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
