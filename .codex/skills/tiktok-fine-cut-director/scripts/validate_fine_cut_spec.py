#!/usr/bin/env python3
"""Deterministically validate the core contract of a fine-cut specification.

This intentionally uses only the Python standard library.  It reads the skill's
JSON Schema for required fields and enum values, but it is not a general JSON
Schema implementation.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SCHEMA = Path(__file__).resolve().parent.parent / "assets" / "fine-cut-spec.schema.json"
SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")


@dataclass(frozen=True)
class ValidationError:
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class Validator:
    def __init__(self, schema: dict[str, Any]) -> None:
        self.schema = schema
        self.errors: list[ValidationError] = []

    def add(self, code: str, path: str, message: str) -> None:
        self.errors.append(ValidationError(code=code, path=path, message=message))

    def require_object(self, value: Any, path: str) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            self.add("type", path, "must be an object")
            return None
        return value

    def check_object_contract(
        self,
        value: dict[str, Any],
        schema: dict[str, Any],
        path: str,
        *,
        unknown_keys: bool = True,
    ) -> None:
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        for key in required:
            if key not in value:
                self.add("required", f"{path}.{key}", "required field is missing")
        if unknown_keys:
            for key in sorted(set(value) - set(properties)):
                self.add("unknown_key", f"{path}.{key}", "field is not allowed by the schema")

    def check_nonempty_string(self, value: Any, path: str) -> None:
        if not isinstance(value, str):
            self.add("type", path, "must be a string")
        elif not value.strip():
            self.add("empty", path, "must not be empty")

    def check_string_array(self, value: Any, path: str, *, min_items: int = 0) -> list[str]:
        if not isinstance(value, list):
            self.add("type", path, "must be an array")
            return []
        if len(value) < min_items:
            self.add("min_items", path, f"must contain at least {min_items} item(s)")
        checked: list[str] = []
        for index, item in enumerate(value):
            self.check_nonempty_string(item, f"{path}[{index}]")
            if isinstance(item, str) and item.strip():
                checked.append(item)
        return checked

    def check_enum(self, value: Any, allowed: list[Any], path: str) -> None:
        if value not in allowed:
            rendered = ", ".join(repr(item) for item in allowed)
            self.add("enum", path, f"must be one of: {rendered}")

    def check_number(self, value: Any, path: str, *, minimum: float = 0.0) -> float | None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            self.add("type", path, "must be a finite number")
            return None
        number = float(value)
        if not math.isfinite(number):
            self.add("finite", path, "must be a finite number")
            return None
        if number < minimum:
            self.add("minimum", path, f"must be at least {minimum:g}")
            return None
        return number

    def validate_audience(self, value: Any) -> None:
        path = "$.audience"
        audience = self.require_object(value, path)
        if audience is None:
            return
        contract = self.schema["properties"]["audience"]
        self.check_object_contract(audience, contract, path)
        for key in ("market", "viewer_need"):
            if key in audience:
                self.check_nonempty_string(audience[key], f"{path}.{key}")
        if "locale" in audience:
            allowed = contract["properties"]["locale"]["enum"]
            self.check_enum(audience["locale"], allowed, f"{path}.locale")

    def validate_typography(self, value: Any) -> None:
        path = "$.typography"
        typography = self.require_object(value, path)
        if typography is None:
            return
        contract = self.schema["properties"]["typography"]
        self.check_object_contract(typography, contract, path)
        for key in ("body_font", "hook_font", "accent_font", "safe_zone_policy"):
            if key in typography:
                self.check_nonempty_string(typography[key], f"{path}.{key}")
        if "caption_mode" in typography:
            allowed = contract["properties"]["caption_mode"]["enum"]
            self.check_enum(typography["caption_mode"], allowed, f"{path}.caption_mode")
        if "palette" in typography:
            palette = typography["palette"]
            if not isinstance(palette, list):
                self.add("type", f"{path}.palette", "must be an array")
            else:
                if not 2 <= len(palette) <= 6:
                    self.add("length", f"{path}.palette", "must contain 2 to 6 colors")
                for index, color in enumerate(palette):
                    self.check_nonempty_string(color, f"{path}.palette[{index}]")

    def validate_audio(self, value: Any) -> str | None:
        path = "$.audio"
        audio = self.require_object(value, path)
        if audio is None:
            return None
        contract = self.schema["properties"]["audio"]
        self.check_object_contract(audio, contract, path)
        for key in ("voiceover", "music_policy"):
            if key in audio:
                allowed = contract["properties"][key]["enum"]
                self.check_enum(audio[key], allowed, f"{path}.{key}")
        if "sfx_policy" in audio:
            self.check_nonempty_string(audio["sfx_policy"], f"{path}.sfx_policy")
        if "mix_notes" in audio:
            self.check_nonempty_string(audio["mix_notes"], f"{path}.mix_notes")
        music_asset_id = audio.get("music_asset_id")
        valid_music_asset_id: str | None = None
        if music_asset_id is not None:
            self.check_nonempty_string(music_asset_id, f"{path}.music_asset_id")
            if isinstance(music_asset_id, str) and music_asset_id.strip():
                valid_music_asset_id = music_asset_id
        policy = audio.get("music_policy")
        if policy in {"verified-local", "platform-cleared"} and not music_asset_id:
            self.add(
                "music_rights",
                f"{path}.music_asset_id",
                "verified or platform-cleared music requires a non-empty music_asset_id",
            )
        if policy in {"voiceover-only", "silent"} and music_asset_id is not None:
            self.add(
                "music_policy",
                f"{path}.music_asset_id",
                "voiceover-only or silent policy requires music_asset_id to be null",
            )
        return valid_music_asset_id

    def validate_claims(self, value: Any) -> dict[str, str]:
        path = "$.claims"
        claims: dict[str, str] = {}
        if not isinstance(value, list):
            self.add("type", path, "must be an array")
            return claims
        if not value:
            self.add("min_items", path, "must contain at least one claim")
            return claims
        contract = self.schema["$defs"]["claim"]
        for index, raw_claim in enumerate(value):
            claim_path = f"{path}[{index}]"
            claim = self.require_object(raw_claim, claim_path)
            if claim is None:
                continue
            self.check_object_contract(claim, contract, claim_path)
            for key in ("claim_id", "text"):
                if key in claim:
                    self.check_nonempty_string(claim[key], f"{claim_path}.{key}")
            status = claim.get("status")
            if "status" in claim:
                self.check_enum(
                    status,
                    contract["properties"]["status"]["enum"],
                    f"{claim_path}.status",
                )
            if "evidence" in claim:
                self.check_string_array(claim["evidence"], f"{claim_path}.evidence", min_items=1)
            claim_id = claim.get("claim_id")
            if isinstance(claim_id, str) and claim_id.strip():
                if claim_id in claims:
                    self.add("duplicate", f"{claim_path}.claim_id", "claim_id must be unique")
                claims[claim_id] = str(status)
        return claims

    def validate_retention_events(
        self,
        value: Any,
        duration: float | None,
        claims: dict[str, str],
    ) -> set[str]:
        path = "$.retention_events"
        source_asset_ids: set[str] = set()
        if not isinstance(value, list):
            self.add("type", path, "must be an array")
            return source_asset_ids
        if not value:
            self.add("min_items", path, "must contain at least one retention event")
            return source_asset_ids

        contract = self.schema["$defs"]["retentionEvent"]
        previous_start: float | None = None
        for index, raw_event in enumerate(value):
            event_path = f"{path}[{index}]"
            event = self.require_object(raw_event, event_path)
            if event is None:
                continue
            self.check_object_contract(event, contract, event_path)
            for key in (
                "spoken_idea",
                "evidence_source",
                "shot_or_overlay",
                "text_treatment",
                "motion",
                "exit_condition",
            ):
                if key in event:
                    self.check_nonempty_string(event[key], f"{event_path}.{key}")
            if "text" in event and event["text"] is not None:
                self.check_nonempty_string(event["text"], f"{event_path}.text")
            if "visual_job" in event:
                allowed = contract["properties"]["visual_job"]["enum"]
                self.check_enum(event["visual_job"], allowed, f"{event_path}.visual_job")
            if "confidence" in event:
                allowed = contract["properties"]["confidence"]["enum"]
                self.check_enum(event["confidence"], allowed, f"{event_path}.confidence")
            start = (
                self.check_number(event["start"], f"{event_path}.start")
                if "start" in event
                else None
            )
            end = self.check_number(event["end"], f"{event_path}.end") if "end" in event else None
            if start is not None and previous_start is not None and start < previous_start:
                self.add("time_order", f"{event_path}.start", "events must be sorted by start time")
            if start is not None:
                previous_start = start
            if start is not None and end is not None and not start < end:
                self.add("time_range", event_path, "start must be less than end")
            if duration is not None and end is not None and end > duration:
                self.add("duration", f"{event_path}.end", "must not exceed target_duration_seconds")
            source_asset_id = event.get("source_asset_id")
            if source_asset_id is not None:
                self.check_nonempty_string(source_asset_id, f"{event_path}.source_asset_id")
                if isinstance(source_asset_id, str) and source_asset_id.strip():
                    source_asset_ids.add(source_asset_id)
            source_in = event.get("source_in")
            source_out = event.get("source_out")
            if (source_in is None) != (source_out is None):
                self.add(
                    "source_range",
                    event_path,
                    "source_in and source_out must either both be numbers or both be null",
                )
            elif source_in is not None and source_out is not None:
                checked_in = self.check_number(source_in, f"{event_path}.source_in")
                checked_out = self.check_number(source_out, f"{event_path}.source_out")
                if (
                    checked_in is not None
                    and checked_out is not None
                    and not checked_in < checked_out
                ):
                    self.add("source_range", event_path, "source_in must be less than source_out")
            if source_asset_id is None and (source_in is not None or source_out is not None):
                self.add(
                    "source_range",
                    f"{event_path}.source_asset_id",
                    "source timecodes require a source_asset_id",
                )
            claim_ids = self.check_string_array(event.get("claim_ids"), f"{event_path}.claim_ids")
            if len(claim_ids) != len(set(claim_ids)):
                self.add("duplicate", f"{event_path}.claim_ids", "claim_ids must be unique")
            for claim_id in claim_ids:
                if claim_id not in claims:
                    self.add(
                        "claim_missing",
                        f"{event_path}.claim_ids",
                        f"unknown claim_id {claim_id!r}",
                    )
                elif claims[claim_id] == "forbidden":
                    self.add(
                        "forbidden_claim",
                        f"{event_path}.claim_ids",
                        f"forbidden claim {claim_id!r} cannot be used in an event",
                    )
                elif claims[claim_id] == "conditional":
                    self.add(
                        "conditional_claim",
                        f"{event_path}.claim_ids",
                        f"conditional claim {claim_id!r} must be verified before use",
                    )
        return source_asset_ids

    def validate_evidence_inserts(self, value: Any, duration: float | None) -> set[str]:
        path = "$.evidence_inserts"
        asset_ids: set[str] = set()
        if not isinstance(value, list):
            self.add("type", path, "must be an array")
            return asset_ids

        contract = self.schema["$defs"]["evidenceInsert"]
        for index, raw_insert in enumerate(value):
            insert_path = f"{path}[{index}]"
            insert = self.require_object(raw_insert, insert_path)
            if insert is None:
                continue
            self.check_object_contract(insert, contract, insert_path)
            for key in ("asset_id", "semantic_trigger", "provenance"):
                if key in insert:
                    self.check_nonempty_string(insert[key], f"{insert_path}.{key}")
            if "claim_boundary" in insert:
                self.check_nonempty_string(
                    insert["claim_boundary"], f"{insert_path}.claim_boundary"
                )
            if "illustrative" in insert and not isinstance(insert["illustrative"], bool):
                self.add("type", f"{insert_path}.illustrative", "must be a boolean")
            asset_id = insert.get("asset_id")
            if isinstance(asset_id, str) and asset_id.strip():
                asset_ids.add(asset_id)
            if "kind" in insert:
                allowed = contract["properties"]["kind"]["enum"]
                self.check_enum(insert["kind"], allowed, f"{insert_path}.kind")
            enter = (
                self.check_number(insert["enter_at"], f"{insert_path}.enter_at")
                if "enter_at" in insert
                else None
            )
            exit_at = (
                self.check_number(insert["exit_at"], f"{insert_path}.exit_at")
                if "exit_at" in insert
                else None
            )
            if enter is not None and exit_at is not None and not enter < exit_at:
                self.add("time_range", insert_path, "enter_at must be less than exit_at")
            if duration is not None and exit_at is not None and exit_at > duration:
                self.add(
                    "duration",
                    f"{insert_path}.exit_at",
                    "must not exceed target_duration_seconds",
                )
        return asset_ids

    def validate_rights(self, value: Any) -> dict[str, bool]:
        path = "$.rights"
        asset_rights: dict[str, bool] = {}
        if not isinstance(value, list):
            self.add("type", path, "must be an array")
            return asset_rights
        if not value:
            self.add("min_items", path, "must contain at least one rights entry")
            return asset_rights

        contract = self.schema["properties"]["rights"]["items"]
        for index, raw_entry in enumerate(value):
            entry_path = f"{path}[{index}]"
            entry = self.require_object(raw_entry, entry_path)
            if entry is None:
                continue
            self.check_object_contract(entry, contract, entry_path)
            for key in ("asset_id", "source", "license_or_authorization", "usage_scope"):
                if key in entry:
                    self.check_nonempty_string(entry[key], f"{entry_path}.{key}")
            asset_id = entry.get("asset_id")
            if isinstance(asset_id, str) and asset_id.strip():
                if asset_id in asset_rights:
                    self.add(
                        "duplicate",
                        f"{entry_path}.asset_id",
                        "asset_id must be unique in rights",
                    )
                asset_rights[asset_id] = bool(entry.get("reference_only"))
            if "reference_only" in entry and not isinstance(entry["reference_only"], bool):
                self.add("type", f"{entry_path}.reference_only", "must be a boolean")
            if "release_status" in entry:
                allowed = contract["properties"]["release_status"]["enum"]
                self.check_enum(entry["release_status"], allowed, f"{entry_path}.release_status")
            if "markets" in entry:
                markets = self.check_string_array(
                    entry["markets"], f"{entry_path}.markets", min_items=1
                )
                if len(markets) != len(set(markets)):
                    self.add("duplicate", f"{entry_path}.markets", "markets must be unique")
            if "expires_on" in entry and entry["expires_on"] is not None:
                self.check_nonempty_string(entry["expires_on"], f"{entry_path}.expires_on")
            if "sha256" in entry:
                digest = entry["sha256"]
                if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                    self.add(
                        "sha256",
                        f"{entry_path}.sha256",
                        "must be exactly 64 hexadecimal characters",
                    )
        return asset_rights

    def validate_verification(self, value: Any) -> None:
        path = "$.verification"
        verification = self.require_object(value, path)
        if verification is None:
            return
        contract = self.schema["properties"]["verification"]
        self.check_object_contract(verification, contract, path)
        for key in ("planned_checks", "not_proven"):
            if key in verification:
                self.check_string_array(verification[key], f"{path}.{key}", min_items=1)

    def validate(self, spec: Any) -> list[ValidationError]:
        root = self.require_object(spec, "$")
        if root is None:
            return self.errors
        self.check_object_contract(root, self.schema, "$")

        properties = self.schema["properties"]
        if "schema_version" in root:
            expected = properties["schema_version"]["const"]
            if root["schema_version"] != expected:
                self.add("const", "$.schema_version", f"must equal {expected!r}")
        for key in ("job_id", "creative_thesis"):
            if key in root:
                self.check_nonempty_string(root[key], f"$.{key}")
        if "audience" in root:
            self.validate_audience(root["audience"])
        if "regime" in root:
            self.check_enum(root["regime"], properties["regime"]["enum"], "$.regime")
        if "route" in root:
            self.check_enum(root["route"], properties["route"]["enum"], "$.route")
        if "typography" in root:
            self.validate_typography(root["typography"])

        duration = None
        if "target_duration_seconds" in root:
            duration = self.check_number(
                root["target_duration_seconds"], "$.target_duration_seconds"
            )
            if duration == 0:
                self.add("exclusive_minimum", "$.target_duration_seconds", "must be greater than 0")
                duration = None

        claims = self.validate_claims(root["claims"]) if "claims" in root else {}
        event_source_ids = (
            self.validate_retention_events(root["retention_events"], duration, claims)
            if "retention_events" in root
            else set()
        )
        evidence_ids = self.validate_evidence_inserts(root.get("evidence_inserts", []), duration)
        music_asset_id = self.validate_audio(root["audio"]) if "audio" in root else None
        asset_rights = self.validate_rights(root["rights"]) if "rights" in root else {}
        if "verification" in root:
            self.validate_verification(root["verification"])

        referenced_ids = set(evidence_ids) | set(event_source_ids)
        if music_asset_id:
            referenced_ids.add(music_asset_id)
        for asset_id in sorted(referenced_ids - set(asset_rights)):
            self.add(
                "rights_missing",
                "$.rights",
                f"referenced asset {asset_id!r} has no rights entry",
            )
        for asset_id in sorted(referenced_ids & set(asset_rights)):
            if asset_rights[asset_id]:
                self.add(
                    "reference_only",
                    "$.rights",
                    f"referenced asset {asset_id!r} is marked reference_only and "
                    "cannot enter the render",
                )
        return self.errors


def _load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{label} does not exist: {path}") from exc
    except OSError as exc:
        raise ValueError(f"could not read {label}: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{label} is not valid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc


def _check_schema_shape(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise ValueError("schema root must be an object")
    if not isinstance(schema.get("properties"), dict):
        raise ValueError("schema must define object properties")
    if not isinstance(schema.get("required"), list):
        raise ValueError("schema must define a required field list")
    definitions = schema.get("$defs")
    required_definitions = {"claim", "retentionEvent", "evidenceInsert"}
    if not isinstance(definitions, dict) or not required_definitions <= set(definitions):
        raise ValueError("schema must define claim, retentionEvent, and evidenceInsert")
    return schema


def validate_spec(spec: Any, schema: dict[str, Any]) -> list[ValidationError]:
    """Return all deterministic core-contract errors in encounter order."""

    return Validator(schema).validate(spec)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a TikTok fine-cut specification without modifying it."
    )
    parser.add_argument("spec", type=Path, help="Path to fine-cut-spec JSON")
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA,
        help=f"Schema contract path (default: {DEFAULT_SCHEMA})",
    )
    return parser


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spec_path = args.spec.resolve()
    schema_path = args.schema.resolve()
    try:
        schema = _check_schema_shape(_load_json(schema_path, "schema"))
        spec = _load_json(spec_path, "spec")
    except ValueError as exc:
        _emit(
            {
                "valid": False,
                "error_count": 1,
                "errors": [{"code": "input_error", "path": "$", "message": str(exc)}],
                "schema_path": str(schema_path),
                "spec_path": str(spec_path),
            }
        )
        return 2

    errors = validate_spec(spec, schema)
    event_count = len(spec.get("retention_events", [])) if isinstance(spec, dict) else 0
    insert_count = len(spec.get("evidence_inserts", [])) if isinstance(spec, dict) else 0
    rights_count = len(spec.get("rights", [])) if isinstance(spec, dict) else 0
    _emit(
        {
            "valid": not errors,
            "error_count": len(errors),
            "errors": [error.as_dict() for error in errors],
            "schema_path": str(schema_path),
            "spec_path": str(spec_path),
            "summary": {
                "evidence_insert_count": insert_count,
                "retention_event_count": event_count,
                "rights_entry_count": rights_count,
            },
        }
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
