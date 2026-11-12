"""Check configured live stores independently and prepare a bounded Settings report."""

from dataclasses import asdict
from pathlib import Path

from services.json_storage import SCHEMA_VERSION
from services.local_work import check_local_cancelled
from services.profile_service import ProfileService
from services.settings_service import SETTINGS_SCHEMA_VERSION, SettingsService
from services.storage_diagnostics import PROBLEMS, StorageScan
from services.storage_references import Finding


MAX_ATTENTION_FINDINGS = 50


def _scan_for(path: Path) -> StorageScan:
    """Limit each check to the configured store's own parent directory."""
    try:
        root = path.parent.resolve()
    except (OSError, RuntimeError):
        root = path.parent.absolute()
    return StorageScan(root, live=True)


def inspect_live_storage(
    *, settings_path: Path, profiles_path: Path, conversations_path: Path, databases: dict[str, Path],
) -> dict:
    """Validate saved files and individual databases without comparing live store snapshots."""
    settings_scan = _scan_for(settings_path)
    settings = settings_scan.json_registry("settings.json", SettingsService, SETTINGS_SCHEMA_VERSION, path=settings_path)
    app_settings = (settings.migration_origin or settings.current) if settings is not None else None
    check_local_cancelled()

    profiles_scan = _scan_for(profiles_path)
    profiles_scan.json_registry(
        "profiles.json", lambda path: ProfileService(path, app_settings=app_settings), SCHEMA_VERSION, path=profiles_path,
    )
    findings = settings_scan.findings + profiles_scan.findings
    check_local_cancelled()

    transcripts = _scan_for(conversations_path)
    transcripts.conversations(None, path=conversations_path)
    findings.extend(transcripts.findings)

    for name, path in databases.items():
        check_local_cancelled()
        scan = _scan_for(path)
        scan.database(name, path=path)
        findings.extend(scan.findings)

    return storage_report(findings, scope="basic")


def storage_report(findings: list[Finding], *, scope: str) -> dict:
    """Summarize findings for Settings while retaining bounded attention details."""
    problems = [item for item in findings if item.status in PROBLEMS]
    review = [item for item in findings if item.status == "review"]
    deferred = [item for item in findings if item.status == "unavailable"]
    attention = problems + deferred + review
    routine = [item for item in findings if item.status not in PROBLEMS | {"review", "unavailable"}]
    return {
        "scope": scope,
        "complete": not deferred,
        "summary": {"problems": len(problems), "review": len(review), "deferred": len(deferred)},
        "findings": [asdict(item) for item in attention[:MAX_ATTENTION_FINDINGS] + routine],
        "omitted_findings": max(0, len(attention) - MAX_ATTENTION_FINDINGS),
    }
