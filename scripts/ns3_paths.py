import json
from pathlib import Path

def find_binary(ns3):
    ns3 = Path(ns3).resolve()
    cache = ns3 / "cmake-cache/CMakeCache.txt"
    if cache.exists():
        for line in cache.read_text().splitlines():
            if line.startswith("ns3-execs:INTERNAL="):
                candidates = [Path(name) for name in line.split("=", 1)[1].split(";")
                              if Path(name).parent.name == "fack-paper"
                              and "-fack-paper-" in Path(name).name]
                if len(candidates) != 1 or not candidates[0].is_file():
                    raise RuntimeError("Build fack-paper in the active ns-3 profile first")
                return candidates[0]
    candidates = [p for p in (ns3 / "build/scratch/fack-paper").glob("ns*-fack-paper-*") if p.is_file()]
    if len(candidates) != 1:
        raise RuntimeError("Cannot identify one active fack-paper executable; configure and build ns-3 first")
    return candidates[0]

PROFILES = {
    "paper11": (False, False),
    "legacy": (False, False),
    "standard": (True, False),
    "ns1": (True, True),
}

DEFAULT_SEEDS = 10

def profile_options(profile):
    wire, historical = PROFILES[profile]
    return [f"--sackOptionBytes={str(wire).lower()}",
            f"--ns1TimestampTimer={str(historical).lower()}"]

def scenario_profile(profile, number=None):
    if profile == "paper11":
        return "ns1" if number == 11 else "legacy"
    return profile

def settings(profile="paper11", seed_count=DEFAULT_SEEDS):
    if profile not in PROFILES:
        raise ValueError(f"Unknown model profile: {profile}")
    if type(seed_count) is not int or not 2 <= seed_count <= 1000:
        raise ValueError("Figure 11 needs between 2 and 1000 common seeds")
    value = {"schema": 1, "profile": profile, "seed_count": seed_count,
             "options": profile_options(profile)}
    if profile == "paper11":
        value["figure11_options"] = profile_options("ns1")
    return value

def read_settings(study):
    path = Path(study) / "results/run-settings.json"
    if not path.exists():
        return settings()
    value = json.loads(path.read_text())
    expected = settings(value["profile"], value["seed_count"])
    if value != expected:
        raise ValueError("Run settings do not match the delivered profile definitions")
    return value

def record_settings(study, profile, seed_count):
    path = Path(study) / "results/run-settings.json"
    value = settings(profile, seed_count)
    if path.exists() and read_settings(study) != value:
        raise ValueError("This results directory uses another profile/seed count. "
                         "Run the alternative in a fresh copy of the source folder.")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")
    return value
