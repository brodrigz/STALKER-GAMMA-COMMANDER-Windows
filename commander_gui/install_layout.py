"""Discover existing GAMMA installations without changing their game files."""

from pathlib import Path

WINDOWS_INSTALLER = ".Grok's Modpack Installer"


def mo2_profiles(gamma: str | Path) -> dict[str, int]:
    """Existing MO2 profiles and their real mod counts (including disabled mods)."""
    root = Path(gamma) / "profiles"
    result = {}
    try:
        directories = sorted(root.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        return result
    for directory in directories:
        try:
            lines = (directory / "modlist.txt").read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError):
            continue
        result[directory.name] = sum(
            line.startswith(("+", "-")) and len(line) > 1 and not line.endswith("_separator")
            for line in lines
        )
    return result


def resolve_mo2_profile(gamma: str | Path, configured: str) -> str:
    """Recover an empty default profile when there is exactly one populated one.

    Explicit custom selections and ambiguous installations require the user's
    choice. New CLI installations without Grok's installer are left alone.
    """
    if configured not in ("", "G.A.M.M.A") or not (Path(gamma) / WINDOWS_INSTALLER).is_dir():
        return configured
    profiles = mo2_profiles(gamma)
    if profiles.get(configured, 0):
        return configured
    populated = [name for name, count in profiles.items() if count]
    return populated[0] if len(populated) == 1 else configured
