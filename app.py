"""PDF Upload UI — thin wrapper over the existing CLI flow.

What it does (and ONLY this):
    1. User clicks "Upload PDF" and picks a .pdf file.
    2. App copies it into input/ and resolves its path.
    3. App continues the EXISTING flow by calling main.main([... --input <path>]).

Page range: "From page" / "To page" boxes -> translated to the existing
    --pages flag (e.g. From=5 To=20 -> --pages 5-20). Blank = all pages.

Merge: after each successful run the UI snapshots output/voters.xlsx +
    output/review.xlsx into output/merge_parts/<pdf>_voters.xlsx. The
    Merge button stacks those snapshots (no dedup, no drops) into
    output/merged_voters.xlsx (+ merged_review.xlsx), e.g. 400 + 200 = 600.

It does NOT modify main.py, config.py, or any pipeline logic.
Run:  python app.py
Requires only stdlib (tkinter) + existing project deps (pandas/openpyxl).

Safety net: every successful run/merge is auto-backed-up into the OS
Downloads folder, the Download button copies the latest Excel there on
demand, and closing the window auto-saves any existing Excel there first —
so data is never lost by an accidental close. "Clear Output" wipes only
generated files inside output/ (snapshots included) after confirmation.
"""
from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from config import SETTINGS, ensure_dirs  # noqa: E402  (read-only use)
import security as sec  # noqa: E402  (UI-only safety helpers)


MERGE_DIR_NAME = "merge_parts"
MERGED_VOTERS_NAME = "merged_voters.xlsx"
MERGED_REVIEW_NAME = "merged_review.xlsx"
LAST_INPUT_NAME = ".ui_last_input"


def _merge_dir() -> Path:
    return SETTINGS.output_dir / MERGE_DIR_NAME


def _sanitize_stem(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "pdf"
    return safe[:80]


def _snapshot_paths(pdf_stem: str) -> tuple[Path, Path]:
    safe = _sanitize_stem(pdf_stem)
    d = _merge_dir()
    return d / f"{safe}_voters.xlsx", d / f"{safe}_review.xlsx"


def _list_snapshots() -> list[Path]:
    d = _merge_dir()
    if not d.exists():
        return []
    return sorted(d.glob("*_voters.xlsx"))


def _read_last_input() -> str:
    try:
        p = SETTINGS.state_dir / LAST_INPUT_NAME
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def _write_last_input(resolved: str) -> None:
    try:
        SETTINGS.state_dir.mkdir(parents=True, exist_ok=True)
        (SETTINGS.state_dir / LAST_INPUT_NAME).write_text(resolved, encoding="utf-8")
    except OSError:
        pass


def build_pages_spec(from_raw: str, to_raw: str,
                     total: int | None = None) -> str | None:
    """From/To boxes -> --pages spec. Returns None for 'all pages'.

    Rules:
      blank + blank -> all (None)
      5 + 20        -> "5-20"
      5 + 5         -> "5"      (single page)
      5 + blank     -> "5-<total>" (or "5" if total unknown)
      blank + 20    -> "1-20"
    Raises ValueError with a user-friendly message on bad input.
    """
    f = (from_raw or "").strip()
    t = (to_raw or "").strip()
    if not f and not t:
        return None
    try:
        from_page = int(f) if f else None
        to_page = int(t) if t else None
    except ValueError:
        raise ValueError("From/To pages must be whole numbers.")
    if from_page is not None and from_page < 1:
        raise ValueError("From page must be >= 1.")
    if to_page is not None and to_page < 1:
        raise ValueError("To page must be >= 1.")
    if from_page is not None and to_page is not None:
        if from_page > to_page:
            raise ValueError("From page cannot be greater than To page.")
        if total is not None:
            if from_page > total:
                raise ValueError(f"From page {from_page} exceeds PDF pages ({total}).")
            if to_page > total:
                raise ValueError(f"To page {to_page} exceeds PDF pages ({total}).")
        if from_page == to_page:
            return str(from_page)
        return f"{from_page}-{to_page}"
    if from_page is not None:  # only From given -> From..end
        if total is not None:
            if from_page > total:
                raise ValueError(f"From page {from_page} exceeds PDF pages ({total}).")
            if from_page == total:
                return str(from_page)
            return f"{from_page}-{total}"
        return str(from_page)
    # only To given -> 1..To
    assert to_page is not None
    if total is not None and to_page > total:
        raise ValueError(f"To page {to_page} exceeds PDF pages ({total}).")
    if to_page == 1:
        return "1"
    return f"1-{to_page}"


class UploadApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        ensure_dirs()
        self.title("Voter Roll Extractor — PDF Upload")
        self.geometry("740x640")
        self.resizable(True, True)

        self.pdf_path: Path | None = None
        self.total_pages: int | None = None
        self.worker: threading.Thread | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self._closing = False
        self._target_pages: list[int] = []
        self.pct_var: tk.StringVar | None = None

        self._build_widgets()
        self._refresh_merge_state()
        self._refresh_output_buttons()
        self._poll_log_queue()
        # Safety net: accidental close must first save Excels to Downloads.
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------------------------------------------------------- UI
    def _build_widgets(self) -> None:
        main = ttk.Frame(self, padding=16)
        main.pack(fill=tk.BOTH, expand=True)

        # Title
        ttk.Label(main, text="Upload Voter Roll PDF",
                  font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(main, text="Pick a PDF, set page range, then Start — the existing pipeline continues unchanged.",
                  foreground="gray").pack(anchor="w", pady=(0, 12))

        # File row
        file_row = ttk.Frame(main)
        file_row.pack(fill=tk.X, pady=4)
        self.upload_btn = ttk.Button(file_row, text="Upload PDF…",
                                     command=self.on_upload)
        self.upload_btn.pack(side=tk.LEFT)
        self.file_label = ttk.Label(file_row, text="No file selected",
                                    foreground="gray", wraplength=540)
        self.file_label.pack(side=tk.LEFT, padx=12, fill=tk.X, expand=True)

        self.info_label = ttk.Label(main, text="", foreground="gray")
        self.info_label.pack(anchor="w", pady=(2, 8))

        # Options row (passed straight to main.py CLI, no pipeline change)
        opts = ttk.LabelFrame(main, text="Page range (same as --pages flag)", padding=10)
        opts.pack(fill=tk.X, pady=6)

        ttk.Label(opts, text="From page:").grid(row=0, column=0, sticky="w")
        self.from_var = tk.StringVar(value="")
        from_entry = ttk.Entry(opts, textvariable=self.from_var, width=10)
        from_entry.grid(row=0, column=1, sticky="w", padx=6)

        ttk.Label(opts, text="To page:").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.to_var = tk.StringVar(value="")
        to_entry = ttk.Entry(opts, textvariable=self.to_var, width=10)
        to_entry.grid(row=0, column=3, sticky="w", padx=6)

        ttk.Label(opts, text='blank = all pages. e.g. From 5 To 20 → pages 5–20.',
                  foreground="gray").grid(row=1, column=0, columnspan=4,
                                           sticky="w", pady=(4, 0))

        self.force_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Force reprocess cached pages (--force)",
                        variable=self.force_var).grid(row=2, column=0,
                                                      columnspan=4, sticky="w",
                                                      pady=(6, 0))

        # Buttons row
        btn_row = ttk.Frame(main)
        btn_row.pack(fill=tk.X, pady=10)
        self.start_btn = ttk.Button(btn_row, text="Start Processing",
                                    command=self.on_start, state=tk.DISABLED)
        self.start_btn.pack(side=tk.LEFT)
        self.dry_btn = ttk.Button(btn_row, text="Dry Run",
                                  command=self.on_dry_run, state=tk.DISABLED)
        self.dry_btn.pack(side=tk.LEFT, padx=6)
        self.retry_btn = ttk.Button(btn_row, text="Retry Failed",
                                    command=self.on_retry, state=tk.DISABLED)
        self.retry_btn.pack(side=tk.LEFT, padx=6)
        self.output_btn = ttk.Button(btn_row, text="Open Output Folder",
                                     command=self.on_open_output)
        self.output_btn.pack(side=tk.RIGHT)

        # Output / safety row: Download to OS Downloads + Clear output/.
        out_row = ttk.Frame(main)
        out_row.pack(fill=tk.X, pady=(0, 10))
        self.download_btn = ttk.Button(out_row, text="Download Excel to Downloads",
                                       command=self.on_download, state=tk.DISABLED)
        self.download_btn.pack(side=tk.LEFT)
        self.clear_btn = ttk.Button(out_row, text="Clear Output",
                                    command=self.on_clear_output)
        self.clear_btn.pack(side=tk.LEFT, padx=6)
        ttk.Label(out_row, text="Download + auto-save go to system Downloads.",
                  foreground="gray").pack(side=tk.LEFT, padx=6)

        # Merge section
        merge = ttk.LabelFrame(main, text="Merge PDFs into one Excel", padding=10)
        merge.pack(fill=tk.X, pady=6)
        self.merge_info = ttk.Label(
            merge,
            text="Process each PDF first — then press Merge.",
            foreground="gray", wraplength=640, justify=tk.LEFT)
        self.merge_info.pack(anchor="w")
        merge_btns = ttk.Frame(merge)
        merge_btns.pack(fill=tk.X, pady=(8, 0))
        self.merge_btn = ttk.Button(merge_btns, text="Merge into one Excel",
                                    command=self.on_merge, state=tk.DISABLED)
        self.merge_btn.pack(side=tk.LEFT)
        self.open_merged_btn = ttk.Button(merge_btns, text="Open Merged Excel",
                                          command=self.on_open_merged,
                                          state=tk.DISABLED)
        self.open_merged_btn.pack(side=tk.LEFT, padx=6)

        self.status_var = tk.StringVar(value="Waiting for PDF upload…")
        ttk.Label(main, textvariable=self.status_var,
                  foreground="#0a58ca").pack(anchor="w", pady=(6, 4))

        # Progress bar + percentage + log
        prog_row = ttk.Frame(main)
        prog_row.pack(fill=tk.X, pady=(0, 6))
        self.progress = ttk.Progressbar(prog_row, mode="determinate",
                                        maximum=100, value=0)
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.pct_var = tk.StringVar(value="0%")
        self.pct_label = ttk.Label(prog_row, textvariable=self.pct_var,
                                   width=6, anchor="e",
                                   font=("Segoe UI", 10, "bold"))
        self.pct_label.pack(side=tk.LEFT, padx=(8, 0))

        log_frame = ttk.LabelFrame(main, text="Progress / Log", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(log_frame, height=12, wrap=tk.WORD,
                                state=tk.DISABLED)
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

    # ---------------------------------------------------------------- helpers
    def log(self, msg: str) -> None:
        self.log_queue.put(msg)

    def _poll_log_queue(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state=tk.NORMAL)
                self.log_text.insert(tk.END, msg + "\n")
                self.log_text.see(tk.END)
                self.log_text.configure(state=tk.DISABLED)
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)

    # ------------------------------------------------------- progress %
    def _progress_set(self, done: int, total: int) -> None:
        """Update determinate bar + '42%' label + status line (UI thread only)."""
        try:
            from utils import calc_progress_pct
            pct = calc_progress_pct(done, total)
        except Exception:
            pct = 0
        try:
            self.progress.configure(maximum=100, value=pct)
        except tk.TclError:
            pass
        try:
            if self.pct_var is not None:
                self.pct_var.set(f"{pct}%")
        except tk.TclError:
            pass
        if total > 0:
            self.status_var.set(
                f"Processing {done}/{total} pages ({pct}%)… (see log below)")

    def _progress_begin(self, total: int) -> None:
        try:
            self.progress.configure(maximum=100, value=0)
        except tk.TclError:
            pass
        try:
            if self.pct_var is not None:
                self.pct_var.set("0%")
        except tk.TclError:
            pass

    def _progress_finish(self, done: int, total: int) -> None:
        self._progress_set(done, total)

    def _resolve_target_pages(self, argv: list[str]) -> list[int]:
        """Best-effort target page list for the % bar (never raises)."""
        try:
            import utils
            import pdf_processor
            total = self.total_pages
            if total is None and self.pdf_path is not None:
                try:
                    total = pdf_processor.get_total_pages(self.pdf_path)
                    self.total_pages = total
                except Exception:
                    total = None
            if "--pages" in argv:
                spec = argv[argv.index("--pages") + 1]
                if total:
                    return utils.parse_pages_arg(spec, total)
                # total unknown: expand simple specs manually
                if "-" in spec:
                    a, b = spec.split("-", 1)
                    return list(range(int(a), int(b) + 1))
                if "," in spec:
                    return sorted({int(x) for x in spec.split(",") if x.strip()})
                return [int(spec)]
            if total:
                if "--retry-failed" in argv:
                    try:
                        import checkpoint
                        todo = []
                        for p in range(1, total + 1):
                            c = checkpoint.load_page_result(
                                SETTINGS.results_dir, p)
                            if not c or c.get("status") in (
                                    "FAILED", "REVIEW_REQUIRED"):
                                todo.append(p)
                        return todo or list(range(1, total + 1))
                    except Exception:
                        pass
                return list(range(1, total + 1))
        except Exception:
            pass
        return []

    def _set_busy(self, busy: bool) -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        self.upload_btn.configure(state=state)
        has_file = self.pdf_path is not None
        self.start_btn.configure(state=tk.DISABLED if (busy or not has_file) else tk.NORMAL)
        self.dry_btn.configure(state=tk.DISABLED if (busy or not has_file) else tk.NORMAL)
        self.retry_btn.configure(state=tk.DISABLED if (busy or not has_file) else tk.NORMAL)
        if busy:
            self.merge_btn.configure(state=tk.DISABLED)
            self.download_btn.configure(state=tk.DISABLED)
            self.clear_btn.configure(state=tk.DISABLED)
        else:
            try:
                self.progress.stop()
            except tk.TclError:
                pass
            self._refresh_merge_state()
            self._refresh_output_buttons()

    def _refresh_output_buttons(self) -> None:
        """Enable Download when any Excel exists in output/."""
        try:
            has_excel = ((SETTINGS.output_dir / "voters.xlsx").exists()
                         or (SETTINGS.output_dir / MERGED_VOTERS_NAME).exists())
        except OSError:
            has_excel = False
        self.download_btn.configure(state=tk.NORMAL if has_excel else tk.DISABLED)

    # ---------------------------------------------------------------- downloads safety net
    def _backup_excels_to_downloads(self, reason: str) -> list[Path]:
        """Copy latest Excel(s) into OS Downloads. Never raises; returns saved paths."""
        saved: list[Path] = []
        stem_base = self.pdf_path.stem if self.pdf_path else "voters"
        candidates = [
            (SETTINGS.output_dir / MERGED_VOTERS_NAME, f"{stem_base}_merged"),
            (SETTINGS.output_dir / "voters.xlsx", stem_base),
        ]
        for src, stem in candidates:
            try:
                if not src.exists() or not src.is_file() or src.stat().st_size <= 0:
                    continue
            except OSError:
                continue
            dest, msg = sec.safe_copy_to_downloads(src, sec.sanitize_filename(
                f"{stem}_{reason}" if reason else stem, ".xlsx")[:-5])
            self.log(f"[BACKUP:{reason}] {src.name} -> {msg}")
            if dest is not None:
                saved.append(dest)
                break  # one backup per event is enough (merged preferred)
        self.after(0, self._refresh_output_buttons)
        return saved

    # ---------------------------------------------------------------- merge state
    def _refresh_merge_state(self) -> None:
        """Enable Merge once >= 2 processed PDFs are snapshotted."""
        snaps = _list_snapshots()
        names = [p.name[: -len("_voters.xlsx")] for p in snaps]
        if len(snaps) >= 2:
            self.merge_btn.configure(state=tk.NORMAL)
            self.merge_info.configure(
                text=f"Ready to merge: {len(snaps)} PDFs ({', '.join(names)}). "
                     f"Merge stacks all rows without dedup — e.g. 400 + 200 = 600.",
                foreground="black")
        elif len(snaps) == 1:
            self.merge_btn.configure(state=tk.DISABLED)
            self.merge_info.configure(
                text=f"1 PDF processed ({names[0]}). Process a second PDF, then Merge will enable.",
                foreground="gray")
        else:
            self.merge_btn.configure(state=tk.DISABLED)
            self.merge_info.configure(
                text="Process each PDF first — then press Merge.",
                foreground="gray")
        merged = SETTINGS.output_dir / MERGED_VOTERS_NAME
        self.open_merged_btn.configure(
            state=tk.NORMAL if merged.exists() else tk.DISABLED)

    def _snapshot_current_output(self) -> None:
        """Copy output/voters.xlsx + review.xlsx aside per-PDF for merging."""
        if self.pdf_path is None:
            return
        voters = SETTINGS.output_dir / "voters.xlsx"
        review = SETTINGS.output_dir / "review.xlsx"
        if not voters.exists():
            self.log("[SNAPSHOT] skipped — output/voters.xlsx not found.")
            return
        dest_v, dest_r = _snapshot_paths(self.pdf_path.stem)
        try:
            dest_v.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(voters), str(dest_v))
            msg = f"[SNAPSHOT] {self.pdf_path.name} -> {dest_v.name}"
            if review.exists():
                shutil.copy2(str(review), str(dest_r))
                msg += f" + {dest_r.name}"
            self.log(msg)
        except Exception as exc:
            self.log(f"[SNAPSHOT] failed: {exc}")

    # ---------------------------------------------------------------- actions
    def on_upload(self) -> None:
        """Step 1+2: let user pick a PDF, copy it to input/, keep its path."""
        src = filedialog.askopenfilename(
            title="Select voter roll PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        if not src:
            return
        src_path = Path(src)
        ok, reason = sec.validate_pdf(src_path)
        if not ok:
            messagebox.showerror("Invalid PDF", reason)
            return

        try:
            ensure_dirs()
            # Sanitize the filename (traversal-safe) and confine to input/.
            safe_name = sec.sanitize_filename(src_path.name, ".pdf")
            dest = SETTINGS.input_dir / safe_name
            if not sec.is_within(SETTINGS.input_dir, dest):
                messagebox.showerror("Invalid PDF", "Unsafe file path blocked.")
                return
            # Copy into input/ (never move/delete the user's original).
            # If already in input/, just use it directly.
            if src_path.resolve() != dest.resolve():
                shutil.copy2(str(src_path), str(dest))
            self.pdf_path = dest
        except Exception as exc:
            messagebox.showerror("Upload failed", f"Could not save PDF:\n{exc}")
            return

        # Show path + quick page count (read-only, no pipeline change)
        self.total_pages = None
        pages_info = ""
        try:
            import pdf_processor
            self.total_pages = pdf_processor.get_total_pages(self.pdf_path)
            pages_info = f" • {self.total_pages} pages"
        except Exception:
            pages_info = ""

        self.file_label.configure(text=str(self.pdf_path), foreground="black")
        self.info_label.configure(
            text=f"Selected: {self.pdf_path.name}{pages_info}\nPath: {self.pdf_path}")
        self.status_var.set("PDF ready — set From/To pages (blank = all), then Start.")
        self.log(f"[UPLOAD] {self.pdf_path}{pages_info}")
        self._set_busy(False)
        self._progress_begin(self.total_pages or 0)
        # keep bar at 0% until Start is pressed
        self.start_btn.configure(state=tk.NORMAL)
        self.dry_btn.configure(state=tk.NORMAL)
        self.retry_btn.configure(state=tk.NORMAL)

    def _build_argv(self, extra: list[str] | None = None) -> list[str]:
        """Translate UI state into the existing CLI args. No pipeline change."""
        assert self.pdf_path is not None
        argv = ["--input", str(self.pdf_path)]
        try:
            spec = build_pages_spec(self.from_var.get(), self.to_var.get(),
                                    self.total_pages)
        except ValueError as exc:
            raise ValueError(str(exc))
        if spec:
            argv += ["--pages", spec]
        force = bool(self.force_var.get())
        # A different PDF reuses results/page_*.json cache from the previous
        # PDF unless forced — that would silently mix data. Auto-force on
        # PDF switch (UI-level only; pipeline untouched).
        try:
            cur = str(self.pdf_path.resolve())
        except OSError:
            cur = str(self.pdf_path)
        last = _read_last_input()
        has_cache = any(SETTINGS.results_dir.glob("page_*.json"))
        if last and last != cur and has_cache and not force:
            self.log("[WARN] Different PDF than last run — auto-enabling "
                     "--force to avoid stale per-page cache from previous PDF.")
            force = True
        if force:
            argv += ["--force"]
        if extra:
            argv += extra
        return argv

    def _run_main_in_background(self, argv: list[str], label: str,
                                snapshot: bool = True) -> None:
        if self.pdf_path is None:
            messagebox.showwarning("No PDF", "Please upload a PDF first.")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "Processing is already running.")
            return
        self._target_pages = self._resolve_target_pages(argv)
        total_n = len(self._target_pages)
        self._set_busy(True)
        self._progress_begin(total_n)
        if total_n:
            self.status_var.set(f"{label} 0/{total_n} pages (0%)… (see log below)")
        else:
            self.status_var.set(f"{label}… (see log below)")
        self.log(f"[RUN] python main.py {' '.join(argv)}")

        def _target() -> None:
            try:
                import main as pipeline  # existing flow, untouched
                # Wrap per-page worker so the determinate bar + % label move
                # in real time (cached pages count too — they are done work).
                _orig_process = pipeline.process_page
                _done = [0]
                _total = [total_n or 0]

                def _counting_process(page_number, input_pdf, force=False):
                    try:
                        return _orig_process(page_number, input_pdf, force=force)
                    finally:
                        _done[0] += 1
                        d, t = _done[0], _total[0]
                        try:
                            from utils import calc_progress_pct as _pct
                            pct = _pct(d, t)
                        except Exception:
                            pct = 0
                        self.log(f"[PROGRESS] {d}/{t} ({pct}%) — page {page_number} done")
                        self.after(0, lambda dd=d, tt=t: self._progress_set(dd, tt))

                pipeline.process_page = _counting_process  # type: ignore[method-assign]
                try:
                    rc = pipeline.main(argv)
                finally:
                    pipeline.process_page = _orig_process  # type: ignore[method-assign]
                # Ensure bar ends at 100% on clean finish with known total.
                if rc == 0 and _total[0]:
                    self.after(0, lambda: self._progress_finish(_total[0], _total[0]))
                self.log(f"[DONE] exit code={rc}. Output: {SETTINGS.output_dir}")
                if rc == 0 and snapshot:
                    try:
                        cur = str(self.pdf_path.resolve())  # type: ignore[union-attr]
                    except OSError:
                        cur = str(self.pdf_path)
                    _write_last_input(cur)
                    self._snapshot_current_output()
                    # Safety net: data from AI is immediately saved to Downloads
                    # too, so an accidental close can never lose it.
                    self._backup_excels_to_downloads("auto")
                self.status_var.set(
                    f"Finished (exit {rc}). Excel: {SETTINGS.output_dir / 'voters.xlsx'}"
                    if rc == 0 else f"Finished with exit code {rc} — check log.")
                if rc == 0:
                    self.after(0, lambda: messagebox.showinfo(
                        "Done",
                        f"Processing finished.\n\nExcel:\n{SETTINGS.output_dir / 'voters.xlsx'}\n"
                        f"Review:\n{SETTINGS.output_dir / 'review.xlsx'}"))
            except SystemExit as exc:
                self.log(f"[DONE] exit code={exc.code}")
                self.status_var.set(f"Finished (exit {exc.code}).")
            except ValueError as exc:
                self.log(f"[ERROR] {exc}")
                self.status_var.set("Error — see log.")
                self.after(0, lambda: messagebox.showerror("Invalid input", str(exc)[:500]))
            except Exception as exc:  # never crash the UI
                self.log(f"[ERROR] {exc}")
                self.status_var.set("Error — see log.")
                self.after(0, lambda: messagebox.showerror("Error", str(exc)[:800]))
            finally:
                self.after(0, lambda: self._set_busy(False))

        self.worker = threading.Thread(target=_target, daemon=True)
        self.worker.start()

    def on_start(self) -> None:
        """Step 3: continue the existing flow with the uploaded PDF's path."""
        try:
            argv = self._build_argv()
        except ValueError as exc:
            messagebox.showerror("Invalid page range", str(exc))
            return
        self._run_main_in_background(argv, "Processing")

    def on_dry_run(self) -> None:
        try:
            argv = self._build_argv(["--dry-run"])
        except ValueError as exc:
            messagebox.showerror("Invalid page range", str(exc))
            return
        self._run_main_in_background(argv, "Dry run", snapshot=False)

    def on_retry(self) -> None:
        try:
            argv = self._build_argv(["--retry-failed"])
        except ValueError as exc:
            messagebox.showerror("Invalid page range", str(exc))
            return
        self._run_main_in_background(argv, "Retrying failed/review pages")

    # ---------------------------------------------------------------- merge
    def on_merge(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "Wait for processing to finish first.")
            return
        snaps = _list_snapshots()
        if len(snaps) < 2:
            messagebox.showinfo("Merge",
                                "Process at least 2 PDFs first — then Merge will enable.\n"
                                f"Currently snapshotted: {len(snaps)}.")
            return
        self._set_busy(True)
        self._progress_begin(100)
        self._progress_set(50, 100)
        self.status_var.set("Merging… (50%)")
        self.log(f"[MERGE] {len(snaps)} files: {', '.join(p.name for p in snaps)}")

        def _target() -> None:
            try:
                total, per = self._do_merge(snaps)
                self.log(f"[MERGE DONE] {per} → total {total} rows (100%). "
                         f"Wrote {SETTINGS.output_dir / MERGED_VOTERS_NAME}")
                self.after(0, lambda: self._progress_set(100, 100))
                self._backup_excels_to_downloads("merged")
                self.status_var.set(
                    f"Merged {len(snaps)} PDFs = {total} rows (100%) → {MERGED_VOTERS_NAME}")
                self.after(0, lambda: messagebox.showinfo(
                    "Merge done",
                    f"Merged {len(snaps)} PDFs.\nRows per PDF: {per}\nTotal rows: {total}\n\n"
                    f"Wrote:\n{SETTINGS.output_dir / MERGED_VOTERS_NAME}\n"
                    f"{SETTINGS.output_dir / MERGED_REVIEW_NAME}"))
            except Exception as exc:
                self.log(f"[MERGE ERROR] {exc}")
                self.after(0, lambda: messagebox.showerror(
                    "Merge failed", str(exc)[:800]))
            finally:
                self.after(0, lambda: self._set_busy(False))

        threading.Thread(target=_target, daemon=True).start()

    @staticmethod
    def _do_merge(snaps: list[Path]) -> tuple[int, str]:
        """Stack snapshot VOTERS sheets with no dedup/drops. Returns (total, per-file summary)."""
        import pandas as pd

        voter_frames: list = []
        review_frames: list = []
        per_counts: list[str] = []
        for snap in snaps:
            source = snap.name[: -len("_voters.xlsx")]  # original pdf stem
            try:
                df = pd.read_excel(snap, sheet_name="VOTERS")
            except Exception as exc:
                raise RuntimeError(f"Cannot read {snap.name}: {exc}")
            if len(df):
                df = df.copy()
                df["Source PDF"] = source
            else:
                df["Source PDF"] = []
            voter_frames.append(df)
            per_counts.append(f"{source}={len(df)}")
            review_snap = snap.parent / f"{source}_review.xlsx"
            if review_snap.exists():
                try:
                    rdf = pd.read_excel(review_snap, sheet_name="REVIEW")
                    if len(rdf):
                        rdf = rdf.copy()
                        rdf["Source PDF"] = source
                    review_frames.append(rdf)
                except Exception:
                    pass  # review sheet optional — voters merge still proceeds

        if not voter_frames:
            raise RuntimeError("No voter data found to merge.")
        merged = pd.concat(voter_frames, ignore_index=True)  # pure append, no dedup
        total = len(merged)

        # Keep original column order; Source PDF last.
        try:
            from excel_writer import VOTER_COLUMNS, REVIEW_COLUMNS, _write_df_atomic
            v_cols = [c for c in VOTER_COLUMNS if c in merged.columns] + \
                     [c for c in merged.columns if c not in VOTER_COLUMNS]
            merged = merged[v_cols]
            _write_df_atomic(merged, SETTINGS.output_dir / MERGED_VOTERS_NAME,
                             "VOTERS", text_cols={3, 7})
            if review_frames:
                r_merged = pd.concat(review_frames, ignore_index=True)
                r_cols = [c for c in REVIEW_COLUMNS if c in r_merged.columns] + \
                         [c for c in r_merged.columns if c not in REVIEW_COLUMNS]
                r_merged = r_merged[r_cols]
            else:
                r_merged = pd.DataFrame([{"Issue": "No records need review",
                                          "Status": "OK"}])
            _write_df_atomic(r_merged, SETTINGS.output_dir / MERGED_REVIEW_NAME,
                             "REVIEW", text_cols={3})
        except ImportError:
            merged.to_excel(SETTINGS.output_dir / MERGED_VOTERS_NAME,
                            sheet_name="VOTERS", index=False)
            if review_frames:
                pd.concat(review_frames, ignore_index=True).to_excel(
                    SETTINGS.output_dir / MERGED_REVIEW_NAME,
                    sheet_name="REVIEW", index=False)
        return total, " + ".join(per_counts)

    def on_open_merged(self) -> None:
        merged = SETTINGS.output_dir / MERGED_VOTERS_NAME
        if not merged.exists():
            messagebox.showinfo("Merged Excel", "No merged file yet — press Merge first.")
            return
        self.log(f"[OUTPUT] {merged}")
        try:
            if os.name == "nt":
                os.startfile(str(merged))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(merged)])
            else:
                subprocess.Popen(["xdg-open", str(merged)])
        except Exception as exc:
            messagebox.showinfo("Merged Excel", f"{merged}\n\n({exc})")

    def on_download(self) -> None:
        """Download button: copy latest Excel into the system Downloads folder."""
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "Wait for processing to finish first.")
            return
        merged = SETTINGS.output_dir / MERGED_VOTERS_NAME
        voters = SETTINGS.output_dir / "voters.xlsx"
        src = merged if merged.exists() else voters
        if not src.exists():
            messagebox.showinfo("Download", "No Excel yet — process a PDF first.")
            return
        stem_base = self.pdf_path.stem if self.pdf_path else src.stem
        dest, msg = sec.safe_copy_to_downloads(src, sec.sanitize_filename(
            stem_base, ".xlsx")[:-5])
        self.log(f"[DOWNLOAD] {src.name} -> {msg}")
        self._refresh_output_buttons()
        if dest is not None:
            messagebox.showinfo("Download saved", f"Excel saved to:\n{dest}")
        else:
            messagebox.showerror("Download failed", msg)

    def on_clear_output(self) -> None:
        """Clear Output button: wipe generated files inside output/ only."""
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "Wait for processing to finish first.")
            return
        snaps = _list_snapshots()
        extra = (f"\n\nThis also removes {len(snaps)} merge snapshot(s) — "
                 f"Merge will disable until you re-process."
                 if snaps else "")
        if not messagebox.askyesno(
                "Clear Output",
                f"Delete all generated Excels/reports in output/?{extra}\n"
                f"The folder itself is kept. This cannot be undone."):
            return
        removed, msg = sec.clear_output_folder(SETTINGS.output_dir)
        self.log(f"[CLEAR] {msg}")
        self.status_var.set(msg)
        self._refresh_merge_state()
        self._refresh_output_buttons()

    def on_close(self) -> None:
        """Window close: auto-save any Excel to Downloads first (mistake-proof)."""
        if self._closing:
            return
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                    "Processing running",
                    "Processing is still running.\nClose anyway?\n\n"
                    "Partial Excel (if any) will be saved to Downloads first."):
                return
        saved = self._backup_excels_to_downloads("autosave")
        if saved:
            try:
                messagebox.showinfo(
                    "Auto-saved",
                    "Your Excel was auto-saved to Downloads:\n" +
                    "\n".join(str(p) for p in saved))
            except tk.TclError:
                pass
        self._closing = True
        try:
            self.progress.stop()
        except tk.TclError:
            pass
        self.destroy()

    def on_open_output(self) -> None:
        """Let the user get/download the resulting Excel files."""
        ensure_dirs()
        out = SETTINGS.output_dir
        self.log(f"[OUTPUT] {out}")
        try:
            if os.name == "nt":
                os.startfile(str(out))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(out)])
            else:
                subprocess.Popen(["xdg-open", str(out)])
        except Exception as exc:
            messagebox.showinfo("Output folder", f"{out}\n\n({exc})")


def main() -> None:
    app = UploadApp()
    app.mainloop()


if __name__ == "__main__":
    main()
