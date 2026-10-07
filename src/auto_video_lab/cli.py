from __future__ import annotations

import argparse
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from .analysis import analyze_job
from .media import media_kind
from .paths import JobPaths, validate_job_id, workspace_root
from .plan import draft_plan, validate_plan
from .qa import qa_job
from .render import render_job
from .util import read_json, safe_resolve_within, sha256_file, write_json
from .voiceover import assess_voiceover_spec, synthesize_voiceover, validate_voiceover_spec


def _emit(value: dict) -> None:
    import json

    # ASCII-escaped JSON remains readable when Windows shells disagree about
    # their active code page. Job files themselves stay UTF-8 and retain text.
    print(json.dumps(value, ensure_ascii=True, indent=2))


def create_job(job_id: str, assets: list[str], brief: str) -> dict:
    validate_job_id(job_id)
    job = JobPaths.for_id(job_id)
    if job.root.exists():
        raise FileExistsError(
            f"Job already exists: {job.root}. Choose a new id; existing jobs are not overwritten."
        )
    source_paths = [Path(item).expanduser().resolve() for item in assets]
    if not source_paths:
        raise ValueError("At least one asset is required")
    for path in source_paths:
        if not path.is_file():
            raise FileNotFoundError(f"Asset does not exist: {path}")
        if media_kind(path) == "unknown":
            raise ValueError(f"Unsupported asset type: {path}")

    job.ensure()
    manifest_assets = []
    for index, source in enumerate(source_paths, start=1):
        filename = f"{index:02d}_{source.name}"
        destination = job.inputs / filename
        shutil.copy2(source, destination)
        manifest_assets.append(
            {
                "asset_id": f"asset-{index:03d}",
                "original_path": str(source),
                "job_path": destination.relative_to(job.root).as_posix(),
                "kind": media_kind(destination),
                "bytes": destination.stat().st_size,
                "sha256": sha256_file(destination),
            }
        )
    job.brief.write_text(brief.strip() + "\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "job_id": job_id,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "brief_path": "brief.txt",
        "assets": manifest_assets,
    }
    write_json(job.manifest, manifest)
    return {
        "status": "JOB_CREATED",
        "job_id": job_id,
        "job_root": str(job.root),
        "brief": brief.strip(),
        "assets": manifest_assets,
        "analysis_run": False,
        "render_run": False,
    }


def job_status(job_id: str) -> dict:
    job = JobPaths.for_id(job_id)
    return {
        "job_id": job_id,
        "job_root": str(job.root),
        "exists": job.root.is_dir(),
        "manifest": job.manifest.is_file(),
        "analysis": job.analysis.is_file(),
        "plan": job.plan.is_file(),
        "render_report": (job.reports / "render.json").is_file(),
        "qa": job.qa.is_file(),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto-video",
        description="Local auditable analysis, plan rendering, and technical QA.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create an isolated job and copy assets")
    create.add_argument("--job", required=True)
    create.add_argument("--asset", action="append", required=True)
    brief_group = create.add_mutually_exclusive_group(required=True)
    brief_group.add_argument("--brief")
    brief_group.add_argument("--brief-file", type=Path)

    analyze = subparsers.add_parser("analyze", help="Probe assets and collect editing evidence")
    analyze.add_argument("--job", required=True)
    analyze.add_argument("--transcribe", action="store_true")
    analyze.add_argument("--language", choices=["en", "es"])

    draft = subparsers.add_parser("draft-plan", help="Draft a conservative all-assets timeline")
    draft.add_argument("--job", required=True)
    draft.add_argument("--title")
    draft.add_argument("--burn-captions", action="store_true")

    validate = subparsers.add_parser("validate", help="Validate a job edit plan")
    validate.add_argument("--job", required=True)
    validate.add_argument("--plan", type=Path)

    render = subparsers.add_parser("render", help="Render a validated edit plan")
    render.add_argument("--job", required=True)
    render.add_argument("--plan", type=Path)

    qa = subparsers.add_parser("qa", help="Run local technical QA on the rendered output")
    qa.add_argument("--job", required=True)

    auto = subparsers.add_parser(
        "baseline-auto",
        help="Analyze, preserve all visual assets in order, render, and run technical QA",
    )
    auto.add_argument("--job", required=True)
    auto.add_argument("--transcribe", action="store_true")
    auto.add_argument("--language", choices=["en", "es"])
    auto.add_argument("--title")
    auto.add_argument("--burn-captions", action="store_true")

    status = subparsers.add_parser("status", help="Show job artifact status")
    status.add_argument("--job", required=True)

    voice_check = subparsers.add_parser(
        "voiceover-check",
        help="Audit audience, selling structure, claim evidence, and delivery direction",
    )
    voice_check.add_argument("--job", required=True)
    voice_check.add_argument("--spec", type=Path, required=True)

    voice_render = subparsers.add_parser(
        "voiceover-synthesize",
        help="Synthesize a validated multi-emotion voiceover and timed subtitles",
    )
    voice_render.add_argument("--job", required=True)
    voice_render.add_argument("--spec", type=Path, required=True)

    subparsers.add_parser("workspace", help="Show the resolved independent workspace")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            if args.brief_file:
                brief = args.brief_file.read_text(encoding="utf-8")
            else:
                brief = args.brief
            result = create_job(args.job, args.asset, brief)
        elif args.command == "analyze":
            result = analyze_job(
                args.job,
                transcribe=args.transcribe,
                language=args.language,
            )
        elif args.command == "draft-plan":
            result = draft_plan(
                args.job,
                title=args.title,
                burn_captions=args.burn_captions,
            )
        elif args.command == "validate":
            job = JobPaths.for_id(args.job)
            result = validate_plan(args.job, read_json(args.plan or job.plan))
        elif args.command == "render":
            result = render_job(args.job, plan_path=args.plan)
        elif args.command == "qa":
            result = qa_job(args.job)
        elif args.command == "baseline-auto":
            analyze_job(
                args.job,
                transcribe=args.transcribe,
                language=args.language,
            )
            draft_plan(
                args.job,
                title=args.title,
                burn_captions=args.burn_captions,
            )
            render_job(args.job)
            result = qa_job(args.job)
        elif args.command == "status":
            result = job_status(args.job)
        elif args.command == "voiceover-check":
            job = JobPaths.for_id(args.job)
            selected_spec = safe_resolve_within(args.spec, job.root)
            spec = validate_voiceover_spec(args.job, read_json(selected_spec))
            result = assess_voiceover_spec(spec)
        elif args.command == "voiceover-synthesize":
            result = synthesize_voiceover(args.job, args.spec)
        else:
            result = {"workspace": str(workspace_root())}
        _emit(result)
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary must return a clean error
        _emit(
            {
                "status": "ERROR",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
