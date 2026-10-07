from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_toolchain() -> dict:
    path = workspace_root() / "config" / "toolchain.json"
    return json.loads(path.read_text(encoding="utf-8"))


def tool_path(key: str) -> Path:
    manifest = load_toolchain()
    path = workspace_root() / manifest["ffmpeg"][key]
    if not path.is_file():
        raise FileNotFoundError(f"Workspace tool is missing: {path}")
    return path


def ffmpeg_path() -> Path:
    return tool_path("ffmpegPath")


def ffprobe_path() -> Path:
    return tool_path("ffprobePath")


def validate_job_id(job_id: str) -> str:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError(
            "Job id must start with an ASCII letter or digit and contain only "
            "letters, digits, underscores, or hyphens (maximum 64 characters)."
        )
    return job_id


@dataclass(frozen=True)
class JobPaths:
    root: Path
    inputs: Path
    work: Path
    output: Path
    reports: Path
    brief: Path
    manifest: Path
    analysis: Path
    plan: Path
    qa: Path

    @classmethod
    def for_id(cls, job_id: str) -> JobPaths:
        validate_job_id(job_id)
        root = workspace_root() / "jobs" / job_id
        return cls(
            root=root,
            inputs=root / "inputs",
            work=root / "work",
            output=root / "output",
            reports=root / "reports",
            brief=root / "brief.txt",
            manifest=root / "manifest.json",
            analysis=root / "reports" / "analysis.json",
            plan=root / "edit-plan.json",
            qa=root / "reports" / "qa.json",
        )

    def ensure(self) -> None:
        for path in (self.root, self.inputs, self.work, self.output, self.reports):
            path.mkdir(parents=True, exist_ok=True)
