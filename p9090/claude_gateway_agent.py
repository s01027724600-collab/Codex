#!/usr/bin/env python3
"""
Claude Code Gateway Agent.

This is a small, dependency-free HTTP gateway for sending natural-language
prompts to Claude Code. It does not interpret the prompt. It authenticates the
request, stores a job, passes the prompt to `claude -p` through stdin, and keeps
the output available through a local web UI or JSON API.
"""

import argparse
import base64
from collections import deque
import datetime as dt
import getpass
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import platform
import re
import shlex
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple


APP_NAME = "claude-code-gateway"
VERSION = "0.1.3"
SESSION_COOKIE = "gateway_session"
PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 260000
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 86400
MAX_SESSION_DAYS = 3650
MAX_REQUEST_BODY_BYTES = 1024 * 1024
MAX_LOGIN_BODY_BYTES = 64 * 1024
MAX_OUTPUT_READ_BYTES = 1024 * 1024
MAX_EVENTS_READ_BYTES = 2 * 1024 * 1024
MAX_EVENTS_RETURN = 2000
MAX_LOADED_JOBS = 500
JOB_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$", re.IGNORECASE)
EFFORT_LEVELS = {"", "low", "medium", "high", "xhigh", "max"}
PERMISSION_MODES = {
    "",
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "default",
    "dontAsk",
    "plan",
}
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def is_windows() -> bool:
    return platform.system().lower() == "windows"


def ensure_background_stdio() -> None:
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stderr is None:
        trace = Path(tempfile.gettempdir()) / APP_NAME / "background-error.log"
        try:
            trace.parent.mkdir(parents=True, exist_ok=True)
            sys.stderr = trace.open("a", encoding="utf-8", errors="replace")
        except OSError:
            sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")


ensure_background_stdio()


_ORIGINAL_SUBPROCESS_POPEN = subprocess.Popen


def _windows_hidden_subprocess_defaults(kwargs: Dict[str, object]) -> Dict[str, object]:
    if not is_windows():
        return kwargs

    creationflags = int(kwargs.get("creationflags") or 0)
    creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    creationflags &= ~getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
    kwargs["creationflags"] = creationflags

    startupinfo = kwargs.get("startupinfo")
    if startupinfo is None:
        startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    kwargs["startupinfo"] = startupinfo
    return kwargs


def _hidden_subprocess_popen(*popenargs, **kwargs):
    return _ORIGINAL_SUBPROCESS_POPEN(*popenargs, **_windows_hidden_subprocess_defaults(kwargs))


if is_windows():
    subprocess.Popen = _hidden_subprocess_popen


def default_state_dir() -> str:
    return str(Path(tempfile.gettempdir()) / APP_NAME)


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def remote_is_loopback(remote: str) -> bool:
    try:
        return ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return False


def json_safe(value: object, depth: int = 0) -> object:
    """Convert data from a CLI event into bounded JSON-compatible values."""
    if depth > 8:
        return "<nested value>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 200_000 else value[:200_000] + "...[truncated]"
    if isinstance(value, dict):
        return {
            str(key): json_safe(item, depth + 1)
            for key, item in list(value.items())[:2_000]
        }
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item, depth + 1) for item in list(value)[:2_000]]
    return str(value)


def json_dumps(value: object, *, indent: Optional[int] = None) -> str:
    return json.dumps(json_safe(value), ensure_ascii=False, indent=indent)


def parse_bool(value: object, field_name: str, default: Optional[bool] = None) -> bool:
    if value is None and default is not None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
    raise ValueError(f"{field_name} must be a boolean")


def parse_optional_int(value: object, field_name: str) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    try:
        # Reject floats such as 1.5 rather than silently truncating them.
        if isinstance(value, float) and not value.is_integer():
            raise ValueError
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field_name} must be an integer") from None


def read_json_file(path: Path) -> object:
    # utf-8-sig accepts both ordinary UTF-8 and the BOM emitted by Windows tools.
    return json.loads(path.read_text(encoding="utf-8-sig"))


def display_command(command: List[str]) -> str:
    if is_windows():
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def describe_exit_code(exit_code: Optional[int]) -> str:
    if exit_code is None:
        return "no exit code"
    unsigned = int(exit_code) & 0xFFFFFFFF
    windows_messages = {
        0xC0000005: "Windows access violation",
        0xC0000409: "Windows stack buffer overrun",
        0xC0000135: "Windows DLL initialization failed",
        0xC0000142: "Windows DLL initialization failed",
        0xC000001D: "Windows illegal instruction",
    }
    detail = windows_messages.get(unsigned)
    return f"{int(exit_code)} (0x{unsigned:08X}{'; ' + detail if detail else ''})"


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        tmp.write_text(text, encoding="utf-8", newline="")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", errors="replace") as f:
        f.write(text)
        f.flush()


def read_tail(path: Path, limit: int) -> str:
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 0
    if limit == 0 or not path.exists():
        return ""
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - limit), os.SEEK_SET)
            data = stream.read(limit)
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def normalize_model(value: object) -> str:
    if value is None:
        model = ""
    elif isinstance(value, str):
        model = value.strip()
    else:
        raise ValueError("model must be a string")
    if not model:
        return ""
    if not MODEL_NAME_RE.fullmatch(model):
        raise ValueError("model may only contain letters, numbers, dot, colon, underscore, or dash")
    return model


def normalize_effort(value: object) -> str:
    if value is None:
        effort = ""
    elif isinstance(value, str):
        effort = value.strip()
    else:
        raise ValueError("effort must be a string")
    if effort not in EFFORT_LEVELS:
        allowed = ", ".join(sorted(item for item in EFFORT_LEVELS if item))
        raise ValueError(f"effort must be one of: {allowed}")
    return effort


def normalize_permission_mode(value: object) -> str:
    if value is None:
        mode = ""
    elif isinstance(value, str):
        mode = value.strip()
    else:
        raise ValueError("permission_mode must be a string")
    if mode not in PERMISSION_MODES:
        allowed = ", ".join(sorted(item for item in PERMISSION_MODES if item))
        raise ValueError(f"permission_mode must be one of: {allowed}")
    return mode


def clean_task_output(text: str) -> str:
    text = ANSI_ESCAPE_RE.sub("", text)
    text = CONTROL_CHARS_RE.sub("", text)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    if lines and lines[0].startswith("Claude Code Gateway job "):
        while lines and lines[0].strip():
            lines.pop(0)
        if lines and not lines[0].strip():
            lines.pop(0)

    cleaned = []
    skip_exact = {"--- Claude Code output ---"}
    for line in lines:
        if line in skip_exact:
            continue
        if line == "Exit code: 0":
            continue
        cleaned.append(line)
    return "\n".join(cleaned).lstrip("\n")


def hidden_subprocess_kwargs() -> Dict[str, object]:
    if not is_windows():
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    flags = subprocess.CREATE_NO_WINDOW
    return {
        "creationflags": flags,
        "startupinfo": startupinfo,
    }


def resolve_command_program(program: str) -> str:
    if os.path.isabs(program) or os.sep in program or (os.altsep and os.altsep in program):
        expanded = os.path.expandvars(os.path.expanduser(program))
        if os.path.exists(expanded):
            return expanded
    return shutil.which(program) or ""


def claude_cli_missing_message(program: str) -> str:
    return (
        "Claude Code CLI is not installed or not available in PATH.\n\n"
        "The gateway is running normally, but 9090 cannot run Claude tasks until the CLI is installed.\n"
        "Install it only when you need Claude task execution, then reopen CMD/PowerShell or restart the gateway.\n\n"
        "Windows install command:\n"
        "curl -fsSL https://claude.ai/install.cmd -o install.cmd && install.cmd latest && del install.cmd\n\n"
        f"Missing command: {program}\n"
    )


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii"))


def make_password_hash(password: str, iterations: int = PASSWORD_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        [
            PASSWORD_ALGORITHM,
            str(iterations),
            b64url_encode(salt),
            b64url_encode(digest),
        ]
    )


def verify_password_hash(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != PASSWORD_ALGORITHM:
            return False
        iterations = int(iterations_text)
        if iterations < 10_000 or iterations > 5_000_000:
            return False
        salt = b64url_decode(salt_text)
        expected = b64url_decode(digest_text)
        if not 8 <= len(salt) <= 64 or not 16 <= len(expected) <= 128:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def write_password_file(path: str) -> None:
    first = getpass.getpass("Gateway password: ")
    second = getpass.getpass("Confirm password: ")
    if len(first) < 4:
        raise SystemExit("Password must be at least 4 characters.")
    if first != second:
        raise SystemExit("Passwords did not match.")
    data = {
        "password_hash": make_password_hash(first),
        "session_secret": secrets.token_urlsafe(32),
        "session_days": 90,
    }
    target = Path(path).expanduser().resolve()
    atomic_text(target, json_dumps(data, indent=2))
    print(f"Password file written: {target}")


@dataclass
class Config:
    _base_dir: str = field(default="", repr=False)
    host: str = "127.0.0.1"
    port: int = 9090
    token: str = ""
    unsafe_no_token: bool = False
    ip_allowlist: List[str] = field(default_factory=list)
    auth_file: str = ""
    password_hash: str = ""
    session_secret: str = ""
    session_days: int = 90

    claude_bin: str = "claude"
    claude_args: List[str] = field(
        default_factory=lambda: ["-p", "--output-format", "text"]
    )
    continue_session: bool = False
    auto_continue: bool = True
    model: str = ""
    effort: str = ""
    permission_mode: str = ""
    system_prompt: str = ""
    stream_json: bool = True
    max_turns: int = 0
    working_dir: str = ""
    timeout_seconds: int = 1800
    max_prompt_chars: int = 20000

    state_dir: str = field(default_factory=default_state_dir)
    audit_log: str = ""
    cors_origins: List[str] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: str) -> "Config":
        cfg = cls()
        config_path = Path(path).expanduser().resolve()
        try:
            data = read_json_file(config_path)
        except FileNotFoundError:
            raise SystemExit(f"Configuration file not found: {config_path}") from None
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSON in configuration file {config_path}: {exc}") from None
        except OSError as exc:
            raise SystemExit(f"Could not read configuration file {config_path}: {exc}") from None
        if not isinstance(data, dict):
            raise SystemExit(f"Configuration file must contain a JSON object: {config_path}")
        for key, value in data.items():
            if not key.startswith("_") and hasattr(cfg, key):
                setattr(cfg, key, value)
        cfg._base_dir = str(config_path.parent)
        return cfg

    def finalize(self) -> None:
        base_dir = Path(self._base_dir) if self._base_dir else None

        def resolve_path(value: str) -> Path:
            path = Path(value).expanduser()
            if base_dir is not None and not path.is_absolute():
                path = base_dir / path
            return path.resolve()

        try:
            self.host = str(self.host).strip()
            if not self.host:
                raise ValueError("host cannot be empty")
            if isinstance(self.port, bool):
                raise ValueError("port must be an integer")
            self.port = int(self.port)
            if not 1 <= self.port <= 65535:
                raise ValueError("port must be between 1 and 65535")

            if not isinstance(self.token, str):
                raise ValueError("token must be a string")
            if not isinstance(self.auth_file, str):
                raise ValueError("auth_file must be a string")
            if not isinstance(self.password_hash, str):
                raise ValueError("password_hash must be a string")
            if not isinstance(self.session_secret, str):
                raise ValueError("session_secret must be a string")
            self.unsafe_no_token = parse_bool(self.unsafe_no_token, "unsafe_no_token")
            self.continue_session = parse_bool(self.continue_session, "continue_session")
            self.auto_continue = parse_bool(self.auto_continue, "auto_continue")
            self.stream_json = parse_bool(self.stream_json, "stream_json")

            self.session_days = int(self.session_days)
            if not 1 <= self.session_days <= MAX_SESSION_DAYS:
                raise ValueError(f"session_days must be between 1 and {MAX_SESSION_DAYS}")
            self.max_turns = int(self.max_turns)
            if not 0 <= self.max_turns <= 1000:
                raise ValueError("max_turns must be between 0 and 1000")
            self.timeout_seconds = int(self.timeout_seconds)
            if not MIN_TIMEOUT_SECONDS <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS:
                raise ValueError(
                    f"timeout_seconds must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS}"
                )
            self.max_prompt_chars = int(self.max_prompt_chars)
            if not 1 <= self.max_prompt_chars <= 1_000_000:
                raise ValueError("max_prompt_chars must be between 1 and 1000000")
            if not isinstance(self.system_prompt, str):
                raise ValueError("system_prompt must be a string")
            if len(self.system_prompt) > 200_000:
                raise ValueError("system_prompt is too long")
            if not isinstance(self.claude_bin, str) or not self.claude_bin.strip():
                raise ValueError("claude_bin must be a non-empty string")
            self.claude_bin = self.claude_bin.strip()
            if os.sep in self.claude_bin or (os.altsep and os.altsep in self.claude_bin):
                self.claude_bin = str(resolve_path(self.claude_bin))
            if not isinstance(self.claude_args, (list, tuple)):
                raise ValueError("claude_args must be an array of strings")
            self.claude_args = [str(item) for item in self.claude_args]
            if any("\x00" in item for item in self.claude_args):
                raise ValueError("claude_args cannot contain NUL characters")
            if isinstance(self.ip_allowlist, str):
                self.ip_allowlist = parse_csv(self.ip_allowlist)
            elif isinstance(self.ip_allowlist, (list, tuple)):
                self.ip_allowlist = [str(item) for item in self.ip_allowlist]
            else:
                raise ValueError("ip_allowlist must be an array of IPs/CIDRs")
            if isinstance(self.cors_origins, str):
                self.cors_origins = parse_csv(self.cors_origins)
            elif isinstance(self.cors_origins, (list, tuple)):
                self.cors_origins = [str(item).strip() for item in self.cors_origins if str(item).strip()]
            else:
                raise ValueError("cors_origins must be an array of origins")
            if any(origin == "*" or "\r" in origin or "\n" in origin for origin in self.cors_origins):
                raise ValueError("cors_origins must contain explicit origins, not '*'")
        except (TypeError, ValueError, OverflowError) as exc:
            raise SystemExit(f"Invalid gateway configuration: {exc}") from None

        if not self.working_dir:
            self.working_dir = str(base_dir or Path.cwd())
        if not isinstance(self.working_dir, str) or not self.working_dir.strip():
            raise SystemExit("Invalid gateway configuration: working_dir must be a directory path")
        self.working_dir = str(resolve_path(self.working_dir))
        if not Path(self.working_dir).is_dir():
            raise SystemExit(f"Working directory does not exist: {self.working_dir}")
        if not isinstance(self.state_dir, str) or not self.state_dir.strip():
            self.state_dir = default_state_dir()
        self.state_dir = str(resolve_path(self.state_dir))
        if not self.audit_log:
            self.audit_log = str(Path(self.state_dir) / "audit.jsonl")
        elif not isinstance(self.audit_log, str):
            raise SystemExit("Invalid gateway configuration: audit_log must be a path")
        else:
            self.audit_log = str(resolve_path(self.audit_log))
        Path(self.state_dir).mkdir(parents=True, exist_ok=True)

        if not self.token:
            self.token = os.environ.get("CLAUDE_GATEWAY_TOKEN", "")
        if self.auth_file:
            auth_path = resolve_path(self.auth_file)
            if not auth_path.exists():
                raise SystemExit(f"Authentication file not found: {auth_path}")
            try:
                auth_data = read_json_file(auth_path)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSON in authentication file {auth_path}: {exc}") from None
            except OSError as exc:
                raise SystemExit(f"Could not read authentication file {auth_path}: {exc}") from None
            if not isinstance(auth_data, dict):
                raise SystemExit(f"Authentication file must contain a JSON object: {auth_path}")
            self.password_hash = auth_data.get("password_hash", self.password_hash)
            self.session_secret = auth_data.get("session_secret", self.session_secret)
            self.session_days = auth_data.get("session_days", self.session_days)
        try:
            if not isinstance(self.password_hash, str):
                raise ValueError("password_hash must be a string")
            if not isinstance(self.session_secret, str):
                raise ValueError("session_secret must be a string")
            if isinstance(self.session_days, bool):
                raise ValueError("session_days must be an integer")
            self.session_days = int(self.session_days)
            if not 1 <= self.session_days <= MAX_SESSION_DAYS:
                raise ValueError(f"session_days must be between 1 and {MAX_SESSION_DAYS}")
        except (TypeError, ValueError, OverflowError) as exc:
            raise SystemExit(f"Invalid authentication configuration: {exc}") from None
        if not self.password_hash:
            self.password_hash = os.environ.get("CLAUDE_GATEWAY_PASSWORD_HASH", "")
        if not self.session_secret:
            self.session_secret = os.environ.get("CLAUDE_GATEWAY_SESSION_SECRET", "")
        if self.password_hash and not self.session_secret:
            secret_path = Path(self.state_dir) / "session.secret"
            if secret_path.exists():
                self.session_secret = secret_path.read_text(encoding="utf-8-sig").strip()
            else:
                self.session_secret = secrets.token_urlsafe(32)
                atomic_text(secret_path, self.session_secret)

        if self.password_hash and not verify_password_hash("", self.password_hash):
            # Verification with an empty password is expected to fail, but parsing
            # the encoded form here prevents malformed or unbounded parameters.
            try:
                algorithm, iterations_text, salt_text, digest_text = self.password_hash.split("$", 3)
                if algorithm != PASSWORD_ALGORITHM:
                    raise ValueError
                iterations = int(iterations_text)
                salt = b64url_decode(salt_text)
                digest = b64url_decode(digest_text)
                if not 10_000 <= iterations <= 5_000_000 or not 8 <= len(salt) <= 64 or not 16 <= len(digest) <= 128:
                    raise ValueError
            except Exception:
                raise SystemExit("Invalid password_hash configuration") from None
        if self.password_hash and (not self.session_secret or len(self.session_secret) < 16):
            raise SystemExit("Invalid session_secret configuration")

        if (
            not self.token
            and not self.password_hash
            and not self.unsafe_no_token
            and not is_loopback_host(self.host)
        ):
            raise SystemExit(
                "Refusing to listen on a non-loopback host without authentication. "
                "Set --token, --auth-file, --password-hash, or pass "
                "--unsafe-no-token only for a trusted lab network."
            )
        self.model = normalize_model(self.model)
        self.effort = normalize_effort(self.effort)
        self.permission_mode = normalize_permission_mode(self.permission_mode)

    def claude_command(
        self,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        permission_mode: Optional[str] = None,
        session_id: str = "",
        resume_session: bool = False,
    ) -> List[str]:
        args = [self.claude_bin]
        if session_id and resume_session:
            args.extend(["--resume", session_id])
        elif session_id:
            args.extend(["--session-id", session_id])
        elif self.continue_session:
            args.append("--continue")
        args.extend(self.claude_args)
        selected_model = self.model if model is None else normalize_model(model)
        selected_effort = self.effort if effort is None else normalize_effort(effort)
        selected_permission_mode = (
            self.permission_mode
            if permission_mode is None
            else normalize_permission_mode(permission_mode)
        )
        if selected_model:
            args.extend(["--model", selected_model])
        if selected_effort:
            args.extend(["--effort", selected_effort])
        if selected_permission_mode:
            args.extend(["--permission-mode", selected_permission_mode])
        if self.max_turns > 0:
            args.extend(["--max-turns", str(self.max_turns)])
        if self.system_prompt:
            args.extend(["--append-system-prompt", self.system_prompt])
        if self.stream_json:
            args = self._stream_json_output_format(args)
            if "--verbose" not in args:
                args.append("--verbose")
        return args

    @staticmethod
    def _stream_json_output_format(args: List[str]) -> List[str]:
        out: List[str] = []
        replaced = False
        i = 0
        while i < len(args):
            if args[i] == "--output-format" and i + 1 < len(args):
                out.extend([args[i], "stream-json"])
                i += 2
                replaced = True
                continue
            out.append(args[i])
            i += 1
        if not replaced:
            out.extend(["--output-format", "stream-json"])
        return out

    def allowlist_networks(self) -> List[ipaddress._BaseNetwork]:
        networks = []
        for item in self.ip_allowlist:
            text = str(item).strip()
            if not text:
                continue
            try:
                if "/" in text:
                    networks.append(ipaddress.ip_network(text, strict=False))
                else:
                    address = ipaddress.ip_address(text)
                    networks.append(ipaddress.ip_network(address.exploded))
            except ValueError:
                raise SystemExit(f"Invalid IP allowlist entry: {text}")
        return networks


@dataclass
class Job:
    job_id: str
    prompt: str
    output_path: Path
    events_path: Path
    prompt_path: Path
    command_display: str
    command: List[str] = field(default_factory=list, repr=False)
    session_id: str = ""
    continued_from: str = ""
    model: str = ""
    effort: str = ""
    permission_mode: str = ""
    created_at: str = field(default_factory=utc_now)
    started_at: str = ""
    ended_at: str = ""
    closed_at: str = ""
    status: str = "queued"
    exit_code: Optional[int] = None
    error: str = ""
    pid: Optional[int] = None
    timed_out: bool = False
    cancel_requested: bool = False
    assistant_text_emitted: bool = False
    process: Optional[subprocess.Popen] = field(default=None, repr=False)
    event_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    event_cache: Deque[Dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=MAX_EVENTS_RETURN), repr=False
    )
    latest_seq: int = 0

    def public(self) -> Dict[str, object]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "closed_at": self.closed_at,
            "exit_code": self.exit_code,
            "error": self.error,
            "pid": self.pid,
            "timed_out": self.timed_out,
            "command": self.command_display,
            "session_id": self.session_id,
            "continued_from": self.continued_from,
            "model": self.model,
            "effort": self.effort,
            "permission_mode": self.permission_mode,
            "output_path": str(self.output_path),
        }


class AuditLog:
    def __init__(self, path: str):
        self.path = Path(path)
        self.lock = threading.Lock()

    def write(self, event: str, remote: str, detail: str = "", ok: bool = True) -> None:
        entry = {
            "ts": utc_now(),
            "event": event,
            "remote": remote,
            "detail": detail,
            "ok": ok,
        }
        try:
            with self.lock:
                append_text(self.path, json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass


class JobManager:
    def __init__(self, config: Config, audit: AuditLog):
        self.config = config
        self.audit = audit
        self.lock = threading.Lock()
        self.jobs: Dict[str, Job] = {}
        self.current_job_id: Optional[str] = None
        self.last_session_path = Path(config.state_dir) / "last_session.json"
        self.last_session = self._load_last_session()
        self._load_jobs()

    def _load_last_session(self) -> Dict[str, str]:
        try:
            if self.last_session_path.exists():
                data = json.loads(self.last_session_path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict) and isinstance(data.get("session_id"), str):
                    return {
                        "session_id": data["session_id"],
                        "job_id": str(data.get("job_id") or ""),
                    }
        except Exception:
            pass
        return {}

    def _save_last_session(self, session_id: str, job_id: str) -> None:
        data = {"session_id": session_id, "job_id": job_id, "updated_at": utc_now()}
        try:
            atomic_text(self.last_session_path, json.dumps(data, ensure_ascii=False))
            self.last_session = {"session_id": session_id, "job_id": job_id}
        except Exception:
            pass

    @staticmethod
    def _job_meta_path(job: Job) -> Path:
        return job.output_path.with_name("job.json")

    def _persist_job(self, job: Job) -> None:
        try:
            atomic_text(self._job_meta_path(job), json.dumps(job.public(), ensure_ascii=False))
        except OSError as exc:
            self.audit.write("job_state_write_failed", "local", f"{job.job_id}: {exc}", ok=False)

    def _load_jobs(self) -> None:
        jobs_root = Path(self.config.state_dir) / "jobs"
        try:
            directories = sorted(
                (path for path in jobs_root.iterdir() if path.is_dir() and JOB_ID_RE.fullmatch(path.name)),
                key=lambda path: path.name,
            )[-MAX_LOADED_JOBS:]
        except OSError:
            return

        terminal = {"done", "failed", "error", "timeout", "killed"}
        for job_dir in directories:
            try:
                data = read_json_file(job_dir / "job.json")
                if not isinstance(data, dict):
                    continue
                prompt_path = job_dir / "prompt.txt"
                prompt = prompt_path.read_text(encoding="utf-8", errors="replace")
                status = str(data.get("status") or "error")
                if status not in terminal and status not in {"queued", "running"}:
                    status = "error"
                exit_code_value = data.get("exit_code")
                exit_code = int(exit_code_value) if exit_code_value is not None else None
                job = Job(
                    job_id=job_dir.name,
                    prompt=prompt,
                    output_path=job_dir / "output.log",
                    events_path=job_dir / "events.jsonl",
                    prompt_path=prompt_path,
                    command_display=str(data.get("command") or ""),
                    session_id=str(data.get("session_id") or ""),
                    continued_from=str(data.get("continued_from") or ""),
                    model=str(data.get("model") or ""),
                    effort=str(data.get("effort") or ""),
                    permission_mode=str(data.get("permission_mode") or ""),
                    created_at=str(data.get("created_at") or ""),
                    started_at=str(data.get("started_at") or ""),
                    ended_at=str(data.get("ended_at") or ""),
                    closed_at=str(data.get("closed_at") or ""),
                    status=status,
                    exit_code=exit_code,
                    error=str(data.get("error") or ""),
                    pid=None,
                    timed_out=bool(data.get("timed_out")),
                )
                for line in read_tail(job.events_path, MAX_EVENTS_READ_BYTES).splitlines():
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict) or int(event.get("seq", 0)) <= 0:
                            continue
                        job.event_cache.append(event)
                        job.latest_seq = max(job.latest_seq, int(event["seq"]))
                        if event.get("kind") == "text" and event.get("text"):
                            job.assistant_text_emitted = True
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                if job.status in {"queued", "running"}:
                    job.status = "error"
                    job.error = "Gateway restarted before the task finished."
                    job.ended_at = utc_now()
                    append_text(job.output_path, "\nGateway restarted before the task finished.\n")
                    self._persist_job(job)
                self.jobs[job.job_id] = job
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue

        open_jobs = [job for job in self.jobs.values() if not job.closed_at]
        self.current_job_id = open_jobs[-1].job_id if open_jobs else None

    def _latest_resumable_job(self) -> Optional[Job]:
        with self.lock:
            candidates = [
                job
                for job in self.jobs.values()
                if job.session_id
                and not job.closed_at
                and job.status in ("done", "failed", "error", "timeout", "killed")
            ]
        if not candidates:
            return None
        return max(candidates, key=lambda j: j.started_at or j.created_at)

    def start(
        self,
        prompt: str,
        timeout: Optional[int],
        remote: str,
        model: str = "",
        effort: str = "",
        permission_mode: str = "",
        continue_from: str = "",
        new_session: bool = False,
    ) -> Tuple[bool, Dict[str, object]]:
        prompt = prompt or ""
        if not prompt.strip():
            return False, {"error": "prompt is required"}
        if len(prompt) > self.config.max_prompt_chars:
            return False, {
                "error": f"prompt is too long ({len(prompt)} > {self.config.max_prompt_chars})"
            }
        if timeout is not None:
            timeout = max(30, min(int(timeout), 86400))

        continued_from = str(continue_from or "").strip()
        if continued_from:
            with self.lock:
                parent = self.jobs.get(continued_from)
            if not parent:
                return False, {"error": "task to continue was not found"}
            if parent.status in ("queued", "running"):
                return False, {"error": "wait for the selected task to finish before continuing it"}
            session_id = parent.session_id
            if not session_id:
                return False, {"error": "selected task does not have a resumable Claude session"}
            with self.lock:
                same_session_running = [
                    job
                    for job in self.jobs.values()
                    if (
                        job.session_id == session_id
                        and not job.closed_at
                        and job.status in ("queued", "running")
                    )
                ]
            if same_session_running:
                return False, {"error": "this Claude session already has a running task"}
            resume_session = True
        elif new_session or not self.config.auto_continue:
            session_id = str(uuid.uuid4())
            resume_session = False
        else:
            latest = self._latest_resumable_job()
            if latest and latest.session_id:
                session_id = latest.session_id
                resume_session = True
                continued_from = latest.job_id
            elif self.last_session.get("session_id"):
                session_id = self.last_session["session_id"]
                resume_session = True
                continued_from = self.last_session.get("job_id", "")
            else:
                session_id = str(uuid.uuid4())
                resume_session = False

        try:
            model = normalize_model(model)
            effort = normalize_effort(effort)
            permission_mode = normalize_permission_mode(permission_mode)
            command = self.config.claude_command(
                model=model,
                effort=effort,
                permission_mode=permission_mode,
                session_id=session_id,
                resume_session=resume_session,
            )
        except ValueError as exc:
            return False, {"error": str(exc)}

        with self.lock:
            job_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(4)
            job_dir = Path(self.config.state_dir) / "jobs" / job_id
            job = Job(
                job_id=job_id,
                prompt=prompt,
                output_path=job_dir / "output.log",
                events_path=job_dir / "events.jsonl",
                prompt_path=job_dir / "prompt.txt",
                command_display=display_command(command),
                command=command,
                session_id=session_id,
                continued_from=continued_from,
                model=model,
                effort=effort,
                permission_mode=permission_mode,
            )
            self.jobs[job_id] = job
            self.current_job_id = job_id

        try:
            atomic_text(job.prompt_path, prompt)
            atomic_text(job.output_path, "")
            atomic_text(job.events_path, "")
            self._persist_job(job)
        except OSError as exc:
            with self.lock:
                self.jobs.pop(job_id, None)
                if self.current_job_id == job_id:
                    self.current_job_id = None
            return False, {"error": f"could not create task files: {exc}"}

        thread = threading.Thread(
            target=self._run_job,
            args=(job, timeout or self.config.timeout_seconds, remote),
            daemon=True,
        )
        thread.start()
        self.audit.write("job_started", remote, job.job_id)
        return True, job.public()

    def _run_job(self, job: Job, timeout: int, remote: str) -> None:
        with self.lock:
            if job.cancel_requested:
                job.status = "killed"
                job.ended_at = utc_now()
                return
            job.status = "running"
            job.started_at = utc_now()
            self._persist_job(job)
        try:
            self._run_headless(job, timeout)
        except Exception as exc:
            if job.cancel_requested:
                job.status = "killed"
            else:
                job.status = "error"
                job.error = str(exc)
                append_text(job.output_path, f"\nGateway error: {exc}\n")
                self.audit.write("job_error", remote, f"{job.job_id}: {exc}", ok=False)
        finally:
            job.ended_at = utc_now()
            self._persist_job(job)
            if job.session_id and job.status in ("done", "failed", "error", "timeout", "killed"):
                self._save_last_session(job.session_id, job.job_id)
            self.audit.write("job_finished", remote, f"{job.job_id}: {job.status}")

    def _kill_on_timeout(self, job: Job) -> None:
        proc = job.process
        if job.cancel_requested or not proc or proc.poll() is not None:
            return
        job.timed_out = True
        append_text(job.output_path, "\nGateway timeout reached; terminating process.\n")
        self._terminate_process_tree(proc)

    def _run_headless(self, job: Job, timeout: int) -> None:
        command = list(job.command or self.config.claude_command())
        executable = resolve_command_program(command[0])
        if not executable:
            job.exit_code = 127
            job.status = "failed"
            job.error = "Claude Code CLI is not installed or not available in PATH."
            append_text(job.output_path, claude_cli_missing_message(command[0]))
            append_text(job.output_path, f"\nExit code: {job.exit_code}\n")
            return
        command[0] = executable

        if job.cancel_requested:
            job.status = "killed"
            return

        timer = threading.Timer(timeout, self._kill_on_timeout, args=(job,))
        timer_started = False
        stderr_path = job.output_path.with_name("stderr.log")
        atomic_text(stderr_path, "")
        try:
            with subprocess.Popen(
                command,
                cwd=self.config.working_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **hidden_subprocess_kwargs(),
            ) as proc:
                job.process = proc
                job.pid = proc.pid
                if job.cancel_requested:
                    self._terminate_process_tree(proc)
                else:
                    timer.start()
                    timer_started = True
                    assert proc.stdin is not None
                    proc.stdin.write(job.prompt)
                    proc.stdin.close()

                    def _drain_stderr() -> None:
                        assert proc.stderr is not None
                        for line in proc.stderr:
                            append_text(stderr_path, line)

                    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
                    stderr_thread.start()

                    assert proc.stdout is not None
                    seq = 0
                    for line in proc.stdout:
                        seq = self._consume_stream_line(job, line, seq)

                    proc.wait()
                    stderr_thread.join(timeout=5)
                job.exit_code = proc.returncode
        finally:
            if timer_started:
                timer.cancel()
            job.process = None

        if job.cancel_requested:
            job.status = "killed"
        elif job.timed_out:
            job.status = "timeout"
        elif job.exit_code == 0:
            job.status = "done"
        else:
            job.status = "failed"
        append_text(job.output_path, f"\nExit code: {job.exit_code}\n")

    @staticmethod
    def _try_parse_json(line: str) -> Optional[Dict[str, object]]:
        stripped = line.strip()
        if not stripped:
            return None
        try:
            obj = json.loads(stripped)
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None

    @staticmethod
    def _extract_events(obj: Dict[str, object]) -> List[Tuple[str, str, Dict[str, object]]]:
        typ = str(obj.get("type") or "")
        if typ == "system":
            sub = str(obj.get("subtype") or "")
            if sub == "init":
                meta = {
                    k: obj.get(k)
                    for k in ("model", "cwd", "session_id", "permissionMode", "apiKeySource")
                    if obj.get(k)
                }
                return [("init", "会话已启动", meta)]
            return []
        if typ == "assistant":
            message = obj.get("message") or {}
            if not isinstance(message, dict):
                return [("raw", json.dumps(obj, ensure_ascii=False), {})]
            content = message.get("content") or []
            if not isinstance(content, list):
                return [("raw", json.dumps(obj, ensure_ascii=False), {})]
            events: List[Tuple[str, str, Dict[str, object]]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = str(block.get("type") or "")
                if bt == "text":
                    events.append(("text", str(block.get("text") or ""), {}))
                elif bt == "tool_use":
                    events.append(
                        (
                            "tool_use",
                            str(block.get("name") or "tool"),
                            {
                                "name": block.get("name") or "tool",
                                "tool_use_id": block.get("id") or "",
                                "input": block.get("input") or {},
                            },
                        )
                    )
                elif bt == "thinking":
                    events.append(("thinking", str(block.get("thinking") or ""), {}))
                else:
                    events.append(("raw", json.dumps(block, ensure_ascii=False), {}))
            return events
        if typ == "user":
            message = obj.get("message") or {}
            if not isinstance(message, dict):
                return [("raw", json.dumps(obj, ensure_ascii=False), {})]
            content = message.get("content") or []
            if not isinstance(content, list):
                return [("raw", json.dumps(obj, ensure_ascii=False), {})]
            events = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = str(block.get("type") or "")
                if bt == "tool_result":
                    inner = block.get("content") or ""
                    if isinstance(inner, list):
                        parts = []
                        for c in inner:
                            if isinstance(c, dict) and c.get("type") == "text":
                                parts.append(str(c.get("text") or ""))
                            else:
                                parts.append(json.dumps(c, ensure_ascii=False))
                        inner = "\n".join(parts)
                    events.append(
                        (
                            "tool_result",
                            str(inner),
                            {
                                "tool_use_id": block.get("tool_use_id") or "",
                                "is_error": bool(block.get("is_error")),
                            },
                        )
                    )
                else:
                    events.append(("raw", json.dumps(block, ensure_ascii=False), {}))
            return events
        if typ == "result":
            sub = str(obj.get("subtype") or "")
            is_err = bool(obj.get("is_error")) or sub == "error_during_execution"
            meta = {
                k: obj.get(k)
                for k in ("subtype", "duration_ms", "duration_api_ms", "num_turns", "total_cost_usd")
                if obj.get(k) is not None
            }
            return [("error" if is_err else "result", str(obj.get("result") or ""), meta)]
        return [("raw", json.dumps(obj, ensure_ascii=False), {})]

    @staticmethod
    def _event_readable(kind: str, text: str, meta: Dict[str, object]) -> str:
        if kind == "text":
            return text + "\n"
        if kind == "tool_use":
            name = str(meta.get("name") or "tool")
            try:
                inp_text = json.dumps(meta.get("input") or {}, ensure_ascii=False)
            except Exception:
                inp_text = str(meta.get("input") or "")
            return f"\n[Tool call: {name}]\n{inp_text}\n"
        if kind == "tool_result":
            err = " (error)" if meta.get("is_error") else ""
            return f"\n[Tool result{err}]\n{text}\n"
        if kind == "thinking":
            return f"\n[Thinking]\n{text}\n"
        if kind == "result":
            return f"\n[Result]\n{text}\n" if text else "\n[Result]\n"
        if kind == "error":
            return f"\n[Error]\n{text}\n"
        if kind == "init":
            model = (meta or {}).get("model", "")
            return f"[Session] model={model}\n" if model else ""
        if kind == "raw":
            return text + "\n"
        return ""

    def _emit_event(
        self,
        job: Job,
        seq: int,
        kind: str,
        text: str,
        meta: Dict[str, object],
    ) -> None:
        event = {"seq": seq, "ts": utc_now(), "kind": kind, "text": text, "meta": meta}
        append_text(job.events_path, json.dumps(event, ensure_ascii=False) + "\n")
        with job.event_lock:
            job.event_cache.append(event)
            job.latest_seq = seq
        readable = self._event_readable(kind, text, meta)
        if readable:
            append_text(job.output_path, readable)

    def _consume_stream_line(self, job: Job, line: str, seq: int) -> int:
        obj = self._try_parse_json(line)
        if obj is None:
            text = line.rstrip("\n")
            if text:
                seq += 1
                self._emit_event(job, seq, "raw", text, {})
            return seq
        for kind, text, meta in self._extract_events(obj):
            if kind == "text" and text:
                job.assistant_text_emitted = True
            elif kind == "result" and job.assistant_text_emitted:
                # Claude's final result repeats the already streamed assistant
                # message. Keep the completion event but do not render it twice.
                text = ""
            seq += 1
            self._emit_event(job, seq, kind, text, meta)
        return seq

    def _terminate_process_tree(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        if is_windows():
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    **hidden_subprocess_kwargs(),
                )
                return
            except Exception:
                pass
        try:
            proc.terminate()
        except Exception:
            pass

    def kill_job(self, job_id: str, remote: str) -> Dict[str, object]:
        with self.lock:
            job = self.jobs.get(job_id or "")
            if not job or job.status not in ("queued", "running"):
                return {"status": "idle"}
            job.cancel_requested = True
            job.status = "killed"
        if job.process:
            self._terminate_process_tree(job.process)
        job.ended_at = utc_now()
        append_text(job.output_path, "\nKilled by gateway request.\n")
        self._persist_job(job)
        self.audit.write("job_killed", remote, job.job_id)
        return job.public()

    def kill_current(self, remote: str) -> Dict[str, object]:
        with self.lock:
            job = self.jobs.get(self.current_job_id or "")
            if not job or job.status not in ("queued", "running"):
                running = [
                    item
                    for item in self.jobs.values()
                    if not item.closed_at and item.status in ("queued", "running")
                ]
                job = running[-1] if running else None
        if not job:
            return {"status": "idle"}
        return self.kill_job(job.job_id, remote)

    def close_job(self, job_id: str, remote: str) -> Dict[str, object]:
        with self.lock:
            job = self.jobs.get(job_id or "")
            if not job:
                return {"status": "idle"}
            if job.status in ("queued", "running"):
                return {
                    "error": "current Claude task is still running; stop it before closing",
                    "current_job": job.public(),
                }
            job.closed_at = utc_now()
            self._persist_job(job)
            if self.current_job_id == job.job_id:
                open_jobs = [item for item in self.jobs.values() if not item.closed_at]
                self.current_job_id = open_jobs[-1].job_id if open_jobs else None
        self.audit.write("job_closed", remote, job.job_id)
        return {"status": "closed", "closed_job": job.public()}

    def close_current(self, remote: str) -> Dict[str, object]:
        with self.lock:
            job = self.jobs.get(self.current_job_id or "")
            if not job:
                open_jobs = [item for item in self.jobs.values() if not item.closed_at]
                job = open_jobs[-1] if open_jobs else None
        if not job:
            return {"status": "idle"}
        return self.close_job(job.job_id, remote)

    def status(self) -> Dict[str, object]:
        with self.lock:
            current = self.jobs.get(self.current_job_id or "")
            open_jobs = [job for job in self.jobs.values() if not job.closed_at]
            if not current or current.closed_at:
                current = open_jobs[-1] if open_jobs else None
            recent = open_jobs[-20:]
            running = [job for job in open_jobs if job.status in ("queued", "running")]
        return {
            "app": APP_NAME,
            "version": VERSION,
            "mode": "background",
            "working_dir": self.config.working_dir,
            "command": display_command(self.config.claude_command()),
            "current_job": current.public() if current else None,
            "recent_jobs": [job.public() for job in recent],
            "running_jobs": [job.public() for job in running],
        }

    def get_job(self, job_id: str) -> Optional[Job]:
        with self.lock:
            return self.jobs.get(job_id)

    def read_events(self, job: Job, after: int) -> Tuple[List[Dict[str, object]], int]:
        after = max(0, int(after))
        with job.event_lock:
            events = [event.copy() for event in job.event_cache if int(event["seq"]) > after]
            latest = max(after, job.latest_seq)
        return events, latest

    def shutdown_all(self) -> None:
        with self.lock:
            jobs = [job for job in self.jobs.values() if job.status in ("queued", "running")]
            for job in jobs:
                job.cancel_requested = True
                job.status = "killed"
        for job in jobs:
            job.ended_at = utc_now()
            self._persist_job(job)
            if job.process:
                self._terminate_process_tree(job.process)


UI_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>9090 网关</title>
<style>
.subtitle{display:none}:root{color-scheme:light;--bg:#f5f5f7;--panel:#fff;--soft:#f8f8fa;--text:#1d1d1f;--muted:#6e6e73;--line:#1d1d1f18;--blue:#0071e3;--blue2:#0066cc;--red:#d70015;--green:#248a3d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px max(18px,calc((100vw - 1240px)/2 + 18px));background:#ffffffd9;border-bottom:1px solid var(--line);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px)}
.title{font-size:18px;font-weight:700;letter-spacing:0}.pill{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:999px;background:#fff;padding:7px 11px;color:var(--muted);font-size:12px;white-space:nowrap;box-shadow:0 1px 3px #00000008}.dot{width:7px;height:7px;border-radius:50%;background:#8e8e93}.pill.ok .dot{background:var(--green)}.pill.run .dot,.pill.running .dot{background:var(--blue)}.pill.err .dot{background:var(--red)}
main{max-width:1240px;margin:0 auto;padding:18px}.grid{display:grid;grid-template-columns:360px minmax(0,1fr);gap:16px;align-items:start}.stack{display:grid;gap:16px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:0 4px 18px #1d1d1f08}.panel h2{margin:0;padding:14px 16px 4px;background:transparent;font-size:13px;color:var(--text);font-weight:650}.body{padding:10px 16px 16px}
label{display:block;margin:10px 0 6px;font-weight:600;font-size:12px;color:var(--muted)}input,textarea,select,button{font:inherit}input,textarea,select{width:100%;border:1px solid #1d1d1f22;border-radius:9px;background:#fff;color:var(--text);padding:10px 11px;outline:none}input:focus,textarea:focus,select:focus{border-color:var(--blue);box-shadow:0 0 0 3px #0071e326}textarea{min-height:210px;resize:vertical;line-height:1.55}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.row>*{min-width:0}.tight{margin-top:10px}.control-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;align-items:start}.hint{margin-top:6px;color:var(--muted);font-size:12px;line-height:1.55}.hint:empty{display:none}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
button{min-height:40px;border:1px solid var(--blue);border-radius:9px;background:var(--blue);color:#fff;padding:8px 13px;font-weight:600;cursor:pointer;transition:background .15s,box-shadow .15s,transform .15s}button:hover{background:var(--blue2);box-shadow:0 3px 10px #0071e32e}button:active{transform:translateY(1px)}button:disabled{opacity:.55;cursor:not-allowed;box-shadow:none}button.secondary{background:#fff;color:var(--text);border-color:var(--line)}button.secondary:hover{background:var(--soft);box-shadow:none}button.ghost{background:transparent;color:var(--muted);border-color:transparent}button.ghost:hover{background:var(--soft);box-shadow:none}
details{margin-top:10px;border:1px solid var(--line);border-radius:9px;background:var(--soft);padding:9px 10px}summary{cursor:pointer;font-weight:600;color:var(--text)}.meta{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.metric{border:1px solid var(--line);background:var(--soft);border-radius:10px;padding:9px 10px;min-height:62px}.metric b{display:block;margin-bottom:4px;color:var(--muted);font-size:12px;font-weight:600}.metric span{color:var(--text);word-break:break-all}.jobs{display:grid;gap:6px;max-height:360px;overflow:auto}.job{width:100%;display:grid;grid-template-columns:1fr auto;gap:8px;text-align:left;border:1px solid var(--line);background:#fff;color:var(--text);border-radius:10px;padding:9px 10px}.job:hover{background:var(--soft)}.job.active{border-color:#0071e366;background:#f0f7ff}.job small{color:var(--muted)}.tag{font-size:12px;color:var(--muted)}.tag.running{color:var(--blue)}.tag.done{color:var(--green)}.tag.failed,.tag.error,.tag.timeout,.tag.killed{color:var(--red)}
.output-head{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:13px 16px 10px;border-bottom:1px solid var(--line);background:transparent}.output-title{font-weight:650;color:var(--text)}.output-tools{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px}pre{margin:0;white-space:pre-wrap;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.stream{min-height:560px;max-height:calc(100vh - 220px);overflow:auto;padding:16px;background:#fff;color:#111827;word-break:break-word}
.stream.raw-mode{white-space:pre-wrap;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.stream .hint{color:var(--muted)}
.ev{padding:6px 0;border-bottom:1px solid #f1f3f5}.ev:last-child{border-bottom:none}
.ev-tag{display:inline-block;margin-right:8px;padding:1px 8px;border-radius:4px;background:#eef2f6;color:#4b5563;font-size:12px;vertical-align:middle}
.ev-tag.ok{background:#d1fae5;color:#065f46}.ev-tag.err{background:#fee2e2;color:#991b1b}
.ev .p{margin:2px 0}.ev .sp{height:8px}.ev .li{margin:2px 0 2px 14px}.ev .li::before{content:"• ";color:#9ca3af}
.ev h1.md,.ev h2.md,.ev h3.md,.ev h4.md,.ev h5.md,.ev h6.md{margin:8px 0 4px;font-weight:700;line-height:1.4}
.ev h1.md{font-size:16px}.ev h2.md{font-size:15px}.ev h3.md{font-size:14px}
.ev code{background:#f1f3f5;padding:1px 5px;border-radius:4px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.ev pre.code{background:#0f172a;color:#e2e8f0;padding:10px;border-radius:6px;overflow:auto;margin:6px 0;white-space:pre}
.ev details{margin:4px 0;border:1px solid var(--line);border-radius:6px;background:var(--soft)}
.ev summary{cursor:pointer;padding:6px 10px;font-weight:650;color:#4b5563}
.ev details pre.code{margin:0;border-radius:0 0 6px 6px}
.ev .think{color:#6b7280;font-style:italic;padding:8px 10px;white-space:pre-wrap}
.ev-meta{color:var(--muted);font-size:12px;margin-top:4px}
@media(max-width:880px){header{padding:12px 14px}.grid{grid-template-columns:1fr}.meta,.control-grid{grid-template-columns:1fr}.stream{min-height:340px;max-height:none}main{padding:12px}.panel{border-radius:12px}}
</style>
<style>
/* Desktop workspace: task and output are the primary surfaces. */
body{font-size:13px;background:#f5f5f7}header{min-height:56px;padding:10px max(20px,calc((100vw - 1440px)/2 + 20px))}.title{font-size:16px}.pill{padding:6px 10px}
main{max-width:1440px;padding:14px 20px 20px}.grid{grid-template-columns:minmax(0,1fr) 308px;grid-template-rows:auto minmax(0,1fr) auto;gap:12px}.stack{display:contents}.panel{border-radius:10px;box-shadow:none}.panel h2{padding:13px 15px 3px;font-size:13px}.body{padding:9px 15px 14px}
.grid>.stack:first-child>.panel:first-child{grid-column:2;grid-row:1}.grid>.stack:first-child>.panel:nth-child(2){grid-column:1;grid-row:1}.grid>.stack:nth-child(2)>.panel:first-child{grid-column:2;grid-row:2}.grid>.stack:nth-child(2)>.panel:nth-child(2){grid-column:2;grid-row:3}.grid>.stack:nth-child(2)>.panel:nth-child(3){grid-column:1;grid-row:2 / span 2;min-width:0}
textarea{min-height:138px}.control-grid{grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.control-grid>div:last-child{grid-column:auto}.tight{margin-top:8px}.row{gap:7px}label{margin:8px 0 5px;font-size:11px}.hint,.tag,.output-tools{font-size:11px}input,textarea,select{padding:8px 9px;border-radius:8px}button{min-height:36px;border-radius:8px;padding:7px 11px}
.meta{grid-template-columns:1fr;gap:0}.metric{display:grid;grid-template-columns:54px minmax(0,1fr);gap:8px;min-height:auto;padding:8px 0;border:0;border-bottom:1px solid var(--line);border-radius:0;background:transparent}.metric:last-child{border-bottom:0}.metric b{margin:0;font-size:11px}.metric span{font-size:11px;line-height:1.45}.jobs{max-height:250px}.job{padding:8px;border-radius:8px}.output-head{padding:12px 15px 9px}.stream{min-height:380px;height:calc(100vh - 320px);max-height:none;padding:15px}
@media(max-width:880px){.grid{display:grid;grid-template-columns:1fr;grid-template-rows:none}.stack{display:grid}.grid>.stack:first-child>.panel:first-child,.grid>.stack:first-child>.panel:nth-child(2),.grid>.stack:nth-child(2)>.panel:first-child,.grid>.stack:nth-child(2)>.panel:nth-child(2),.grid>.stack:nth-child(2)>.panel:nth-child(3){grid-column:auto;grid-row:auto}.control-grid{grid-template-columns:1fr}.stream{height:auto;min-height:340px}main{padding:12px}}
 </style>
<style>
.header-tools{display:flex;align-items:center;gap:8px}.header-tools .ghost{min-height:32px;padding:5px 8px;font-size:12px}.grid{grid-template-rows:auto minmax(0,1fr)}
.grid>.stack:first-child>.panel{grid-column:1;grid-row:1}.grid>.stack:nth-child(2)>.panel:first-child{grid-column:2;grid-row:1}.grid>.stack:nth-child(2)>.panel:nth-child(2){grid-column:2;grid-row:2}.grid>.stack:nth-child(2)>.panel:nth-child(3){grid-column:1;grid-row:2;min-width:0}.jobs{max-height:calc(100vh - 350px)}.stream{height:calc(100vh - 315px);min-height:360px}
.access-dialog{width:min(390px,calc(100vw - 28px));margin:auto;border:1px solid var(--line);border-radius:12px;padding:0;background:var(--panel);color:var(--text);box-shadow:0 20px 60px #0000002a}.access-dialog::backdrop{background:#1d1d1f38}.access-head{display:flex;align-items:center;justify-content:space-between;padding:14px 15px 2px}.access-head h2{margin:0;font-size:15px}.access-logout{display:flex;justify-content:flex-end;margin-top:14px}.access-dialog .body{padding:8px 15px 15px}.access-dialog details{margin-top:12px}
@media(max-width:880px){.grid>.stack:first-child>.panel,.grid>.stack:nth-child(2)>.panel:first-child,.grid>.stack:nth-child(2)>.panel:nth-child(2),.grid>.stack:nth-child(2)>.panel:nth-child(3){grid-column:auto;grid-row:auto}.stream{height:auto}}
</style>
<style>
.grid>.stack:first-child>.panel>h2{display:none}.grid>.stack:first-child>.panel .body{padding-top:13px;padding-bottom:12px}.grid>.stack:first-child>.panel textarea{min-height:96px}.grid>.stack:first-child>.panel label{margin-top:5px;margin-bottom:4px}.grid>.stack:first-child>.panel .tight{margin-top:6px}.grid>.stack:first-child>.panel input,.grid>.stack:first-child>.panel select{padding-top:7px;padding-bottom:7px}.stream{height:calc(100vh - 405px);min-height:330px}
</style>
</head>
<body>
<header><div class="title">9090</div><div class="header-tools"><button id="accessOpen" class="ghost" type="button">账户</button><div id="state" class="pill"><span class="dot"></span><span id="stateText">连接中</span></div></div></header>
<main><div class="grid"><div class="stack">
<dialog id="accessDialog" class="access-dialog"><div class="access-head"><h2>登录</h2><button id="accessClose" class="ghost" type="button">关闭</button></div><div class="body"><label for="password">密码</label><div class="row"><input id="password" type="password" autocomplete="current-password" placeholder="输入密码"><button id="login" type="button">登录</button></div><details><summary>令牌</summary><label for="token">令牌</label><input id="token" type="password" placeholder="旧版令牌"></details><div class="access-logout"><button id="logout" class="secondary" type="button">退出登录</button></div></div></dialog>
<section class="panel"><h2>新建</h2><div class="body"><label for="prompt">任务</label><textarea id="prompt" placeholder="描述要执行的任务"></textarea><div class="control-grid tight"><div><label for="model">模型</label><select id="model"><option value="">默认</option><option value="sonnet">Sonnet</option><option value="opus">Opus</option><option value="haiku">Haiku</option></select></div><div><label for="effort">思考</label><select id="effort"><option value="">默认</option><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option><option value="xhigh">XHigh</option><option value="max">Max</option></select></div><div><label for="permissionMode">权限</label><select id="permissionMode"><option value="auto" selected>自动</option><option value="bypassPermissions">不询问</option><option value="default">默认</option></select><div id="permissionHelp" class="hint"></div></div></div><div class="row tight"><div style="flex:1;min-width:160px"><label for="timeout">超时</label><input id="timeout" type="number" min="30" step="30" placeholder="默认"></div></div><div class="row tight"><label class="row" style="margin:0 8px 0 0"><input id="newSession" type="checkbox" style="width:auto"> 新会话</label></div><div class="row tight"><button id="run" type="button">发送</button><button id="continueRun" class="secondary" type="button">接续</button><button id="kill" class="secondary" type="button">停止</button><button id="closeJob" class="secondary" type="button">关闭</button><button id="refresh" class="ghost" type="button">刷新</button></div></div></section>
</div><div class="stack">
<section class="panel"><h2>环境</h2><div class="body"><div class="meta"><div class="metric"><b>模式</b><span id="mode">-</span></div><div class="metric"><b>目录</b><span id="workdir" class="mono">-</span></div><div class="metric"><b>命令</b><span id="command" class="mono">-</span></div></div></div></section>
<section class="panel"><h2>任务</h2><div class="body"><div id="jobs" class="jobs"><div class="hint">登录后查看</div></div></div></section>
<section class="panel"><div class="output-head"><div><div class="output-title">输出</div><div id="selectedJob" class="hint">未选</div></div><div class="output-tools"><label class="row" style="margin:0"><input id="raw" type="checkbox" style="width:auto"> 原文</label><label class="row" style="margin:0"><input id="follow" type="checkbox" checked style="width:auto"> 跟随</label></div></div><div id="out" class="stream"><div class="hint">等待登录</div></div></section>
</div></div></main>
<script>
const $ = (id) => document.getElementById(id);
const token = $('token');
const password = $('password');
const promptBox = $('prompt');
const timeoutBox = $('timeout');
const modelBox = $('model');
const effortBox = $('effort');
const permissionModeBox = $('permissionMode');
const runBtn = $('run');
const continueBtn = $('continueRun');
const closeBtn = $('closeJob');
const newSession = $('newSession');
const out = $('out');
const jobsEl = $('jobs');
const follow = $('follow');
const raw = $('raw');
const accessDialog = $('accessDialog');
const accessOpen = $('accessOpen');
const accessClose = $('accessClose');
let selectedJobId = null;
let loadedJobId = null;
let lastSeq = 0;
let loggedIn = false;
let polling = false;
let authRequired = false;
let nextPollDelay = 3000;
function showAccess(){if(!accessDialog.open) accessDialog.showModal();}
function hideAccess(){if(accessDialog.open) accessDialog.close();}
function headers(json=true){const h={}; if(json) h['Content-Type']='application/json'; if(token.value) h['Authorization']='Bearer '+token.value; return h;}
function withCreds(options={}){return Object.assign({credentials:'same-origin'}, options);}
function setState(text,kind=''){$('state').className='pill '+kind; $('stateText').textContent=text;}
function setText(id,text){$(id).textContent=text||'-';}
function jobStatusClass(status){return ['running','done','failed','error','timeout','killed'].includes(status)?status:'';}
function jobStatusText(status){const map={queued:'排队',running:'运行',done:'完成',failed:'失败',error:'错误',timeout:'超时',killed:'已停止'}; return map[status]||status||'-';}
function updatePermissionHelp(){$('permissionHelp').textContent='';}
async function api(path,options={}){const res=await fetch(path,withCreds(options)); const body=await res.json().catch(()=>({})); if(!res.ok){const err=new Error(body.error||('HTTP '+res.status)); err.status=res.status; err.body=body; throw err;} return body;}
async function login(){ $('login').disabled=true; try{await api('/login',{method:'POST',headers:headers(),body:JSON.stringify({password:password.value})}); password.value=''; loggedIn=true; authRequired=false; hideAccess(); setState('已登录','ok'); await poll(true);}catch(err){out.textContent=JSON.stringify(err.body||{error:err.message},null,2); setState('登录失败','err');}finally{$('login').disabled=false;}}
async function logout(){await fetch('/logout',withCreds({method:'POST'})); loggedIn=false; authRequired=true; selectedJobId=null; jobsEl.innerHTML='<div class="hint">已退出</div>'; out.textContent='已退出'; setState('已退出','err'); showAccess();}
async function run(continueSelected=false){const prompt=promptBox.value.trim(); if(!prompt){out.textContent='请输入任务'; return;} if(continueSelected&&!selectedJobId){setState('请先选任务','err'); return;} runBtn.disabled=true; continueBtn.disabled=true; out.textContent=continueSelected?'已接续':'已发送'; try{const payload={prompt}; if(continueSelected) payload.continue_from=selectedJobId; else payload.new_session=newSession.checked; if(modelBox.value) payload.model=modelBox.value; if(effortBox.value) payload.effort=effortBox.value; if(permissionModeBox.value) payload.permission_mode=permissionModeBox.value; const timeout=Number(timeoutBox.value||0); if(timeout>0) payload.timeout=timeout; const data=await api('/run',{method:'POST',headers:headers(),body:JSON.stringify(payload)}); selectedJobId=data.job_id; setState('运行 '+selectedJobId,'run'); await poll();}catch(err){out.textContent=JSON.stringify(err.body||{error:err.message},null,2); setState(err.status===401?'请先登录':'发送失败','err');}finally{runBtn.disabled=false; continueBtn.disabled=false;}}
async function kill(){if(!selectedJobId){setState('请先选任务','err'); return;} try{const data=await api('/jobs/'+encodeURIComponent(selectedJobId)+'/kill',{method:'POST',headers:headers(),body:'{}'}); setState(data.status==='idle'?'任务未运行':'已停止 '+selectedJobId,data.status==='idle'?'':'run'); await poll();}catch(err){out.textContent=JSON.stringify(err.body||{error:err.message},null,2); setState('停止失败','err');}}
async function closeCurrentJob(){if(!selectedJobId){setState('请先选任务','err'); return;} closeBtn.disabled=true; try{const closingId=selectedJobId; const data=await api('/jobs/'+encodeURIComponent(closingId)+'/close',{method:'POST',headers:headers(),body:'{}'}); if(data.error){setState(data.error,'err'); return;} selectedJobId=null; $('selectedJob').textContent='未选'; out.textContent='已关闭'; setState('就绪','ok'); await poll();}catch(err){out.textContent=JSON.stringify(err.body||{error:err.message},null,2); setState('关闭失败','err');}finally{closeBtn.disabled=false;}}
function renderJobs(status){const jobs=(status.recent_jobs||[]).slice().reverse(); if(!jobs.length){jobsEl.innerHTML='<div class="hint">暂无</div>'; return;} jobsEl.textContent=''; for(const job of jobs){const button=document.createElement('button'); button.className='job'+(job.job_id===selectedJobId?' active':''); const left=document.createElement('div'); const name=document.createElement('div'); name.className='mono'; name.textContent=job.job_id; const time=document.createElement('small'); const modelMap={sonnet:'Sonnet',opus:'Opus',haiku:'Haiku'}; const effortMap={low:'Low',medium:'Medium',high:'High',xhigh:'XHigh',max:'Max'}; const permMap={auto:'自动',bypassPermissions:'不询问',default:'默认',acceptEdits:'允许编辑',dontAsk:'不询问',plan:'计划'}; const opts=[modelMap[job.model]||job.model,effortMap[job.effort]||job.effort,permMap[job.permission_mode]||job.permission_mode].filter(Boolean).join(' / '); time.textContent=(job.started_at||job.created_at||'')+(opts?' | '+opts:''); left.append(name,time); const tag=document.createElement('span'); tag.className='tag '+jobStatusClass(job.status); tag.textContent=jobStatusText(job.status); button.append(left,tag); button.addEventListener('click',()=>{selectedJobId=job.job_id; openJob(job.job_id); renderJobs(status);}); jobsEl.appendChild(button);}}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function renderInline(s){let t=escapeHtml(s); t=t.replace(/`([^`]+)`/g,'<code>$1</code>'); t=t.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>'); return t;}
function renderMarkdown(text){const lines=String(text||'').replace(/\r\n/g,'\n').split('\n'); let html=''; let i=0; let inCode=false; let codeBuf=[]; function flushCode(){if(!inCode) return; html+='<pre class="code">'+escapeHtml(codeBuf.join('\n'))+'</pre>'; codeBuf=[]; inCode=false;} for(;i<lines.length;i++){const line=lines[i]; if(!inCode&&/^```/.test(line.trim())){inCode=true; codeBuf=[]; continue;} if(inCode){if(/^```/.test(line.trim())){flushCode(); continue;} codeBuf.push(line); continue;} if(/^\s*$/.test(line)){html+='<div class="sp"></div>'; continue;} if(/^#{1,6}\s/.test(line)){const m=line.match(/^(#{1,6})\s+(.*)/); html+='<h'+m[1].length+' class="md">'+renderInline(m[2])+'</h'+m[1].length+'>'; continue;} if(/^\s*[-*]\s+/.test(line)){html+='<div class="li">'+renderInline(line.replace(/^\s*[-*]\s+/,''))+'</div>'; continue;} html+='<div class="p">'+renderInline(line)+'</div>';} flushCode(); return html;}
function renderEvent(evt){const kind=evt.kind||'raw'; const el=document.createElement('div'); el.className='ev ev-'+kind; if(kind==='init'){const m=(evt.meta||{}).model||''; el.innerHTML='<span class="ev-tag">会话</span>'+escapeHtml(m)+' 就绪';} else if(kind==='text'){el.innerHTML=renderMarkdown(evt.text||'');} else if(kind==='tool_use'){const name=(evt.meta||{}).name||'tool'; const input=(evt.meta||{}).input||{}; const d=document.createElement('details'); const s=document.createElement('summary'); const tag=document.createElement('span'); tag.className='ev-tag'; tag.textContent='工具'; s.appendChild(tag); s.appendChild(document.createTextNode(' '+name)); d.appendChild(s); const pre=document.createElement('pre'); pre.className='code'; pre.textContent=JSON.stringify(input,null,2); d.appendChild(pre); el.appendChild(d);} else if(kind==='tool_result'){const err=(evt.meta||{}).is_error; const d=document.createElement('details'); const s=document.createElement('summary'); const tag=document.createElement('span'); tag.className='ev-tag'+(err?' err':''); tag.textContent=err?'结果错误':'结果'; s.appendChild(tag); d.appendChild(s); const pre=document.createElement('pre'); pre.className='code'; pre.textContent=evt.text||''; d.appendChild(pre); el.appendChild(d);} else if(kind==='thinking'){const d=document.createElement('details'); const s=document.createElement('summary'); const tag=document.createElement('span'); tag.className='ev-tag'; tag.textContent='思考'; s.appendChild(tag); d.appendChild(s); const p=document.createElement('div'); p.className='think'; p.textContent=evt.text||''; d.appendChild(p); el.appendChild(d);} else if(kind==='result'){const cost=(evt.meta||{}).total_cost_usd; el.innerHTML='<span class="ev-tag ok">完成</span>'+renderMarkdown(evt.text||'')+(cost!=null?'<div class="ev-meta">$'+cost+' · '+(evt.meta.duration_ms||'')+'ms</div>':'');} else if(kind==='error'){el.innerHTML='<span class="ev-tag err">错误</span>'+renderMarkdown(evt.text||'');} else {el.innerHTML=renderMarkdown(evt.text||'');} return el;}
function appendEvents(events){if(!events||!events.length) return; const hint=out.querySelector('.hint'); if(hint) hint.remove(); for(const ev of events){out.appendChild(renderEvent(ev));} if(follow.checked) out.scrollTop=out.scrollHeight;}
async function loadRawOutput(jobId){if(!jobId) return; const data=await api('/jobs/'+encodeURIComponent(jobId)+'/output?limit=300000',{headers:headers(false)}); if(jobId!==selectedJobId) return; out.textContent=data.output||''; if(follow.checked) out.scrollTop=out.scrollHeight;}
async function loadEvents(jobId,after){const data=await api('/jobs/'+encodeURIComponent(jobId)+'/events?after='+after,{headers:headers(false)}); if(jobId!==selectedJobId) return; lastSeq=data.seq||after; if(data.events&&data.events.length) appendEvents(data.events);}
async function openJob(jobId){if(!jobId){out.textContent=''; loadedJobId=null; return;} const changed=loadedJobId!==jobId; if(changed){out.textContent=''; lastSeq=0; loadedJobId=jobId;} $('selectedJob').textContent=jobId; out.classList.toggle('raw-mode',raw.checked); try{if(raw.checked){await loadRawOutput(jobId);}else{await loadEvents(jobId,lastSeq);}}catch(err){if(changed) out.textContent=JSON.stringify(err.body||{error:err.message},null,2);}}
async function poll(force=false){if(polling||(authRequired&&!force)) return; polling=true; try{const status=await api('/status',{headers:headers(false)}); loggedIn=true; authRequired=false; setText('mode',status.mode==='background'?'后台':'前台'); setText('workdir',status.working_dir); setText('command',status.command); const jobs=status.recent_jobs||[]; const known=new Set(jobs.map(j=>j.job_id)); if(selectedJobId&&!known.has(selectedJobId)){selectedJobId=null; $('selectedJob').textContent='未选';} if(!selectedJobId&&status.current_job){selectedJobId=status.current_job.job_id;} renderJobs(status); const selected=jobs.find(j=>j.job_id===selectedJobId); const runningCount=(status.running_jobs||[]).length; nextPollDelay=runningCount>0?750:3000; if(selected){setState(jobStatusText(selected.status)+' '+selected.job_id,jobStatusClass(selected.status));}else if(runningCount>0){setState('运行 '+runningCount,'running');}else if(loggedIn){setState('就绪','ok');} if(selectedJobId) await openJob(selectedJobId);}catch(err){nextPollDelay=3000; if(err.status===401){loggedIn=false; authRequired=true; setState('请先登录','err'); jobsEl.innerHTML='<div class="hint">登录后查看</div>';}else{setState('离线','err');}}finally{polling=false;}}
$('login').addEventListener('click',login);
$('logout').addEventListener('click',logout);
accessOpen.addEventListener('click',showAccess);
accessClose.addEventListener('click',hideAccess);
$('run').addEventListener('click',()=>run(false));
$('continueRun').addEventListener('click',()=>run(true));
$('kill').addEventListener('click',kill);
$('closeJob').addEventListener('click',closeCurrentJob);
$('refresh').addEventListener('click',()=>{authRequired=false;poll(true);});
permissionModeBox.addEventListener('change',updatePermissionHelp);
password.addEventListener('keydown',(event)=>{if(event.key==='Enter') login();});
promptBox.addEventListener('keydown',(event)=>{if((event.ctrlKey||event.metaKey)&&event.key==='Enter'){event.preventDefault(); run(false);}});
raw.addEventListener('change',()=>{loadedJobId=null; lastSeq=0; if(selectedJobId) openJob(selectedJobId);});
token.addEventListener('change',()=>{authRequired=false;poll(true);});
async function pollLoop(){await poll(); setTimeout(pollLoop,nextPollDelay);}
updatePermissionHelp();
pollLoop();
</script>
</body>
</html>
"""

class GatewayHandler(http.server.BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{VERSION}"

    def log_message(self, fmt: str, *args: object) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {self.client_address[0]} {fmt % args}", file=sys.stderr)

    @property
    def config(self) -> Config:
        return self.server.config  # type: ignore[attr-defined]

    @property
    def manager(self) -> JobManager:
        return self.server.manager  # type: ignore[attr-defined]

    @property
    def audit(self) -> AuditLog:
        return self.server.audit  # type: ignore[attr-defined]

    def _remote(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        return forwarded or self.client_address[0]

    def _cookies(self) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for item in self.headers.get("Cookie", "").split(";"):
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            result[key.strip()] = value.strip()
        return result

    def _sign_session_payload(self, payload: str) -> str:
        secret = self.config.session_secret.encode("utf-8")
        digest = hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest()
        return b64url_encode(digest)

    def _new_session_cookie(self) -> str:
        max_age = max(1, int(self.config.session_days)) * 24 * 60 * 60
        expires = dt.datetime.fromtimestamp(
            int(time.time()) + max_age, dt.timezone.utc
        ).strftime("%a, %d %b %Y %H:%M:%S GMT")
        payload = {
            "exp": int(time.time()) + max_age,
            "nonce": secrets.token_urlsafe(12),
        }
        payload_text = b64url_encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        signature = self._sign_session_payload(payload_text)
        return (
            f"{SESSION_COOKIE}={payload_text}.{signature}; "
            f"Max-Age={max_age}; Expires={expires}; Path=/; HttpOnly; SameSite=Lax"
        )

    def _session_ok(self) -> bool:
        if not self.config.password_hash or not self.config.session_secret:
            return False
        raw = self._cookies().get(SESSION_COOKIE, "")
        if "." not in raw:
            return False
        payload_text, signature = raw.rsplit(".", 1)
        expected = self._sign_session_payload(payload_text)
        if not hmac.compare_digest(signature, expected):
            return False
        try:
            payload = json.loads(b64url_decode(payload_text).decode("utf-8"))
            return int(payload.get("exp", 0)) >= int(time.time())
        except Exception:
            return False

    def _auth_ok(self) -> Tuple[bool, str]:
        remote = self._remote()
        networks = self.server.allowlist_networks  # type: ignore[attr-defined]
        if networks:
            try:
                remote_addr = ipaddress.ip_address(remote)
                if not any(remote_addr in net for net in networks):
                    return False, "remote IP is not allowed"
            except ValueError:
                return False, "remote IP is invalid"

        if self.config.token:
            auth = self.headers.get("Authorization", "")
            alt = self.headers.get("X-Gateway-Token", "")
            if auth == f"Bearer {self.config.token}" or alt == self.config.token:
                return True, ""

        if self._session_ok():
            return True, ""

        if self.config.token and not self.config.password_hash:
            return False, "missing or invalid token"
        if self.config.password_hash:
            return False, "login required"

        if remote_is_loopback(remote):
            return True, ""
        if self.config.unsafe_no_token:
            return True, ""
        return False, "token is required for remote access"

    def _require_auth(self) -> bool:
        ok, reason = self._auth_ok()
        if ok:
            return True
        self.audit.write("auth_failed", self._remote(), reason, ok=False)
        self._send_json({"error": reason}, status=401)
        return False

    def _read_body(self, maximum: int) -> Optional[str]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self._send_json({"error": "invalid Content-Length"}, status=400)
            return None
        if length < 0:
            self._send_json({"error": "invalid Content-Length"}, status=400)
            return None
        if length > maximum:
            self._send_json({"error": "request body is too large"}, status=413)
            return None
        try:
            return self.rfile.read(length).decode("utf-8", errors="replace")
        except OSError as exc:
            self._send_json({"error": f"could not read request body: {exc}"}, status=400)
            return None

    def _query_int(
        self,
        query: Dict[str, List[str]],
        name: str,
        default: int,
        minimum: int,
        maximum: int,
    ) -> Optional[int]:
        try:
            value = int(query.get(name, [str(default)])[0])
        except (TypeError, ValueError, OverflowError):
            self._send_json({"error": f"{name} must be an integer"}, status=400)
            return None
        return max(minimum, min(value, maximum))

    def _send_json(
        self,
        data: Dict[str, object],
        status: int = 200,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, X-Gateway-Token, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, X-Gateway-Token, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            self._send_text(UI_HTML, content_type="text/html; charset=utf-8")
            return
        if path == "/health":
            self._send_json({"ok": True, "app": APP_NAME, "version": VERSION})
            return

        if not self._require_auth():
            return

        if path == "/status":
            self._send_json(self.manager.status())
            return

        if path == "/processes":
            self._send_json(
                {"error": "Windows program control moved to port 9091"},
                status=404,
            )
            return
        if path == "/taskbar":
            self._send_json(
                {"error": "Windows taskbar control moved to port 9091"},
                status=404,
            )
            return

        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "jobs":
            job = self.manager.get_job(parts[1])
            if not job:
                self._send_json({"error": "job not found"}, status=404)
                return
            self._send_json(job.public())
            return

        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "output":
            job = self.manager.get_job(parts[1])
            if not job:
                self._send_json({"error": "job not found"}, status=404)
                return
            query = urllib.parse.parse_qs(parsed.query)
            limit = self._query_int(query, "limit", 200_000, 0, MAX_OUTPUT_READ_BYTES)
            if limit is None:
                return
            self._send_json(
                {
                    "job_id": job.job_id,
                    "output": clean_task_output(read_tail(job.output_path, limit)),
                }
            )
            return

        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "events":
            job = self.manager.get_job(parts[1])
            if not job:
                self._send_json({"error": "job not found"}, status=404)
                return
            query = urllib.parse.parse_qs(parsed.query)
            after = self._query_int(query, "after", 0, 0, 2**63 - 1)
            if after is None:
                return
            events, latest_seq = self.manager.read_events(job, after)
            self._send_json(
                {
                    "job_id": job.job_id,
                    "seq": latest_seq,
                    "status": job.status,
                    "done": job.status not in ("queued", "running"),
                    "events": events,
                }
            )
            return

        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/login":
            if not self.config.password_hash:
                self._send_json({"error": "password login is not configured"}, status=404)
                return
            raw = self._read_body(MAX_LOGIN_BODY_BYTES)
            if raw is None:
                return
            try:
                payload = json.loads(raw or "{}")
            except json.JSONDecodeError as exc:
                self._send_json({"error": f"invalid JSON: {exc}"}, status=400)
                return
            if not isinstance(payload, dict):
                self._send_json({"error": "JSON body must be an object"}, status=400)
                return
            password = str(payload.get("password") or "")
            if verify_password_hash(password, self.config.password_hash):
                self.audit.write("login", self._remote(), "password")
                self._send_json(
                    {"ok": True, "session_days": self.config.session_days},
                    extra_headers={"Set-Cookie": self._new_session_cookie()},
                )
                return
            self.audit.write("login_failed", self._remote(), "password", ok=False)
            self._send_json({"error": "invalid password"}, status=401)
            return

        if path == "/logout":
            self._send_json(
                {"ok": True},
                extra_headers={
                    "Set-Cookie": f"{SESSION_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"
                },
            )
            return

        if not self._require_auth():
            return

        if path in ("/run", "/claude"):
            raw = self._read_body(MAX_REQUEST_BODY_BYTES)
            if raw is None:
                return
            content_type = self.headers.get("Content-Type", "")
            if "application/json" in content_type:
                try:
                    payload = json.loads(raw or "{}")
                except json.JSONDecodeError as exc:
                    self._send_json({"error": f"invalid JSON: {exc}"}, status=400)
                    return
                if not isinstance(payload, dict):
                    self._send_json({"error": "JSON body must be an object"}, status=400)
                    return
                prompt = payload.get("prompt") or payload.get("text") or payload.get("message") or ""
                timeout = payload.get("timeout")
                model = payload.get("model") or ""
                effort = payload.get("effort") or ""
                permission_mode = payload.get("permission_mode") or payload.get("permissionMode") or ""
                continue_from = payload.get("continue_from") or payload.get("continueFrom") or ""
                new_session_value = payload.get("new_session", payload.get("newSession", False))
                try:
                    new_session = parse_bool(new_session_value, "new_session", default=False)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
            else:
                prompt = raw
                timeout = None
                model = ""
                effort = ""
                permission_mode = ""
                continue_from = ""
                new_session = False

            if not isinstance(prompt, str):
                self._send_json({"error": "prompt must be a string"}, status=400)
                return
            try:
                timeout_int = parse_optional_int(timeout, "timeout")
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return

            ok, data = self.manager.start(
                prompt,
                timeout_int,
                self._remote(),
                str(model),
                str(effort),
                str(permission_mode),
                str(continue_from),
                new_session=bool(new_session),
            )
            if not ok:
                self._send_json(data, status=409 if "current_job" in data else 400)
                return
            self._send_json({"status": "started", **data}, status=202)
            return

        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "kill":
            self._send_json(self.manager.kill_job(parts[1], self._remote()))
            return

        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "close":
            data = self.manager.close_job(parts[1], self._remote())
            self._send_json(data, status=409 if "error" in data else 200)
            return

        if path == "/kill":
            self._send_json(self.manager.kill_current(self._remote()))
            return

        if path == "/close":
            data = self.manager.close_current(self._remote())
            self._send_json(data, status=409 if "error" in data else 200)
            return

        if path in ("/processes/kill", "/taskbar/kill", "/taskbar/close"):
            self._send_json(
                {"error": "Windows program control moved to port 9091"},
                status=404,
            )
            return

        self._send_json({"error": "not found"}, status=404)


class GatewayServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], config: Config):
        self.config = config
        self.audit = AuditLog(config.audit_log)
        self.allowlist_networks = config.allowlist_networks()
        self.manager = JobManager(config, self.audit)
        super().__init__(address, GatewayHandler)


def parse_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HTTP gateway for Claude Code prompts")
    parser.add_argument("--config", help="JSON config file")
    parser.add_argument("--set-password-file", help="Interactively create/update an auth JSON file")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--token")
    parser.add_argument("--auth-file", help="JSON file containing password_hash/session_secret")
    parser.add_argument("--password-hash")
    parser.add_argument("--session-secret")
    parser.add_argument("--session-days", type=int)
    parser.add_argument("--unsafe-no-token", action="store_true")
    parser.add_argument("--ip-allowlist", help="Comma-separated IPs/CIDRs")
    parser.add_argument("--claude-bin")
    parser.add_argument(
        "--claude-args",
        help="Shell-like Claude argument string. Default: -p --output-format text",
    )
    parser.add_argument("--workdir")
    parser.add_argument("--model")
    parser.add_argument("--effort", choices=sorted(item for item in EFFORT_LEVELS if item))
    parser.add_argument("--continue-session", action="store_true")
    parser.add_argument("--permission-mode")
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--state-dir")
    return parser


def load_config(args: argparse.Namespace) -> Config:
    cfg = Config.from_file(args.config) if args.config else Config()
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.token:
        cfg.token = args.token
    if args.auth_file:
        cfg.auth_file = args.auth_file
    if args.password_hash:
        cfg.password_hash = args.password_hash
    if args.session_secret:
        cfg.session_secret = args.session_secret
    if args.session_days:
        cfg.session_days = args.session_days
    if args.unsafe_no_token:
        cfg.unsafe_no_token = True
    if args.ip_allowlist:
        cfg.ip_allowlist = parse_csv(args.ip_allowlist)
    if args.claude_bin:
        cfg.claude_bin = args.claude_bin
    if args.claude_args:
        cfg.claude_args = shlex.split(args.claude_args)
    if args.workdir:
        cfg.working_dir = args.workdir
    if args.model:
        cfg.model = args.model
    if args.effort:
        cfg.effort = args.effort
    if args.continue_session:
        cfg.continue_session = True
    if args.permission_mode:
        cfg.permission_mode = args.permission_mode
    if args.max_turns:
        cfg.max_turns = args.max_turns
    if args.timeout:
        cfg.timeout_seconds = args.timeout
    if args.state_dir:
        cfg.state_dir = args.state_dir
    cfg.finalize()
    return cfg


def main() -> int:
    args = build_arg_parser().parse_args()
    if args.set_password_file:
        write_password_file(args.set_password_file)
        return 0
    config = load_config(args)

    server = GatewayServer((config.host, config.port), config)
    url = f"http://{config.host}:{config.port}/"
    if config.password_hash:
        token_state = "password"
    elif config.token:
        token_state = "token"
    else:
        token_state = "disabled-local-only"
    print(f"{APP_NAME} {VERSION}")
    print(f"URL: {url}")
    print(f"Auth: {token_state}")
    print(f"Workdir: {config.working_dir}")
    print(f"State: {config.state_dir}")
    print(f"Claude command: {display_command(config.claude_command())}")
    print("Press Ctrl+C to stop.")

    def shutdown(signum: int, frame: object) -> None:
        print("\nShutting down...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    try:
        server.serve_forever()
    finally:
        server.manager.shutdown_all()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
