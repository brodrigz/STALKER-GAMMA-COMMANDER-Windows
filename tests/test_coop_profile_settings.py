import json

from commander_gui.coop import CoopManager
from commander_gui.settings import CliProfile, CliSettings, load_settings


def test_profile_modes_survive_cli_settings_rewrite(tmp_path):
    path = tmp_path / "settings.json"
    profile = CliProfile(active=True, gamma=str(tmp_path / "gamma"), anomaly=str(tmp_path / "anomaly"))
    assert profile.mo2_coop_profile == ""
    profile.mo2_singleplayer_profile = "Solo"
    profile.mo2_coop_profile = "Friends"
    profile.select_mo2_profile("Friends")
    CliSettings(profiles=[profile]).save(path)
    # The CLI only knows Mo2Profile. It may rewrite its JSON without GUI fields.
    path.write_text(json.dumps({"Profiles": [profile.to_dict()]}))
    restored = load_settings(path).active_profile
    assert restored.mo2_profile == "Friends"
    assert restored.singleplayer_profile == "Solo"
    assert restored.mo2_coop_profile == "Friends"
    restored.select_mo2_profile(restored.singleplayer_profile)
    assert restored.mo2_profile == "Solo"
    assert restored.mo2_coop_profile == "Friends"


def test_legacy_managed_profile_migrates_but_explicit_blank_is_preserved(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    profile = CliProfile(active=True, gamma=str(tmp_path / "gamma"), anomaly=str(tmp_path / "anomaly"),
                         mo2_profile="G.A.M.M.A. Co-op")
    path.write_text(json.dumps({"Profiles": [profile.to_dict()]}))
    monkeypatch.setattr(CoopManager, "state", lambda _: {"installed": True, "source_profile": "Solo"})
    settings = load_settings(path)
    restored = settings.active_profile
    assert restored.singleplayer_profile == "Solo"
    assert restored.mo2_coop_profile == "G.A.M.M.A. Co-op"
    restored.mo2_coop_profile = ""
    settings.save(path)
    assert load_settings(path).active_profile.mo2_coop_profile == ""


def test_new_installation_has_no_coop_profile(tmp_path):
    settings = load_settings(tmp_path / "settings.json")
    assert settings.active_profile.mo2_coop_profile == ""
    assert settings.active_profile.singleplayer_profile == "G.A.M.M.A"
    assert not (tmp_path / "gamma/profiles/G.A.M.M.A. Co-op").exists()
