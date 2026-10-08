from __future__ import annotations

from pathlib import Path
from queue import Queue
import signal
from threading import Lock, Thread

from .desktop import DesktopController
from .desktop_presenter import (
    ActivityStep,
    DesktopApprovalRequest,
    DesktopActivityModel,
    DesktopProgressReporter,
    format_approval_prompt,
    semantic_action_name,
)
from .desktop_windows import (
    QuickAction,
    open_run_logs,
    show_approval_dialog,
    show_command_palette,
    show_diagnostics_window,
)


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
        self._event_timer = None
        self._closed = False
        self._activity_visible = False
        self._activity = DesktopActivityModel()
        self._task_items = ()
        self._approval_lock = Lock()
        self._pending_approvals: list[DesktopApprovalRequest] = []
        self._approval_dialogs = {}

        root.title("SaiWorks")
        root.geometry("1040x720")
        root.minsize(820, 560)
        root.configure(background="#f3f5f8")
        root.protocol("WM_DELETE_WINDOW", self.request_close)
        root.bind("<Command-k>", lambda _event: self._show_command_palette())
        root.bind("<Control-k>", lambda _event: self._show_command_palette())

        self.workspace = tk.StringVar(value=str(Path(initial_workspace or Path.cwd()).resolve()))
        self.status = tk.StringVar(value="Choose a workspace")
        self._build_layout()

        runtime.configure_interaction(
            approval_callback=self._request_approval,
            progress_reporter=DesktopProgressReporter(self._post_status),
        )
        self._event_timer = root.after(50, self._drain_events)
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
        style.configure("Activity.TFrame", background="#f7f8fb")
        style.configure("ActivityTitle.TLabel", background="#f7f8fb", foreground="#202a3b", font=("Helvetica Neue", 14, "bold"))
        style.configure("ActivityStep.TLabel", background="#f7f8fb", foreground="#526078", font=("Helvetica Neue", 10), wraplength=240)
        style.configure("ProgressCard.TFrame", background="#f4f7fd", bordercolor="#dce4f2", relief="solid", borderwidth=1)
        style.configure("ProgressTitle.TLabel", background="#f4f7fd", foreground="#263b63", font=("Helvetica Neue", 10, "bold"))
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
        header.grid(row=0, column=0, columnspan=3, sticky="ew")
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

        self.ttk.Label(sidebar, text="TASKS", style="Section.TLabel").grid(row=4, column=0, sticky="w", pady=(28, 8))
        self.new_task_button = self.ttk.Button(sidebar, text="＋ New task", style="Primary.TButton", command=self._new_task)
        self.new_task_button.grid(row=5, column=0, sticky="ew")
        self.task_list = self.tk.Listbox(
            sidebar, borderwidth=0, highlightthickness=0, background="#e9edf3",
            foreground="#33415b", selectbackground="#d8e3f8", selectforeground="#1e335c",
            activestyle="none", font=("Helvetica Neue", 10), exportselection=False,
        )
        self.task_list.grid(row=6, column=0, sticky="nsew", pady=(10, 0))
        self.task_list.bind("<Double-Button-1>", lambda _event: self._resume_selected_task())
        self.ttk.Button(sidebar, text="Resume selected", style="Secondary.TButton", command=self._resume_selected_task).grid(row=7, column=0, sticky="ew", pady=(8, 0))
        self.session_label = self.ttk.Label(sidebar, text="No active task", style="Session.TLabel")
        self.session_label.grid(row=8, column=0, sticky="ew", pady=(12, 0))
        self.ttk.Label(
            sidebar,
            text="Your workspace and run history stay local. Actions that can change the system require approval.",
            style="Hint.TLabel",
        ).grid(row=10, column=0, sticky="sw", pady=(20, 0))
        sidebar.rowconfigure(6, weight=1)
        sidebar.rowconfigure(9, weight=1)

        main = self.ttk.Frame(app, style="Surface.TFrame", padding=(26, 22, 26, 18))
        main.grid(row=1, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)
        title_row = self.ttk.Frame(main, style="Surface.TFrame")
        title_row.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        title_row.columnconfigure(0, weight=1)
        self.ttk.Label(title_row, text="Conversation", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.ttk.Button(title_row, text="Activity", style="Secondary.TButton", command=self._toggle_activity).grid(row=0, column=1, padx=(8, 0))
        self.ttk.Button(title_row, text="⌘K", style="Secondary.TButton", command=self._show_command_palette).grid(row=0, column=2, padx=(8, 0))

        self.progress_card = self.ttk.Frame(main, style="ProgressCard.TFrame", padding=(14, 10))
        self.progress_card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.progress_card.columnconfigure(0, weight=1)
        self.progress_text = self.tk.StringVar(value="SaiWorks is ready")
        self.ttk.Label(self.progress_card, textvariable=self.progress_text, style="ProgressTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.ttk.Button(self.progress_card, text="View activity →", style="Secondary.TButton", command=lambda: self._toggle_activity(True)).grid(row=0, column=1)
        self.progress_card.grid_remove()

        transcript_frame = self.ttk.Frame(main, style="Surface.TFrame")
        transcript_frame.grid(row=2, column=0, sticky="nsew")
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
        self.transcript.tag_configure("result_ok", background="#eef8f1", foreground="#245b38", lmargin1=16, lmargin2=16, rmargin=50, spacing1=8, spacing3=12)
        self.transcript.tag_configure("result_error", background="#fff1f1", foreground="#7f3030", lmargin1=16, lmargin2=16, rmargin=50, spacing1=8, spacing3=12)
        self.transcript.tag_configure("empty", foreground="#8792a4", justify="center", spacing1=80)

        input_row = self.ttk.Frame(main, style="Composer.TFrame", padding=10)
        input_row.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        input_row.columnconfigure(0, weight=1)
        self.prompt_entry = self.tk.Text(
            input_row, height=3, wrap="word", borderwidth=0, highlightthickness=1,
            highlightbackground="#c8d1df", highlightcolor="#3867e8",
            background="#ffffff", foreground="#202a3b", padx=10, pady=8,
            font=("Helvetica Neue", 12),
        )
        self.prompt_entry.grid(row=0, column=0, sticky="ew")
        self.prompt_entry.bind("<Command-Return>", self._submit_from_key)
        self.prompt_entry.bind("<Control-Return>", self._submit_from_key)
        self.send_button = self.ttk.Button(input_row, text="Send", style="Primary.TButton", command=self._submit)
        self.send_button.grid(row=0, column=1, padx=(10, 0))
        self.cancel_button = self.ttk.Button(input_row, text="Stop", style="Danger.TButton", command=self._cancel, state="disabled")
        self.cancel_button.grid(row=0, column=2, padx=(8, 0))

        self.activity_panel = self.ttk.Frame(app, style="Activity.TFrame", padding=(18, 22), width=290)
        self.activity_panel.grid(row=1, column=2, sticky="nsew")
        self.activity_panel.grid_propagate(False)
        self.activity_panel.columnconfigure(0, weight=1)
        activity_head = self.ttk.Frame(self.activity_panel, style="Activity.TFrame")
        activity_head.grid(row=0, column=0, sticky="ew")
        activity_head.columnconfigure(0, weight=1)
        self.ttk.Label(activity_head, text="Activity", style="ActivityTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.ttk.Button(activity_head, text="Close", style="Secondary.TButton", command=lambda: self._toggle_activity(False)).grid(row=0, column=1)
        self.activity_body = self.ttk.Frame(self.activity_panel, style="Activity.TFrame")
        self.activity_body.grid(row=1, column=0, sticky="nsew", pady=(22, 0))
        self.activity_panel.rowconfigure(1, weight=1)
        self.ttk.Button(self.activity_panel, text="Diagnostics", style="Secondary.TButton", command=self._show_diagnostics).grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self.activity_panel.grid_remove()
        self._render_activity()

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
        self.session_label.configure(text=f"Active task\n{view.session_id}")
        self._render(view.messages)
        self._refresh_tasks()
        self._reset_activity()
        self.status.set("Ready")
        self.prompt_entry.focus_set()

    def _new_task(self) -> None:
        if self._running:
            return
        self._open_workspace()

    def _refresh_tasks(self) -> None:
        self._task_items = self.controller.list_tasks(cwd=self.workspace.get())
        self.task_list.delete(0, "end")
        for item in self._task_items[:50]:
            marker = "●" if item.status == "active" else "○"
            self.task_list.insert("end", f"{marker}  {item.title}")

    def _resume_selected_task(self) -> None:
        if self._running:
            return
        selected = self.task_list.curselection()
        if not selected:
            return
        item = self._task_items[selected[0]]
        try:
            view = self.controller.resume(item.session_id)
        except Exception as error:
            self.messagebox.showerror("Unable to resume task", str(error))
            return
        self.workspace.set(view.cwd)
        self.session_label.configure(text=f"Resumed task\n{view.session_id}")
        self._render(view.messages)
        self._reset_activity()
        self.status.set("Ready")

    def _resume_latest(self) -> None:
        if self._running:
            return
        try:
            view = self.controller.resume_latest()
        except Exception as error:
            self.messagebox.showinfo("Unable to resume", str(error))
            return
        self.workspace.set(view.cwd)
        self.session_label.configure(text=f"Resumed task\n{view.session_id}")
        self._render(view.messages)
        self._refresh_tasks()
        self._reset_activity()
        self.status.set("Ready")

    def _submit(self) -> None:
        prompt = self.prompt_entry.get("1.0", "end-1c").strip()
        if not prompt or self._running:
            return
        self.prompt_entry.delete("1.0", "end")
        self._append("user", prompt)
        self._activity.start()
        self._render_activity()
        self.progress_text.set(self._activity.headline)
        self.progress_card.grid()
        self._set_running(True)
        Thread(target=self._run_prompt, args=(prompt,), daemon=True).start()

    def _submit_from_key(self, _event):
        self._submit()
        return "break"

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
        request = DesktopApprovalRequest(action, reason)
        with self._approval_lock:
            if self._close_requested or self._closed:
                return False
            self._pending_approvals.append(request)
        self._events.put(("approval", request))
        try:
            return request.wait()
        finally:
            with self._approval_lock:
                if request in self._pending_approvals:
                    self._pending_approvals.remove(request)

    def _reject_pending_approvals(self) -> None:
        with self._approval_lock:
            pending = tuple(self._pending_approvals)
        for request, dialog in tuple(self._approval_dialogs.items()):
            dialog.close(False)
            self._approval_dialogs.pop(request, None)
        for request in pending:
            request.resolve(False)

    def _resolve_approval(self, request: DesktopApprovalRequest, approved: bool) -> None:
        self._approval_dialogs.pop(request, None)
        request.resolve(approved)

    def _drain_events(self) -> None:
        self._event_timer = None
        while not self._closed and not self._events.empty():
            kind, value = self._events.get_nowait()
            if kind == "status":
                self.status.set(str(value))
                self._record_activity(str(value))
            elif kind == "approval":
                request = value
                if request.is_resolved or self._close_requested:
                    request.resolve(False)
                    continue
                self._record_activity(f"Waiting for approval: {semantic_action_name(request.action.name)}")
                self._toggle_activity(True)
                dialog = show_approval_dialog(
                    self.root,
                    self.tk,
                    self.ttk,
                    format_approval_prompt(request.action, request.reason),
                    lambda approved, current=request: self._resolve_approval(current, approved),
                )
                self._approval_dialogs[request] = dialog
            elif kind == "finished":
                if value is not None:
                    self._append_result(value)
                else:
                    self._render(self.controller.view().messages)
                self._set_running(False)
                self._finish_activity(getattr(value, "outcome", "completed") if value is not None else "completed")
                self._refresh_tasks()
                self._close_if_requested()
            elif kind == "interrupted":
                self._set_running(False)
                self.status.set("Interrupted")
                self._finish_activity("interrupted")
                self._close_if_requested()
            elif kind == "error":
                self._set_running(False)
                self.status.set("Run failed")
                self._finish_activity("runtime_error")
                self.messagebox.showerror("Run failed", str(value))
                self._close_if_requested()
        if not self._closed:
            self._event_timer = self.root.after(50, self._drain_events)

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.send_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")

    def _record_activity(self, message: str) -> None:
        self._activity.update(message)
        self.progress_text.set(self._activity.headline)
        self.progress_card.grid()
        self._render_activity()

    def _finish_activity(self, outcome: str) -> None:
        label = self._activity.finish(outcome)
        self.progress_text.set(self._activity.headline)
        self.status.set("Ready" if outcome == "completed" else label)
        self._render_activity()
        if outcome != "completed":
            self._toggle_activity(True)

    def _reset_activity(self) -> None:
        self._activity.reset()
        self.progress_card.grid_remove()
        self._render_activity()

    def _render_activity(self) -> None:
        for child in self.activity_body.winfo_children():
            child.destroy()
        steps = self._activity.steps or (ActivityStep("pending", "No active task"),)
        symbols = {
            "done": "●", "current": "◉", "failed": "!", "pending": "○",
            "cancelled": "■", "blocked": "!", "warning": "!",
        }
        colors = {
            "done": "#438a63", "current": "#3867e8", "failed": "#b54b4b", "pending": "#8b95a5",
            "cancelled": "#6f7887", "blocked": "#b7791f", "warning": "#b7791f",
        }
        for row, step in enumerate(steps[-10:]):
            state, label = step.state, step.label
            rail = self.tk.Label(
                self.activity_body, text=symbols[state], background="#f7f8fb",
                foreground=colors[state], font=("Helvetica Neue", 13, "bold"),
            )
            rail.grid(row=row, column=0, sticky="n", padx=(0, 10), pady=(0, 12))
            self.ttk.Label(self.activity_body, text=label, style="ActivityStep.TLabel").grid(row=row, column=1, sticky="nw", pady=(1, 12))

    def _toggle_activity(self, visible: bool | None = None) -> None:
        self._activity_visible = not self._activity_visible if visible is None else visible
        if self._activity_visible:
            self.activity_panel.grid()
        else:
            self.activity_panel.grid_remove()

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

    def _append_result(self, summary) -> None:
        self.transcript.configure(state="normal")
        outcome = getattr(summary, "outcome", "completed")
        success = outcome == "completed"
        self.transcript.insert("end", "RESULT\n", "assistant_label")
        heading = "Completed" if success else f"Needs attention · {outcome.replace('_', ' ')}"
        blockers = getattr(summary, "blockers", [])
        blocker_text = ""
        if blockers:
            blocker_text = "\n\nWhat blocked this:\n" + "\n".join(
                f"• {getattr(item, 'summary', str(item))}" for item in blockers[:3]
            )
        self.transcript.insert(
            "end",
            f"{heading}\n\n{summary.final_message.strip()}{blocker_text}\n\n",
            "result_ok" if success else "result_error",
        )
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _insert_message(self, role: str, content: str) -> None:
        is_user = role.lower() == "user"
        label = "YOU" if is_user else "SAIWORKS"
        self.transcript.insert("end", f"{label}\n", "user_label" if is_user else "assistant_label")
        self.transcript.insert("end", f"{content.strip()}\n\n", "user_body" if is_user else "assistant_body")

    def _show_command_palette(self) -> None:
        actions = (
            QuickAction("New task", self._new_task),
            QuickAction("Resume latest task", self._resume_latest),
            QuickAction("View current activity", lambda: self._toggle_activity(True)),
            QuickAction("Open diagnostics", self._show_diagnostics),
            QuickAction("Cancel current task", self._cancel),
            QuickAction("Choose workspace", self._choose_workspace),
        )
        show_command_palette(self.root, self.tk, self.ttk, actions)

    def _show_diagnostics(self) -> None:
        try:
            diagnostic = self.controller.diagnostics()
        except Exception as error:
            self.messagebox.showinfo("Diagnostics unavailable", str(error))
            return
        show_diagnostics_window(self.root, self.tk, self.ttk, diagnostic, self._open_run_logs)

    def _open_run_logs(self, log_path: str) -> None:
        open_run_logs(log_path, self.messagebox)

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
            self._reject_pending_approvals()
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
        self._reject_pending_approvals()
        if self._shutdown_timer is not None:
            self.root.after_cancel(self._shutdown_timer)
            self._shutdown_timer = None
        if self._event_timer is not None:
            self.root.after_cancel(self._event_timer)
            self._event_timer = None
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
