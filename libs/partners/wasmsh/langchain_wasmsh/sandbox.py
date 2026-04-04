"""Wasmsh sandbox implementation."""

from __future__ import annotations

import base64
import io
import json
import logging
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileData,
    FileDownloadResponse,
    FileOperationError,
    FileUploadResponse,
    ReadResult,
)
from deepagents.backends.sandbox import BaseSandbox
from wasmsh_pyodide_runtime import get_dist_dir, get_node_host_script

DEFAULT_WORKSPACE_DIR = "/workspace"


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _encode_content(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def _decode_content(content: str) -> bytes:
    return base64.b64decode(content.encode("ascii"))


def _to_initial_files(
    files: dict[str, str | bytes] | None,
) -> list[dict[str, str]]:
    if not files:
        return []
    encoded: list[dict[str, str]] = []
    for path, content in files.items():
        if isinstance(content, str):
            payload = content.encode("utf-8")
        else:
            payload = content
        encoded.append({"path": path, "contentBase64": _encode_content(payload)})
    return encoded


def _extract_diagnostic(events: list[dict[str, Any]] | None) -> str | None:
    if not events:
        return None
    for event in events:
        diagnostic = event.get("Diagnostic")
        if isinstance(diagnostic, list) and len(diagnostic) >= 2:
            return str(diagnostic[1])
    return None


def _map_error(message: str | None) -> FileOperationError:
    normalized = (message or "").lower()
    if "not found" in normalized:
        return "file_not_found"
    if "directory" in normalized:
        return "is_directory"
    if "permission" in normalized:
        return "permission_denied"
    return "invalid_path"


class WasmshSandbox(BaseSandbox):
    """Local Node-backed wasmsh sandbox conforming to SandboxBackendProtocol."""

    def __init__(
        self,
        *,
        node_executable: str = "node",
        dist_dir: str | Path | None = None,
        working_directory: str = DEFAULT_WORKSPACE_DIR,
        step_budget: int = 0,
        initial_files: dict[str, str | bytes] | None = None,
    ) -> None:
        if shutil.which(node_executable) is None:
            msg = f"Node executable not found: {node_executable}"
            raise FileNotFoundError(msg)

        self._node_executable = node_executable
        self._dist_dir = Path(dist_dir) if dist_dir is not None else get_dist_dir()
        self._working_directory = working_directory
        self._id = f"wasmsh-python-{uuid4()}"
        self._lock = threading.Lock()
        self._process = subprocess.Popen(
            [
                self._node_executable,
                str(get_node_host_script()),
                "--asset-dir",
                str(self._dist_dir),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._next_request_id = 0
        self._stderr_buffer = io.StringIO()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True
        )
        self._stderr_thread.start()
        self._request(
            "init",
            {
                "stepBudget": step_budget,
                "initialFiles": _to_initial_files(initial_files),
            },
        )

    @property
    def id(self) -> str:
        """Return the sandbox identifier."""
        return self._id

    def _drain_stderr(self) -> None:
        """Continuously drain stderr to prevent pipe buffer deadlock."""
        assert self._process.stderr is not None
        for line in self._process.stderr:
            self._stderr_buffer.write(line)

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._process.stdin or not self._process.stdout:
            msg = "wasmsh node host is not available"
            raise RuntimeError(msg)

        with self._lock:
            self._next_request_id += 1
            request_id = self._next_request_id
            payload = {"id": request_id, "method": method, "params": params}
            self._process.stdin.write(json.dumps(payload) + "\n")
            self._process.stdin.flush()
            while True:
                response_line = self._process.stdout.readline()
                if not response_line:
                    break
                try:
                    response = json.loads(response_line)
                    break
                except json.JSONDecodeError:
                    continue  # skip non-JSON output from node host
            else:
                response = None

        if not response_line or response is None:
            stderr = self._stderr_buffer.getvalue().strip()
            msg = stderr or "wasmsh node host terminated unexpectedly"
            raise RuntimeError(msg)

        if not response.get("ok"):
            raise RuntimeError(str(response.get("error", "unknown wasmsh host error")))
        return response["result"]

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Execute a shell command inside the sandbox."""
        del timeout
        result = self._request(
            "run",
            {
                "command": f"cd {_shell_quote(self._working_directory)} && {command}",
            },
        )
        return ExecuteResponse(
            output=str(result["output"]),
            exit_code=result.get("exitCode"),
            truncated=False,
        )

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:  # type: ignore[override]
        """Read file content via download_files.

        Overrides BaseSandbox which runs a Python script via execute() —
        that approach fails under wasmsh's Pyodide runtime with I/O errors.
        Returns a plain string for compatibility with langchain-tests v1
        standard suite which uses ``"Error:" not in result``.
        """
        responses = self.download_files([file_path])
        if responses[0].error or responses[0].content is None:
            return f"Error: File '{file_path}' not found"
        content = responses[0].content.decode("utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        page = lines[offset : offset + limit]
        return "".join(f"{i + offset + 1:6d}\t{line}" for i, line in enumerate(page))

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """Edit a file via download + string replace + upload.

        Overrides BaseSandbox which runs a Python script via execute() —
        that approach fails under wasmsh's Pyodide runtime with I/O errors.
        Uses download_files/upload_files directly instead.
        """
        responses = self.download_files([file_path])
        if responses[0].error or responses[0].content is None:
            return EditResult(error=f"Error: File '{file_path}' not found")

        text = responses[0].content.decode("utf-8", errors="replace")

        if not old_string:
            if text:
                return EditResult(error="oldString must not be empty unless file is empty")
            if not new_string:
                return EditResult(path=file_path, occurrences=0)
            data = new_string.encode("utf-8")
            upload = self.upload_files([(file_path, data)])
            if upload[0].error:
                return EditResult(error=f"Failed to write '{file_path}': {upload[0].error}")
            return EditResult(path=file_path, occurrences=1)

        idx = text.find(old_string)
        if idx == -1:
            return EditResult(error=f"String not found in file '{file_path}'")

        if old_string == new_string:
            return EditResult(path=file_path, occurrences=1)

        if replace_all:
            count = text.count(old_string)
            new_text = text.replace(old_string, new_string)
        else:
            second = text.find(old_string, idx + len(old_string))
            if second != -1:
                return EditResult(
                    error=f"Multiple occurrences found in '{file_path}'. "
                    "Use replace_all=True to replace all.",
                )
            count = 1
            new_text = text[:idx] + new_string + text[idx + len(old_string) :]

        data = new_text.encode("utf-8")
        upload = self.upload_files([(file_path, data)])
        if upload[0].error:
            return EditResult(error=f"Failed to write '{file_path}': {upload[0].error}")
        return EditResult(path=file_path, occurrences=count)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download files from the sandbox.

        Checks for directories and unreadable files before attempting
        download, since Emscripten's VFS does not enforce permissions
        and reads directories as empty bytes.
        """
        responses: list[FileDownloadResponse] = []
        for path in paths:
            if not path.startswith("/"):
                responses.append(
                    FileDownloadResponse(path=path, content=None, error="invalid_path")
                )
                continue

            # Pre-check: detect directories since Emscripten's VFS reads
            # them as empty bytes instead of returning an error.
            try:
                check = self.execute(
                    f"test -d {_shell_quote(path)} && echo DIR || true"
                )
                if check.output.strip() == "DIR":
                    responses.append(
                        FileDownloadResponse(
                            path=path, content=None, error="is_directory"
                        )
                    )
                    continue
            except (RuntimeError, KeyError):
                pass  # pre-check failed, proceed with download

            try:
                result = self._request("readFile", {"path": path})
            except RuntimeError as exc:
                responses.append(
                    FileDownloadResponse(
                        path=path,
                        content=None,
                        error=_map_error(str(exc)),
                    )
                )
                continue
            diagnostic = _extract_diagnostic(result.get("events"))
            if diagnostic:
                responses.append(
                    FileDownloadResponse(
                        path=path,
                        content=None,
                        error=_map_error(diagnostic),
                    )
                )
                continue
            responses.append(
                FileDownloadResponse(
                    path=path,
                    content=_decode_content(str(result["contentBase64"])),
                    error=None,
                )
            )
        return responses

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload files into the sandbox."""
        responses: list[FileUploadResponse] = []
        for path, content in files:
            if not path.startswith("/"):
                responses.append(FileUploadResponse(path=path, error="invalid_path"))
                continue
            try:
                result = self._request(
                    "writeFile",
                    {
                        "path": path,
                        "contentBase64": _encode_content(content),
                    },
                )
            except RuntimeError as exc:
                responses.append(
                    FileUploadResponse(path=path, error=_map_error(str(exc)))
                )
                continue
            diagnostic = _extract_diagnostic(result.get("events"))
            responses.append(
                FileUploadResponse(
                    path=path,
                    error=_map_error(diagnostic) if diagnostic else None,
                )
            )
        return responses

    def close(self) -> None:
        """Stop the local node host."""
        if self._process.poll() is not None:
            return
        try:
            self._request("close", {})
        except RuntimeError:
            logger.debug(
                "close request to node host failed (process will be terminated)",
                exc_info=True,
            )
        finally:
            if self._process.stdin:
                self._process.stdin.close()
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)

    def stop(self) -> None:
        """Alias for `close()`."""
        self.close()
