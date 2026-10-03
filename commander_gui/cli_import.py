"""Import existing installation metadata into the CLI's update contract."""

from __future__ import annotations

import json

from .atomic import write_text
from .modlist import modlist_path_for
from .updates import local_modpack_records


def prepare_update_snapshot(profile) -> str | None:
    """Create a missing CLI snapshot without overwriting existing metadata.

    Unknown installed hashes stay null: the CLI must refresh these addons,
    never assume a downloaded catalogue describes the installed version.
    Returns a log message only when an import was performed.
    """
    if profile is None:
        raise ValueError("Select a Commander profile before updating.")
    modlist = modlist_path_for(profile.gamma, profile.mo2_profile)
    if modlist is None or not modlist.parent.is_dir():
        raise ValueError("Select an existing MO2 profile in Profiles before updating.")
    snapshot = modlist.with_name("modpack_maker_list.json")
    if snapshot.exists():
        try:
            entries = json.loads(snapshot.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Cannot read the installed-addon snapshot: {snapshot}: {exc}") from exc
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"The installed-addon snapshot is empty or invalid: {snapshot}")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("dlLink"), str) or not entry["dlLink"].strip():
                raise ValueError(f"Invalid addon entry in {snapshot}")
            for key in ("patch", "addonName", "modDbUrl", "zipName", "md5ModDb"):
                if entry.get(key) is not None and not isinstance(entry[key], str):
                    raise ValueError(f"Invalid addon {key} in {snapshot}")
            if entry.get("commanderChecksumKnown") is not None and not isinstance(entry["commanderChecksumKnown"], bool):
                raise ValueError(f"Invalid addon checksum status in {snapshot}")
            instructions = entry.get("instructions")
            if instructions is not None and (
                not isinstance(instructions, list) or any(not isinstance(item, str) for item in instructions)
            ):
                raise ValueError(f"Invalid addon instructions in {snapshot}")
        return None

    records = local_modpack_records(profile.gamma, profile.mo2_profile)
    if not records:
        raise ValueError(
            "Cannot import installed addons. Check the GAMMA folder and MO2 profile; "
            "the installation needs a local modpack catalogue and matching installed mod folders."
        )
    entries = []
    for record in records.values():
        if not record.dl_link:
            continue
        patch = record.patch.removeprefix("- ")
        entries.append({
            "dlLink": record.dl_link,
            "instructions": record.instructions.split(":") if record.instructions not in ("", "0") else [],
            "patch": patch,
            "addonName": record.addon_name,
            "modDbUrl": record.mod_db_url,
            "zipName": record.zip_name,
            "md5ModDb": record.md5_mod_db if record.checksum_known else None,
            "commanderChecksumKnown": record.checksum_known,
            "commanderCounter": record.counter,
        })
    if not entries:
        raise ValueError("No installed addons with download identities could be imported.")
    write_text(snapshot, json.dumps(entries, indent=2, ensure_ascii=False) + "\n")
    return f"Imported {len(entries)} installed addons for CLI updates. Addons with unknown checksums will be refreshed."
